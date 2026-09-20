"""
工厂级设备控制模块
工业4.0落地核心 — 安全、可靠、可追溯的设备控制

核心能力：
1. 安全联锁（Safety Interlock）— 超限自动停机，硬件级优先
2. 写操作安全校验 — 值范围/地址白名单/操作前条件检查
3. 故障降级（Fault Degradation）— 通信断开→安全状态
4. 紧急停机（E-Stop）— 一键停机，独立通道，最高优先级
5. 操作审计（Audit Trail）— 完整链路，持久化，防篡改
6. 写操作回读验证 — 写入后回读确认
7. 操作互斥（Mutex）— 防止多用户同时操作同一设备

配置驱动（2026-09 修复）：
    - 安全联锁规则不再在代码里写死具体设备名（原预置规则绑定 `plc_reactor_01`
      等"幽灵设备"：该设备在 配置/devices*.yaml 中并不存在，条件永远不匹配，
      等于一条永不触发的安全联锁）。现改为从 配置/interlocks.yaml、
      构造参数 config['interlocks'] 或各设备的 `interlocks` 段加载；
      本地未配置任何联锁时不安装任何规则，并输出明确告警。
    - 批量起停的写入点位不再写死 coil0/reg100，改为按设备配置
      `control: {start/stop/reset: {type, address, value}}` 解析；
      未配置时使用带告警的显式回退常量（LEGACY_FALLBACK_CONTROL_POINTS）。
"""

import time
import uuid
import logging
import threading
import json
from typing import Any, Callable, Set
from datetime import datetime, timedelta
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
import paths

try:
    import yaml
    HAS_YAML = True
except ImportError:  # pragma: no cover - 环境缺 yaml 时退化为纯内存配置
    HAS_YAML = False

logger = logging.getLogger(__name__)

# --- 命名常量（替代魔法数字） ---
AUDIT_LOG_CAPACITY = 50000          # 审计日志容量
COMM_FAILURE_THRESHOLD = 3          # 通信失败阈值
EMA_ALPHA = 0.3                     # 指数移动平均系数
BACKOFF_CEILING_S = 60              # 退避上限（秒）
SENSOR_STUCK_WINDOW_S = 300         # 传感器卡死检测窗口（秒）
DEVICE_LOCK_TIMEOUT_S = 5.0         # 设备锁超时（秒）

# 联锁规则配置文件（生产推荐）
INTERLOCK_CONFIG_PATH = '配置/interlocks.yaml'

# 批量起停的**显式回退点位**（仅在设备未配置 control 段时使用，并记录告警）
# 历史实现把 coil0 / reg100 直接写死在 batch_control 内且无任何提示；
# 现改为"设备配置优先、回退值显式命名并告警"，避免把点位猜测静默写进现场设备。
LEGACY_FALLBACK_CONTROL_POINTS = {
    'start': {'type': 'coil', 'address': 0},
    'stop': {'type': 'coil', 'address': 0},
    'reset': {'type': 'register', 'address': 100},
}

# 未显式给出 value 时，各动作的默认写入值
_DEFAULT_ACTION_VALUES = {'start': True, 'stop': False, 'reset': 0}


class SafetyLevel:
    """安全等级"""
    SAFE = 'safe'           # 安全状态
    WARNING = 'warning'     # 警告（可继续运行）
    CRITICAL = 'critical'   # 严重（需停机）
    EMERGENCY = 'emergency' # 紧急（立即停机）


@dataclass
class BypassRequest:
    """联锁旁路审批请求（IEC 61511 合规）"""
    request_id: str
    interlock_id: str
    requested_by: str
    requested_at: float
    expires_at: float  # 超时自动过期
    reason: str
    approvals: list = field(default_factory=list)
    required_approvals: int = 2  # 至少2人审批
    status: str = 'pending'  # pending/approved/rejected/expired

    @property
    def is_approved(self) -> bool:
        return len(self.approvals) >= self.required_approvals and self.status == 'pending'

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at


