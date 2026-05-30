"""
高可用管理器 - 主备切换
工业SCADA系统要求99.99%可用性
"""
import threading
import time
import logging
import socket
import json
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
                 peer_port: int = 9999):
        self.node_id = node_id
        self.priority = priority
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_timeout = heartbeat_timeout
        self.peer_address = peer_address
        self.peer_port = peer_port

        self.role = HARole.UNKNOWN
        self.state = HAState.INITIALIZING
        self._lock = threading.RLock()
        self._running = False

        # 心跳相关
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._listen_thread: Optional[threading.Thread] = None
        self._last_peer_heartbeat = 0.0
        self._peer_node: Optional[HANode] = None

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
        """心跳发送循环"""
        while self._running:
            try:
                if self.role == HARole.PRIMARY:
                    self._send_heartbeat()

                # 检查对端心跳超时（在锁内读取共享状态）
                should_failover = False
                with self._lock:
                    if (self.role == HARole.STANDBY and
                        self._last_peer_heartbeat > 0 and
                        time.time() - self._last_peer_heartbeat > self.heartbeat_timeout):
                        should_failover = True

                if should_failover:
                    self._trigger_failover()

                time.sleep(self.heartbeat_interval)
            except Exception as e:
                logger.error(f"心跳循环异常: {e}")
                time.sleep(1)

    def _send_heartbeat(self):
        """发送心跳"""
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

            # UDP广播心跳
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
                except Exception:
                    pass

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
                    continue
                except Exception as e:
                    logger.debug(f"心跳接收异常: {e}")
        except Exception as e:
            logger.error(f"心跳监听启动失败: {e}")
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _handle_heartbeat(self, msg: dict, addr: tuple):
        """处理收到的心跳"""
        peer_id = msg.get('node_id', 'unknown')
        peer_priority = msg.get('priority', 0)

        with self._lock:
            self._last_peer_heartbeat = time.time()
            self.stats['heartbeats_received'] += 1

            # 如果自己是主，但收到更高优先级的主心跳，降级为备
            if (self.role == HARole.PRIMARY and
                msg.get('role') == 'primary' and
                peer_priority > self.priority):
                logger.warning(f"收到更高优先级主节点 {peer_id}，降级为备")
                self._set_role(HARole.STANDBY)
                self.state = HAState.PASSIVE

    def _trigger_failover(self):
        """触发主备切换"""
        with self._lock:
            if self.role != HARole.STANDBY:
                return

            logger.warning(f"HA切换: 主节点心跳超时，备节点接管")
            self.state = HAState.FAILOVER
            self.stats['failovers'] += 1

            self._set_role(HARole.PRIMARY)
            self.state = HAState.ACTIVE
            self._last_peer_heartbeat = 0  # 重置

            if self._on_failover:
                try:
                    self._on_failover()
                except Exception as e:
                    logger.error(f"切换回调失败: {e}")

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
            self._set_role(role)
            self.state = HAState.ACTIVE if role == HARole.PRIMARY else HAState.PASSIVE

    def set_on_role_change(self, callback: Callable):
        """设置角色变更回调"""
        self._on_role_change = callback

    def set_on_failover(self, callback: Callable):
        """设置切换回调"""
        self._on_failover = callback
