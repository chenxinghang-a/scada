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
    first_trigger_time: float = 0
    last_escalation_time: float = 0
    escalation_history: List[Dict[str, Any]] = field(default_factory=list)
    acknowledged: bool = False


class AlarmEscalationManager:
    """告警升级管理器"""

    # 默认升级规则
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
        """加载配置"""
        rules_cfg = config.get('rules', [])
        if rules_cfg:
            self._rules = []
            for r in rules_cfg:
                self._rules.append(EscalationRule(
                    level=r.get('level', 1),
                    timeout_seconds=r.get('timeout_seconds', 300),
                    actions=r.get('actions', ['notify']),
                    notify_roles=r.get('notify_roles', ['operator']),
                    message_template=r.get('message_template', ''),
                ))

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

    def record_alarm(self, alarm_id: str, device_id: str, register_name: str):
        """记录新告警"""
        key = f"{alarm_id}:{device_id}:{register_name}"
        with self._lock:
            if key not in self._states:
                self._states[key] = EscalationState(
                    alarm_id=alarm_id,
                    device_id=device_id,
                    register_name=register_name,
                    current_level=0,
                    first_trigger_time=time.time(),
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
        """检查所有告警是否需要升级"""
        now = time.time()
        to_escalate = []

        with self._lock:
            for key, state in self._states.items():
                if state.acknowledged:
                    continue

                # 找到当前应该升级到的级别
                elapsed = now - state.first_trigger_time
                target_level = state.current_level

                for rule in self._rules:
                    if rule.level > state.current_level and elapsed >= rule.timeout_seconds:
                        target_level = rule.level

                if target_level > state.current_level:
                    to_escalate.append((key, state, target_level))

        # 执行升级
        for key, state, target_level in to_escalate:
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
                    'elapsed_seconds': time.time() - state.first_trigger_time,
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
                        'elapsed_seconds': time.time() - v.first_trigger_time,
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
        """更新升级规则"""
        self._rules = []
        for r in rules:
            self._rules.append(EscalationRule(
                level=r.get('level', 1),
                timeout_seconds=r.get('timeout_seconds', 300),
                actions=r.get('actions', ['notify']),
                notify_roles=r.get('notify_roles', ['operator']),
                message_template=r.get('message_template', ''),
            ))
        logger.info(f"升级规则已更新: {len(self._rules)} 条")