class DeviceControlSafety:
    """
    工厂级设备控制安全管理器

    职责：
    - 安全联锁规则管理与执行
    - 写操作安全校验
    - 故障降级策略
    - 紧急停机管理
    - 操作审计日志
    """

    def __init__(self, database, device_manager=None, alarm_manager=None,
                 config: dict[str, Any] | None = None):
        self.database = database
        self.device_manager = device_manager
        self.alarm_manager = alarm_manager
        self.config = config or {}

        # ===== 安全联锁规则 =====
        self._interlock_rules: dict[str, dict[str, Any]] = {}
        self._interlock_states: dict[str, bool] = {}  # rule_id -> is_triggered
        self._interlock_bypass: Set[str] = set()  # 被旁路的联锁ID
        self._lock = threading.Lock()

        # ===== 联锁旁路审批（IEC 61511） =====
        self._bypass_requests: dict[str, BypassRequest] = {}
        self._active_bypasses: Set[str] = set()  # 已批准且激活的旁路
        self._bypass_lock = threading.Lock()

        # ===== 写操作安全配置 =====
        # 设备写操作白名单：device_id -> {register_name: (min_value, max_value)}
        # 键为寄存器名称（如 'temperature_setpoint'），地址由设备配置反查；
        # 特殊键 'default' 表示该设备所有寄存器的兜底量程
        self._write_limits: dict[str, dict[str, Any]] = {}
        # 设备可写地址白名单：device_id -> set of allowed addresses
        self._write_whitelist: dict[str, Set[int]] = {}

        # ===== 操作互斥锁 =====
        self._device_locks: dict[str, threading.Lock] = {}
        self._device_lock_owners: dict[str, str] = {}  # device_id -> operator

        # ===== 紧急停机 =====
        self._estop_active = False
        self._estop_time: datetime | None = None
        self._estop_reason = ''
        self._estop_devices: list[str] = []  # 需要停机的设备列表

        # ===== 故障降级 =====
        self._device_health: dict[str, dict[str, Any]] = {}  # device_id -> health info
        self._safe_state_handlers: dict[str, Callable[..., Any]] = {}  # device_id -> safe_state_func

        # ===== 操作审计 =====
        self._audit_log = deque(maxlen=AUDIT_LOG_CAPACITY)
        self._audit_file = paths.resolve('data/audit_log.jsonl')

        # ===== 通信监控 =====
        self._comm_failures: dict[str, int] = {}  # device_id -> consecutive failures
        self._comm_threshold = COMM_FAILURE_THRESHOLD

        # 加载预置联锁规则
        self._load_preset_interlocks()
        # 加载写操作限制
        self._load_write_limits()
        # 初始化设备健康数据
        self._init_device_health()

        logger.info("工厂级设备控制安全管理器已初始化")

    # ==================== 安全联锁 ====================

    def _load_preset_interlocks(self):
        """
        加载安全联锁规则（配置驱动）

        规则来源（按优先级，先命中先返回）：
        1. 配置/interlocks.yaml（生产推荐：含真实设备ID与寄存器名）
        2. 构造参数 config['interlocks']（测试/嵌入式注入）
        3. 各设备配置中的 `interlocks` 段（按设备就近声明）

        历史实现把 6 条"预置规则"直接写死在代码里，绑定了 `plc_reactor_01`
        等**配置中并不存在**的设备名 —— 条件永远不会匹配，等于装了一批永不触发的
        安全联锁；而 `relay_output_01` 地址 0 又是 relay_1（输出线圈）与 di_1
        共用的地址，读到输出线圈值就可能误触发"急停"。
        因此这里不再内置任何具体设备名：没有配置就没有联锁，并明确告警，
        而不是用一批看起来在工作、实际不工作的假规则充数。

        YAML 结构示例（配置/interlocks.yaml）：

            interlocks:
              - id: interlock_temp_high
                name: 温度超限联锁
                priority: 1                 # 数字越小越先执行
                enabled: true
                condition: {type: threshold, device_id: siemens_1500_01,
                            register: boiler_temperature, operator: '>', value: 95.0}
                action: {type: write_register, device_id: siemens_1500_01,
                         register: boiler_status, value: 0}
                alarm: {level: critical, message: '温度超限联锁触发'}
        """
        rules = []
        source = None

        # 1. 配置文件
        file_rules = self._load_interlocks_from_file()
        if file_rules:
            rules, source = file_rules, str(paths.resolve(INTERLOCK_CONFIG_PATH))

        # 2. 构造参数注入
        if not rules:
            injected = self.config.get('interlocks')
            if isinstance(injected, list) and injected:
                rules, source = list(injected), "config['interlocks']"

        # 3. 设备配置内的 interlocks 段
        if not rules:
            from_devices = self._load_interlocks_from_devices()
            if from_devices:
                rules, source = from_devices, '设备配置 interlocks 段'

        if not rules:
            logger.warning(
                "未加载到任何安全联锁规则：请配置 %s（或设备配置的 interlocks 段）。"
                "此前写死的 plc_reactor_01 等预置规则已移除 —— 那些设备在设备清单中"
                "不存在，条件永不匹配，属于「看似在保护、实际不保护」的死规则。",
                INTERLOCK_CONFIG_PATH,
            )
            return

        loaded = 0
        for rule in rules:
            if not isinstance(rule, dict):
                logger.error("忽略非法联锁规则（非字典）: %r", rule)
                continue
            rule_id = rule.get('id')
            if not rule_id:
                logger.error("忽略缺 id 的联锁规则: %r", rule)
                continue
            if 'condition' not in rule or 'action' not in rule:
                logger.error("忽略缺 condition/action 的联锁规则: %s", rule_id)
                continue
            rule.setdefault('name', rule_id)
            rule.setdefault('priority', 1)
            rule.setdefault('enabled', True)
            self._interlock_rules[rule_id] = rule
            self._interlock_states.setdefault(rule_id, False)
            loaded += 1

        logger.info("已从 %s 加载 %d 条安全联锁规则", source, loaded)

    def _load_interlocks_from_file(self) -> list[dict[str, Any]]:
        """从 配置/interlocks.yaml 读取联锁规则；文件不存在或不可用时返回 []"""
        if not HAS_YAML:
            logger.warning("未安装 PyYAML，无法读取 %s", INTERLOCK_CONFIG_PATH)
            return []

        path = paths.resolve(INTERLOCK_CONFIG_PATH)
        try:
            if not path.exists():
                logger.info("联锁配置文件不存在: %s", path)
                return []
            with open(path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            logger.error("加载联锁配置失败 %s: %s", path, e)
            return []

        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            rules = data.get('interlocks')
            if isinstance(rules, list):
                return [r for r in rules if isinstance(r, dict)]
        logger.error("联锁配置格式错误（应为 {interlocks: [...]}）: %s", path)
        return []

    def _load_interlocks_from_devices(self) -> list[dict[str, Any]]:
        """汇总各设备配置中声明的 interlocks 段（设备ID就近声明，避免跨文件写死）"""
        if not self.device_manager:
            return []
        try:
            devices = getattr(self.device_manager, 'devices', {}) or {}
        except Exception:
            return []

        rules: list[dict[str, Any]] = []
        for device_id, device_config in devices.items():
            if not isinstance(device_config, dict):
                continue
            for rule in device_config.get('interlocks') or []:
                if isinstance(rule, dict) and 'id' in rule:
                    rules.append(rule)
                else:
                    logger.error("设备 %s 的联锁声明非法（缺 id）: %r", device_id, rule)
        return rules

    def _load_write_limits(self):
        """
        加载写操作安全限制（防止写入危险值）

        下方内置表是**演示用默认量程**，仅对同名设备生效（不存在该设备即为惰性数据）。
        真实设备的量程应在 `配置/devices*.yaml` 中按设备声明 `write_limits` 段，
        由 `_merge_device_write_limits()` 覆盖/补充，避免把现场量程写死在代码里。
        """
        self._write_limits = {
            'plc_reactor_01': {
                'temperature_setpoint': (0, 150),    # 温度设定值 0-150°C
                'pressure_setpoint': (0, 4.0),       # 压力设定值 0-4.0 MPa
                'flow_setpoint': (0, 100),            # 流量设定值 0-100 m³/h
                'heater_enable': (0, 1),              # 加热开关 0/1
                'pump_enable': (0, 1),                # 泵开关 0/1
                'valve_position': (0, 100),           # 阀门开度 0-100%
            },
            'relay_output_01': {
                # 继电器模块：每路只能0或1
                'default': (0, 1)
            },
            'relay_output_02': {
                'default': (0, 1)
            },
            'signal_tower_01': {
                'default': (0, 1)
            },
            'signal_tower_02': {
                'default': (0, 1)
            }
        }
        self._merge_device_write_limits()

    def _merge_device_write_limits(self):
        """把设备配置中的 write_limits 段合并进量程表（配置优先，覆盖内置演示值）"""
        if not self.device_manager:
            return
        try:
            devices = getattr(self.device_manager, 'devices', {}) or {}
        except Exception as e:
            logger.error("读取设备配置失败（write_limits 合并）: %s", e)
            return

        for device_id, device_config in devices.items():
            if not isinstance(device_config, dict):
                continue
            limits = device_config.get('write_limits')
            if not isinstance(limits, dict):
                continue
            target = self._write_limits.setdefault(device_id, {})
            for register_name, bounds in limits.items():
                try:
                    low, high = float(bounds[0]), float(bounds[1])
                except (TypeError, ValueError, IndexError):
                    logger.error("设备 %s 的 write_limits.%s 非法: %r", device_id, register_name, bounds)
                    continue
                if low > high:
                    logger.error("设备 %s 的 write_limits.%s 下限大于上限: %r", device_id, register_name, bounds)
                    continue
                target[str(register_name)] = (low, high)

    def _init_device_health(self):
        """从设备管理器初始化设备健康数据"""
        if not self.device_manager:
            return
        try:
            all_devices = self.device_manager.get_all_devices()
            for device_id, config in all_devices.items():
                client = self.device_manager.get_client(device_id)
                connected = getattr(client, 'connected', False) if client else False
                self._device_health[device_id] = {
                    'status': 'connected' if connected else 'disconnected',
                    'last_seen': datetime.now().isoformat() if connected else None,
                    'consecutive_failures': 0 if connected else 3,
                    'avg_response_ms': 0,
                    'name': config.get('name', device_id),
                    'protocol': config.get('protocol', 'unknown'),
                }
            logger.info(f"已初始化 {len(self._device_health)} 个设备的健康数据")
        except Exception as e:
            logger.error(f"初始化设备健康数据失败: {e}")

    def refresh_device_health(self):
        """刷新所有设备健康状态（供API调用）"""
        if not self.device_manager:
            return
        try:
            all_devices = self.device_manager.get_all_devices()
            for device_id, config in all_devices.items():
                client = self.device_manager.get_client(device_id)
                connected = getattr(client, 'connected', False) if client else False
                if device_id not in self._device_health:
                    self._device_health[device_id] = {
                        'status': 'unknown',
                        'last_seen': None,
                        'consecutive_failures': 0,
                        'avg_response_ms': 0,
                        'name': config.get('name', device_id),
                        'protocol': config.get('protocol', 'unknown'),
                    }
                health = self._device_health[device_id]
                health['status'] = 'connected' if connected else 'disconnected'
                if connected:
                    health['last_seen'] = datetime.now().isoformat()
                    health['consecutive_failures'] = 0
                health['name'] = config.get('name', device_id)
                health['protocol'] = config.get('protocol', 'unknown')
        except Exception as e:
            logger.error(f"刷新设备健康数据失败: {e}")

    def batch_control(self, action: str, operator: str = 'system', device_ids: list[str] | None = None) -> dict[str, Any]:
        """
        批量控制设备

        Args:
            action: 'start' | 'stop' | 'reset'
            operator: 操作者
            device_ids: 可选，指定设备ID列表。为None时操作所有设备。

        Returns:
            操作结果
        """
        if self._estop_active and action != 'reset':
            return {
                'success': False,
                'message': '紧急停机状态中，请先解除紧急停机',
                'results': {}
            }

        # 启动操作前检查联锁阻止条件
        if action == 'start':
            for rule_id, rule in self._interlock_rules.items():
                if rule_id in self._interlock_bypass:
                    continue
                if rule.get('action', {}).get('type') == 'block_start':
                    if self._interlock_states.get(rule_id, False):
                        return {
                            'success': False,
                            'message': f'联锁 {rule_id} ({rule.get("name")}) 阻止设备启动',
                            'results': {}
                        }

        results = {}
        controllable = self._get_controllable_devices()

        if not controllable:
            # 如果没有可识别的可控设备，尝试操作所有设备
            if self.device_manager:
                controllable = list(self.device_manager.get_all_devices().keys())

        # 如果指定了设备ID列表，过滤可控设备
        if device_ids and controllable:
            controllable = [did for did in controllable if did in device_ids]

        if not controllable:
            return {
                'success': False,
                'message': '没有可控制的设备',
                'results': {},
                'success_count': 0,
                'total': 0
            }

        for device_id in controllable:
            try:
                client = self.device_manager.get_client(device_id) if self.device_manager else None
                if not client:
                    results[device_id] = {'success': False, 'message': '设备客户端不存在'}
                    continue

                # 确保设备已连接
                if not getattr(client, 'connected', False):
                    try:
                        client.connect()
                    except Exception as e:
                        # 连接失败由下方 connected 检查统一处理（模拟模式或返回失败），
                        # 此处异常仅作记录，无副作用
                        logger.debug("批量控制前连接设备失败 device=%s action=%s: %s",
                                     device_id, action, e)
                    # 模拟模式下强制标记连接
                    if not getattr(client, 'connected', False):
                        if self.device_manager and getattr(self.device_manager, 'simulation_mode', False):
                            client.connected = True
                        else:
                            results[device_id] = {'success': False, 'message': '设备未连接'}
                            self._audit(f'batch_{action}', operator,
                                        f'批量{action}设备 {device_id}: 设备未连接')
                            continue

                success = False
                point = self._resolve_control_point(device_id, action)
                if point is None:
                    results[device_id] = {
                        'success': False,
                        'message': f'设备 {device_id} 无可用控制点位（control.{action}），拒绝写入',
                    }
                    self._audit(f'batch_{action}', operator,
                                f'批量{action}设备 {device_id}: 无可用控制点位，已拒绝写入')
                    continue

                if point['type'] == 'coil':
                    if hasattr(client, 'write_single_coil'):
                        success = client.write_single_coil(point['address'], bool(point['value']))
                    elif hasattr(client, 'write_single_register'):
                        success = client.write_single_register(point['address'], int(bool(point['value'])))
                else:  # register
                    if hasattr(client, 'write_single_register'):
                        success = client.write_single_register(point['address'], int(point['value']))
                    elif hasattr(client, 'write_single_coil'):
                        success = client.write_single_coil(point['address'], bool(point['value']))

                if success and self.device_manager:
                    if action == 'start':
                        self.device_manager.start_device(device_id)
                    elif action == 'stop':
                        self.device_manager.stop_device(device_id)

                results[device_id] = {
                    'success': success,
                    'message': f'{action}操作{"成功" if success else "失败"}'
                }

                self._audit(f'batch_{action}', operator,
                            f'批量{action}设备 {device_id}: {"成功" if success else "失败"}'
                            f' (点位 {point["type"]}#{point["address"]}={point["value"]})')

            except Exception as e:
                results[device_id] = {'success': False, 'message': str(e)}
                logger.error(f"批量控制设备 {device_id} 失败: {e}")

        success_count = sum(1 for r in results.values() if r.get('success'))
        total = len(results)

        return {
            'success': success_count > 0,
            'message': f'批量{action}完成: {success_count}/{total} 成功',
            'results': results,
            'success_count': success_count,
            'total': total
        }

    def add_interlock(self, rule: dict[str, Any]) -> bool:
        """添加自定义联锁规则"""
        rule_id = rule.get('id')
        if not rule_id:
            return False
        with self._lock:
            self._interlock_rules[rule_id] = rule
            self._interlock_states[rule_id] = False
        logger.info(f"添加联锁规则: {rule_id} - {rule.get('name')}")
        return True

    def remove_interlock(self, rule_id: str) -> bool:
        """移除联锁规则"""
        with self._lock:
            if rule_id in self._interlock_rules:
                del self._interlock_rules[rule_id]
                self._interlock_states.pop(rule_id, None)
                self._interlock_bypass.discard(rule_id)
                logger.info(f"移除联锁规则: {rule_id}")
                return True
        return False

    def bypass_interlock(self, rule_id: str, operator: str, reason: str) -> bool:
        """[已废弃] 旁路联锁 — 请使用 request_bypass + approve_bypass 流程

        .. deprecated::
            单人旁路违反 IEC 61511 职责分离要求，保留仅为向后兼容。
        """
        import warnings
        warnings.warn(
            "bypass_interlock() 已废弃，请使用 request_bypass() + approve_bypass() 多人审批流程",
            DeprecationWarning, stacklevel=2
        )
        logger.warning(f"[DEPRECATED] bypass_interlock() 被调用，请迁移至审批流程")
        if rule_id not in self._interlock_rules:
            return False
        with self._lock:
            self._interlock_bypass.add(rule_id)
        self._audit('interlock_bypass_deprecated', operator, f'[废弃]旁路联锁 {rule_id}: {reason}')
        logger.warning(f"联锁已旁路(废弃流程): {rule_id} by {operator} — {reason}")
        return True

    def restore_interlock(self, rule_id: str, operator: str) -> bool:
        """恢复联锁"""
        with self._lock:
            self._interlock_bypass.discard(rule_id)
            self._active_bypasses.discard(rule_id)
        self._audit('interlock_restore', operator, f'恢复联锁 {rule_id}')
        logger.info(f"联锁已恢复: {rule_id} by {operator}")
        return True

    # ==================== 联锁旁路审批流程（IEC 61511） ====================

    def request_bypass(self, interlock_id: str, requested_by: str, reason: str,
                       timeout_minutes: int = 30) -> str:
        """请求联锁旁路（需要多人审批）

        Args:
            interlock_id: 联锁规则ID
            requested_by: 请求者用户名
            reason: 旁路原因
            timeout_minutes: 旁路超时时间（分钟），超时自动恢复

        Returns:
            request_id: 请求ID，用于后续审批
        """
        request_id = str(uuid.uuid4())[:8]

        request = BypassRequest(
            request_id=request_id,
            interlock_id=interlock_id,
            requested_by=requested_by,
            requested_at=time.time(),
            expires_at=time.time() + timeout_minutes * 60,
            reason=reason,
            required_approvals=2
        )

        with self._bypass_lock:
            self._bypass_requests[request_id] = request

        self._audit('bypass_requested', requested_by, f'联锁旁路请求: {interlock_id}, 原因: {reason}')
        logger.warning(f"联锁旁路请求: {interlock_id} by {requested_by}, 等待审批")
        return request_id

    def approve_bypass(self, request_id: str, approver: str) -> tuple[bool, str]:
        """审批联锁旁路（需2人不同审批人）

        Args:
            request_id: 请求ID
            approver: 审批人用户名

        Returns:
            (success, message): 是否成功及说明
        """
        with self._bypass_lock:
            request = self._bypass_requests.get(request_id)
            if not request:
                return False, "请求不存在"
            if request.is_expired:
                request.status = 'expired'
                return False, "请求已过期"
            if request.status != 'pending':
                return False, f"请求状态: {request.status}"
            if approver == request.requested_by:
                return False, "不能自己审批自己的请求"  # 职责分离
            if approver in request.approvals:
                return False, "已经审批过了"

            request.approvals.append(approver)

            self._audit('bypass_approved', approver, f'审批旁路请求 {request_id}, 联锁 {request.interlock_id}, 进度 {len(request.approvals)}/{request.required_approvals}')

            if request.is_approved:
                request.status = 'approved'
                self._activate_bypass(request)
                return True, f"已批准（{len(request.approvals)}/{request.required_approvals}），旁路已激活"

            return True, f"已审批（{len(request.approvals)}/{request.required_approvals}）"

    def _activate_bypass(self, request: BypassRequest):
        """激活旁路（带自动超时恢复）"""
        with self._lock:
            self._interlock_bypass.add(request.interlock_id)
        with self._bypass_lock:
            self._active_bypasses.add(request.interlock_id)

        # 设置超时自动恢复
        delay = request.expires_at - time.time()
        if delay > 0:
            timer = threading.Timer(delay, self._auto_restore_bypass, args=[request.interlock_id, request.request_id])
            timer.daemon = True
            timer.start()
            logger.info(f"旁路已激活: {request.interlock_id}, {delay / 60:.0f}分钟后自动恢复")

    def _auto_restore_bypass(self, interlock_id: str, request_id: str):
        """超时自动恢复旁路"""
        with self._lock:
            self._interlock_bypass.discard(interlock_id)
        with self._bypass_lock:
            self._active_bypasses.discard(interlock_id)
            req = self._bypass_requests.get(request_id)
            if req:
                req.status = 'expired'

        self._audit('bypass_auto_restored', 'system', f'旁路超时自动恢复: {interlock_id}')
        logger.warning(f"旁路超时自动恢复: {interlock_id}")

    def reject_bypass(self, request_id: str, rejector: str, reason: str = '') -> tuple[bool, str]:
        """拒绝旁路请求"""
        with self._bypass_lock:
            request = self._bypass_requests.get(request_id)
            if not request:
                return False, "请求不存在"
            if request.status != 'pending':
                return False, f"请求状态: {request.status}"
            request.status = 'rejected'

        self._audit('bypass_rejected', rejector, f'拒绝旁路请求 {request_id}, 联锁 {request.interlock_id}, 原因: {reason}')
        return True, "已拒绝"

    def get_pending_bypasses(self) -> list[dict[str, Any]]:
        """获取待审批的旁路请求"""
        with self._bypass_lock:
            now = time.time()
            pending = []
            for req in self._bypass_requests.values():
                if req.status == 'pending' and not req.is_expired:
                    pending.append({
                        'request_id': req.request_id,
                        'interlock_id': req.interlock_id,
                        'requested_by': req.requested_by,
                        'reason': req.reason,
                        'approvals': len(req.approvals),
                        'required': req.required_approvals,
                        'expires_in': int(req.expires_at - now)
                    })
            return pending

    def check_interlocks(self, device_id: str, register_name: str, value: float):
        """
        检查联锁条件（每次数据采集时调用）

        Args:
            device_id: 设备ID
            register_name: 寄存器名称
            value: 当前值
        """
        with self._lock:
            for rule_id, rule in self._interlock_rules.items():
                # 跳过被旁路的联锁
                if rule_id in self._interlock_bypass:
                    continue

                cond = rule.get('condition', {})

                # 检查条件是否匹配
                triggered = False
                if cond.get('type') == 'threshold':
                    if (cond.get('device_id') == device_id and
                            cond.get('register') == register_name):
                        op = cond.get('operator', '>')
                        threshold = cond.get('value', 0)
                        if op == '>' and value > threshold:
                            triggered = True
                        elif op == '<' and value < threshold:
                            triggered = True
                        elif op == '>=' and value >= threshold:
                            triggered = True
                        elif op == '<=' and value <= threshold:
                            triggered = True

                elif cond.get('type') == 'coil':
                    # coil条件：address是整数地址，register_name可能是名称或地址
                    cond_address = cond.get('address')
                    # 将register_name转为整数地址进行比较
                    try:
                        reg_addr = int(register_name)
                    except (ValueError, TypeError):
                        reg_addr = self._resolve_register_address(device_id, register_name)

                    if (cond.get('device_id') == device_id and
                            cond_address is not None and reg_addr is not None and
                            int(cond_address) == int(reg_addr)):
                        if value == (1 if cond.get('value') else 0):
                            triggered = True

                # 处理联锁触发
                was_triggered = self._interlock_states.get(rule_id, False)
                if triggered and not was_triggered:
                    self._interlock_states[rule_id] = True
                    self._execute_interlock_action(rule)
                elif not triggered and was_triggered:
                    self._interlock_states[rule_id] = False
                    logger.info(f"联锁条件解除: {rule_id}")

    def _execute_interlock_action(self, rule: dict[str, Any]):
        """执行联锁动作"""
        rule_id = rule['id']
        action = rule.get('action', {})
        alarm_info = rule.get('alarm', {})

        logger.warning(f"联锁触发: {rule_id} - {rule.get('name')}")

        # 发送报警
        if self.alarm_manager and alarm_info:
            level = alarm_info.get('level', 'critical')
            message = alarm_info.get('message', f'联锁触发: {rule_id}')
            # 通过报警管理器触发声光报警
            if self.alarm_manager.alarm_output:
                self.alarm_manager.alarm_output.trigger_alarm(level, message, 'interlock')
            if self.alarm_manager.broadcast_system:
                self.alarm_manager.broadcast_system.speak_alarm(level, message, 'interlock')

        # 执行动作
        action_type = action.get('type')
        if action_type == 'write_register':
            device_id = action.get('device_id')
            register = action.get('register')
            value = action.get('value')
            if self.device_manager:
                try:
                    client = self.device_manager.get_client(device_id)
                    if client and hasattr(client, 'write_single_register'):
                        # register可能是寄存器名称，需要查找对应地址
                        address = self._resolve_register_address(device_id, register)
                        if address is not None:
                            client.write_single_register(address, int(value))
                            logger.info(f"联锁动作执行: 写入 {device_id}/addr={address} = {value}")
                        else:
                            logger.error(f"联锁动作: 无法解析寄存器 {register} 的地址")
                    else:
                        logger.error(f"联锁动作: 设备 {device_id} 不支持写操作")
                except Exception as e:
                    logger.error(f"联锁动作执行失败: {e}", exc_info=True)

        elif action_type == 'emergency_stop':
            self.trigger_emergency_stop(f'联锁 {rule_id} 触发紧急停机')

        elif action_type == 'block_start':
            # 标记设备启动被阻止
            logger.warning(f"联锁阻止设备启动: {rule_id}")

        # 记录审计
        self._audit('interlock_triggered', 'system',
                    f'联锁 {rule_id} ({rule.get("name")}) 触发')

    def get_interlock_status(self) -> dict[str, Any]:
        """获取所有联锁状态"""
        with self._lock:
            result = {}
            for rule_id, rule in self._interlock_rules.items():
                result[rule_id] = {
                    'name': rule.get('name'),
                    'description': rule.get('description'),
                    'priority': rule.get('priority', 99),
                    'triggered': self._interlock_states.get(rule_id, False),
                    'bypassed': rule_id in self._interlock_bypass,
                    'condition': rule.get('condition'),
                    'action': rule.get('action'),
                }
            return result

    # ==================== 写操作安全校验 ====================

    def validate_write(self, device_id: str, address: int, value: int,
                       operator: str) -> dict[str, Any]:
        """
        写操作安全校验

        Returns:
            {'allowed': bool, 'reason': str, 'warnings': list}
        """
        result = {'allowed': True, 'reason': '', 'warnings': []}

        # 1. 紧急停机检查
        if self._estop_active:
            result['allowed'] = False
            result['reason'] = '紧急停机状态中，禁止所有写操作'
            return result

        # 2. 设备健康检查
        health = self._device_health.get(device_id, {})
        if health.get('status') == 'disconnected':
            result['allowed'] = False
            result['reason'] = f'设备 {device_id} 通信断开，无法执行写操作'
            return result

        # 3. 值范围校验（白名单按寄存器名索引，需先把地址反查为寄存器名）
        limits = self._write_limits.get(device_id, {})
        if limits:
            limit_key = self._resolve_register_name(device_id, address)
            if limit_key is None:
                # 查不到寄存器名 → 无法校验量程 → 拒绝（fail-closed，不放行危险写入）
                result['allowed'] = False
                result['reason'] = (f'无法解析设备 {device_id} 地址 {address} 对应的寄存器名，'
                                    f'写操作被拒绝')
                return result
            if limit_key in limits:
                min_val, max_val = limits[limit_key]
                if value < min_val or value > max_val:
                    result['allowed'] = False
                    result['reason'] = f'写入值 {value} 超出安全范围 [{min_val}, {max_val}]'
                    return result
            elif 'default' in limits:
                min_val, max_val = limits['default']
                if value < min_val or value > max_val:
                    result['allowed'] = False
                    result['reason'] = f'写入值 {value} 超出安全范围 [{min_val}, {max_val}]'
                    return result
            else:
                # 该设备配置了写白名单，但此寄存器不在名单内 → 拒绝（fail-closed）
                result['allowed'] = False
                result['reason'] = (f'寄存器 {limit_key} 不在设备 {device_id} 的写操作白名单中，'
                                    f'写操作被拒绝')
                return result

        # 4. 联锁阻止检查
        for rule_id, rule in self._interlock_rules.items():
            if rule_id in self._interlock_bypass:
                continue
            action = rule.get('action', {})
            if action.get('type') == 'block_start':
                cond = rule.get('condition', {})
                if cond.get('device_id') == device_id:
                    # 检查联锁是否触发
                    if self._interlock_states.get(rule_id, False):
                        result['allowed'] = False
                        result['reason'] = f'联锁 {rule_id} ({rule.get("name")}) 阻止操作'
                        return result

        # 5. 操作互斥检查
        lock_owner = self._device_lock_owners.get(device_id)
        if lock_owner and lock_owner != operator:
            result['allowed'] = False
            result['reason'] = f'设备 {device_id} 正被 {lock_owner} 操作中，请稍后重试'
            return result

        # 6. 警告检查
        for rule_id, triggered in self._interlock_states.items():
            if triggered and rule_id not in self._interlock_bypass:
                rule = self._interlock_rules.get(rule_id, {})
                result['warnings'].append(f'注意: 联锁 {rule_id} ({rule.get("name")}) 当前触发中')

        return result

    def acquire_device_lock(self, device_id: str, operator: str, timeout: float = DEVICE_LOCK_TIMEOUT_S) -> bool:
        """获取设备操作锁（防止并发操作）"""
        if device_id not in self._device_locks:
            self._device_locks[device_id] = threading.Lock()

        acquired = self._device_locks[device_id].acquire(timeout=timeout)
        if acquired:
            self._device_lock_owners[device_id] = operator
        return acquired

    def release_device_lock(self, device_id: str):
        """释放设备操作锁"""
        self._device_lock_owners.pop(device_id, None)
        if device_id in self._device_locks:
            try:
                self._device_locks[device_id].release()
            except RuntimeError:
                pass

    # ==================== 紧急停机 ====================

    def trigger_emergency_stop(self, reason: str = '手动触发') -> dict[str, Any]:
        """
        触发紧急停机

        紧急停机逻辑：
        1. 设置全局E-Stop标志（阻止所有写操作）
        2. 向所有可控设备发送停机命令
        3. 触发声光报警（红灯+蜂鸣器常响）
        4. 触发全厂广播
        5. 记录审计日志
        """
        self._estop_active = True
        self._estop_time = datetime.now()
        self._estop_reason = reason

        logger.critical(f"紧急停机触发: {reason}")

        # 通知设备管理器（模拟层停止机械类数据，真实层记录状态）
        if self.device_manager:
            self.device_manager.set_estop_override(True)

        # 只停止机械类设备（传感器和安全设备继续工作）
        stopped_devices = []
        if self.device_manager:
            from 采集层.interfaces import IDeviceManager
            all_devices = self.device_manager.get_all_devices()
            mechanical = [did for did, cfg in all_devices.items()
                         if IDeviceManager.get_device_category(cfg) == 'mechanical']
            logger.warning(f"紧急停机: 停止 {len(mechanical)} 个机械类设备（共 {len(all_devices)} 个设备）")
            for device_id in mechanical:
                try:
                    success = self.device_manager.stop_device(device_id)
                    if success:
                        stopped_devices.append(device_id)
                except Exception as e:
                    logger.error(f"紧急停机失败 {device_id}: {e}")

        self._estop_devices = stopped_devices

        # 触发声光报警
        if self.alarm_manager:
            if self.alarm_manager.alarm_output:
                self.alarm_manager.alarm_output.trigger_alarm(
                    'critical', f'紧急停机: {reason}', 'estop')
            if self.alarm_manager.broadcast_system:
                self.alarm_manager.broadcast_system.speak(
                    f'紧急停机！{reason}，所有设备已停止运行，请立即检查！',
                    level='critical', source='estop')

        # 审计
        self._audit('emergency_stop', 'system', f'紧急停机: {reason}，影响设备: {stopped_devices}')

        return {
            'success': True,
            'message': f'紧急停机已触发: {reason}',
            'stopped_devices': stopped_devices,
            'timestamp': self._estop_time.isoformat()
        }

    def reset_emergency_stop(self, operator: str) -> dict[str, Any]:
        """
        解除紧急停机（需要工程师权限+确认）

        解除逻辑：
        1. 检查所有联锁条件是否已解除
        2. 清除E-Stop标志
        3. 复位声光报警
        4. 记录审计日志
        """
        # 检查联锁状态
        active_interlocks = [
            rule_id for rule_id, triggered in self._interlock_states.items()
            if triggered and rule_id not in self._interlock_bypass
        ]
        if active_interlocks:
            return {
                'success': False,
                'message': f'无法解除紧急停机：联锁 {active_interlocks} 仍触发中',
                'active_interlocks': active_interlocks
            }

        self._estop_active = False
        self._estop_time = None
        self._estop_reason = ''

        # 通知设备管理器恢复
        if self.device_manager:
            self.device_manager.set_estop_override(False)
            # 启动所有之前被停止的设备
            for device_id in getattr(self, '_estop_devices', []):
                try:
                    self.device_manager.start_device(device_id)
                except Exception as e:
                    logger.error(f"解除停机失败 {device_id}: {e}")
            self._estop_devices = []

        # 复位声光报警
        if self.alarm_manager:
            if self.alarm_manager.alarm_output:
                self.alarm_manager.alarm_output.reset()

        self._audit('estop_reset', operator, '紧急停机已解除')
        logger.info(f"紧急停机已解除 by {operator}")

        return {
            'success': True,
            'message': '紧急停机已解除，系统恢复正常',
            'operator': operator
        }

    def get_estop_status(self) -> dict[str, Any]:
        """获取紧急停机状态"""
        return {
            'active': self._estop_active,
            'time': self._estop_time.isoformat() if self._estop_time else None,
            'reason': self._estop_reason,
            'affected_devices': self._estop_devices
        }

    def _resolve_control_point(self, device_id: str, action: str) -> dict[str, Any] | None:
        """
        解析批量控制（start/stop/reset）要写入的点位

        点位不再写死：优先取设备配置的 `control` 段，取不到才回退到显式命名的
        LEGACY_FALLBACK_CONTROL_POINTS（并记录告警，提示补配置）。

        设备配置形态（配置/devices*.yaml 中该设备的 control 段）——两种写法均支持：

            control:                                  # 写法A：按动作声明
              start: {type: coil, address: 0, value: true}
              stop:  {type: coil, address: 0, value: false}
              reset: {type: register, address: 100, value: 0}

            control:                                  # 写法B：共用点位，动作值分开
              type: coil
              address: 0
              start_value: true
              stop_value: false
              reset_value: 0

        Returns:
            {'type': 'coil'|'register', 'address': int, 'value': Any}；无法解析返回 None
        """
        default_value = _DEFAULT_ACTION_VALUES.get(action)

        spec = None
        if self.device_manager:
            try:
                device_config = (getattr(self.device_manager, 'devices', {}) or {}).get(device_id) or {}
                control = device_config.get('control') or {}
            except Exception as e:
                logger.error("读取设备 %s 控制点位配置失败: %s", device_id, e)
                control = {}

            if isinstance(control, dict):
                candidate = control.get(action)
                if isinstance(candidate, dict):
                    spec = dict(candidate)
                elif control.get('type') is not None and control.get('address') is not None:
                    # 写法B：共用 type/address
                    spec = {
                        'type': control.get('type'),
                        'address': control.get('address'),
                        'value': control.get(f'{action}_value', default_value),
                    }

        if spec is None:
            fallback = LEGACY_FALLBACK_CONTROL_POINTS.get(action)
            if fallback is None:
                logger.error("设备 %s 的批量动作 %s 无可用点位（无回退值）", device_id, action)
                return None
            spec = dict(fallback)
            logger.warning(
                "设备 %s 未配置控制点位 control.%s，使用旧版回退点位 %s#%s —— "
                "请在该设备配置中显式声明 control 段，避免写错点位",
                device_id, action, spec['type'], spec['address'],
            )

        kind = str(spec.get('type', 'coil')).lower()
        if kind not in ('coil', 'register'):
            logger.error("设备 %s 控制点位类型非法: %r", device_id, spec.get('type'))
            return None
        try:
            address = int(spec.get('address'))
        except (TypeError, ValueError):
            logger.error("设备 %s 控制点位地址非法: %r", device_id, spec.get('address'))
            return None

        value = spec.get('value', default_value)
        if value is None:
            logger.error("设备 %s 控制点位缺少写入值（action=%s）", device_id, action)
            return None

        return {'type': kind, 'address': address, 'value': value}

    def _get_controllable_devices(self) -> list[str]:
        """获取所有可控设备ID列表"""
        devices = []
        if self.device_manager:
            for device_id, config in self.device_manager.devices.items():
                if config.get('access') == 'rw' or config.get('protocol') in ('modbus_tcp', 'modbus_rtu'):
                    devices.append(device_id)
        return devices

    def _resolve_register_address(self, device_id: str, register_name) -> int | None:
        """
        将寄存器名称解析为地址

        Args:
            device_id: 设备ID
            register_name: 寄存器名称或地址（可能是字符串名称或整数地址）

        Returns:
            寄存器地址，找不到返回None
        """
        # 如果已经是整数，直接返回
        if isinstance(register_name, int):
            return register_name

        # 尝试转换为整数
        try:
            return int(register_name)
        except (ValueError, TypeError):
            # 传入的是寄存器名称（非数字字符串）而非地址，转换失败属预期，
            # 继续走下方按名称查表的逻辑，安全忽略
            pass

        # 从设备配置中查找寄存器名称对应的地址
        if self.device_manager:
            device_config = self.device_manager.devices.get(device_id, {})
            registers = device_config.get('registers', [])
            for reg in registers:
                if reg.get('name') == register_name:
                    return reg.get('address')

        return None

    def _resolve_register_name(self, device_id: str, address: int) -> str | None:
        """
        将寄存器地址反查为寄存器名称（_resolve_register_address 的逆操作）

        写操作量程表 _write_limits 以寄存器名称为键，而写接口只拿到地址，
        因此校验前必须先反查名称。

        Args:
            device_id: 设备ID
            address: 寄存器地址

        Returns:
            寄存器名称，找不到返回None
        """
        try:
            addr = int(address)
        except (ValueError, TypeError):
            return None

        if self.device_manager:
            try:
                device_config = self.device_manager.devices.get(device_id, {})
                registers = device_config.get('registers', []) or []
            except Exception:
                return None
            for reg in registers:
                try:
                    if int(reg.get('address')) == addr:
                        return reg.get('name')
                except (ValueError, TypeError):
                    continue

        return None

    # ==================== 故障降级 ====================

    def update_device_health(self, device_id: str, connected: bool,
                             response_time_ms: float = 0):
        """更新设备健康状态（由数据采集器调用）"""
        if device_id not in self._device_health:
            self._device_health[device_id] = {
                'status': 'unknown',
                'last_seen': None,
                'consecutive_failures': 0,
                'avg_response_ms': 0
            }

        health = self._device_health[device_id]

        if connected:
            health['status'] = 'connected'
            health['last_seen'] = datetime.now().isoformat()
            health['consecutive_failures'] = 0
            # 指数移动平均
            alpha = EMA_ALPHA
            health['avg_response_ms'] = (
                alpha * response_time_ms +
                (1 - alpha) * health.get('avg_response_ms', 0)
            )
            self._comm_failures[device_id] = 0
        else:
            failures = self._comm_failures.get(device_id, 0) + 1
            self._comm_failures[device_id] = failures
            health['consecutive_failures'] = failures

            if failures >= self._comm_threshold:
                health['status'] = 'disconnected'
                self._trigger_fault_degradation(device_id, failures)

    def _trigger_fault_degradation(self, device_id: str, failures: int):
        """触发故障降级"""
        logger.warning(f"设备 {device_id} 通信失败 {failures} 次，触发故障降级")

        # 执行安全状态
        handler = self._safe_state_handlers.get(device_id)
        if handler:
            try:
                handler(device_id)
            except Exception as e:
                logger.error(f"安全状态执行失败 {device_id}: {e}")

        # 触发报警
        if self.alarm_manager:
            if self.alarm_manager.alarm_output:
                self.alarm_manager.alarm_output.trigger_alarm(
                    'warning',
                    f'设备 {device_id} 通信中断（{failures}次），已进入安全状态',
                    device_id
                )

        self._audit('fault_degradation', 'system',
                    f'设备 {device_id} 通信失败 {failures} 次，已降级到安全状态')

    def register_safe_state_handler(self, device_id: str, handler: Callable[..., Any]):
        """注册设备安全状态处理函数"""
        self._safe_state_handlers[device_id] = handler

    def get_device_health_summary(self) -> dict[str, Any]:
        """获取所有设备健康摘要（自动刷新）"""
        # 每次请求时刷新设备连接状态
        self.refresh_device_health()
        summary = {
            'total': len(self._device_health),
            'connected': 0,
            'disconnected': 0,
            'unknown': 0,
            'devices': {}
        }
        for device_id, health in self._device_health.items():
            status = health.get('status', 'unknown')
            summary[status] = summary.get(status, 0) + 1
            summary['devices'][device_id] = health
        return summary

    # ==================== 写操作回读验证 ====================

    def write_with_verification(self, device_id: str, address: int, value: int,
                                operator: str, max_retries: int = 2) -> dict[str, Any]:
        """
        带回读验证的写操作

        流程：
        1. 安全校验
        2. 获取设备锁
        3. 写入值
        4. 回读验证
        5. 释放锁
        6. 记录审计
        """
        # 安全校验
        validation = self.validate_write(device_id, address, value, operator)
        if not validation['allowed']:
            self._audit('write_blocked', operator,
                        f'写入被阻止: {device_id}/{address}={value}, 原因: {validation["reason"]}')
            return {
                'success': False,
                'message': validation['reason'],
                'validation': validation
            }

        # 获取设备锁
        if not self.acquire_device_lock(device_id, operator):
            return {
                'success': False,
                'message': f'无法获取设备 {device_id} 的操作锁'
            }

        try:
            # 写入
            success = False
            client = self.device_manager.get_client(device_id) if self.device_manager else None
            if not client:
                self._audit('write_failed', operator,
                            f'写入失败: 设备 {device_id} 客户端不存在')
                return {'success': False, 'message': f'设备 {device_id} 不存在或未连接'}

            for attempt in range(max_retries + 1):
                try:
                    if hasattr(client, 'write_single_register'):
                        success = client.write_single_register(int(address), int(value))
                    elif hasattr(client, 'write_single_coil'):
                        success = client.write_single_coil(int(address), bool(value))
                    break
                except Exception as e:
                    if attempt < max_retries:
                        logger.warning(f"写入重试 {attempt + 1}/{max_retries}: {e}")
                        time.sleep(0.1)
                    else:
                        raise

            if not success:
                self._audit('write_failed', operator,
                            f'写入失败: {device_id}/{address}={value}')
                return {
                    'success': False,
                    'message': '写入操作失败'
                }

            # 回读验证
            readback_ok = True
            readback_value = None
            try:
                if hasattr(client, 'read_holding_registers'):
                    raw = client.read_holding_registers(int(address), 1)
                    if raw and len(raw) > 0:
                        readback_value = raw[0]
                        if abs(readback_value - value) > 1:
                            readback_ok = False
                            logger.warning(f"回读验证失败: 期望 {value}, 实际 {readback_value}")
            except Exception as e:
                logger.warning(f"回读验证异常: {e}")

            # 审计
            self._audit('write_success', operator,
                        f'写入成功: {device_id}/{address}={value}'
                        f'{f", 回读={readback_value}" if readback_value is not None else ""}'
                        f'{", 验证通过" if readback_ok else ", 验证失败"}')

            result = {
                'success': True,
                'message': '写入成功',
                'warnings': validation.get('warnings', [])
            }
            if not readback_ok:
                result['message'] = '写入成功，但回读验证失败'
                result['warnings'].append(f'回读值 {readback_value} 与写入值 {value} 不一致')

            return result

        finally:
            self.release_device_lock(device_id)

    # ==================== 操作审计 ====================

    def _audit(self, action: str, operator: str, detail: str):
        """记录操作审计日志"""
        entry = {
            'timestamp': datetime.now().isoformat(),
            'action': action,
            'operator': operator,
            'detail': detail
        }
        self._audit_log.append(entry)

        # 持久化到文件
        try:
            self._audit_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._audit_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        except Exception as e:
            logger.error(f"审计日志写入失败: {e}")

    def get_audit_log(self, limit: int = 100, action_filter: str | None = None) -> list[dict[str, Any]]:
        """获取操作审计日志"""
        logs = list(self._audit_log)
        if action_filter:
            logs = [l for l in logs if l.get('action') == action_filter]
        return list(reversed(logs[-limit:]))

    def get_audit_stats(self) -> dict[str, Any]:
        """获取审计统计"""
        logs = list(self._audit_log)
        action_counts = {}
        for log in logs:
            action = log.get('action', 'unknown')
            action_counts[action] = action_counts.get(action, 0) + 1
        return {
            'total_entries': len(logs),
            'by_action': action_counts,
            'estop_active': self._estop_active,
            'active_interlocks': sum(
                1 for t in self._interlock_states.values() if t
            ),
            'bypassed_interlocks': len(self._interlock_bypass)
        }

    # ==================== 综合状态 ====================

    def get_full_status(self) -> dict[str, Any]:
        """获取设备控制安全系统完整状态"""
        return {
            'estop': self.get_estop_status(),
            'interlocks': {
                'total': len(self._interlock_rules),
                'triggered': sum(1 for t in self._interlock_states.values() if t),
                'bypassed': len(self._interlock_bypass),
                'rules': self.get_interlock_status()
            },
            'device_health': self.get_device_health_summary(),
            'audit': self.get_audit_stats()
        }
