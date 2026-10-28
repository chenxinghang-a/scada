"""
告警升级机制模块
实现超时未处理告警的自动升级

功能：
- 多级升级（警告→严重→紧急）
- 可配置升级规则
- 升级历史记录
- 升级通知
"""
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field

from core.safe_numeric import require_positive_int

logger = logging.getLogger(__name__)


@dataclass
class EscalationRule:
    """升级规则"""
    level: int  # 升级级别 (1=初始, 2=升级, 3=紧急)
    timeout_seconds: int  # 超时时间
    actions: List[str]  # 升级动作: notify, broadcast, sms, email
    notify_roles: List[str]  # 通知角色
    message_template: str = ""  # 消息模板


@dataclass
class EscalationState:
    """告警升级状态"""
    alarm_id: str
    device_id: str
    register_name: str
    current_level: int = 0
    #: **墙钟**首次触发时刻 —— 仅用于展示（`get_status` 会算 elapsed）
    first_trigger_time: float = 0
    #: **单调时钟**首次触发时刻 —— 升级超时判断用它。
    #: round 280：`check_escalations` 原来用 `time.time() - first_trigger_time`
    #: 算"过了多久"。升级超时表达的是**时长**，墙钟被回拨一次（NTP 校时 /
    #: 现场机开机后校时 / 手工改时间）→ `elapsed` 变负 → **永不升级**，
    #: on-call 永远收不到电话；向前跳则一次补跑所有级别，半夜给三级主管连环打电话。
    first_trigger_mono: float = 0
    last_escalation_time: float = 0
    escalation_history: List[Dict[str, Any]] = field(default_factory=list)
    acknowledged: bool = False


def _validate_rules(rules: Any) -> List[EscalationRule]:
    """把升级规则配置收敛成 `EscalationRule` 列表，非法一律抛 ValueError。

    round 249 增加。原来 `update_rules` / `_load_config` 对入参**零校验**：

    - `rules: []` → `update_rules` 把 `self._rules` 清空 → **升级机制被关掉**，
      且接口回「更新成功」；
    - `timeout_seconds: 0` → 报警一触发就升级，升级机制变成打鸣；
    - `level` 缺失/为 0 → `_do_escalation` 里按 level 找规则找不到
      → **静默不升级**。

    升级是安全功能，配置它的口子必须和删数据一样保守：
    非空列表 + 每条 level/timeout 都是 >= 1 的整数 + level 不重复。
    """
    if not isinstance(rules, list) or not rules:
        raise ValueError(
            '升级规则必须是非空列表 —— 空列表等于关闭全部升级，不接受'
        )

    validated: List[EscalationRule] = []
    seen_levels = set()
    for i, r in enumerate(rules):
        if not isinstance(r, dict):
            raise ValueError(f'第 {i + 1} 条规则不是字典: {r!r}')
        level = require_positive_int(r.get('level', 1), f'第 {i + 1} 条规则的 level')
        timeout = require_positive_int(
            r.get('timeout_seconds', 300), f'第 {i + 1} 条规则的 timeout_seconds')
        if level in seen_levels:
            raise ValueError(
                f'升级级别重复: level={level} —— 同一级两条规则会让 '
                f'_do_escalation 只命中第一条，另一条静默失效'
            )
        seen_levels.add(level)
        validated.append(EscalationRule(
            level=level,
            timeout_seconds=timeout,
            actions=r.get('actions', ['notify']),
            notify_roles=r.get('notify_roles', ['operator']),
            message_template=r.get('message_template', ''),
        ))
    return validated


