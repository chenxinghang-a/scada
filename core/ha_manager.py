"""
高可用管理器 - 主备切换
工业SCADA系统要求99.99%可用性
"""
import threading
import time
import hmac
import hashlib
import logging
import socket
import json
import secrets
from enum import Enum
from typing import Optional, Callable, Dict, Any
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class HARole(Enum):
    """高可用角色"""
    PRIMARY = 'primary'
    STANDBY = 'standby'
    UNKNOWN = 'unknown'


class HAState(Enum):
    """高可用状态"""
    INITIALIZING = 'initializing'
    ACTIVE = 'active'        # 主节点活跃
    PASSIVE = 'passive'      # 备节点待命
    FAILOVER = 'failover'    # 切换中
    FAILED = 'failed'        # 故障


def _parse_role(value) -> HARole:
    """把心跳里的 role 字符串安全地转成 HARole（非法值归为 UNKNOWN）"""
    try:
        return HARole(value)
    except (ValueError, TypeError):
        return HARole.UNKNOWN


def _parse_state(value) -> HAState:
    """把心跳里的 state 字符串安全地转成 HAState（非法值归为 INITIALIZING）"""
    try:
        return HAState(value)
    except (ValueError, TypeError):
        return HAState.INITIALIZING


@dataclass
class HANode:
    """HA节点信息"""
    node_id: str
    role: HARole
    state: HAState
    last_heartbeat: float
    priority: int = 100      # 优先级，越高越优先成为主
    address: str = ''
    metadata: Dict[str, Any] = field(default_factory=dict)


