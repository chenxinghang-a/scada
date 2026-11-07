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
import hashlib
from typing import Any, Callable, Set
from datetime import datetime, timedelta
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
import paths
from core.safe_numeric import to_finite_float, try_finite_float, try_int
from core.engineering_units import to_raw
from core.action_result import is_failure
from 采集层.base_client import encode_float32_registers, encode_int32_registers

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


#: 联锁规则里**真正会被执行**的条件类型（round 313）。
#:
#: `check_interlocks()` 只判 `threshold` 与 `coil`，其它值会让 `matched` 保持
#: False 然后 `continue` —— 这条规则**永不触发，且一个字都不报**。
#: 现场表现：yaml 里 `type: thresold`（少一个 h），界面上规则存在、enabled: true，
#: 实际超温时什么都不发生。
SUPPORTED_CONDITION_TYPES = frozenset({'threshold', 'coil'})

#: 联锁规则里**真正会被执行**的动作类型（round 313）。
#:
#: 比条件拼错更隐蔽：`_execute_interlock_action()` 对未知类型**什么都不做，
#: 但 `action_ok` 保持 True**，审计里记的是 `interlock_triggered`（成功）。
#: 现场表现：`action: {type: write_registor}`（少一个 e）→ 条件成立、
#: 状态显示"已触发"、审计说"已执行"，而**停机值一次都没下发**。
SUPPORTED_ACTION_TYPES = frozenset({'write_register', 'emergency_stop', 'block_start'})


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