class AlarmEscalationManager:
    """告警升级管理器"""

    #: 默认升级规则。**只在配置文件完全没给 rules 段时使用** ——
    #: 显式的 `rules: []` 会被 `_load_config` 拒绝（见其 docstring）。
    DEFAULT_RULES = [
        EscalationRule(
            level=1,
            timeout_seconds=300,  # 5分钟
            actions=['notify'],
            notify_roles=['operator'],
            message_template='告警超时未处理: {alarm_message}'
        ),
        EscalationRule(
            level=2,
            timeout_seconds=900,  # 15分钟
            actions=['notify', 'broadcast'],
            notify_roles=['engineer', 'operator'],
            message_template='告警升级: {alarm_message} 已超时15分钟'
        ),
        EscalationRule(
            level=3,
            timeout_seconds=1800,  # 30分钟
            actions=['notify', 'broadcast', 'sms'],
            notify_roles=['admin', 'engineer'],
            message_template='紧急告警升级: {alarm_message} 已超时30分钟'
        ),
    ]

    def __init__(self, config: Dict[str, Any] = None):
        self._lock = threading.Lock()
        self._states: Dict[str, EscalationState] = {}
        self._rules: List[EscalationRule] = self.DEFAULT_RULES.copy()
        self._callbacks: List[Callable] = []
        self._check_interval = 30  # 30秒检查一次
        self._running = False
        self._thread: Optional[threading.Thread] = None

        if config:
            self._load_config(config)

    def _load_config(self, config: Dict[str, Any]):
        """加载配置

        ⚠️ round 249：`rules` 段**不允许为空列表**。空列表会被下面
        `if rules_cfg:` 判为 falsy 而**静默保留 DEFAULT_RULES**
        （300/900/1800 秒）—— 于是「配置里写了一条 600 秒规则」
        和「配置里写了个空列表」表现不同，而后者看起来像"关掉了升级"，
        实际仍在按 300 秒升级。删数据/关保护的方向上不接受空，
        一律抛 ValueError，让问题在加载时就炸出来。
        """
        rules_cfg = config.get('rules', [])
        if rules_cfg:
            self._rules = _validate_rules(rules_cfg)
        elif 'rules' in config:
            # 显式写了 rules 但是空/非法 → 不容错
            raise ValueError(
                'escalation.rules 不能为空列表 —— 空列表会被当成"关闭升级"，'
                '但 DEFAULT_RULES 仍在按 300/900/1800 秒升级。'
                '如确需单级升级，请给一条规则。'
            )

        self._check_interval = config.get('check_interval', 30)

    def start(self):
        """启动升级检查"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()
        logger.info("告警升级管理器已启动")

    def stop(self):
        """停止升级检查"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("告警升级管理器已停止")

    def _check_loop(self):
        """检查循环"""
        while self._running:
            try:
                self.check_escalations()
            except Exception as e:
                logger.error(f"升级检查异常: {e}")
            time.sleep(self._check_interval)

    def record_alarm(self, alarm_id: str, device_id: str, register_name: str,
                     first_trigger_time: float | None = None):
        """记录新告警

        Args:
            first_trigger_time: 报警**最初**触发的墙钟时刻（epoch 秒）。
                进程重启后从库里恢复时传入，这样"已超时多久"接着算，
                而不是从重启那一刻重新计时（否则一条已经拖了 2 小时的
                未确认报警，重启后升级倒计时归零，**永远不会升级**）。
                缺省取当前时刻。
        """
        key = f"{alarm_id}:{device_id}:{register_name}"
        with self._lock:
            if key not in self._states:
                now_wall = time.time()
                anchor_wall = (first_trigger_time
                               if isinstance(first_trigger_time, (int, float))
                               and first_trigger_time > 0 else now_wall)
                # 用"从 anchor 到现在经过的墙钟时长"反推单调时钟起点，
                # 让两个基准保持同一个相对关系（判定用 mono，展示用 wall）。
                anchor_mono = time.monotonic() - max(0.0, now_wall - anchor_wall)
                self._states[key] = EscalationState(
                    alarm_id=alarm_id,
                    device_id=device_id,
                    register_name=register_name,
                    current_level=0,
                    first_trigger_time=anchor_wall,
                    first_trigger_mono=anchor_mono,
                )
                logger.debug(f"记录告警升级跟踪: {key}")

    def acknowledge_alarm(self, alarm_id: str, device_id: str, register_name: str):
        """确认告警"""
        key = f"{alarm_id}:{device_id}:{register_name}"
        with self._lock:
            if key in self._states:
                self._states[key].acknowledged = True
                logger.debug(f"告警已确认，停止升级跟踪: {key}")

    def remove_alarm(self, alarm_id: str, device_id: str, register_name: str):
        """移除告警"""
        key = f"{alarm_id}:{device_id}:{register_name}"
        with self._lock:
            if key in self._states:
                del self._states[key]

    def add_callback(self, callback: Callable):
        """添加升级回调"""
        self._callbacks.append(callback)

    def check_escalations(self):
        """检查所有告警是否需要升级

        round 249：原实现一次只跳到**当前适用的最高级别**
        （`for rule in self._rules: ... target_level = rule.level` 取最后一个
        命中的），于是当检查迟到（定时器被阻塞 / 首次检查就远超多级阈值）时，
        中间级别被**静默跳过** —— 每条规则有自己的 notify_roles，
        跳级意味着「该第一级通知的人从来没被通知过」，升级链断了一节。

        改成逐级补跑：从 current_level+1 开始，把每一级按顺序触发完。
        正常路径（每级阈值之间都有检查）行为完全不变，只是一次检查
        最多触发多级。
        """
        now = time.monotonic()
        to_escalate = []

        with self._lock:
            for key, state in self._states.items():
                if state.acknowledged:
                    continue
                # 单调时钟：升级超时是时长语义（墙钟回拨会让它永不升级）
                elapsed = now - state.first_trigger_mono
                # 逐级补跑：按 level 升序，把已超时且未触发过的级都挑出来
                for rule in sorted(self._rules, key=lambda r: r.level):
                    if rule.level <= state.current_level:
                        continue
                    if elapsed >= rule.timeout_seconds:
                        to_escalate.append((key, state, rule.level))

        # 执行升级（按级别从低到高，保证通知顺序）
        for key, state, target_level in sorted(to_escalate, key=lambda t: t[2]):
            # round 280：**执行前重新校验**。`to_escalate` 是锁内快照，
            # 而 `_do_escalation` 会调回调（发短信/打电话，可能耗时数秒）——
            # 这段时间里报警可能已经恢复（`remove_alarm`）或被确认。
            # 不重查的话，会对一条**已经不存在的报警**执行升级：
            # 半夜把值班电话打起来，而现场早就正常了。
            with self._lock:
                live = self._states.get(key)
                if live is None or live.acknowledged:
                    logger.debug("升级前复核：报警已恢复/已确认，跳过 %s", key)
                    continue
            self._do_escalation(key, state, target_level)

    def _do_escalation(self, key: str, state: EscalationState, target_level: int):
        """执行升级"""
        rule = None
        for r in self._rules:
            if r.level == target_level:
                rule = r
                break

        if not rule:
            return

        with self._lock:
            state.current_level = target_level
            state.last_escalation_time = time.time()

            escalation_record = {
                'level': target_level,
                'time': datetime.now().isoformat(),
                'actions': rule.actions,
                'notify_roles': rule.notify_roles,
            }
            state.escalation_history.append(escalation_record)

        logger.warning(f"告警升级: {state.alarm_id} ({state.device_id}/{state.register_name}) "
                      f"升级到级别 {target_level}")

        # 触发升级回调
        for callback in self._callbacks:
            try:
                callback({
                    'alarm_id': state.alarm_id,
                    'device_id': state.device_id,
                    'register_name': state.register_name,
                    'level': target_level,
                    'rule': rule,
                    # round 311：**与 318 行同一口径**（单调时钟）。
                    # 这里原来用墙钟减 `first_trigger_time`，而判定升级用的是
                    # `first_trigger_mono` —— 同一条报警的"已超时多久"在
                    # 判定里和通知里**可能是两个数**：校时后通知里会出现
                    # 负数（"已超时 -3598 秒"），直接打到三级主管的电话话术里。
                    'elapsed_seconds': time.monotonic() - state.first_trigger_mono,
                })
            except Exception as e:
                logger.error(f"升级回调异常: {e}")

    def get_status(self) -> Dict[str, Any]:
        """获取升级状态"""
        with self._lock:
            return {
                'running': self._running,
                'tracked_alarms': len(self._states),
                'rules_count': len(self._rules),
                'check_interval': self._check_interval,
                'states': {
                    k: {
                        'alarm_id': v.alarm_id,
                        'device_id': v.device_id,
                        'register_name': v.register_name,
                        'current_level': v.current_level,
                        'acknowledged': v.acknowledged,
                        # 时长用单调时钟（墙钟回拨会算出负数）
                        'elapsed_seconds': time.monotonic() - v.first_trigger_mono,
                        'escalation_count': len(v.escalation_history),
                    }
                    for k, v in self._states.items()
                },
            }

    def get_rules(self) -> List[Dict[str, Any]]:
        """获取升级规则"""
        return [
            {
                'level': r.level,
                'timeout_seconds': r.timeout_seconds,
                'actions': r.actions,
                'notify_roles': r.notify_roles,
                'message_template': r.message_template,
            }
            for r in self._rules
        ]

    def update_rules(self, rules: List[Dict[str, Any]]):
        """更新升级规则（热更新，不重启）

        ⚠️ round 249：空列表 = **取消全部升级**，一律拒绝。
        升级是安全功能，`PUT {"rules": []}` 不该能把静默关掉。
        每条规则的 `level` / `timeout_seconds` 也校验（>= 1 的整数）：
        `timeout_seconds: 0` 会让报警**一触发就升级`，等于升级机制打鸣。
        """
        self._rules = _validate_rules(rules)
        logger.info(f"升级规则已更新: {len(self._rules)} 条")
