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
from core.safe_numeric import to_finite_float, try_finite_float, try_int

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
#
# ⚠️ 真实模式**不经过**此回退（2026-10-14 外部审查 #3 响应，防"值存在"被误读为"会被用"）：
#   真实设备模式下点位解析不出会**直接拒绝写入**（_resolve_control_point 返回 None →
#   调用处记 success=False + 审计），本回退**只服务模拟模式**（无物理风险）。
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
    expires_at: float  # 超时自动过期（**墙钟**，仅用于展示"旁路到 14:30 结束"）
    reason: str
    approvals: list = field(default_factory=list)
    required_approvals: int = 2  # 至少2人审批
    status: str = 'pending'  # pending/approved/rejected/expired
    #: 与 `expires_at` 等价的**单调时钟**副本，**只用于过期判定**（round 311）。
    #:
    #: 为什么需要两份：`expires_at` 要给人看（绝对时刻），而判定表达的是
    #: **时长**。用墙钟判定时，NTP 校时/手工改时间会让旁路
    #: ①往后拨 → 旁路**延长**（本该 30 分钟恢复的联锁，实际旁路了两小时）；
    #: ②往前拨 → 旁路**立即过期**，维护窗口中途联锁突然恢复，正在检修的人被夹。
    #: `0.0` 表示"没有单调时钟副本"（旧数据 / 反序列化），此时回退墙钟。
    expires_at_mono: float = 0.0

    @property
    def is_approved(self) -> bool:
        return len(self.approvals) >= self.required_approvals and self.status == 'pending'

    @property
    def is_expired(self) -> bool:
        # round 311：优先单调时钟；没有副本（旧数据）时回退墙钟
        if self.expires_at_mono:
            return time.monotonic() > self.expires_at_mono
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
        #: 最近一次加载的摘要（round 308）：声明/生效/丢弃。
        #: `None` 表示还没加载过 —— 与「加载了但一条都没有」必须可区分。
        self._interlock_load_summary: dict[str, Any] | None = None
        self._interlock_states: dict[str, bool] = {}  # rule_id -> is_triggered
        # round 302：条件成立但**动作没发出去**的次数（写停机值失败 / 急停抛异常）。
        # 状态位此时保持 False（等下一个采样点重试），所以光看 `triggered`
        # 会以为"没触发"—— 这个计数让「触发过但没执行」在状态接口里可见。
        self._interlock_action_failures: dict[str, int] = {}
        self._interlock_bypass: Set[str] = set()  # 被旁路的联锁ID
        self._lock = threading.Lock()

        # ===== 联锁旁路审批（IEC 61511） =====
        #: 旁路请求表（按 request_id）。**必须有上限**（round 311）：
        #: request_id 是 uuid，每申请一次就多一条，而审批完成后
        #: `approve_bypass` / `reject_bypass` 都只改 `status`、**从不删除** ——
        #: 一个每年几百次维护旁路的现场，进程跑一年就攒几千条（每条含原因文本），
        #: 且 `get_pending_bypasses()` 每次都要遍历全表。
        #: 上限见 `_trim_bypass_requests()`：只淘汰**已结束**的，pending 永不丢。
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
        # round 303：已进入故障降级的设备。没有它，`failures >= 阈值` 之后
        # **每个失败采样点**都会重跑安全状态 handler + 报警 + 审计
        # （5 分钟掉线 = 60 次重复动作，审计被自己的降级日志刷满）。
        # 与联锁的 `triggered and not was_triggered` 同一判据：只在边沿迁移。
        self._degraded_devices: set[str] = set()
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
        1. 构造参数 config['interlocks']（显式传入，测试/嵌入式注入）
        2. 配置/interlocks.yaml（生产推荐：含真实设备ID与寄存器名）
        3. 各设备配置中的 `interlocks` 段（按设备就近声明）

        ⚠️ round 247 调整过 1/2 的顺序：原来是「文件 > 注入」，
        在补了 `配置/interlocks.yaml` 之后暴露出真实问题 ——
        调用方显式注入的规则会被仓库里的文件**静默顶掉**，
        注入方以为自己的规则生效了，实际跑的是文件里的规则。
        显式传入优先于环境文件，是更稳的默认方向。

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

        # 1. 构造参数注入（**显式传入优先于环境文件**）
        # 优先级理由：调用方在代码里明确给出的配置，比磁盘上恰好存在的文件
        # 更具体。原顺序是「文件 > 注入」，在 round 247 补了
        # `配置/interlocks.yaml` 之后暴露出真实问题：测试（以及任何嵌入方）
        # 注入的规则会被仓库里的配置文件**静默顶掉**，注入方以为自己的规则
        # 生效了，实际跑的是文件里的规则 —— 和「联锁看似在保护」同一种病。
        injected = self.config.get('interlocks')
        if isinstance(injected, list) and injected:
            rules, source = list(injected), "config['interlocks']"

        # 2. 配置文件（生产环境的规则来源）
        if not rules:
            file_rules = self._load_interlocks_from_file()
            if file_rules:
                rules, source = file_rules, str(paths.resolve(INTERLOCK_CONFIG_PATH))

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
            # round 308：**空载也要留摘要**。否则 `get_interlock_status()`
            # 返回空字典，界面/接口看到的是"没有联锁"而不是"联锁加载失败"，
            # 与"配了但全被丢弃"无法区分。
            self._interlock_load_summary = {
                'source': None, 'declared': 0, 'loaded': 0,
                'rejected': 0, 'empty': True,
            }
            return

        loaded = 0
        rejected = 0
        for rule in rules:
            if not isinstance(rule, dict):
                logger.error("忽略非法联锁规则（非字典）: %r", rule)
                rejected += 1
                continue
            rule_id = rule.get('id')
            if not rule_id:
                logger.error("忽略缺 id 的联锁规则: %r", rule)
                rejected += 1
                continue
            if 'condition' not in rule or 'action' not in rule:
                logger.error("忽略缺 condition/action 的联锁规则: %s", rule_id)
                rejected += 1
                continue
            rule.setdefault('name', rule_id)
            rule.setdefault('priority', 1)
            rule.setdefault('enabled', True)
            self._interlock_rules[rule_id] = rule
            self._interlock_states.setdefault(rule_id, False)
            loaded += 1

        # round 308：**部分丢弃必须汇总上报**。
        # 原来只有逐条 `logger.error` + 一句 INFO「加载 N 条」——
        # `interlocks.yaml` 里 12 条规则、5 条写错（缺 condition / 缩进吃掉
        # action），现场看到的是「已加载 7 条」，**没有任何地方提示还有 5 条
        # 被丢了**。那 5 台设备的保护等于不存在，而系统显示"联锁正常"。
        # 判据：加载结果必须是 (声明数, 生效数, 丢弃数) 三元组，
        # 丢弃 > 0 时用 WARNING（进得了日志告警），并把摘要暴露给状态接口。
        self._interlock_load_summary = {
            'source': source,
            'declared': len(rules),
            'loaded': loaded,
            'rejected': rejected,
            'empty': False,
        }
        if rejected:
            logger.warning(
                "安全联锁**部分丢弃**：%s 声明 %d 条，生效 %d 条，丢弃 %d 条"
                "（被丢弃的规则不会保护任何设备，请检查上面的 error 日志）",
                source, len(rules), loaded, rejected,
            )
        else:
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
                    # `write_limits: [-.inf, .inf]`（YAML 合法）等于「不设限」，
                    # 但界面上看是配了量程的 —— 必须当非法配置拒掉。
                    low = to_finite_float(bounds[0], 'write_limits.low')
                    high = to_finite_float(bounds[1], 'write_limits.high')
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
                    # round 303：**从判据那份派生**，不另写一个数字。
                    # 原来未连接时直接写死 3 —— 阈值是 3 的话，界面一上来就
                    # 显示「已经失败 3 次」，而 `_comm_failures` 还是 0；
                    # 两套数字从第一秒就不一致。
                    'consecutive_failures': self._comm_failures.get(device_id, 0),
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
                    # round 303：**同一份真值只能有一个写入口**。
                    # 原来这里只清 `health['consecutive_failures']`、不清
                    # `_comm_failures`：界面上的失败次数被抹成 0，而判据用的
                    # 那一份还是 3 —— 再失败一次就 `4 >= 阈值` 立刻判断线并
                    # 再次降级，实际只失败了一次。两处必须一起清。
                    self._comm_failures[device_id] = 0
                    health['consecutive_failures'] = 0
                    # 恢复健康后允许下一次掉线重新降级
                    self._degraded_devices.discard(device_id)
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

                # 按点位类型走对应写法，**不允许跨类型退回**：
                # 线圈和寄存器在现场是两个对象，退回写等于写错地方，
                # 而调用方只会看到「成功」。不支持就明确失败。
                unsupported = None
                if point['type'] == 'coil':
                    if hasattr(client, 'write_single_coil'):
                        success = client.write_single_coil(point['address'], bool(point['value']))
                    else:
                        unsupported = '客户端不支持线圈写入'
                else:  # register
                    if hasattr(client, 'write_single_register'):
                        success = client.write_single_register(point['address'], int(point['value']))
                    else:
                        unsupported = '客户端不支持寄存器写入'

                if unsupported:
                    results[device_id] = {'success': False, 'message': unsupported}
                    self._audit(f'batch_{action}', operator,
                                f'批量{action}设备 {device_id}: {unsupported}')
                    continue

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

    def get_interlock_load_summary(self) -> dict[str, Any]:
        """最近一次联锁加载的摘要（round 308）。

        单独一个方法，**不塞进 `get_interlock_status()` 的返回值** ——
        那个方法的契约是 `{rule_id: 规则状态}`，多一个魔法 key 会让
        `== {}` 的调用方和遍历 key 的前端把摘要当成一条规则。

        Returns:
            dict: `{source, declared, loaded, rejected, empty}`
            `empty=True` 且 `declared=0` = 没配任何规则；
            `declared>loaded` = 有规则被丢弃（保护缺失，必须告警）。
        """
        summary = self._interlock_load_summary
        if summary is None:
            return {'source': None, 'declared': 0, 'loaded': 0,
                    'rejected': 0, 'empty': True, 'loaded_once': False}
        return {**summary, 'loaded_once': True}

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
            expires_at_mono=time.monotonic() + timeout_minutes * 60,
            reason=reason,
            required_approvals=2
        )

        with self._bypass_lock:
            self._bypass_requests[request_id] = request
            self._trim_bypass_requests()

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
        activated = False
        progress = ''

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
            progress = f'{len(request.approvals)}/{request.required_approvals}'

            self._audit('bypass_approved', approver, f'审批旁路请求 {request_id}, 联锁 {request.interlock_id}, 进度 {progress}')

            if request.is_approved:
                request.status = 'approved'
                activated = True

        # ⚠️ 必须在锁**外**激活。
        # `_activate_bypass()` 自己会取 `self._bypass_lock`，而它是**不可重入**的
        # `threading.Lock` —— 在锁内调用必然死锁，而且卡死的正是
        # 「第二个人审批、旁路即将生效」的那一刻：审批线程永久占着锁，
        # 之后任何旁路操作（request/approve/reject/查询）全部挂住，
        # 整个联锁旁路功能静默失效（安全相关流程，不能靠重启才能恢复）。
        if activated:
            self._activate_bypass(request)
            return True, f"已批准（{progress}），旁路已激活"

        return True, f"已审批（{progress}）"

    def _activate_bypass(self, request: BypassRequest):
        """激活旁路（带自动超时恢复）"""
        with self._lock:
            self._interlock_bypass.add(request.interlock_id)
        with self._bypass_lock:
            self._active_bypasses.add(request.interlock_id)

        # 设置超时自动恢复
        # round 311：`threading.Timer` 要的是**相对时长**，必须用单调时钟算。
        # 用墙钟算的话，审批通过到启动 Timer 之间只要发生一次校时，
        # 旁路的自动恢复就会提前或推迟（极端情况下 delay 变负数 → 永不恢复）。
        delay = (request.expires_at_mono - time.monotonic()
                 if request.expires_at_mono else request.expires_at - time.time())
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

    #: 旁路请求表的内存上限。超出时**只淘汰已结束（非 pending）的**，
    #: 按申请时间从旧到新 —— 待审批的请求一条都不能丢（那是安全流程的一环）。
    MAX_BYPASS_REQUESTS_IN_MEMORY = 200

    def _trim_bypass_requests(self) -> int:
        """把旁路请求表裁到上限内（round 311）。调用方必须持有 `_bypass_lock`。

        Returns:
            int: 本次淘汰的条数。

        为什么需要：`request_id` 是 uuid，每次申请都新增一条，而
        `approve_bypass` / `reject_bypass` 只改 `status` 不删除 —— 内存**无界增长**。
        审计记录已经通过 `_audit()` 落库，内存里这份只是为了"近期可查"。
        """
        overflow = len(self._bypass_requests) - self.MAX_BYPASS_REQUESTS_IN_MEMORY
        if overflow <= 0:
            return 0

        # 只淘汰已结束的，最旧的先走
        finished = sorted(
            ((rid, r) for rid, r in self._bypass_requests.items()
             if r.status != 'pending'),
            key=lambda kv: kv[1].requested_at,
        )
        dropped = 0
        for rid, _req in finished[:overflow]:
            self._bypass_requests.pop(rid, None)
            dropped += 1

        if dropped < overflow:
            # 剩下全是 pending —— 说明待审批请求堆积到 200 条以上，
            # 这本身就是需要人介入的事（审批流程卡住了），不能静默。
            logger.error(
                "旁路请求表已达上限 %d 条，仍有 %d 条**待审批**无法淘汰"
                "（审批流程可能已停滞，请检查待办）",
                self.MAX_BYPASS_REQUESTS_IN_MEMORY,
                len(self._bypass_requests))
        else:
            logger.info("旁路请求表超出上限，淘汰 %d 条已结束请求", dropped)
        return dropped

    def get_pending_bypasses(self) -> list[dict[str, Any]]:
        """获取待审批的旁路请求"""
        with self._bypass_lock:
            now_mono = time.monotonic()
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
                        'expires_in': int(
                            (req.expires_at_mono - now_mono) if req.expires_at_mono
                            else (req.expires_at - time.time()))
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
        # 非有限值（NaN / ±Infinity）= 读数无效，**不是「条件解除」**。
        #
        # 原实现直接往下走：所有阈值比较对 NaN 都返回 False → `triggered=False`
        # → 走 `elif not triggered and was_triggered` 分支 →
        # `self._interlock_states[rule_id] = False`，还打一条
        # 「联锁条件解除」的 INFO。
        #
        # 而 `_interlock_states` 正是 **block_start 联锁的准入依据**
        # （`control_device(action='start')` 与 `validate_operation` 都读它）：
        # 状态被清成 False 之后，运维可以在超温状态下把设备启动起来。
        # 也就是说**一条 NaN 读数能把安全联锁整个绕过**，日志还会告诉他
        # 「条件已解除」—— 比不报警更糟。
        #
        # 实测（注入 block_start 规则 + 120 触发 + NaN）：
        #   {'il_temp': True}  →  {'il_temp': False}
        #
        # 处理方式与 `AlarmManager.check_alarm` 一致：本条不更新状态
        # （既不触发也不解除），留 warning。设备真离线有采集失败/熔断器兜底。
        numeric = try_finite_float(value)
        if numeric is None:
            logger.warning(
                "联锁检查收到非有限值，跳过本条（不触发也不解除已有联锁）: "
                "%s/%s value=%r", device_id, register_name, value)
            return
        value = numeric

        # round 302：锁内只做「判定 + 排队」，I/O 一律锁外执行（理由见下方执行块）
        to_execute: list[tuple[str, dict[str, Any]]] = []
        to_clear: list[str] = []

        with self._lock:
            for rule_id, rule in self._interlock_rules.items():
                # 跳过被旁路的联锁
                if rule_id in self._interlock_bypass:
                    continue

                cond = rule.get('condition', {})

                # 检查条件是否匹配
                triggered = False
                # round 298：**这次采样是不是本条规则关心的点位**。
                # 原实现没有这个标记 —— 「D1/temperature 触发联锁」之后，
                # 下一条任意其它寄存器的数据（D1/pressure、D2/level…）
                # 走到 `elif not triggered and was_triggered` 就把状态清成 False。
                # 现场 5s 一轮，联锁存活不超过一个分发周期；
                # 而 `_interlock_states` 是启机/操作校验的唯一准入依据 ——
                # 超温状态下运维仍能启动设备，日志还写着「联锁条件解除」。
                matched = False
                if cond.get('type') == 'threshold':
                    if (cond.get('device_id') == device_id and
                            cond.get('register') == register_name):
                        matched = True          # 这条规则关心的点位到了
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
                    reg_addr = try_int(register_name)
                    if reg_addr is None:
                        reg_addr = self._resolve_register_address(device_id, register_name)

                    if (cond.get('device_id') == device_id and
                            cond_address is not None and reg_addr is not None and
                            int(cond_address) == int(reg_addr)):
                        matched = True          # 这条规则关心的点位到了
                        if value == (1 if cond.get('value') else 0):
                            triggered = True

                # round 298：采样不是这条规则的点位 → 不许动它的状态
                if not matched:
                    continue

                # 处理联锁触发
                was_triggered = self._interlock_states.get(rule_id, False)
                if triggered and not was_triggered:
                    # round 302：只**排队**，执行成功后才置位（见锁外执行块）
                    to_execute.append((rule_id, rule))
                elif not triggered and was_triggered:
                    to_clear.append(rule_id)

        # 锁外执行 I/O（round 302）
        #
        # 1) **状态锁绝不跨 I/O 持有**。`_execute_interlock_action` 要写 Modbus
        #    寄存器、写 DO 线圈、发 MQTT 广播，`emergency_stop` 还会循环停掉
        #    所有设备。网关断线时一次写能卡 3–20 秒，而 `self._lock` 同时是
        #    `get_interlock_status()` / 旁路申请 / **所有设备的联锁判定**的锁 ——
        #    卡住的是安全判定本身。这与 round 286 给 alarm_output 定的判据同源
        #    （状态锁 / I/O 锁分离），device_control 此前是唯一没做分离的核心模块。
        # 2) **动作成功才算触发**。原来先置 `_interlock_states[rule_id] = True`
        #    再执行动作：动作抛异常（声光网关断线、DO 熔断）后状态已经是 True，
        #    下一轮 `triggered and not was_triggered` 恒假 → 写停机值、急停、
        #    `interlock_triggered` 审计**永久跳过**；而界面显示「联锁已触发」、
        #    启机校验也被拦住 —— 一套看起来生效、实际什么都没做的保护。
        for rule_id, rule in to_execute:
            ok = self._execute_interlock_action(rule)
            with self._lock:
                if ok:
                    self._interlock_states[rule_id] = True
                    self._interlock_action_failures.pop(rule_id, None)
                else:
                    # 状态保持 False → 下一个采样点自动重试；
                    # 累计失败次数在 get_interlock_status() 里可见
                    self._interlock_action_failures[rule_id] = \
                        self._interlock_action_failures.get(rule_id, 0) + 1

        for rule_id in to_clear:
            with self._lock:
                self._interlock_states[rule_id] = False
            logger.info(f"联锁条件解除: {rule_id}")

    def _execute_interlock_action(self, rule: dict[str, Any]) -> bool:
        """执行联锁动作。

        Returns:
            bool: **核心动作是否真的发出去了**。False 表示调用方不该把
            `_interlock_states[rule_id]` 置 True —— 下一个采样点会自动重试。

        round 302：
        · 三个动作分支都可能抛（声光网关断线、DO 熔断、MQTT 掉线），
          原来 `trigger_alarm` / `speak_alarm` 是**裸调**：抛出去整轮报废，
          而调用方已经把状态置成 True（见那边注释）。现在逐个隔离，
          任何一个播报通道失败都不影响「写停机值」这条命脉动作。
        · 返回值改成 bool：写停机值是安全动作的关键路径，它失败必须让
          调用方知道 —— 原来只打一行 error，状态照样置位，等于假装执行过。
        """
        rule_id = rule['id']
        action = rule.get('action', {})
        alarm_info = rule.get('alarm', {})

        logger.warning(f"联锁触发: {rule_id} - {rule.get('name')}")

        # 发送报警（声光/广播：尽力而为，失败不阻断停机动作）
        if self.alarm_manager and alarm_info:
            level = alarm_info.get('level', 'critical')
            message = alarm_info.get('message', f'联锁触发: {rule_id}')
            # 通过报警管理器触发声光报警
            if self.alarm_manager.alarm_output:
                try:
                    self.alarm_manager.alarm_output.trigger_alarm(level, message, 'interlock')
                except Exception as e:      # noqa: BLE001
                    logger.error("联锁声光输出失败（继续执行停机动作）: %s", e)
            if self.alarm_manager.broadcast_system:
                try:
                    self.alarm_manager.broadcast_system.speak_alarm(level, message, 'interlock')
                except Exception as e:      # noqa: BLE001
                    logger.error("联锁广播失败（继续执行停机动作）: %s", e)

        # 执行动作
        action_ok = True
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
                            action_ok = False
                            logger.error(f"联锁动作: 无法解析寄存器 {register} 的地址")
                    else:
                        action_ok = False
                        logger.error(f"联锁动作: 设备 {device_id} 不支持写操作")
                except Exception as e:      # noqa: BLE001 —— 写失败要能重试，不能吞
                    action_ok = False
                    logger.error(f"联锁动作执行失败: {e}", exc_info=True)

        elif action_type == 'emergency_stop':
            try:
                self.trigger_emergency_stop(f'联锁 {rule_id} 触发紧急停机')
            except Exception as e:          # noqa: BLE001
                action_ok = False
                logger.error("联锁急停执行失败: %s", e, exc_info=True)

        elif action_type == 'block_start':
            # 标记设备启动被阻止
            logger.warning(f"联锁阻止设备启动: {rule_id}")

        # 记录审计（写文件也可能失败，不能因为审计挂了就丢掉动作结果）
        try:
            self._audit(
                'interlock_triggered' if action_ok else 'interlock_action_failed',
                'system',
                f'联锁 {rule_id} ({rule.get("name")}) '
                + ('触发' if action_ok else '触发但动作未发出'))
        except Exception as e:              # noqa: BLE001
            logger.error("联锁审计写入失败: %s", e)

        return action_ok

    def forget_device(self, device_id: str) -> bool:
        """设备被删除时清掉它的健康/失败计数/写锁/安全状态处理器（round 310）。

        这些容器都以 `device_id` 为 key 且没有删除点，后果里最硬的是
        **重新添加同名设备会继承旧的 `_comm_failures`**：

            # 删设备前它已经连续失败 5 次
            del device; add_device(same_id)
            # → 新设备第一次采样前就 `_comm_failures[same_id] == 5`
            # → 直接判离线 / 直接拒绝写操作（`check_write` 的健康检查）

        现场表现：设备明明在线，界面显示离线、写操作被拒，重启才恢复。
        `_device_health` 同理（`status: disconnected` 会被继承）。
        """
        removed = False
        for container in (self._device_health, self._comm_failures,
                          self._device_locks, self._safe_state_handlers):
            if device_id in container:
                container.pop(device_id, None)
                removed = True
        if removed:
            logger.info("设备控制安全模块已清理已删除设备的状态: %s", device_id)
        return removed

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
                    # round 302：条件已成立但动作没发出去（写寄存器失败/急停抛异常）。
                    # `triggered` 保持 False 是为了下个采样点重试；没有这个字段，
                    # 界面上就是"条件没成立"，而事实是"成立过、但没执行成功"。
                    'action_failures': self._interlock_action_failures.get(rule_id, 0),
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

        # 0. 非有限值（NaN / ±Infinity）一律拒绝。
        #
        #    `NaN` 与任何数比较都返回 False，所以下面第 3 步的
        #    `value < min_val or value > max_val` **判不出 NaN** —— 量程校验会
        #    直接放行，NaN 被下发到现场设备（float32 寄存器会写进一个 NaN 位型）。
        #    入口处的 `safe_float` 已经拦了一道，这里再挡一次：本方法是安全闸门，
        #    不能假设调用方都走过 API 参数解析（edge_decision / 测试可直接调用）。
        try:
            to_finite_float(value, 'value')
        except ValueError:
            result['allowed'] = False
            result['reason'] = f'写入值 {value!r} 不是有限数值（NaN/Infinity）或无法解析，拒绝写入'
            return result

        # 0. 联锁配置缺失 → fail-closed（仅真实设备模式）
        #    「没有任何联锁规则」= 一条永不触发的保护。历史实现只在加载时打
        #    warning，写操作照放 —— 等于无保护地操作现场设备。真实设备模式下
        #    必须拒绝；模拟模式没有物理风险，保持可演示。
        if not self._interlock_rules and not self._is_simulation():
            result['allowed'] = False
            result['reason'] = (
                '未加载到任何安全联锁规则，真实设备模式下拒绝所有写操作'
                f'（请配置 {INTERLOCK_CONFIG_PATH} 或设备的 interlocks 段）'
            )
            return result

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

        # 3.0 该设备**完全没配量程**时怎么办 —— 与联锁保持同一套口径。
        #
        # 原实现 `if limits:` 一判，没配量程就整段跳过：**不做任何范围校验，
        # 也不给任何提示**。而随仓库发布的 `配置/devices.yaml` 里
        # `write_limits` 出现次数为 0 —— 也就是说开箱状态下所有设备都能被写入
        # 任意值（把 100 写成 10000 也照样下发）。
        #
        # 联锁那边的口径是「真实设备模式没配规则 → fail-closed 拒绝一切写」，
        # 理由同样适用：**没有量程 = 一条永不触发的保护**。
        # 模拟模式没有物理风险，放行但留痕（演示要能跑）。
        if not limits:
            if self._is_simulation():
                result['warnings'].append(
                    f'设备 {device_id} 未配置写入量程(write_limits)，本次写入未做范围校验'
                )
            else:
                result['allowed'] = False
                result['reason'] = (
                    f'设备 {device_id} 未配置写入量程(write_limits)，'
                    f'真实设备模式下拒绝写入 —— 请在 配置/devices*.yaml 的该设备下'
                    f'声明 write_limits（按寄存器名给 [下限, 上限]）'
                )
                return result

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

    def _is_simulation(self) -> bool:
        """当前是否运行在模拟模式（无真实 PLC）。

        ⚠️ **语义决策（2026-10-13 复核后明确）**：拿不到标记
        （``device_manager is None``）时**按「真实设备」处理**，即 fail-closed。

        为什么不是「没有设备管理器 = 没有真实设备，当模拟放行」：
        本类是**写操作的安全闸门**，判定不了运行环境时必须拒绝；
        把 None 当模拟，会让「忘了接设备管理器」从一处显式错误变成**静默放行**
        （联锁也会同时为空 → 一路绿灯写下去）。
        代价可控：生产只有一处构造点（``run.py`` 始终传真实 ``device_manager``），
        None 实际只会出现在纯单元测试里。
        """
        return bool(getattr(self.device_manager, 'simulation_mode', False))

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
            # 真实设备模式下**不允许**回退到猜测点位：coil0 / reg100 只是历史默认，
            # 未必对应现场任何真实线圈/寄存器，写错点位就是误操作。
            # 只有模拟模式才允许回退（无物理风险，且保证演示链路可用）。
            if not self._is_simulation():
                logger.error(
                    "设备 %s 未配置控制点位 control.%s，真实设备模式下拒绝回退到默认点位"
                    "（请在该设备配置中显式声明 control 段）",
                    device_id, action,
                )
                return None
            fallback = LEGACY_FALLBACK_CONTROL_POINTS.get(action)
            if fallback is None:
                logger.error("设备 %s 的批量动作 %s 无可用点位（无回退值）", device_id, action)
                return None
            spec = dict(fallback)
            logger.warning(
                "设备 %s 未配置控制点位 control.%s，模拟模式下使用回退点位 %s#%s",
                device_id, action, spec['type'], spec['address'],
            )

        kind = str(spec.get('type', 'coil')).lower()
        if kind not in ('coil', 'register'):
            logger.error("设备 %s 控制点位类型非法: %r", device_id, spec.get('type'))
            return None
        address = try_int(spec.get('address'))
        if address is None:
            # YAML 的 `.inf` / `.nan` 是合法字面量 → `int(inf)` 抛 OverflowError
            # （不在 TypeError/ValueError 里），原本会让控制点位加载直接崩。
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

        # 尝试转换为整数（`try_int` 同时挡住 inf/nan 与 OverflowError）
        converted = try_int(register_name)
        if converted is not None:
            return converted
        # 传入的是寄存器名称（非数字字符串）而非地址，转换失败属预期，
        # 继续走下方按名称查表的逻辑，安全忽略

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
        addr = try_int(address)
        if addr is None:
            return None

        if self.device_manager:
            try:
                device_config = self.device_manager.devices.get(device_id, {})
                registers = device_config.get('registers', []) or []
            except Exception:
                return None
            for reg in registers:
                reg_addr = try_int(reg.get('address'))
                if reg_addr is not None and reg_addr == addr:
                    return reg.get('name')

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
            # 恢复后清掉「已降级」标记，下一次掉线才能再触发降级
            self._degraded_devices.discard(device_id)
        else:
            failures = self._comm_failures.get(device_id, 0) + 1
            self._comm_failures[device_id] = failures
            health['consecutive_failures'] = failures

            if failures >= self._comm_threshold:
                health['status'] = 'disconnected'
                # round 303：**只在跨过阈值那一刻降级一次**。
                # 原来这里没有任何「已降级」标记，`failures >= 阈值` 之后
                # 每一个失败采样点都会重跑一遍：安全状态 handler（关阀/切手动）
                # 被重复下发、声光被重复触发、审计被重复写。
                # 一台设备掉线 5 分钟（5s 一采）= 60 次重复动作；
                # 审计日志是 `deque(maxlen=50000)`，一台设备几小时就能刷满，
                # **真实的操作审计被挤出去**。
                # 判据与联锁的 `triggered and not was_triggered` 同源：
                # 状态迁移必须只在**边沿**发生。
                if device_id not in self._degraded_devices:
                    self._degraded_devices.add(device_id)
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
                                operator: str, max_retries: int = 2,
                                kind: str = 'register') -> dict[str, Any]:
        """
        带回读验证的写操作

        流程：
        1. 安全校验
        2. 获取设备锁
        3. 写入值
        4. 回读验证
        5. 释放锁
        6. 记录审计

        Args:
            kind: ``'register'``（保持寄存器）或 ``'coil'``（线圈）。
                ⚠️ 必须由调用方明确指定：Modbus 客户端**同时**有
                `write_single_register` 和 `write_single_coil`，原先这里按
                `hasattr(write_single_register)` 优先判断 —— 于是
                `/api/control/<id>/write-coil` 走安全模块时**写的是同一地址的
                保持寄存器**，而不是线圈。寄存器 0 和线圈 0 在现场是**两个完全
                不同的对象**（一个数据字 vs 一个开关位），等于操作员以为按下了
                启动位、实际往数据寄存器里写了个数。
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
            # round 303：**拿锁之后再复核一次**。
            #
            # `validate_write` 是在拿锁**之前**调的（1549 行），而这中间还要：
            # 等设备锁（最长 5 秒）→ connect → 最多 3 次重试（每次之间 sleep 0.1s）。
            # 这段时间里别的线程完全可以把 `_estop_active` 置起来（急停按钮、
            # 联锁动作、看门狗都走这条路），或者某条 block_start 联锁刚触发。
            # 「校验通过」是一个会过期的前提，而急停的语义是
            # **立刻生效、不许有在途写入** —— 所以执行前必须重新确认一次。
            #
            # 复核只读内存（急停标志 / 设备健康 / 联锁状态 / 量程），
            # 没有任何 I/O，代价可以忽略。
            recheck = self.validate_write(device_id, address, value, operator)
            if not recheck['allowed']:
                self._audit('write_blocked', operator,
                            f'写入被复核阻止（拿锁后状态已变）: '
                            f'{device_id}/{address}={value}, 原因: {recheck["reason"]}')
                return {
                    'success': False,
                    'message': f'写入前复核未通过：{recheck["reason"]}',
                    'validation': recheck,
                    'rechecked': True,
                }

            # 写入
            success = False
            client = self.device_manager.get_client(device_id) if self.device_manager else None
            if not client:
                self._audit('write_failed', operator,
                            f'写入失败: 设备 {device_id} 客户端不存在')
                return {'success': False, 'message': f'设备 {device_id} 不存在或未连接'}

            if kind == 'coil':
                if not hasattr(client, 'write_single_coil'):
                    self._audit('write_failed', operator,
                                f'写入失败: 客户端不支持线圈写入 ({device_id}/{address})')
                    return {'success': False, 'message': '该设备的客户端不支持线圈写入'}
            elif not hasattr(client, 'write_single_register'):
                self._audit('write_failed', operator,
                            f'写入失败: 客户端不支持寄存器写入 ({device_id}/{address})')
                return {'success': False, 'message': '该设备的客户端不支持寄存器写入'}

            for attempt in range(max_retries + 1):
                try:
                    if kind == 'coil':
                        success = client.write_single_coil(int(address), bool(value))
                    else:
                        success = client.write_single_register(int(address), int(value))
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
            # ⚠️ 三条都是必须的：
            #   1. 读**同一种对象**：写线圈就读线圈、写寄存器就读寄存器。
            #      原来无论写什么都读保持寄存器 —— 写线圈时校验的是另一个物理对象；
            #   2. 回读异常/拿不到值 → `verified=None`（**未验证**），不能当通过。
            #      原来是 `readback_ok=True` 兜底，通信断了也报「写入成功」；
            #   3. 寄存器按**精确值**比对。原来容差 ±1，写 100 读到 99 也算通过。
            verified: bool | None = None
            readback_value = None
            try:
                if kind == 'coil':
                    if hasattr(client, 'read_coils'):
                        raw = client.read_coils(int(address), 1)
                        if raw:
                            readback_value = bool(raw[0])
                            verified = (readback_value == bool(value))
                else:
                    if hasattr(client, 'read_holding_registers'):
                        raw = client.read_holding_registers(int(address), 1)
                        if raw:
                            readback_value = int(raw[0])
                            verified = (readback_value == int(value))
                if verified is False:
                    logger.warning(f"回读验证失败: 期望 {value}, 实际 {readback_value}")
            except Exception as e:
                logger.warning(f"回读验证异常（按未验证处理）: {e}")
                verified = None

            # 审计
            verify_text = ('未验证' if verified is None
                           else ('验证通过' if verified else '验证失败'))
            self._audit('write_success', operator,
                        f'写入成功({kind}): {device_id}/{address}={value}'
                        f'{f", 回读={readback_value}" if readback_value is not None else ""}'
                        f', {verify_text}')

            result = {
                'success': True,
                'message': '写入成功',
                'kind': kind,
                'verified': verified,
                'warnings': list(validation.get('warnings', []))
            }
            if verified is False:
                result['message'] = '写入成功，但回读验证失败（设备未接受该值）'
                result['warnings'].append(
                    f'回读值 {readback_value} 与写入值 {value} 不一致')
            elif verified is None:
                result['message'] = '写入成功，但无法回读验证'
                result['warnings'].append('未能回读该点位（客户端不支持或回读失败）')

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