class HAManager:
    """高可用管理器

    实现主备切换机制：
    1. 主节点定期发送心跳
    2. 备节点监听心跳
    3. 心跳超时时备节点接管
    4. 原主恢复后成为备节点
    """

    def __init__(self,
                 node_id: str,
                 priority: int = 100,
                 heartbeat_interval: float = 2.0,
                 heartbeat_timeout: float = 10.0,
                 peer_address: str = '',
                 peer_port: int = 9999,
                 shared_secret: str = ''):
        self.node_id = node_id
        self.priority = priority
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.peer_address = peer_address
        self.peer_port = peer_port
        # HMAC 共享密钥（主备节点必须一致）
        self._shared_secret = (shared_secret or secrets.token_hex(16)).encode('utf-8')

        self.role = HARole.UNKNOWN
        self.state = HAState.INITIALIZING
        self._lock = threading.RLock()
        self._running = False

        # 心跳相关
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._listen_thread: Optional[threading.Thread] = None
        self._last_peer_heartbeat = 0.0
        self._peer_node: Optional[HANode] = None
        self._started_at = 0.0
        # 运维是否用 force_role() 手动指定过角色（手动指定后不再自动仲裁）
        self._manual_role_set = False

        # 回调
        self._on_role_change: Optional[Callable] = None
        self._on_failover: Optional[Callable] = None

        # 统计
        self.stats = {
            'failovers': 0,
            'heartbeats_sent': 0,
            'heartbeats_received': 0,
            'role_changes': 0,
        }

    def start(self):
        """启动HA管理器"""
        with self._lock:
            self._running = True
            self._started_at = time.time()
            self._manual_role_set = False

            # 初始角色：如果没有对端，自己是主
            if not self.peer_address:
                self._set_role(HARole.PRIMARY)
                self.state = HAState.ACTIVE
                logger.info(f"HA节点 {self.node_id}: 无对端，成为主节点")
            else:
                self._set_role(HARole.STANDBY)
                self.state = HAState.PASSIVE
                logger.info(f"HA节点 {self.node_id}: 备节点模式，监听 {self.peer_address}")

            # 启动心跳线程
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, daemon=True, name="ha-heartbeat")
            self._heartbeat_thread.start()

            # 启动监听线程
            if self.peer_address:
                self._listen_thread = threading.Thread(
                    target=self._listen_loop, daemon=True, name="ha-listen")
                self._listen_thread.start()

    def stop(self):
        """停止HA管理器"""
        self._running = False
        logger.info(f"HA节点 {self.node_id}: 停止")

    def _set_role(self, new_role: HARole):
        """设置角色"""
        old_role = self.role
        if old_role == new_role:
            return

        self.role = new_role
        self.stats['role_changes'] += 1

        logger.warning(f"HA角色变更: {old_role.value} -> {new_role.value}")

        if self._on_role_change:
            try:
                self._on_role_change(old_role, new_role)
            except Exception as e:
                logger.error(f"角色变更回调失败: {e}")

    def _heartbeat_loop(self):
        """心跳发送 + 失效检测循环

        心跳必须**双向**发送：主节点和备节点都要发。
        历史缺陷：只有 PRIMARY 发送心跳，而切换判定又要求
        ``_last_peer_heartbeat > 0``（"曾经收到过对端心跳"）。于是备节点
        既收不到（对端根本不发）又永远不满足切换条件 —— 主节点宕机后备机
        永不接管；对称配置下两台节点还会一起停在 STANDBY，谁都不采集。
        """
        while self._running:
            try:
                # 在锁内读取共享状态，避免竞态
                with self._lock:
                    current_role = self.role
                    now = time.time()
                    peer_ever_seen = self._last_peer_heartbeat > 0
                    peer_timed_out = (
                        peer_ever_seen and
                        now - self._last_peer_heartbeat > self.heartbeat_timeout
                    )
                    # 配了对端却从未收到任何心跳（对端进程没起来 / 端口不可达）：
                    # 超过一个超时窗口后同样按「对端失效」处理，否则备机永远待命。
                    peer_never_seen = (
                        not peer_ever_seen and
                        bool(self.peer_address) and
                        self._started_at > 0 and
                        now - self._started_at > self.heartbeat_timeout
                    )

                # 心跳双向发送：不区分角色
                self._send_heartbeat()

                if current_role == HARole.STANDBY and (peer_timed_out or peer_never_seen):
                    self._trigger_failover()
                else:
                    self._try_election()

                time.sleep(self.heartbeat_interval)
            except Exception as e:
                logger.error(f"心跳循环异常: {e}")
                time.sleep(1)

    def _compute_hmac(self, payload: str) -> str:
        """计算 HMAC-SHA256 签名"""
        return hmac.new(self._shared_secret, payload.encode('utf-8'), hashlib.sha256).hexdigest()

    def _verify_hmac(self, payload: str, signature: str) -> bool:
        """验证 HMAC-SHA256 签名"""
        expected = self._compute_hmac(payload)
        return hmac.compare_digest(expected, signature)

    def _send_heartbeat(self):
        """发送心跳（带 HMAC 签名）"""
        if not self.peer_address:
            return

        sock = None
        try:
            heartbeat = {
                'type': 'heartbeat',
                'node_id': self.node_id,
                'role': self.role.value,
                'priority': self.priority,
                'timestamp': time.time(),
                'state': self.state.value,
            }
            # 签名：对核心字段计算 HMAC
            sign_data = f"{heartbeat['node_id']}:{heartbeat['role']}:{heartbeat['priority']}:{heartbeat['timestamp']}"
            heartbeat['signature'] = self._compute_hmac(sign_data)

            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(1)
            sock.sendto(json.dumps(heartbeat).encode(),
                       (self.peer_address, self.peer_port))

            self.stats['heartbeats_sent'] += 1
        except Exception as e:
            logger.debug(f"心跳发送失败: {e}")
        finally:
            if sock:
                try:
                    sock.close()
                except Exception as e:
                    # 预期内且无副作用：心跳已发送完毕，套接字即将废弃，关闭失败不影响后续逻辑
                    logger.debug(f"关闭心跳发送套接字失败(套接字将废弃): {e}")

    def _listen_loop(self):
        """监听心跳"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('0.0.0.0', self.peer_port))
            sock.settimeout(2)

            while self._running:
                try:
                    data, addr = sock.recvfrom(4096)
                    msg = json.loads(data.decode())

                    if msg.get('type') == 'heartbeat':
                        self._handle_heartbeat(msg, addr)
                except socket.timeout:
                    # 安全忽略：套接字 2s 接收超时的正常空转路径，用于周期性检查 _running，非错误。
                    continue
                except Exception as e:
                    logger.debug(f"心跳接收异常: {e}")
        except Exception as e:
            logger.error(f"心跳监听启动失败: {e}")
        finally:
            try:
                sock.close()
            except Exception as e:
                # 预期内且无副作用：监听循环已退出，套接字即将废弃，关闭失败不影响后续逻辑
                logger.debug(f"关闭心跳监听套接字失败(套接字将废弃): {e}")

    def _handle_heartbeat(self, msg: dict, addr: tuple):
        """处理收到的心跳（验证 HMAC 签名）"""
        # 验证签名
        signature = msg.get('signature', '')
        sign_data = f"{msg.get('node_id', '')}:{msg.get('role', '')}:{msg.get('priority', 0)}:{msg.get('timestamp', 0)}"
        if not signature or not self._verify_hmac(sign_data, signature):
            logger.warning(f"心跳签名验证失败，丢弃: {addr}")
            return

        peer_id = msg.get('node_id', 'unknown')
        peer_priority = msg.get('priority', 0)
        peer_role = _parse_role(msg.get('role'))

        with self._lock:
            self._last_peer_heartbeat = time.time()
            self.stats['heartbeats_received'] += 1
            self._peer_node = HANode(
                node_id=peer_id,
                role=peer_role,
                state=_parse_state(msg.get('state')),
                last_heartbeat=self._last_peer_heartbeat,
                priority=peer_priority,
                address=addr[0] if addr else '',
            )

            # 如果自己是主，但收到更高优先级的主心跳，降级为备
            if (self.role == HARole.PRIMARY and
                peer_role == HARole.PRIMARY and
                peer_priority > self.priority):
                logger.warning(f"收到更高优先级主节点 {peer_id}，降级为备")
                self._set_role(HARole.STANDBY)
                self.state = HAState.PASSIVE

            # 对端也是备节点时做仲裁，避免双方都停在 STANDBY（无人采集）
            self._try_election()

    def _wins_election(self, peer_priority: int, peer_id: str) -> bool:
        """按 (priority, node_id) 全序比较决定谁当主。

        两个节点各自计算同一比较，结果必然互斥，不会双方同时晋升。
        """
        if self.priority != peer_priority:
            return self.priority > peer_priority
        return self.node_id < peer_id

    def _try_election(self):
        """两个备节点之间的主节点仲裁。

        对称配置（双方都填了 peer_address）下两台节点都会以 STANDBY 启动。
        心跳改成双向后双方都能感知对端存活，但如果不做仲裁，双方会一直
        停在 STANDBY —— 谁都不采集。这里做确定性仲裁：只有胜者晋升。

        运维用 force_role() 手动指定过角色后不再自动仲裁，避免自动逻辑
        把运维的决定立刻推翻。
        """
        with self._lock:
            if self._manual_role_set:
                return
            if self.role != HARole.STANDBY:
                return
            peer = self._peer_node
            if peer is None or peer.role != HARole.STANDBY:
                return
            if time.time() - self._last_peer_heartbeat > self.heartbeat_timeout:
                return
            if not self._wins_election(peer.priority, peer.node_id):
                return

            self._promote_to_primary(
                f"对端 {peer.node_id} 同为备节点且优先级不高于本节点，仲裁胜出")

    def _promote_to_primary(self, reason: str):
        """晋升为主节点（调用方无需持锁，RLock 可重入）"""
        with self._lock:
            if self.role == HARole.PRIMARY:
                return

            logger.warning(f"HA节点 {self.node_id} 晋升为主节点: {reason}")
            self.state = HAState.FAILOVER

            self._set_role(HARole.PRIMARY)
            self.state = HAState.ACTIVE

            if self._on_failover:
                try:
                    self._on_failover()
                except Exception as e:
                    logger.error(f"切换回调失败: {e}")

    def _trigger_failover(self):
        """触发主备切换（对端心跳超时）"""
        with self._lock:
            if self.role != HARole.STANDBY:
                return

            self.stats['failovers'] += 1
            self._last_peer_heartbeat = 0  # 重置

        self._promote_to_primary("主节点心跳超时，备节点接管")

    def get_status(self) -> dict:
        """获取HA状态"""
        with self._lock:
            return {
                'node_id': self.node_id,
                'role': self.role.value,
                'state': self.state.value,
                'priority': self.priority,
                'peer_address': self.peer_address,
                'peer_alive': (time.time() - self._last_peer_heartbeat < self.heartbeat_timeout
                              if self._last_peer_heartbeat > 0 else False),
                'stats': dict(self.stats),
            }

    def force_role(self, role: HARole):
        """强制切换角色（运维用）"""
        with self._lock:
            logger.warning(f"强制切换角色: {self.role.value} -> {role.value}")
            self._manual_role_set = True
            self._set_role(role)
            self.state = HAState.ACTIVE if role == HARole.PRIMARY else HAState.PASSIVE

    def set_on_role_change(self, callback: Callable):
        """设置角色变更回调"""
        self._on_role_change = callback

    def set_on_failover(self, callback: Callable):
        """设置切换回调"""
        self._on_failover = callback