@dataclass
class PendingAction:
    """HITL 待审批动作（round 378）。

    HITL 的语义是“人必须做决定”，所以自动来源的写入在 manual / 未武装的
    supervised 下不是直接失败，而是变成一条**提案**：人可以批准、改值或驳回。
    """
    action_id: str
    device_id: str
    address: int
    value: Any
    kind: str
    engineering: bool
    control_point: bool
    source: str                    # 谁发起的（edge_decision / pid / 外部系统）
    reason: str = ''
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_mono: float = 0.0
    updates: int = 1               # 同一目标被自动重复提案的次数
    status: str = 'pending'        # pending / executing / approved / rejected / expired

    def to_dict(self) -> dict[str, Any]:
        remaining = max(0, int(self.expires_mono - time.monotonic())) \
            if self.expires_mono else 0
        return {
            'action_id': self.action_id,
            'device_id': self.device_id,
            'address': self.address,
            'value': self.value,
            'kind': self.kind,
            'engineering': self.engineering,
            'control_point': self.control_point,
            'source': self.source,
            'reason': self.reason,
            'created_at': datetime.fromtimestamp(self.created_at).isoformat(),
            'updates': self.updates,
            'status': self.status,
            'remaining_seconds': remaining,
        }


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
        # RLock：`acquire_device_lock` 需要在持锁状态下惰性创建设备锁，
        # 而调用链上（validate/审计/旁路）已有 `with self._lock` 的路径，
        # 用普通 Lock 会自死锁。
        self._lock = threading.RLock()

        # ===== 联锁旁路审批（IEC 61511） =====
        #: 旁路请求表（按 request_id）。**必须有上限**（round 311）：
        #: request_id 是 uuid，每申请一次就多一条，而审批完成后
        #: `approve_bypass` / `reject_bypass` 都只改 `status`、**从不删除** ——
        #: 一个每年几百次维护旁路的现场，进程跑一年就攒几千条（每条含原因文本），
        #: 且 `get_pending_bypasses()` 每次都要遍历全表。
        #: 上限见 `_trim_bypass_requests()`：只淘汰**已结束**的，pending 永不丢。
        self._bypass_requests: dict[str, BypassRequest] = {}
        self._active_bypasses: Set[str] = set()  # 已批准且激活的旁路
        #: round 369：`interlock_id -> (request_id, Timer)`。
        #: 原来的 Timer 句柄**从未保存**，于是：
        #:   · `restore_interlock()` 手动恢复时无法取消旧定时器；
        #:   · 同一联锁再次申请旁路后，上一轮的孤儿定时器到期会无条件撤销
        #:     新旁路（它不知道自己已经过期）；
        #:   · `remove_interlock()` 删规则后定时器仍会触发并写审计。
        self._bypass_timers: dict[str, tuple[str, Any]] = {}
        self._bypass_lock = threading.Lock()

        # ===== 自主等级（round 378） =====
        #: 每台设备的控制权归属：
        #:  * manual     —— 人工在环（human-in-the-loop）：只接受人工下发；
        #:  * supervised —— 人在环上（human-on-the-loop）：自动写入必须由人
        #:                  “武装/授权”且未过期，人可随时解除；
        #:  * automatic  —— 人在环外（human-out-of-the-loop）：全自动，无人值守。
        #: 配置基线来自设备配置 `autonomy:`；运行期可用 API 覆盖。
        self._AUTONOMY_MODES = ('manual', 'supervised', 'automatic')
        #: 现场常直接写英文缩写，配置/API 都接受别名
        self._AUTONOMY_ALIASES = {
            'hitl': 'manual',
            'hotl': 'supervised',
            'hootl': 'automatic',
        }
        self._autonomy_override: dict[str, str] = {}
        #: device_id -> 武装到期时刻（time.monotonic()）
        self._autonomy_armed_until: dict[str, float] = {}
        self._autonomy_armed_by: dict[str, str] = {}

        #: HITL 待审批动作：action_id -> PendingAction
        self._pending_actions: dict[str, PendingAction] = {}
        self._pending_lock = threading.Lock()
        #: 提案有效期与上限：PID 每周期都会提案，靠 (device,address,kind)
        #: 去重合并，避免几百条同目标提案把队列撑爆。
        self.PENDING_TTL_S = 300
        self.PENDING_MAX = 200

        # ===== 写操作安全配置 =====
        # 设备写操作白名单：device_id -> {register_name: (min_value, max_value)}
        # 键为寄存器名称（如 'temperature_setpoint'），地址由设备配置反查；
        # 特殊键 'default' 表示该设备所有寄存器的兜底量程
        self._write_limits: dict[str, dict[str, Any]] = {}
        # 设备可写地址白名单：device_id -> set of allowed addresses
        self._write_whitelist: dict[str, Set[int]] = {}

        # ===== 操作互斥锁 =====
        self._device_locks: dict[str, threading.Lock] = {}
        #: 联锁去抖的计时表：rule_id -> {'first': monotonic, 'count': int}
        #: 只对配了 `debounce` / `samples` 的规则生效，其余规则不写这里。
        self._interlock_pending: dict[str, dict[str, float]] = {}
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
        # round 322：审计完整性 —— 串行写 + 哈希链。
        # 哈希链的作用：每条记录带上前一条的哈希，**删/改中间任何一条都会断链**，
        # 事后能证明"日志没被篡改过"。成本只是多一次 sha256。
        self._audit_lock = threading.Lock()
        # round 353：**从落盘日志恢复链尾哈希**。
        #
        # 原来这里恒为 `''`，而 `_audit()` 以追加模式写同一个文件 ——
        # 进程每次重启后首条记录的 `prev` 都是空串 → **哈希链在每个重启点断开**。
        # 「日志没被篡改过」这个结论因此只在单次运行内成立，
        # 而审计日志的价值恰恰是跨重启、跨班次自证。
        self._audit_last_hash = self._load_last_audit_hash()

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

        # round 315：恢复跨重启保持的安全状态（急停 / 旁路）
        self._restore_persisted_state()
        # 加载写操作限制
        self._load_write_limits()
        # 初始化设备健康数据
        self._init_device_health()

        logger.info("工厂级设备控制安全管理器已初始化")

    # ==================== 安全联锁 ====================

    def reload_interlocks(self) -> dict[str, Any]:
        """重新加载 `配置/interlocks.yaml`，返回差集（round 318）。

        Returns:
            dict: `{'added': [...], 'removed': [...], 'changed': [...],
                   'kept': [...], 'source': str}`

        ## 为什么需要

        `_load_preset_interlocks()` **只在 `__init__` 里调用一次** ——
        也就是说改 `interlocks.yaml`（调阈值、加一条、停用一条）必须
        **重启进程**才生效，而这是 24/7 的现场系统。

        更糟的是**没有任何提示**：运维改完文件、看到 `git status` 干净，
        就以为生效了。方向虽然"安全"（旧规则继续保护），但两种误判都很危险：

        * 改**高**阈值想减少误跳闸 → 实际没生效 → 继续误跳闸；
        * 改**低**阈值想更严格 → 实际仍是旧阈值 → **保护不足而无人知**。

        ## 差集怎么算

        规则按 `id` 比对；`id` 相同但 `condition` / `action` 变了算 **changed**。
        `changed` 的规则必须**重置运行时状态**：
        `_interlock_states`（旧阈值下的"已触发"不能沿用）、
        `_interlock_bypass`（旧规则的审批不该自动套到新规则上）、
        `_interlock_action_failures`。

        `removed` 的规则同理要清干净 —— 否则一条被删的联锁仍挂在
        `_interlock_bypass` 里，将来同 id 规则重新加回来时会**自动处于旁路状态**。
        """
        old_rules = dict(self._interlock_rules)
        old_ids = set(old_rules)
        #: 旧状态要留着 —— **没变的规则**（kept）必须沿用它的"已触发"位。
        #: 一刀切重置会让一条正在触发的联锁突然变"未触发"，
        #: 下一轮采样再重新触发一次（重复播报 + 重复执行动作）。
        old_states = dict(self._interlock_states)

        # **构造全新的空表再让 `_load_preset_interlocks()` 往里填**。
        # 不能"先清空旧表再逐条填"：判定线程（`check_interlocks`）正在遍历它，
        # 中间态会抛 `dictionary changed size during iteration`。
        # 直接赋新对象是原子的：读者要么看到旧表、要么看到新表。
        #
        # 加载失败（文件不存在/全是坏规则）时新表保持为空 →
        # `_load_preset_interlocks` 会走"空规则"分支告警，
        # 而 `validate_write` 在真实设备模式下 fail-closed 拒绝写操作。
        # 填表期间持锁：读者都持 `_lock`，写侧不持锁时仍会看到“边填边遍历”的中间态。
        # 填充中途异常必须回滚，否则一次热重载就让运行中的联锁静默消失。
        with self._lock:
            self._interlock_rules = {}
            self._interlock_states = {}
            try:
                self._load_preset_interlocks()
            except Exception as e:      # noqa: BLE001
                self._interlock_rules = old_rules
                self._interlock_states = old_states
                logger.error(
                    '联锁热重载填充异常（%s: %s），已回滚到重载前的 %d 条',
                    type(e).__name__, e, len(old_rules))
                return {
                    'added': [], 'removed': [], 'changed': [],
                    'kept': sorted(old_rules), 'source': 'rollback',
                    'error': (f'重载异常，已回滚到重载前的规则: '
                              f'{type(e).__name__}: {e}'),
                }
            new_rules = self._interlock_rules

        # ===== 回滚保护（round 341）=====
        #
        # 加载失败（`interlocks.yaml` 语法错 / 权限不足 / 被别的东西临时占位）
        # 时新表会是 0 条。原实现照单全收 —— **运行中的联锁保护静默消失**：
        # 上一秒还有 5 条规则在盯着，下一秒一条都没有，系统只留一行 warning。
        # `validate_write` 在真实模式空规则时确实会 fail-closed 拒绝写入，
        # 但**已经在触发的联锁不会继续执行动作**（该停机的设备不停）。
        #
        # 判据：**新表 0 条而旧表非空 → 视为加载失败，回滚**。
        # 真要清空规则，应该在文件里写 `interlocks: []` 后重启，
        # 而不是靠一次热重载把保护卸掉。
        if not new_rules and old_rules:
            logger.error(
                "联锁重载后规则为 0（文件语法错或不可读），**已回滚到重载前的 %d 条** —— "
                "否则运行中的联锁保护会静默消失。请检查 %s",
                len(old_rules), INTERLOCK_CONFIG_PATH)
            self._interlock_rules = old_rules
            self._interlock_states = old_states
            return {
                'added': [], 'removed': [], 'changed': [],
                'kept': sorted(old_rules), 'source': 'rollback',
                'error': '重载后规则为 0，已回滚到重载前的规则',
            }

        new_ids = set(new_rules)
        added = sorted(new_ids - old_ids)
        removed = sorted(old_ids - new_ids)
        changed = sorted(
            rid for rid in (new_ids & old_ids)
            if (old_rules[rid].get('condition') != new_rules[rid].get('condition')
                or old_rules[rid].get('action') != new_rules[rid].get('action')
                or old_rules[rid].get('enabled', True) != new_rules[rid].get('enabled', True))
        )
        # ⚠️ kept 只能取**旧表与新表的交集**再排除 changed ——
        # 写成 `new_ids - set(changed)` 会把新增的规则也算进 kept。
        kept = sorted((new_ids & old_ids) - set(changed))

        # 失效规则（删除 / 改动）的运行时状态必须清干净
        if removed or changed:
            invalid = set(removed) | set(changed)
            with self._lock:
                # removed：规则没了，状态表里也不该留 key
                for rid in removed:
                    self._interlock_states.pop(rid, None)
                    self._interlock_action_failures.pop(rid, None)
                    self._interlock_bypass.discard(rid)
                # changed：规则还在（新条件），状态**重置为未触发**而不是删 key ——
                # 删掉会让 `_interlock_states` 缺少这条规则的条目，
                # 与"加载完成"时的形状不一致（下游按 key 存在性判断会漏）。
                for rid in changed:
                    if rid in self._interlock_states:
                        self._interlock_states[rid] = False
                    self._interlock_action_failures.pop(rid, None)
                    self._interlock_bypass.discard(rid)

            # round 370（BUG-041）：旁路状态有**五份**，只清 `_interlock_bypass`
            # 等于没清干净 —— 改高阈值后，旧的"已旁路"仍在 `_active_bypasses`、
            # 库里的 approved 请求、以及那个还没到期的 Timer 里，
            # 到期时 `_auto_restore_bypass` 会把**新规则**的旁路一起撤掉。
            with self._bypass_lock:
                for rid in invalid:
                    self._active_bypasses.discard(rid)
                    self._interlock_pending.pop(rid, None)
                    stale = [req_id for req_id, req in self._bypass_requests.items()
                             if getattr(req, 'interlock_id', None) == rid]
                    for req_id in stale:
                        self._bypass_requests.pop(req_id, None)
            for rid in invalid:
                self._cancel_bypass_timer(rid)
            # 落库放在锁外（与 remove/restore 两条路径一致）
            self._persist_bypasses()

        # 恢复 **kept** 规则的旧状态（`_load_preset_interlocks` 会把新表
        # 全部填成 False，这里只覆盖没变的那部分）
        with self._lock:
            for rid in kept:
                if rid in old_states:
                    self._interlock_states[rid] = old_states[rid]

        if added or removed or changed:
            logger.warning(
                "安全联锁配置已热重载：新增 %s，移除 %s，变更 %s（变更/移除的规则"
                "运行时状态已重置，旁路审批不继承）",
                added, removed, changed)
        else:
            logger.info("安全联锁配置已重载（无变化）")

        return {
            'added': added,
            'removed': removed,
            'changed': changed,
            'kept': kept,
            'source': (self._interlock_load_summary or {}).get('source'),
        }

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
            # round 313：**类型拼错 = 静默失效的死规则**。
            # 条件类型不认识 → `matched` 永远 False，规则永不触发；
            # 动作类型不认识 → 条件会触发、审计记"已执行"，但什么都没做。
            # 两种都不能靠"运行期没报错"发现，必须在加载时拒绝并汇总告警。
            # condition 写成字符串/列表时，(x or {}) 仍是原值，.get 直接崩。
            _cond = rule.get('condition')
            cond_type = _cond.get('type') if isinstance(_cond, dict) else None
            if cond_type not in SUPPORTED_CONDITION_TYPES:
                logger.error(
                    "忽略联锁规则 %s：condition.type=%r 不被支持（只支持 %s）—— "
                    "这条规则**永远不会触发**", rule_id, cond_type,
                    sorted(SUPPORTED_CONDITION_TYPES))
                rejected += 1
                continue
            # 同 condition：action 非字典时 .get 直接崩，先判形态。
            _act = rule.get('action')
            act_type = _act.get('type') if isinstance(_act, dict) else None
            if act_type not in SUPPORTED_ACTION_TYPES:
                logger.error(
                    "忽略联锁规则 %s：action.type=%r 不被支持（只支持 %s）—— "
                    "条件会触发但**动作不会执行**", rule_id, act_type,
                    sorted(SUPPORTED_ACTION_TYPES))
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

                # ===== round 343：**不许用 `client.connected` 覆盖采集结论** =====
                #
                # `connected` 只说明 TCP 连着。现场更常见的是「连着但读不到数据」
                # （读超时、异常码、寄存器不存在）—— 这时 `connected=True`，
                # 原实现会把 `status` 改回 connected，并把
                # `consecutive_failures` 和 `_comm_failures` **一起清零**。
                # 后果：
                #   · 界面永远显示「连接正常」，实际数据已经采不到；
                #   · `_comm_failures` 是触发设备安全状态的依据，被清零 =
                #     安全状态永不触发（关阀/切手动不会执行）。
                #
                # 实测（tools/e2e_smoke.py 第 7 步）：停掉从站后采集返回 False、
                # `last_read_source='cache'`，但健康摘要仍报 `connected`。
                #
                # 现在的口径：
                #   · 采集侧报过 → 以采集结果为准（`last_collect_ok`）；
                #   · 从没报过（纯新增设备）→ 用 `connected` 做初判；
                #   · 失败计数**只由成功的采集清零**，这里一律不动。
                last_ok = health.get('last_collect_ok')
                if last_ok is None:
                    health['status'] = 'connected' if connected else 'disconnected'
                    if connected:
                        health['last_seen'] = datetime.now().isoformat()
                elif last_ok:
                    health['status'] = 'connected'
                else:
                    # 采集失败：TCP 也断了 → disconnected（更严重）；
                    # 只是读不到 → degraded。
                    health['status'] = 'disconnected' if not connected else 'degraded'

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
        # round 330：**急停期间一律拦截，包括 reset**。
        # 原来写的是 `and action != 'reset'` —— 但 `batch_control('reset')`
        # 是往现场 PLC 的 control.reset 点位**下发硬件复位**，不是解除软件急停
        # （解除走 `reset_emergency_stop()`，只改标志位、不碰 PLC）。
        # 急停锁死时还允许向 PLC 发复位，等于给设备一个"重启"的机会。
        # 正确顺序：先 `reset_emergency_stop()` 解除急停，再单独复位 PLC。
        if self._estop_active:
            return {
                'success': False,
                'message': '紧急停机状态中，请先解除紧急停机再操作设备',
                'results': {}
            }

        # 启动/复位前检查联锁阻止条件
        #
        # round 330：**不只看 block_start**。原来只拦 action.type == 'block_start'
        # 的联锁，于是某设备因超温触发了 write_register（写停机值）或
        # emergency_stop 联锁、`_interlock_states[rule_id]` 已经是 True 时，
        # batch_control('start') 会**无视正在触发的联锁强行启动**。
        #
        # round 369：再修两处——
        #   ① 原来只对 `action == 'start'` 检查。`batch_control('reset')` 是向
        #      PLC 下发**硬件复位脉冲**，对正处于联锁锁死状态的设备复位，
        #      等于把保护条件直接清掉。reset 必须同口径拦截。
        #   ② `device_ids=None`（全厂）时，`device_ids is not None` 恒为假，
        #      任何一台设备的局部联锁都会在进入循环前把**整个工厂**一刀切拒绝。
        #      现在把联锁归属到具体设备：只跳过被该联锁覆盖的设备，其余设备
        #      照常执行；只有当规则确实没有绑定任何设备（全厂级）时才整体拦。
        device_blocks: dict[str, tuple[str, Any, str]] = {}
        if action in ('start', 'reset'):
            scope = set(device_ids) if device_ids else None
            global_blocks: list[tuple[str, Any, str]] = []
            # round 373：**锁内取快照再遍历**。`_interlock_rules` 会被
            # `reload_interlocks()` 整体替换、`_interlock_states` 由采集线程
            # 更新 —— 直接迭代活字典会抛 `RuntimeError: dictionary changed
            # size during iteration`，API 侧表现为 500 或"这次批量控制没反应"。
            with self._lock:
                interlock_items = list(self._interlock_rules.items())
            for rule_id, rule in interlock_items:
                if rule_id in self._interlock_bypass:
                    continue
                if not self._interlock_states.get(rule_id, False):
                    continue
                act = rule.get('action', {}) or {}
                cond = rule.get('condition', {}) or {}
                act_type = act.get('type')
                # 与 api_control 的判定口径一致：动作目标**和**条件设备
                # 都算这条联锁的相关设备。
                targets = {t for t in (act.get('device_id'), cond.get('device_id'))
                           if t}
                if not targets:
                    global_blocks.append((rule_id, rule.get('name'), act_type))
                    continue
                relevant = targets if scope is None else (targets & scope)
                for did in relevant:
                    device_blocks.setdefault(
                        did, (rule_id, rule.get('name'), act_type))
            if global_blocks:
                names = '、'.join(f'{rid}({name}/{t})'
                                  for rid, name, t in global_blocks)
                logger.error("批量%s被全厂级联锁阻止: %s", action, names)
                return {
                    'success': False,
                    'message': f'联锁正在触发，禁止{action}：{names}',
                    'results': {},
                    'blocked_by': [rid for rid, _, _ in global_blocks],
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
                # 该设备被已触发的联锁覆盖 → 只跳过它，不连坐整批
                if device_id in device_blocks:
                    rule_id, rule_name, act_type = device_blocks[device_id]
                    results[device_id] = {
                        'success': False,
                        'message': f'联锁正在触发，禁止{action}：'
                                   f'{rule_id}({rule_name}/{act_type})',
                        'blocked_by': rule_id,
                    }
                    self._audit(f'batch_{action}', operator,
                                f'批量{action}设备 {device_id}: 被已触发联锁 {rule_id} 阻止')
                    continue

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

                # round 330：**先写审计、再下发**（round 322 的 fail-closed 原则）。
                # 原来审计是在写完之后调的（第 768 行附近），而且不看返回值 ——
                # 磁盘满 / 目录只读时会出现「指令已经发给 PLC、记录却没有」，
                # 事故调查时无法自证「谁在什么时候下发了什么」。
                if not self._audit(
                        f'batch_{action}_attempt', operator,
                        f'批量{action}设备 {device_id}: 即将写 '
                        f'{point["type"]}#{point["address"]}={point["value"]}'):
                    results[device_id] = {
                        'success': False,
                        'message': '审计写入失败，拒绝下发（无法自证的操作不允许执行）',
                    }
                    continue

                # 按点位类型走对应写法，**不允许跨类型退回**：
                # 线圈和寄存器在现场是两个对象，退回写等于写错地方，
                # 而调用方只会看到「成功」。不支持就明确失败。
                #
                # round 369：**批量写也必须走安全通道**。
                # 原来这里直接 `client.write_single_coil/register(...)` ——
                # 绕过了 `acquire_device_lock`（与单点写并发时互斥失效）、
                # `validate_write`（急停/联锁/量程/旁路）与回读验证：
                # 同一台设备，单点写要过三道闸门，批量写一路绿灯。
                # 而批量恰恰是"一次动一片"的动作，风险只高不低。
                unsupported = None
                if point['type'] == 'coil':
                    if not hasattr(client, 'write_single_coil'):
                        unsupported = '客户端不支持线圈写入'
                else:  # register
                    if not hasattr(client, 'write_single_register'):
                        unsupported = '客户端不支持寄存器写入'

                if unsupported:
                    results[device_id] = {'success': False, 'message': unsupported}
                    self._audit(f'batch_{action}', operator,
                                f'批量{action}设备 {device_id}: {unsupported}')
                    continue

                _pv = point['value']
                if point['type'] == 'coil':
                    # bool("0")/bool("false") 都是 True —— 配置里的字符串关闭
                    # 会被下发成开启。按字面量解析。
                    if isinstance(_pv, str):
                        _pv = _pv.strip().lower() not in (
                            '0', 'false', 'no', 'off', 'disabled', '')
                    else:
                        _pv = bool(_pv)
                else:
                    _pv = int(_pv)
                write_res = self.write_with_verification(
                    device_id,
                    int(point['address']),
                    _pv,
                    operator,
                    kind='coil' if point['type'] == 'coil' else 'register',
                    # 控制点位来自 devices 配置白名单，不套 write_limits
                    control_point=True,
                )
                success = bool(write_res.get('success'))
                if not success:
                    msg = write_res.get('message') or f'{action}写入失败'
                    results[device_id] = {
                        'success': False,
                        'message': msg,
                        'verified': write_res.get('verified'),
                    }
                    self._audit(f'batch_{action}', operator,
                                f'批量{action}设备 {device_id}: 安全通道拒绝/失败 — {msg}')
                    continue

                # round 369：**不要再让管理器重复下发一次**。
                #
                # 上面 `write_with_verification` 已经把 control.start / control.stop
                # 点位写进 PLC 了；而 RealDeviceManager.start_device/stop_device
                # 内部会**再调一次 `_write_control`** —— 同一根点位一次批量
                # 操作连写两遍（继电器/启停位抖动），且这里原来完全丢弃了
                # 布尔返回值，同步失败也照样报成功。
                # `write=False` 只同步本地状态（_stopped_devices / 模拟标志），
                # 不再碰硬件；返回值仍然必须检查。
                if success and self.device_manager and action in ('start', 'stop'):
                    fn = getattr(self.device_manager,
                                 'start_device' if action == 'start' else 'stop_device',
                                 None)
                    if fn is not None:
                        import inspect
                        accepts_write = 'write' in inspect.signature(fn).parameters
                        state_ok = None
                        if accepts_write:
                            try:
                                state_ok = fn(device_id, write=False)
                            except Exception as e:
                                logger.error("批量%s 状态同步异常 device=%s: %s",
                                             action, device_id, e)
                                state_ok = False
                        else:
                            # 不支持 write 参数的管理器：宁可不同步状态，
                            # 也不能向同一根点位重复下发。
                            logger.warning(
                                "管理器 %s 不支持 write=False，跳过状态同步以避免"
                                "重复下发（device=%s, action=%s）",
                                type(self.device_manager).__name__, device_id, action)
                        if state_ok is False:
                            results[device_id] = {
                                'success': False,
                                'message': f'点位已写入，但设备状态同步失败（{action}）',
                                'verified': write_res.get('verified'),
                            }
                            self._audit(f'batch_{action}', operator,
                                        f'批量{action}设备 {device_id}: 点位已写但状态同步失败')
                            continue

                results[device_id] = {
                    'success': success,
                    'message': f'{action}操作{"成功" if success else "失败"}',
                    'verified': write_res.get('verified'),
                }

                self._audit(f'batch_{action}', operator,
                            f'批量{action}设备 {device_id}: {"成功" if success else "失败"}'
                            f' (点位 {point["type"]}#{point["address"]}={point["value"]})')

            except Exception as e:
                results[device_id] = {'success': False, 'message': str(e)}
                logger.error(f"批量控制设备 {device_id} 失败: {e}")

        success_count = sum(1 for r in results.values() if r.get('success'))
        total = len(results)

        # round 330：**部分成功不算成功**。
        # 原来 `success_count > 0` —— 批量停机 10 台只停了 1 台也回 True，
        # 操作员看到「成功」就走人，其余 9 台还在转。停机场景尤其致命。
        all_ok = total > 0 and success_count == total
        partial = 0 < success_count < total
        msg = f'批量{action}完成: {success_count}/{total} 成功'
        if partial:
            failed = [d for d, r in results.items() if not r.get('success')]
            msg += f'；**{len(failed)} 台失败**：{"、".join(failed[:5])}'
        payload = {
            'success': all_ok,
            'partial_success': partial,
            'message': msg,
            'results': results,
            'success_count': success_count,
            'total': total,
        }
        blocked_by = sorted({r['blocked_by'] for r in results.values()
                             if r.get('blocked_by')})
        if blocked_by:
            payload['blocked_by'] = blocked_by
        return payload

    #: 跨重启保持的安全状态键（`system_state` 表）。
    STATE_KEY_ESTOP = 'estop'
    STATE_KEY_BYPASS = 'interlock_bypass'

    def _persist_estop(self) -> None:
        """把急停状态落库（round 315）。

        为什么必须：`_estop_active` 是**纯内存**的。急停激活时进程重启 →
        内存清空 → `validate_write()` 不再拦写操作，而**现场急停按钮可能仍锁着**。
        落库失败只记 ERROR，不改急停本身的状态（内存里仍是急停，
        宁可"重启后丢"也不能"现在就放行"）。
        """
        db = getattr(self, 'database', None)
        if db is None or not hasattr(db, 'set_system_state'):
            return
        payload = None
        if self._estop_active:
            payload = {
                'active': True,
                'time': self._estop_time.isoformat() if self._estop_time else None,
                'reason': self._estop_reason,
                'affected_devices': list(getattr(self, '_estop_devices', []) or []),
            }
        if not db.set_system_state(self.STATE_KEY_ESTOP, payload):
            logger.error("急停状态落库失败 —— 进程重启后急停标记会丢失，"
                         "请人工确认现场急停按钮状态")

    def _persist_bypasses(self) -> None:
        """把当前**仍生效**的联锁旁路落库（round 315）。

        存的是墙钟 `expires_at`（单调时钟不能跨进程），恢复时用它判断是否已过期。
        """
        db = getattr(self, 'database', None)
        if db is None or not hasattr(db, 'set_system_state'):
            return
        with self._bypass_lock:
            payload = {}
            for interlock_id in self._active_bypasses:
                # round 336：只认 **approved 且未过期**的请求，并取**最新**那条。
                #
                # 原来 `next((... for r in values() if status in ('pending','approved')))`
                # 有两个问题：
                #   · 允许 `pending` —— 那是「还没满 2 人审批」的请求，
                #     它的 `expires_at` **不是**已生效旁路的过期时间。
                #     落库后重启会按一个错误的时间点恢复/丢弃旁路。
                #   · 正序遍历取到的是**最早**那条历史请求（dict 保插入序），
                #     它的过期时间很可能早已过去 → 重启后旁路立即被判过期失效，
                #     检修中的设备被联锁动作。
                req = None
                for r in reversed(list(self._bypass_requests.values())):
                    if (r.interlock_id == interlock_id
                            and r.status == 'approved'
                            and not r.is_expired):
                        req = r
                        break
                if req is None:
                    continue
                payload[interlock_id] = {
                    'request_id': req.request_id,
                    'expires_at': req.expires_at,
                    'requested_by': req.requested_by,
                }
        if not db.set_system_state(self.STATE_KEY_BYPASS, payload or None):
            logger.error("联锁旁路状态落库失败 —— 进程重启后旁路会失效，"
                         "正在检修的设备可能被联锁动作")

    def _restore_persisted_state(self) -> dict[str, int]:
        """启动时恢复急停与旁路（round 315）。

        Returns:
            dict: `{'estop': 0/1, 'bypass': 恢复条数, 'expired': 过期丢弃条数}`

        旁路恢复时**必须校验过期**：库里可能留着一条早就该自动恢复的旁路
        （上次进程在 Timer 触发前被杀），不校验就等于"永久旁路"。
        """
        result = {'estop': 0, 'bypass': 0, 'expired': 0}
        db = getattr(self, 'database', None)
        if db is None or not hasattr(db, 'get_system_state'):
            return result

        # --- 急停 ---
        estop = db.get_system_state(self.STATE_KEY_ESTOP)
        if isinstance(estop, dict) and estop.get('active'):
            self._estop_active = True
            raw_time = estop.get('time')
            try:
                self._estop_time = (datetime.fromisoformat(raw_time)
                                    if isinstance(raw_time, str) else datetime.now())
            except ValueError:
                self._estop_time = datetime.now()
            self._estop_reason = estop.get('reason') or '（重启前触发，原因未记录）'
            self._estop_devices = list(estop.get('affected_devices') or [])
            result['estop'] = 1
            logger.critical(
                "**紧急停机状态从数据库恢复**：进程重启前处于急停（%s，触发于 %s）。"
                "写操作继续被禁止，直到工程师执行 reset_emergency_stop()",
                self._estop_reason, self._estop_time)

        # --- 旁路 ---
        bypasses = db.get_system_state(self.STATE_KEY_BYPASS)
        if isinstance(bypasses, dict):
            now_wall = time.time()
            for interlock_id, info in bypasses.items():
                if not isinstance(info, dict):
                    continue
                expires_at = info.get('expires_at')
                if not isinstance(expires_at, (int, float)) or expires_at <= now_wall:
                    result['expired'] += 1
                    continue        # 已过期，不恢复（由下面的重写清掉）
                with self._lock:
                    self._interlock_bypass.add(interlock_id)
                # 重建定时器：用"还剩多久"换算成单调时钟
                remaining = expires_at - now_wall
                req_id = str(info.get('request_id') or f'restored-{interlock_id}')
                # round 370（BUG-040）：**必须同时重建 `_bypass_requests` 记录**。
                # `_persist_bypasses()` 只落库「`_bypass_requests` 里 approved 且
                # 未过期」的旁路；恢复出来的旁路原来只进 `_active_bypasses`，
                # 于是下面第 1128 行那次 `_persist_bypasses()` 算出空表、
                # 把数据库里的记录**当场抹掉** —— 再重启一次旁路就永久消失，
                # 正在检修的设备会被联锁动作。
                restored = BypassRequest(
                    request_id=req_id,
                    interlock_id=interlock_id,
                    requested_by=str(info.get('requested_by') or 'unknown'),
                    requested_at=now_wall,
                    expires_at=float(expires_at),
                    reason='进程重启后从数据库恢复',
                    status='approved',
                    expires_at_mono=time.monotonic() + remaining,
                )
                restored.approvals = ['restored'] * restored.required_approvals
                with self._bypass_lock:
                    self._active_bypasses.add(interlock_id)
                    self._bypass_requests[req_id] = restored
                try:
                    timer = threading.Timer(
                        remaining, self._auto_restore_bypass,
                        args=[interlock_id, req_id])
                    timer.daemon = True
                    # round 369：句柄必须登记，否则重启恢复出来的旁路
                    # 既撤不掉、也无法识别孤儿定时器。
                    with self._bypass_lock:
                        self._bypass_timers[interlock_id] = (req_id, timer)
                    timer.start()
                except Exception as e:      # noqa: BLE001
                    logger.error("恢复旁路定时器失败 %s: %s", interlock_id, e)
                result['bypass'] += 1
                logger.warning(
                    "**联锁旁路从数据库恢复**：%s（还剩 %.0f 分钟，原申请人 %s）",
                    interlock_id, remaining / 60, info.get('requested_by', '?'))

            if result['bypass'] or result['expired']:
                # 把过期的从库里清掉，避免下次启动又数一遍
                self._persist_bypasses()

        return result

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
        """添加自定义联锁规则。

        round 370（BUG-042）：**与 `reload_interlocks` 同口径校验**。
        原来只检查 `id`，API 可以写入任何 `condition` / `action` 类型 ——
        一条 `condition: 'foo'` 的规则永远不会触发，界面上却显示"已启用"，
        属于最危险的假保护。校验放在这里，所有入口（API / 配置写入）共用。
        """
        rule_id = rule.get('id')
        if not rule_id:
            return False

        cond_type = rule.get('condition', {}).get('type') if isinstance(
            rule.get('condition'), dict) else None
        if cond_type not in SUPPORTED_CONDITION_TYPES:
            logger.error("拒绝添加联锁 %s：不支持的条件类型 %r（支持: %s）",
                         rule_id, cond_type, sorted(SUPPORTED_CONDITION_TYPES))
            return False

        action_type = rule.get('action', {}).get('type') if isinstance(
            rule.get('action'), dict) else None
        if action_type not in SUPPORTED_ACTION_TYPES:
            logger.error("拒绝添加联锁 %s：不支持的动作类型 %r（支持: %s）",
                         rule_id, action_type, sorted(SUPPORTED_ACTION_TYPES))
            return False

        with self._lock:
            self._interlock_rules[rule_id] = rule
            self._interlock_states[rule_id] = False
        logger.info(f"添加联锁规则: {rule_id} - {rule.get('name')}")
        return True

    def remove_interlock(self, rule_id: str) -> bool:
        """移除联锁规则"""
        with self._lock:
            if rule_id not in self._interlock_rules:
                return False
            del self._interlock_rules[rule_id]
            self._interlock_states.pop(rule_id, None)
            # round 353：**旁路状态有两份，必须一起清**。
            #
            # 原来只 `discard(_interlock_bypass)`，漏了 `_active_bypasses` 与落盘 ——
            # 而 `_persist_bypasses()` 遍历的正是 `_active_bypasses`。于是：
            # 删掉的联锁在库里仍是「已旁路」，进程重启后又加载回来；
            # 同 id 规则重新添加时**默认就处于旁路状态**，超温不动作，
            # 而界面显示「旁路：否」（它读的是 `_interlock_bypass`）。
            # `restore_interlock` 早就是正确的对称实现，这里跟着对齐。
            self._interlock_bypass.discard(rule_id)
            self._active_bypasses.discard(rule_id)
        # round 369：规则都没了，自动恢复定时器不能留着继续触发
        self._cancel_bypass_timer(rule_id)
        logger.info(f"移除联锁规则: {rule_id}")
        # 落盘放在锁外（与 `restore_interlock` 一致，避免持锁做文件 I/O）
        self._persist_bypasses()
        return True

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
        """恢复联锁（手动解除旁路）"""
        # round 369：手动恢复必须**取消自动恢复定时器**。
        # 原来只清内存集合，旧 Timer 到期后照样执行，且会把「同一联锁的
        # 下一次旁路」一起撤销（`_auto_restore_bypass` 不看 request_id）。
        self._cancel_bypass_timer(rule_id)
        with self._lock:
            self._interlock_bypass.discard(rule_id)
            self._active_bypasses.discard(rule_id)
        # round 336：**必须落库**。
        #
        # 原来只改内存 —— SQLite 的 `system_state` 里仍留着这条旁路，
        # 进程重启时 `_restore_persisted_state()` 又把它加载生效：
        # **「已手动恢复的联锁在重启后复活」**。检修现场以为保护已经回来了，
        # 实际旁路还在，联锁触发时不会动作。
        #
        # 同族的 `_auto_restore_bypass`（超时自动恢复）是**有**这一行的，
        # 两条恢复路径对同一份持久化状态的写入必须等价。
        self._persist_bypasses()
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

            # ===== round 347：审批记录是**合规核心证据**，审计写不进去就不算审批成功 =====
            #
            # 原顺序是「先 append 审批人、再写审计」，且不看审计返回值。
            # 磁盘满 / 目录只读时：内存里显示「已 2/2 审批通过、旁路生效」，
            # 而磁盘上**没有任何审批记录** —— 事故调查时无法证明「谁批的」，
            # 而联锁旁路恰恰是最需要双人复核留痕的操作。
            #
            # 现在：先写审计，成功才落 approvals；失败则整个审批不生效。
            # 这是**唯一**可以 fail-closed 的地方 —— 审批本身还没生效，
            # 拒绝执行不会造成任何现场影响，只是让操作员重试。
            new_progress = f'{len(request.approvals) + 1}/{request.required_approvals}'
            if not self._audit(
                    'bypass_approved', approver,
                    f'审批旁路请求 {request_id}, 联锁 {request.interlock_id}, '
                    f'进度 {new_progress}'):
                return False, ("审计写入失败，审批未生效 —— 无法自证的审批不允许通过"
                               "（请检查审计目录权限与磁盘空间）")

            request.approvals.append(approver)
            progress = new_progress

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

    def _cancel_bypass_timer(self, interlock_id: str) -> None:
        """取消某条联锁的自动恢复定时器（如存在）。"""
        with self._bypass_lock:
            entry = self._bypass_timers.pop(interlock_id, None)
        if entry is not None:
            try:
                entry[1].cancel()
            except Exception:  # noqa: BLE001
                logger.debug("取消旁路定时器失败: %s", interlock_id)

    def _activate_bypass(self, request: BypassRequest):
        """激活旁路（带自动超时恢复）"""
        # round 369：**有效期从审批通过时刻起算**。
        # 原来 `expires_at_mono` 在申请时就固定了：审批人拖 25 分钟才批，
        # 旁路实际只剩 5 分钟；更糟的是审批完成瞬间若 delay <= 0，
        # 旧代码照样把联锁加入 `_interlock_bypass` 却**不启动 Timer** ——
        # 该联锁就永久处于旁路状态（保护彻底失效，且界面看不出来）。
        from core.safe_numeric import try_finite_float
        expires = try_finite_float(request.expires_at)
        requested = try_finite_float(request.requested_at)
        window = max(0.0, (expires or 0.0) - (requested or 0.0))
        request.expires_at = time.time() + window
        request.expires_at_mono = time.monotonic() + window
        if window <= 0:
            request.status = 'expired'
            self._audit('bypass_expired', 'system',
                        f'旁路请求 {request.request_id} 有效期已耗尽，拒绝激活')
            logger.error("旁路请求 %s 有效期已耗尽（window=%.0fs），拒绝激活",
                         request.request_id, window)
            return

        # 先把新定时器登记进 _bypass_timers，再激活旁路：旧定时器若在
        # “激活→落库”这段窗口到期，会看到自己的 request_id 已被替换而放行。
        timer = threading.Timer(window, self._auto_restore_bypass,
                                args=[request.interlock_id, request.request_id])
        timer.daemon = True
        with self._bypass_lock:
            stale = self._bypass_timers.pop(request.interlock_id, None)
            self._bypass_timers[request.interlock_id] = (request.request_id, timer)
        if stale is not None:
            try:
                stale[1].cancel()
            except Exception:       # noqa: BLE001
                logger.debug('取消旧旁路定时器失败: %s', request.interlock_id)

        with self._lock:
            self._interlock_bypass.add(request.interlock_id)
        with self._bypass_lock:
            self._active_bypasses.add(request.interlock_id)

        # round 315：落库 —— 维护窗口内重启不能让旁路失效
        self._persist_bypasses()
        timer.start()
        logger.info(f"旁路已激活: {request.interlock_id}, {window / 60:.0f}分钟后自动恢复")

    def _auto_restore_bypass(self, interlock_id: str, request_id: str):
        """超时自动恢复旁路。

        round 369：只恢复**自己那一次**旁路。同一联锁被重新申请旁路后，
        上一轮的定时器到期时必须放行（否则会把新旁路误撤销）。
        """
        # 判定与撤销必须同一次持锁完成：中间放锁会让新旁路装好定时器后
        # 被这里无条件 discard。
        with self._bypass_lock:
            current = self._bypass_timers.get(interlock_id)
            if current is not None and current[0] != request_id:
                logger.info(
                    "忽略已失效的旁路定时器: %s (req=%s, 当前=%s)",
                    interlock_id, request_id, current[0])
                return
            self._bypass_timers.pop(interlock_id, None)
            with self._lock:
                self._interlock_bypass.discard(interlock_id)
            self._active_bypasses.discard(interlock_id)
            req = self._bypass_requests.get(request_id)
            if req:
                req.status = 'expired'

        self._audit('bypass_auto_restored', 'system', f'旁路超时自动恢复: {interlock_id}')
        logger.warning(f"旁路超时自动恢复: {interlock_id}")
        # round 315：同步清库
        self._persist_bypasses()

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

                # ===== 去抖（round 343）=====
                #
                # 原实现条件一满足就立即动作。对「急停按钮」这类规则是对的
                # （安全优先，不能等），但对「超温 / 超压」这类**测量值**规则
                # 很危险：Modbus 偶发坏值（一次读到 9999、float32 位型异常）
                # 会让联锁立即执行 `emergency_stop`，现场直接停线。
                #
                # 规则可选配：
                #   samples: 2     连续 N 个采样点都满足才动作
                #   debounce: 10   持续满足 N 秒才动作
                # **默认不配 = 立即触发**（保持现有安全语义，不改变默认行为）。
                if triggered and (rule.get('debounce') or rule.get('samples')):
                    triggered = self._debounce_satisfied(rule_id, rule)
                elif not triggered:
                    # 条件已不满足 → 计时清零（下一轮重新开始计）
                    self._interlock_pending.pop(rule_id, None)

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

    def _debounce_satisfied(self, rule_id: str, rule: dict[str, Any]) -> bool:
        """判断这条联锁的去抖条件是否已满足。

        由 `check_interlocks()` 每轮调用一次（**只有条件成立时**才会进来）。
        返回 False 表示「还在计时，先别动作」。

        为什么用 `time.monotonic()` 而不是 `time.time()`：墙上时钟会被
        NTP 校正或运维手改，一次回拨 5 分钟会让 `debounce: 10` 的规则
        永远等不到 10 秒。单调时钟不受影响。
        """
        now = time.monotonic()
        entry = self._interlock_pending.get(rule_id)
        if entry is None:
            entry = {'first': now, 'count': 0.0}
            self._interlock_pending[rule_id] = entry
        entry['count'] += 1

        from core.safe_numeric import try_finite_float, try_int

        samples = rule.get('samples')
        if samples is not None and samples != '':
            # 用 try_int 而不是裸 int()：裸转换漏 OverflowError
            # （`int(float('inf'))`），而 samples 来自配置。
            parsed = try_int(samples)
            need = 1 if parsed is None else max(1, parsed)
            if entry['count'] < need:
                logger.debug("[联锁] %s 去抖中: %d/%d 次", rule_id, int(entry['count']), need)
                return False
            return True

        delay = rule.get('debounce')
        parsed_sec = try_finite_float(delay)
        need_sec = 0.0 if parsed_sec is None else max(0.0, parsed_sec)
        elapsed = now - entry['first']
        if elapsed < need_sec:
            logger.debug("[联锁] %s 去抖中: %.1f/%.0f 秒", rule_id, elapsed, need_sec)
            return False
        return True

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
        #
        # round 330：**默认 False（fail-closed）**。
        # 原实现 `action_ok = True` 起手，于是三条路径都会「假成功」：
        #   ① `write_single_register` 的返回值被丢弃 —— Modbus 写失败
        #      **返回 False 而不抛异常**（见 采集层/modbus_client.py），
        #      PLC 没写进去也算成功；
        #   ② `if self.device_manager:` 没有 else 分支，未初始化同样算成功；
        #   ③ `trigger_emergency_stop` 的返回值（可能带 success=False）被丢弃。
        # 后果：调用方 `check_interlocks` 见 ok=True 就把 `_interlock_states[rule_id]`
        # 锁死，之后每个采样周期的 `triggered and not was_triggered` 恒为 False ——
        # **停机指令永不重试**，而审计和看板都记着「已执行」。
        action_ok = False
        action_type = action.get('type')
        if action_type == 'write_register':
            device_id = action.get('device_id')
            register = action.get('register')
            value = action.get('value')
            if not self.device_manager:
                logger.error("联锁动作失败: device_manager 未初始化 (rule=%s)", rule_id)
            else:
                try:
                    client = self.device_manager.get_client(device_id)
                    if client and hasattr(client, 'write_single_register'):
                        # register可能是寄存器名称，需要查找对应地址
                        address = self._resolve_register_address(device_id, register)
                        if address is not None:
                            # round 370（BUG-044）：联锁动作是**工程值**（如 80.0℃），
                            # 而寄存器存的是 (value-offset)/scale 的原始整数，
                            # float32/int32 还占两个寄存器。原来直接
                            # `int(value)` 单寄存器写：带缩放的设备会写错量级，
                            # 浮点联锁值被截断 —— 联锁动作"执行成功"但没进入安全状态。
                            write_words, expected_raw = self._encode_engineering_write(
                                device_id, address, value)
                            # 设备互斥锁：防止与操作员手动写入交叉。
                            # 联锁是安全动作，抢不到锁也要继续（只记录）。
                            lock_holder = f'interlock:{rule_id}'
                            got_lock = False
                            try:
                                got_lock = self.acquire_device_lock(device_id, lock_holder)
                            except Exception as lock_err:   # noqa: BLE001
                                logger.warning("联锁动作获取设备锁异常 %s: %s",
                                               device_id, lock_err)
                            try:
                                if write_words is not None:
                                    write_res = self._write_registers_compat(
                                        client, address, write_words)
                                else:
                                    write_res = client.write_single_register(
                                        address, expected_raw)
                            finally:
                                if got_lock:
                                    try:
                                        # ⚠️ `release_device_lock(device_id)` 只收一个参数。
                                        # round 370 引入时把 `lock_holder` 也传了进来 →
                                        # TypeError 被下面的 except 吞成 debug 日志，
                                        # 锁**永不释放**（threading.Lock 不可重入、_device_locks
                                        # 常驻）：此后操作员手动写等锁 5s 后被拒 —— RRR 之后
                                        # 无法手动复位；联锁自己也拿不到锁，互斥形同虚设。
                                        self.release_device_lock(device_id)
                                    except Exception as rel_err:   # noqa: BLE001
                                        logger.warning("释放联锁设备锁失败 %s: %s",
                                                       device_id, rel_err)
                            # round 372：**不能用 `bool()`** ——
                            # `bool({'success': False})` 是 True，写入失败会被
                            # 记成"联锁动作执行成功"，随后 `_interlock_states`
                            # 锁死，停机指令永不重试。统一口径见 action_result。
                            action_ok = not is_failure(write_res)
                            if action_ok:
                                logger.info(f"联锁动作执行: 写入 {device_id}/addr={address} = {value}")
                            else:
                                logger.error(
                                    "联锁动作写入返回失败: %s/addr=%s = %s —— "
                                    "设备可能未进入安全状态，下个采样点会重试",
                                    device_id, address, value)
                        else:
                            logger.error(f"联锁动作: 无法解析寄存器 {register} 的地址")
                    else:
                        logger.error(f"联锁动作: 设备 {device_id} 不支持写操作")
                except Exception as e:      # noqa: BLE001 —— 写失败要能重试，不能吞
                    action_ok = False
                    logger.error(f"联锁动作执行失败: {e}", exc_info=True)

        elif action_type == 'emergency_stop':
            try:
                estop_res = self.trigger_emergency_stop(f'联锁 {rule_id} 触发紧急停机')
                # 急停返回值可能是 dict（含 success / 部分失败明细）。
                # 显式读它 ——「部分设备停机失败」不能让联锁状态被锁死。
                if isinstance(estop_res, dict):
                    action_ok = bool(estop_res.get('success', True))
                    if not action_ok:
                        logger.error(
                            "联锁急停未完全成功（rule=%s）：%s —— 下个采样点会重试",
                            rule_id, estop_res.get('message'))
                else:
                    action_ok = True if estop_res is None else bool(estop_res)
            except Exception as e:          # noqa: BLE001
                action_ok = False
                logger.error("联锁急停执行失败: %s", e, exc_info=True)

        elif action_type == 'block_start':
            # 标记设备启动被阻止。
            # ⚠️ 默认值改成 False 后**必须显式置 True** —— 否则「阻止启动」这个
            # 本来就算执行成功的动作会被判失败，联锁状态永不置位、每周期重复告警。
            logger.warning(f"联锁阻止设备启动: {rule_id}")
            action_ok = True

        else:
            # round 313：**未知动作类型绝不能算成功**。
            # 原来这里没有 else —— `action_ok` 保持 True，审计记
            # `interlock_triggered`（成功），而实际上什么动作都没发出。
            # 加载时已经拦掉这类规则，但运行期 `add_interlock()` 注入的
            # 规则绕过加载校验，所以这里必须再兜一次。
            action_ok = False
            logger.error(
                "联锁 %s 的动作类型 %r 不被支持（只支持 %s）—— "
                "条件已成立但**动作未执行**，设备可能并未进入安全状态",
                rule_id, action_type, sorted(SUPPORTED_ACTION_TYPES))

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
        # round 369：持锁清理。采集线程正在 update_device_health /
        # acquire_device_lock 里读写同一批字典，无锁 pop 会撞上
        # "dictionary changed size during iteration"。
        with self._lock:
            # round 372：补 `_write_limits` / `_write_whitelist` —— 它们同样是
            # `device_id -> {...}`，漏清时**同名设备重建会继承旧量程/白名单**：
            # 改了工艺量程却仍按旧范围校验，或旧地址白名单继续放行。
            for container in (self._device_health, self._comm_failures,
                              self._device_locks, self._safe_state_handlers,
                              self._device_lock_owners, self._write_limits,
                              self._write_whitelist,
                              # round 378：自主等级/武装状态必须以设备为键清理，
                              # 否则同名设备重建会继承旧的“全自动”授权。
                              self._autonomy_override,
                              self._autonomy_armed_until,
                              self._autonomy_armed_by):
                if device_id in container:
                    container.pop(device_id, None)
                    removed = True

        # round 355：`_degraded_devices` 是 **set**（上面循环都是 dict.pop），
        # 所以它从来没被清过。后果：删掉再重加的设备如果之前处于故障降级态，
        # 会一直留在集合里 —— 而 `update_device_health` 的判断是
        # `if device_id not in self._degraded_devices:`，
        # 于是新设备**再次掉线时不会触发安全降级**（关阀/切手动），
        # 而界面上它看起来是台正常设备。
        with self._lock:
            if device_id in self._degraded_devices:
                self._degraded_devices.discard(device_id)
                removed = True

        # 待审批提案以 action_id 为键，但同样属于该设备：设备删掉后不能再
        # 让人批准一个已经不存在的设备的写入。
        with self._pending_lock:
            stale = [aid for aid, act in self._pending_actions.items()
                     if act.device_id == device_id]
            for aid in stale:
                self._pending_actions.pop(aid, None)
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

    # ------------------------------------------------------------------
    # 自主等级（HOTL / HOOTL）
    # ------------------------------------------------------------------

    def _normalize_autonomy(self, mode: Any) -> str | None:
        text = str(mode or '').lower()
        text = self._AUTONOMY_ALIASES.get(text, text)
        return text if text in self._AUTONOMY_MODES else None

    def _device_autonomy(self, device_id: str) -> str:
        """当前生效的自主等级：运行期覆盖 > 设备配置 > 默认 automatic。"""
        with self._lock:
            override = self._normalize_autonomy(
                self._autonomy_override.get(device_id))
        if override:
            return override

        devices = getattr(self.device_manager, 'devices', {}) or {}
        cfg = devices.get(device_id) if isinstance(devices, dict) else None
        return self._normalize_autonomy((cfg or {}).get('autonomy')) or 'automatic'

    def set_autonomy_mode(self, device_id: str, mode: str, operator: str,
                          reason: str = '') -> tuple[bool, str]:
        """切换设备自主等级（运行期覆盖，不写回配置文件）。"""
        mode = self._normalize_autonomy(mode)
        if mode is None:
            return False, ('非法自主等级（允许 manual/supervised/automatic，'
                           '或别名 hitl/hotl/hootl）')

        with self._lock:
            self._autonomy_override[device_id] = mode
            if mode != 'supervised':
                # 切走监督模式时撤销武装，避免下次切回来带着旧授权
                self._autonomy_armed_until.pop(device_id, None)
                self._autonomy_armed_by.pop(device_id, None)

        self._audit('autonomy_mode_changed', operator,
                    f'设备 {device_id} 自主等级 → {mode}'
                    + (f'，原因: {reason}' if reason else ''))
        logger.warning("设备 %s 自主等级切换为 %s（操作人: %s）",
                       device_id, mode, operator)
        return True, mode

    def arm_autonomy(self, device_id: str, operator: str,
                     minutes: int = 60) -> tuple[bool, str]:
        """武装自动控制（HOTL）：在指定时长内允许自动写入。"""
        if isinstance(minutes, bool):
            return False, '武装时长必须是整数分钟'
        minutes = try_int(minutes)
        if minutes is None or not (1 <= minutes <= 480):
            return False, '武装时长必须在 1~480 分钟之间'

        if self._device_autonomy(device_id) != 'supervised':
            return False, '只有 supervised（人在环上）模式需要武装'

        until = time.monotonic() + minutes * 60
        with self._lock:
            self._autonomy_armed_until[device_id] = until
            self._autonomy_armed_by[device_id] = operator

        self._audit('autonomy_armed', operator,
                    f'设备 {device_id} 自动控制武装 {minutes} 分钟')
        logger.warning("设备 %s 自动控制已武装 %d 分钟（操作人: %s）",
                       device_id, minutes, operator)
        return True, f'已武装 {minutes} 分钟'

    def disarm_autonomy(self, device_id: str, operator: str) -> bool:
        """解除自动控制授权（人在环上时的“接管”动作）。"""
        with self._lock:
            existed = self._autonomy_armed_until.pop(device_id, None) is not None
            self._autonomy_armed_by.pop(device_id, None)
        if existed:
            self._audit('autonomy_disarmed', operator,
                        f'设备 {device_id} 自动控制解除武装')
            logger.warning("设备 %s 自动控制已解除武装（操作人: %s）",
                           device_id, operator)
        return existed

    def get_autonomy(self, device_id: str) -> dict[str, Any]:
        mode = self._device_autonomy(device_id)
        with self._lock:
            until = self._autonomy_armed_until.get(device_id)
            armed_by = self._autonomy_armed_by.get(device_id)
        remaining = max(0, int(until - time.monotonic())) if until else 0
        return {
            'device_id': device_id,
            'mode': mode,
            'armed': remaining > 0,
            'armed_by': armed_by if remaining > 0 else None,
            'armed_remaining_seconds': remaining,
        }

    def _autonomy_allows_auto(self, device_id: str) -> tuple[bool, str]:
        """自动来源的写入是否允许（人工写入永远放行）。"""
        mode = self._device_autonomy(device_id)
        if mode == 'automatic':
            return True, 'automatic'
        if mode == 'manual':
            return False, (f'设备 {device_id} 处于 manual（人工在环）模式，'
                           f'拒绝自动写入')
        with self._lock:
            until = self._autonomy_armed_until.get(device_id, 0.0)
        if time.monotonic() >= until:
            return False, (f'设备 {device_id} 处于 supervised（人在环上）模式，'
                           f'自动写入未武装或已过期，请先授权')
        return True, 'supervised'

    # ------------------------------------------------------------------
    # HITL 待审批提案
    # ------------------------------------------------------------------

    def _prune_pending_locked(self) -> list[str]:
        """清理过期/超限提案（调用方必须已持有 `_pending_lock`）。"""
        now = time.monotonic()
        removed = [
            aid for aid, act in self._pending_actions.items()
            if act.status == 'pending' and act.expires_mono and now >= act.expires_mono
        ]
        for aid in removed:
            self._pending_actions.pop(aid, None)

        overflow = len(self._pending_actions) - self.PENDING_MAX
        if overflow > 0:
            oldest = sorted(self._pending_actions.items(),
                            key=lambda kv: kv[1].created_at)
            for aid, act in oldest[:overflow]:
                if act.status == 'pending':
                    self._pending_actions.pop(aid, None)
                    removed.append(aid)
        return removed

    def _propose_action(self, device_id: str, address: int, value: Any,
                        source: str, *, kind: str, engineering: bool,
                        control_point: bool, reason: str = '') -> PendingAction:
        """自动写入在当前策略下不能直接执行 → 生成/合并一条待审批提案。"""
        key = (device_id, int(address), kind)
        with self._pending_lock:
            expired = self._prune_pending_locked()
            proposal = None
            for act in self._pending_actions.values():
                if act.status == 'pending' and \
                        (act.device_id, act.address, act.kind) == key:
                    act.value = value
                    act.engineering = engineering
                    act.control_point = control_point
                    act.source = source
                    act.reason = reason or act.reason
                    act.updated_at = time.time()
                    # 持续提案意味着“现在仍然想做”，有效期要跟着刷新；
                    # 否则操作员 5 分钟后回来看到的是不断重建的新提案，
                    # 队列里 id 一直在变。
                    act.expires_mono = time.monotonic() + self.PENDING_TTL_S
                    act.updates += 1
                    proposal = act
                    break
            if proposal is None:
                proposal = PendingAction(
                    action_id=uuid.uuid4().hex[:8],
                    device_id=device_id,
                    address=int(address),
                    value=value,
                    kind=kind,
                    engineering=engineering,
                    control_point=control_point,
                    source=source,
                    reason=reason,
                    expires_mono=time.monotonic() + self.PENDING_TTL_S,
                )
                self._pending_actions[proposal.action_id] = proposal

        for aid in expired:
            self._audit('pending_action_expired', 'system',
                        f'待审批动作 {aid} 已过期，自动作废')
        self._audit('write_pending_approval', source,
                    f'{device_id}/{address}={value}（{kind}）等待人工审批，'
                    f'action={proposal.action_id}')
        return proposal

    def get_pending_actions(self, device_id: str | None = None) -> list[dict[str, Any]]:
        with self._pending_lock:
            expired = self._prune_pending_locked()
            items = [
                act.to_dict() for act in self._pending_actions.values()
                if act.status == 'pending'
                and (device_id is None or act.device_id == device_id)
            ]
        for aid in expired:
            self._audit('pending_action_expired', 'system',
                        f'待审批动作 {aid} 已过期，自动作废')
        items.sort(key=lambda x: x['created_at'])
        return items

    def approve_pending(self, action_id: str, approver: str,
                        value: Any | None = None) -> tuple[bool, Any]:
        """批准提案并执行（可传 `value` 修改后再执行）。

        Returns:
            (是否找到并处理, 执行结果 dict 或失败说明字符串)
        """
        with self._pending_lock:
            act = self._pending_actions.get(action_id)
            if act is None or act.status != 'pending':
                return False, '待审批动作不存在或已处理'
            if act.expires_mono and time.monotonic() >= act.expires_mono:
                self._pending_actions.pop(action_id, None)
                return False, '待审批动作已过期'
            act.status = 'executing'
            execute_value = act.value if value is None else value
            params = (act.device_id, act.address, execute_value, act.kind,
                      act.control_point, act.engineering)

        device_id, address, execute_value, kind, control_point, engineering = params
        try:
            result = self.write_with_verification(
                device_id, address, execute_value, approver,
                kind=kind, control_point=control_point,
                engineering=engineering, origin='human')
        except Exception as exc:      # noqa: BLE001 —— 审批接口不能 500
            logger.exception("执行待审批动作 %s 时异常", action_id)
            result = {'success': False, 'message': f'执行失败: {exc}'}
        finally:
            with self._pending_lock:
                self._pending_actions.pop(action_id, None)

        ok = bool(isinstance(result, dict) and result.get('success'))
        self._audit('write_approved' if ok else 'write_approval_failed',
                    approver,
                    f'批准 {device_id}/{address}={execute_value} '
                    f'(action={action_id}) → {"成功" if ok else "失败"}')
        return True, result

    def reject_pending(self, action_id: str, approver: str,
                       reason: str = '') -> bool:
        with self._pending_lock:
            act = self._pending_actions.get(action_id)
            # 只允许驳回“仍在等待”的提案；正在执行的不能靠驳回假装取消
            if act is None or act.status != 'pending':
                return False
            self._pending_actions.pop(action_id, None)
        self._audit('write_rejected', approver,
                    f'驳回 {act.device_id}/{act.address}={act.value}'
                    + (f'，原因: {reason}' if reason else ''))
        logger.warning("待审批动作 %s 被 %s 驳回", action_id, approver)
        return True

    def validate_write(self, device_id: str, address: int, value: int,
                       operator: str, control_point: bool = False,
                       origin: str = 'human') -> dict[str, Any]:
        """
        写操作安全校验

        Args:
            control_point: ``True`` 表示这次写的是设备配置里显式声明的
                **控制点位**（``control.start/stop/reset``）。这类点位的地址与
                取值都来自 ``配置/devices*.yaml`` 的白名单，不是操作员输入的
                工艺设定值，因此不套用 ``write_limits``（那张表按寄存器名索引，
                控制点位不在 ``registers`` 里，硬套只会把批量启停全部拒掉）。
                其余闸门（急停/联锁/健康/互斥/回读）一样不少。

        Returns:
            {'allowed': bool, 'reason': str, 'warnings': list}
        """
        result = {'allowed': True, 'reason': '', 'warnings': []}

        # 0. 自主等级闸门（round 378）。
        #    人工下发（origin='human'）永远放行 —— 这一层只约束自动来源
        #    （边缘决策 / PID / 外部系统），把“人在环上/环外”变成可执行的策略：
        #    manual 拒绝、supervised 需武装、automatic 放行。
        if origin != 'human':
            auto_allowed, auto_reason = self._autonomy_allows_auto(device_id)
            if not auto_allowed:
                result['allowed'] = False
                result['reason'] = auto_reason
                return result
            if auto_reason == 'supervised':
                result['warnings'].append(
                    f'自动写入已授权（人在环上，操作人: '
                    f'{self.get_autonomy(device_id).get("armed_by") or "?"}）')

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
            if control_point:
                # 控制点位：批量控制入口已经按 `_interlock_rules` 逐台做过
                # 「已触发联锁 → 跳过该设备」的拦截；「没有任何规则」在这里
                # 只影响**任意寄存器设定值**的写（那些点位没有其它闸门）。
                # 若此处也拒绝，等于开箱状态下启停/复位全部不可用。
                result['warnings'].append(
                    '未加载到安全联锁规则：控制点位写入仅按急停/健康/互斥校验放行')
            else:
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
        if control_point:
            # 控制点位走配置白名单，不适用 write_limits
            result['warnings'].append(
                f'{device_id} 控制点位写入：按 devices 配置白名单执行，跳过 write_limits 量程校验'
            )

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
        if not limits and not control_point:
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

        if limits and not control_point:
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
        # round 373：锁内快照（理由同 batch_control）。
        with self._lock:
            interlock_rules = list(self._interlock_rules.items())
        for rule_id, rule in interlock_rules:
            if rule_id in self._interlock_bypass:
                continue
            action = rule.get('action', {})
            if action.get('type') == 'block_start':
                cond = rule.get('condition', {})
                # round 370（BUG-043）：**跨设备联锁要看动作目标**。
                # 典型配置是「A 的温度高 → 禁止 B 启动」：条件源是 A、
                # 受控目标是 B。原来只比对 `cond.device_id`，对 B 的写请求
                # 永远匹配不上，这条联锁形同虚设（而界面上显示已启用）。
                action_device = action.get('device_id')
                blocked = (cond.get('device_id') == device_id
                           or action_device == device_id
                           or action_device is None)
                if blocked:
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
        with self._lock:
            interlock_states = list(self._interlock_states.items())
        for rule_id, triggered in interlock_states:
            if triggered and rule_id not in self._interlock_bypass:
                rule = self._interlock_rules.get(rule_id, {})
                result['warnings'].append(f'注意: 联锁 {rule_id} ({rule.get("name")}) 当前触发中')

        return result

    def acquire_device_lock(self, device_id: str, operator: str, timeout: float = DEVICE_LOCK_TIMEOUT_S) -> bool:
        """获取设备操作锁（防止并发操作）。

        ⚠️ round 369：锁对象必须在 ``self._lock`` 内**原子**创建。
        原来的 `if device_id not in self._device_locks: self._device_locks[...] = Lock()`
        是典型的 TOCTOU：两个线程同时首次操作同一台设备时，A 创建 Lock_1 并
        获取成功，B 紧接着覆盖成 Lock_2 再获取 Lock_2 也成功 —— 两个线程
        同时进入同一台设备的写临界区，设备锁形同虚设。
        """
        with self._lock:
            lock = self._device_locks.setdefault(device_id, threading.Lock())

        acquired = lock.acquire(timeout=timeout)
        if acquired:
            with self._lock:
                self._device_lock_owners[device_id] = operator
        return acquired

    def release_device_lock(self, device_id: str):
        """释放设备操作锁"""
        with self._lock:
            self._device_lock_owners.pop(device_id, None)
            lock = self._device_locks.get(device_id)
        if lock is not None:
            try:
                lock.release()
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
        failed_devices = []
        if self.device_manager:
            from 采集层.interfaces import IDeviceManager
            all_devices = self.device_manager.get_all_devices()
            mechanical = [did for did, cfg in all_devices.items()
                         if IDeviceManager.get_device_category(cfg) == 'mechanical']
            logger.warning(f"紧急停机: 停止 {len(mechanical)} 个机械类设备（共 {len(all_devices)} 个设备）")
            for device_id in mechanical:
                try:
                    if self.device_manager.stop_device(device_id):
                        stopped_devices.append(device_id)
                    else:
                        # round 352：**停机失败必须留下痕迹**。
                        # 原来 False 被静默丢弃：10 台只停了 3 台，接口照样回
                        # `success: True`；下游 `_execute_interlock_action` 读
                        # `estop_res.get('success', True)` 拿到 True，把
                        # `_interlock_states[rule_id]` 锁成「已触发」——
                        # 剩下 7 台**永不再重试停机**。
                        failed_devices.append(device_id)
                        logger.error("紧急停机未成功: %s（stop_device 返回 False）", device_id)
                except Exception as e:
                    failed_devices.append(device_id)
                    logger.error(f"紧急停机失败 {device_id}: {e}")

        self._estop_devices = stopped_devices

        # round 315：落库 —— 重启后必须仍然处于急停（否则写操作会被放行）
        self._persist_estop()

        # 触发声光报警
        if self.alarm_manager:
            if self.alarm_manager.alarm_output:
                self.alarm_manager.alarm_output.trigger_alarm(
                    'critical', f'紧急停机: {reason}', 'estop')
            if self.alarm_manager.broadcast_system:
                self.alarm_manager.broadcast_system.speak(
                    f'紧急停机！{reason}，所有设备已停止运行，请立即检查！',
                    level='critical', source='estop')

        # 审计：失败清单必须进审计，不然事后只看到"已停机 N 台"。
        # ⚠️ 这里**只能有一条** `emergency_stop` 审计 —— 上一轮加这条时
        # 没注意到上面已经有一条同类型调用，结果每次急停写两条重复记录。
        self._audit('emergency_stop', 'system',
                    f'紧急停机: {reason}，已停 {len(stopped_devices)} 台'
                    + (f'，**停机失败 {len(failed_devices)} 台: {failed_devices}**'
                       if failed_devices else ''))

        return {
            # round 352：**有设备没停下来就不能叫成功**。
            # 急停的语义是"所有机械类设备停止"，部分成功等于没成功 ——
            # 下游联锁与界面都据此判断是否进入安全态。
            'success': not failed_devices,
            'message': (f'紧急停机已触发: {reason}' if not failed_devices
                        else f'紧急停机部分失败: {reason}'
                             f'（{len(failed_devices)} 台未能停止，请立即现场确认）'),
            'stopped_devices': stopped_devices,
            'failed_devices': failed_devices,
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

        # round 369：**审计先行 + fail-closed**。
        # 解除急停是"恢复生产"的动作，是整个系统里风险最高的一次状态切换：
        # 它把写闸门重新打开。原来审计写在所有状态变更**之后**，且不看返回值 ——
        # 磁盘满/目录只读时会出现「闸门已开、日志里查不到是谁开的」。
        if not self._audit('estop_reset_attempt', operator,
                           '请求解除紧急停机（联锁检查已通过）'):
            return {
                'success': False,
                'message': '审计写入失败，拒绝解除急停（无法自证的操作不允许执行）',
                'operator': operator,
            }

        self._estop_active = False
        self._estop_time = None
        self._estop_reason = ''

        # round 315：解除急停要同步清库，否则下次重启又"恢复"成急停
        self._persist_estop()

        # 通知设备管理器恢复
        #
        # round 369：**start_device 的返回值必须看**。
        # 原来 `self.device_manager.start_device(device_id)` 直接把 bool 丢掉，
        # 异常也只打日志，然后 `self._estop_devices = []` 一把清空 ——
        # 结果：10 台里 3 台没起来，接口照样回 `success: True`
        # "系统恢复正常"，而那 3 台**再也不会被重试**（名单已被抹掉）。
        restarted_devices: list[str] = []
        failed_devices: list[str] = []
        if self.device_manager:
            self.device_manager.set_estop_override(False)
            for device_id in list(getattr(self, '_estop_devices', []) or []):
                try:
                    if self.device_manager.start_device(device_id):
                        restarted_devices.append(device_id)
                    else:
                        failed_devices.append(device_id)
                        logger.error("解除停机失败 %s：start_device 返回 False"
                                     "（现场设备未恢复运行）", device_id)
                except Exception as e:
                    failed_devices.append(device_id)
                    logger.error(f"解除停机异常 {device_id}: {type(e).__name__}: {e}")
            # 只把**已成功恢复**的移出名单；失败的留着，下次还能重试
            self._estop_devices = failed_devices

        # 复位声光报警
        #
        # round 369：**复位灯之前必须先看还有没有别的活跃报警**。
        # 原来无条件 `alarm_output.reset()` —— 急停解除的同时把红灯/蜂鸣器
        # 一并清零成绿灯常亮，哪怕此刻还有超温、超压这类 Critical 工艺报警在响。
        # 现场看到的是"绿灯正常"，而实际仍在报警态，这是最危险的一种误指示。
        warnings: list[str] = []
        remaining_alarms: list[dict[str, Any]] | None = []
        tower_reset: bool | None = None
        if self.alarm_manager:
            out = getattr(self.alarm_manager, 'alarm_output', None)
            try:
                remaining_alarms = self.alarm_manager.get_active_alarms()
            except Exception as e:
                # 拿不到活跃报警 = 状态未知 → **保守：不灭灯**
                remaining_alarms = None
                logger.error("读取活跃报警失败，保守处理：不复位声光塔 — %s", e)
            if out:
                if remaining_alarms:
                    # 按最高活跃级别重新驱动，而不是清零
                    _rank = {'critical': 3, 'warning': 2, 'info': 1}
                    top = max(
                        remaining_alarms,
                        key=lambda a: _rank.get(str(a.get('alarm_level', '')).lower(), 0))
                    level = str(top.get('alarm_level', 'warning')).lower()
                    try:
                        out.activate_alarm(
                            level,
                            f'急停已解除，但仍存在 {len(remaining_alarms)} 条活跃报警')
                    except Exception as e:
                        logger.error("按存活报警重新驱动声光塔失败: %s", e)
                    warnings.append(
                        f'仍有 {len(remaining_alarms)} 条活跃报警，声光塔保持 {level} 态未复位')
                elif remaining_alarms is not None:
                    try:
                        tower_reset = bool(out.reset())
                    except Exception as e:
                        tower_reset = False
                        logger.error("声光塔复位异常: %s", e)
                    if tower_reset is False:
                        warnings.append('声光塔 DO 复位失败，现场红灯/蜂鸣器可能仍在动作')

        detail = (f'紧急停机已解除：恢复 {len(restarted_devices)} 台'
                  + (f'，**启动失败 {len(failed_devices)} 台: {failed_devices}**'
                     if failed_devices else ''))
        if not self._audit('estop_reset', operator, detail):
            warnings.append('解除结果审计写入失败（操作已执行，但日志缺失）')
        logger.info("紧急停机已解除 by %s（恢复 %d 台，失败 %d 台）",
                    operator, len(restarted_devices), len(failed_devices))

        # success 不能只看设备重启：声光塔复位失败时现场红灯/蜂鸣器可能
        # 仍在动作，仍回“系统恢复正常”会让操作员把真实报警当误亮。
        reset_problems: list[str] = []
        if failed_devices:
            reset_problems.append(
                f'{len(failed_devices)} 台设备未能恢复启动，'
                f'请现场确认：{"、".join(failed_devices)}')
        if tower_reset is False:
            reset_problems.append(
                '声光报警塔复位失败（红灯/蜂鸣器可能仍在动作），请现场确认')
        reset_ok = not reset_problems

        return {
            'success': reset_ok,
            'message': ('紧急停机已解除，系统恢复正常' if reset_ok
                        else '紧急停机已解除，但 ' + '；'.join(reset_problems)),
            'operator': operator,
            'restarted_devices': restarted_devices,
            'failed_devices': failed_devices,
            'warnings': warnings,
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
        # ⚠️ round 335：**bool 必须先排除**（bool 是 int 子类）。
        #
        # YAML 1.1 里未加引号的 `on`/`off`/`yes`/`no` 会被解析成 True/False。
        # 如果联锁动作写的是 `register: on`（本意是寄存器**名字**叫 "on"），
        # 走到这里 `isinstance(True, int)` 为真 → 直接返回 True（等于地址 1），
        # 而下游 `int(value)` 也把它当 1 —— **联锁写到了地址 1 的寄存器上**。
        # 同样 `try_int(True)` 也会返回 1，所以必须在两个入口之前就挡住。
        if isinstance(register_name, bool):
            logger.error(
                "联锁动作的 register 字段是布尔值 %r —— YAML 里 on/off/yes/no "
                "未加引号会被解析成布尔。如果它是寄存器名，请加引号（'on'）",
                register_name)
            return None

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
        # 采集侧是**唯一权威**的健康来源（它真的去读了寄存器）。
        # `refresh_device_health()` 只允许在「采集侧从没报过」时做初判，
        # 一旦有这条记录就必须以它为准 —— 见那里的注释。
        health['last_collect_ok'] = bool(connected)

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
                # round 343：用 `degraded` 而不是 `disconnected` ——
                # 采集失败不等于 TCP 断开。区分两者对现场处置很关键：
                #   disconnected = 网络/设备断电，先查线路；
                #   degraded     = 连着但读不到，先查地址/从站号/异常码。
                # `refresh_device_health()` 在确认 TCP 也断了时会升级成
                # `disconnected`（见那里）。
                health['status'] = 'degraded'
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
                    # round 352：**先执行、成功后才标记**。
                    # 原来先 `add` 再执行，而 `_trigger_fault_degradation`
                    # 把 handler 异常吞成一行日志 —— 于是"关阀/切手动"没执行时
                    # 设备已进 `_degraded_devices`，该标记只在采集成功时才清：
                    # 一次失败 = 整个掉线期间**永不重试**，而审计写着"已降级"。
                    if self._trigger_fault_degradation(device_id, failures):
                        self._degraded_devices.add(device_id)

    def _trigger_fault_degradation(self, device_id: str, failures: int) -> bool:
        """触发故障降级。

        Returns:
            bool: 安全状态 handler 是否执行成功。**False 时调用方不得把设备
            标记为「已降级」** —— 否则整个掉线期间都不会再重试（见
            `update_device_health` 里的 round 352 注释）。
        """
        logger.warning(f"设备 {device_id} 通信失败 {failures} 次，触发故障降级")

        # 执行安全状态
        handler = self._safe_state_handlers.get(device_id)
        handler_ok = True
        if handler:
            try:
                handler(device_id)
            except Exception as e:
                # round 352：**handler 失败不能被吞成"降级成功"**。
                # 原来这里只记一行 error 就往下走，调用方照样把设备加进
                # `_degraded_devices`（该标记只在采集成功时才清）——
                # 一次 handler 异常 = 设备整个掉线期间**一次都不重试**
                # 「关阀/切手动」，而审计里写着"已降级到安全状态"。
                handler_ok = False
                logger.error(f"安全状态执行失败 {device_id}: {e}", exc_info=True)
        else:
            # 没有注册 handler 不算失败（有些设备本来就没有安全动作），
            # 但要让这件事可见 —— 掉线了却没有任何安全动作，本身值得看一眼。
            logger.warning("设备 %s 掉线但没有注册安全状态 handler", device_id)

        # 触发报警
        if self.alarm_manager:
            if self.alarm_manager.alarm_output:
                self.alarm_manager.alarm_output.trigger_alarm(
                    'warning',
                    f'设备 {device_id} 通信中断（{failures}次），'
                    f'{"已进入安全状态" if handler_ok else "安全动作执行失败"}',
                    device_id
                )

        # 审计要如实记录结果 —— 失败时写"已降级"比不写更糟
        self._audit('fault_degradation', 'system',
                    f'设备 {device_id} 通信失败 {failures} 次，'
                    f'{"已降级到安全状态" if handler_ok else "安全状态执行失败，待重试"}')
        return handler_ok

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
            # round 343 把连续采集失败的状态改成了 'degraded'，
            # 但这里漏了这个固定键：看板读 summary['degraded'] 会 KeyError，
            # 前端只能显示 undefined。键必须与状态机一一对应。
            'degraded': 0,
            'unknown': 0,
            'devices': {}
        }
        # round 373：健康表由采集线程持续写入，读侧必须拿快照 ——
        # 否则看板刷新时会随机 500（dictionary changed size during iteration）。
        with self._lock:
            health_items = list(self._device_health.items())
        for device_id, health in health_items:
            status = health.get('status', 'unknown')
            summary[status] = summary.get(status, 0) + 1
            summary['devices'][device_id] = health
        return summary

    # ==================== 写操作回读验证 ====================

    def _resolve_register_config(self, device_id: str,
                                 name_or_address: Any) -> dict[str, Any] | None:
        """按点位名或地址取寄存器配置（含 scale/offset/data_type）。"""
        devices = getattr(self.device_manager, 'devices', None) or {}
        device = devices.get(device_id) or {}
        registers = device.get('registers') or []
        target = str(name_or_address)
        for reg in registers:
            if isinstance(reg, dict) and str(reg.get('name')) == target:
                return reg
        addr = try_int(name_or_address)
        if addr is not None:
            for reg in registers:
                if isinstance(reg, dict) and try_int(reg.get('address')) == addr:
                    return reg
        return None

    def _encode_engineering_write(self, device_id: str, address: int,
                                  value: Any) -> tuple[list[int] | None, int]:
        """把工程值转换成寄存器写入形态。

        Returns:
            `(words, raw)`：多寄存器点位返回 `(编码后的字列表, 0)`；
            单寄存器点位返回 `(None, 原始整数)`。
        """
        reg_cfg = self._resolve_register_config(device_id, address) or {}
        # round 372：换算统一走 `core.engineering_units.to_raw` ——
        # 本仓库原来有 5 份 `(value-offset)/scale`，其中两份漏换算、
        # 一份双算。scale=0 也由它显式报错，不再静默当成 1.0。
        _raw = to_raw(value, reg_cfg.get('scale'), reg_cfg.get('offset'))
        _scale = try_finite_float(reg_cfg.get('scale', 1.0)) or 1.0
        _dtype = str(reg_cfg.get('data_type', 'uint16')).lower()
        _order = self._device_byte_order(device_id)
        if _dtype in ('float32', 'float', 'real'):
            return encode_float32_registers(float(_raw), _order), 0
        if _dtype in ('int32', 'uint32'):
            return (encode_int32_registers(int(round(_raw)), _order,
                                           signed=(_dtype == 'int32')), 0)
        return None, int(round(_raw))

    def _device_byte_order(self, device_id: str) -> str:
        devices = getattr(self.device_manager, 'devices', None) or {}
        return str((devices.get(device_id) or {}).get('byte_order') or 'ABCD').upper()

    @staticmethod
    def _write_registers_compat(client: Any, address: int,
                                words: list[int]) -> bool:
        """连续写多个寄存器；客户端不支持时逐字退化写入。"""
        fn = getattr(client, 'write_multiple_registers', None)
        if callable(fn):
            try:
                return bool(fn(address, [int(w) & 0xFFFF for w in words]))
            except NotImplementedError:
                pass
        ok = True
        for offset, word in enumerate(words):
            ok = bool(client.write_single_register(address + offset,
                                                   int(word) & 0xFFFF)) and ok
        return ok

    def write_with_verification(self, device_id: str, address: int, value: int,
                                operator: str, max_retries: int = 2,
                                kind: str = 'register',
                                control_point: bool = False,
                                engineering: bool = False,
                                origin: str = 'human') -> dict[str, Any]:
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
        # HITL：自动来源在当前策略下不能直接执行时，**不是拒绝，而是提案**。
        # manual / 未武装的 supervised 都把动作排进待审批队列，由人批准、
        # 改值或驳回 —— 这才是"人在回路"，而不是"人盯着看"。
        if origin != 'human':
            auto_allowed, auto_reason = self._autonomy_allows_auto(device_id)
            if not auto_allowed:
                # 提案前先跑一遍**静态安全校验**（急停/联锁/量程/健康/互斥），
                # 明显违规的动作直接拒绝，不塞给人去批准 —— 否则 HITL 队列
                # 会变成"把危险动作包装成待办"。
                precheck = self.validate_write(
                    device_id, address, value, operator,
                    control_point=control_point, origin='human')
                if not precheck['allowed']:
                    self._audit('write_proposal_rejected', operator,
                                f'{device_id}/{address}={value} 提案被安全校验拒绝: '
                                f'{precheck["reason"]}')
                    return {
                        'success': False,
                        'message': f'自动动作未进入审批：{precheck["reason"]}',
                        'validation': precheck,
                    }
                proposal = self._propose_action(
                    device_id, address, value, operator,
                    kind=kind, engineering=engineering,
                    control_point=control_point, reason=auto_reason)
                return {
                    'success': False,
                    'pending_approval': True,
                    'action_id': proposal.action_id,
                    'message': f'已提交人工审批（{auto_reason}）',
                }

        # 安全校验
        validation = self.validate_write(device_id, address, value, operator,
                                         control_point=control_point,
                                         origin=origin)
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
            recheck = self.validate_write(device_id, address, value, operator,
                                          control_point=control_point)
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

            # round 322：**执行前先落一条 attempt 审计**。
            #
            # 为什么在"执行前"：写不进去就不许执行 —— "执行了但没记录"
            # 等于出事时说不清。这是审计完整性的标准做法（先记意图，再动手）。
            if not self._audit(
                    'write_attempt', operator,
                    f'即将写入({kind}): {device_id}/{address}={value}'):
                return {
                    'success': False,
                    'message': '审计日志不可写，已拒绝执行写入（无法留痕，请先修复日志目录）',
                    'audit_failed': True,
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

            # round 370（BUG-002/052）：**工程值写入**。
            #
            # 联锁动作、边缘 PID 输出、`/adjust` 传的都是工艺值（50.0°C），
            # 而寄存器里存的是 (value-offset)/scale 的原始整数；float32/int32/
            # uint32 还要占两个寄存器。原来这条路径既不换算也不编码：
            # 非整数直接拒绝（PID 永远写不进去），整数则按未缩放值写错量。
            #
            # 安全校验用**工程值**（write_limits 的上下限就是工艺单位），
            # 换算放在校验/审计之后 —— 这样审计日志里记的也是人能看懂的值。
            write_words: list[int] | None = None
            expected_raw: int | None = None
            if kind != 'coil' and engineering:
                reg_cfg = self._resolve_register_config(device_id, address)
                if reg_cfg is None:
                    self._audit('write_failed', operator,
                                f'写入失败: 无法解析 {device_id}/{address} 的寄存器配置')
                    return {'success': False,
                            'message': f'设备 {device_id} 的地址 {address} 不在寄存器表中'}
                # round 372：与 `_encode_engineering_write` 共用同一个换算入口。
                _scale = try_finite_float(reg_cfg.get('scale', 1.0)) or 1.0
                _raw = to_raw(value, reg_cfg.get('scale'), reg_cfg.get('offset'))
                _dtype = str(reg_cfg.get('data_type', 'uint16')).lower()
                _order = self._device_byte_order(device_id)
                if _dtype in ('float32', 'float', 'real'):
                    write_words = encode_float32_registers(float(_raw), _order)
                elif _dtype in ('int32', 'uint32'):
                    write_words = encode_int32_registers(
                        int(round(_raw)), _order, signed=(_dtype == 'int32'))
                else:
                    expected_raw = int(round(_raw))
                    if expected_raw != _raw and abs(expected_raw - _raw) > 1e-9:
                        logger.info("工程值 %s 按 scale=%s 换算为 %s（非整数，四舍五入）",
                                    value, _scale, expected_raw)

            # round 339：**寄存器写入不接受非整数**，不做静默截断。
            #
            # 原实现直接 `int(value)`：`1.9 → 1`，PLC 收到 1，而审计日志记的是
            # 1.9（`_audit` 里 `f'{device_id}/{address}={value}'`）——
            # **日志与现场不一致**，而且系统报「写入成功」。
            # 实测链路：入口 `safe_float` 放行 1.9 → 量程校验通过（1.9 在 INT16 内）
            # → 到这里被截成 1。
            #
            # 本路径**不具备 float32 写能力**（那要按 2 个寄存器编位 + 回读解码），
            # 所以宁可拒绝也不猜 —— 猜错的代价是写了一个错误的设定值。
            # 线圈分支保持 `bool(value)`：非零即真本来就是线圈语义。
            if kind != 'coil' and not engineering:
                _fv = try_finite_float(value)
                if _fv is None:
                    self._audit('write_failed', operator,
                                f'写入失败: 值不是有限数值 ({device_id}/{address}={value!r})')
                    return {'success': False,
                            'message': f'写入值不是有限数值: {value!r}'}
                if not _fv.is_integer():
                    self._audit('write_failed', operator,
                                f'写入失败: 寄存器只接受整数，拒绝 {value!r} '
                                f'({device_id}/{address})')
                    return {
                        'success': False,
                        'message': (
                            f'寄存器写入只接受整数值：{value!r} 会被截断成 '
                            f'{int(_fv)} —— 已拒绝。若该点位是 float32，'
                            f'需要按双寄存器编码写入，当前接口不支持。'),
                    }

            for attempt in range(max_retries + 1):
                try:
                    if kind == 'coil':
                        success = client.write_single_coil(int(address), bool(value))
                    elif write_words is not None:
                        success = self._write_registers_compat(
                            client, int(address), write_words)
                    else:
                        success = client.write_single_register(
                            int(address),
                            expected_raw if expected_raw is not None else int(value))
                    break
                except Exception as e:
                    if attempt < max_retries:
                        logger.warning(f"写入重试 {attempt + 1}/{max_retries}: {e}")
                        time.sleep(0.1)
                    else:
                        # round 341：**异常必须在这里收住，不能 raise 出去**。
                        #
                        # 原来最后一次重试仍失败就 `raise`，异常一路穿到
                        # API 层变成 **500 Internal Server Error**：
                        #   ① 调用方看到 500，分不清「设备超时」还是「代码 bug」；
                        #   ② **审计里没有 write_failed** —— 这次写入尝试
                        #      在系统里不留任何痕迹，事后查不到「谁试过写什么」。
                        # 写超时是现场最常见的失败，必须走正常失败路径。
                        logger.error(
                            "写入失败（已重试 %d 次）: %s/%s=%s — %s",
                            max_retries, device_id, address, value, e)
                        self._audit('write_failed', operator,
                                    f'写入异常({kind}): {device_id}/{address}={value}'
                                    f' — {type(e).__name__}: {e}')
                        return {
                            'success': False,
                            'message': f'写入异常（已重试 {max_retries} 次）：{e}',
                            'kind': kind,
                            'verified': None,
                        }

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
                # ⚠️ round 341：回读**必须是新鲜数据**。
                #
                # `ModbusClient` 在读取失败时会返回「最后已知良好值」并把
                # `last_read_source` 置为 'cache'。这里原来只看 `if raw:`，
                # 于是两个方向都会错：
                #   ① 写失败 + 缓存里恰好是同一个值 → verified=True
                #      → 报「写入成功且已校验」，设备其实没动；
                #   ② 写成功 + 回读超时 → 缓存里是**旧值** → verified=False
                #      → 误报失败，上层重试 → 对累加类寄存器**重复下发**。
                # 判据：读回来不是 fresh，一律按「未验证」处理（None），
                # 既不算通过也不算失败 —— 交给调用方按通信状态判断。
                if kind == 'coil':
                    if hasattr(client, 'read_coils'):
                        raw = client.read_coils(int(address), 1)
                        fresh = getattr(client, 'last_read_source', 'fresh') == 'fresh'
                        if raw and fresh:
                            readback_value = bool(raw[0])
                            verified = (readback_value == bool(value))
                        elif raw:
                            logger.warning(
                                "回读来自陈旧缓存，按未验证处理（不能当通过）: %s/%s",
                                device_id, address)
                else:
                    if hasattr(client, 'read_holding_registers'):
                        _count = len(write_words) if write_words else 1
                        raw = client.read_holding_registers(int(address), _count)
                        fresh = getattr(client, 'last_read_source', 'fresh') == 'fresh'
                        if raw and fresh:
                            if write_words is not None:
                                # 双寄存器点位按**字**比对（解码后比对会引入浮点误差）
                                readback_value = [int(w) & 0xFFFF for w in raw[:_count]]
                                verified = readback_value == write_words
                            else:
                                readback_value = int(raw[0])
                                _expected = (expected_raw
                                             if expected_raw is not None else int(value))
                                verified = (readback_value == _expected)
                        elif raw:
                            logger.warning(
                                "回读来自陈旧缓存，按未验证处理（不能当通过）: %s/%s",
                                device_id, address)
                if verified is False:
                    logger.warning(f"回读验证失败: 期望 {value}, 实际 {readback_value}")
            except Exception as e:
                logger.warning(f"回读验证异常（按未验证处理）: {e}")
                verified = None

            # 审计
            #
            # ⚠️ 审计动作必须与**真实结论**一致。原来无论回读结果如何一律记
            # `write_success` —— 回读明确证明「设备没接受该值」时，审计流里
            # 仍是一条成功的写入记录。事故复盘时这条记录会把责任指向操作员
            # （"他写成功了"），而现场实际没动。分类：
            #   · 验证通过   → write_success
            #   · 无法验证   → write_unverified（指令下发成功，但没拿到新鲜回读）
            #   · 验证失败   → write_verify_failed（下面 success 会被置 False）
            verify_text = ('未验证' if verified is None
                           else ('验证通过' if verified else '验证失败'))
            if verified is False:
                audit_action = 'write_verify_failed'
                audit_head = '写入回读验证失败'
            elif verified is None:
                audit_action = 'write_unverified'
                audit_head = '写入成功但未验证'
            else:
                audit_action = 'write_success'
                audit_head = '写入成功'
            self._audit(audit_action, operator,
                        f'{audit_head}({kind}): {device_id}/{address}={value}'
                        f'{f", 回读={readback_value}" if readback_value is not None else ""}'
                        f', {verify_text}')

            result = {
                'success': True,
                'message': '写入成功',
                'kind': kind,
                'verified': verified,
                #: 现场状态是否**确认**已改变。`verified is False` 时为 False。
                #: 与 `success` 的区别：`success` 表示"指令下发成功"，
                #: `effective` 表示"设备真的变成了这个值"。
                'effective': verified is not False,
                'warnings': list(validation.get('warnings', []))
            }
            if verified is False:
                # round 322：**回读不一致必须算失败**。
                #
                # 原来这里只把 message 改成"写入成功，但回读验证失败"，
                # 而 `success` 仍是 True —— 于是：
                #   · 前端只看 `success` → 弹"写入成功"的绿条；
                #   · 接口调用方按 2xx 判定成功 → 继续往下走。
                # 但设备**并没有变成期望的值**。操作员以为设定值改了，
                # 实际没有 —— 这在联锁/联锁旁路/温控这类场景是能出事的。
                # 判据：**"没生效"就是失败**，把 `verified` 降级成 warning 是自欺。
                result['success'] = False
                result['message'] = (
                    f'写入未生效：回读值为 {readback_value}，与写入值 {value} 不一致'
                    f'（指令已下发，但设备未接受该值）')
                result['warnings'].append(
                    f'回读值 {readback_value} 与写入值 {value} 不一致')
            elif verified is None:
                result['message'] = '写入成功，但无法回读验证'
                result['warnings'].append('未能回读该点位（客户端不支持或回读失败）')

            return result

        finally:
            self.release_device_lock(device_id)

    # ==================== 操作审计 ====================

    def _load_last_audit_hash(self) -> str:
        """从落盘日志恢复链尾哈希 —— 让哈希链跨重启连续。

        只倒读文件尾部 8KB：审计日志是 `deque(maxlen=50000)` + 追加文件，
        可能几万行，没必要整读。倒读会截断首行，`json.loads` 失败就跳过，
        这正是我们要的（那一行本来就不完整）。
        """
        try:
            if not self._audit_file.exists():
                return ''
            size = self._audit_file.stat().st_size
            if size <= 0:
                return ''
            with open(self._audit_file, 'rb') as f:
                f.seek(max(0, size - 8192))
                tail = f.read().decode('utf-8', 'replace')
            for line in reversed([ln for ln in tail.splitlines() if ln.strip()]):
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                h = entry.get('hash')
                if isinstance(h, str) and h:
                    return h
        except Exception as e:      # noqa: BLE001 —— 恢复失败不该挡住启动
            logger.warning("恢复审计链尾失败，本次以空 prev 起链: %s", e)
        return ''

    def _audit(self, action: str, operator: str, detail: str) -> bool:
        """记录操作审计日志。返回**是否成功落盘**（round 322）。

        ## 为什么要有返回值

        原来这里 `except` 只打一行 ERROR，**调用方完全不知道**。
        于是磁盘满 / 目录只读 / 权限错的时候：操作照常执行，
        但**没有任何记录**。事故调查时"系统里查不到"和"没发生过"没法区分，
        这在需要自证的场景里是致命的。

        现在调用方（`write_with_verification`）在**执行前**先写一条
        `write_attempt`，写不进去就**拒绝执行**。

        ## 哈希链

        每条记录带 `prev`（上一条的 `hash`）与自己的 `hash`
        （`sha256(prev + 记录内容)[:16]`）。删掉或改动中间任意一条，
        后面的链就对不上 —— 能证明"日志没被动过"。
        `prev` 为空串表示链的起点（或历史日志，老记录没有这两个字段）。
        """
        with self._audit_lock:
            prev_hash = self._audit_last_hash
            entry = {
                'timestamp': datetime.now().isoformat(timespec='milliseconds'),
                'action': action,
                'operator': operator,
                'detail': detail,
                'prev': prev_hash,
            }
            entry['hash'] = hashlib.sha256(
                (prev_hash + json.dumps(entry, ensure_ascii=False, sort_keys=True)
                 ).encode('utf-8')).hexdigest()[:16]

            try:
                self._audit_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self._audit_file, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + '\n')
            except Exception as e:
                # 落盘失败：**不进内存列表、不推进链尾** —— 内存与文件保持一致，
                # 否则接口里能看到一条"文件里没有"的记录。
                #
                # round 347：**日志必须带上是哪次操作**，级别升为 CRITICAL。
                #
                # 全仓 19 处 `_audit` 调用里只有 2 处检查了返回值
                # （`write_with_verification` 与 `batch_control` 的 attempt），
                # 其余 17 处（急停、旁路审批、降级、结果记录）**丢弃返回值**。
                # 原来这里只打一行「审计日志写入失败」，不带 action/operator ——
                # 现场看到这行也不知道「哪次操作没被记下来」，
                # 而急停/旁路审批正是最需要自证的两类动作。
                #
                # 现在把三元组打进日志，级别 CRITICAL（自证缺口是严重事件）。
                # 不在这里强制 fail-closed：急停必须执行，不能因为记不下来就不停设备；
                # 需要 fail-closed 的调用点自己检查返回值（已由调用点决定）。
                logger.critical(
                    "【自证缺口】审计写入失败 —— 本次操作**不会留下任何记录**: "
                    "action=%s operator=%s detail=%s — %s",
                    action, operator, detail, e)
                return False

            self._audit_log.append(entry)
            self._audit_last_hash = entry['hash']
            return True

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
