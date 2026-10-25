"""
报警管理器模块（重写版）
实现报警检测、记录、通知，集成工业声光报警器、语音广播、前端推送

核心改进：警告去重机制
- 冷却窗口：同一报警在冷却时间内只推送一次前端通知
- 确认抑制：用户确认报警后，同一报警在抑制时间内不再弹窗
- 前端dismissed跟踪：用户关闭弹窗后，该报警在冷却期内不重复显示
- 去重配置API：可动态调整去重参数
"""

import logging
import shutil
import yaml
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
import paths
from core.safe_numeric import to_finite_float, try_finite_float
from core.config_io import dump_yaml, ConfigPersistError
from 报警层.alarm_engine import (
    CLEAR as _ACT_CLEAR,
    FIRE as _ACT_FIRE,
    PENDING as _ACT_PENDING,
    UPDATE as _ACT_UPDATE,
    decide,
    evaluate_condition,
    new_state,
    normalize_delay,
)

logger = logging.getLogger(__name__)


class AlarmDedupConfig:
    """报警去重配置"""

    def __init__(self, config: dict[str, Any] | None = None):
        config = config or {}
        # 冷却窗口（秒）：同一报警在此时间内只推送一次前端通知
        self.emit_cooldown_seconds: int = config.get('emit_cooldown_seconds', 300)
        # 确认后抑制时间（秒）：用户确认报警后，同一报警在此时间内不再弹窗
        self.acknowledge_suppress_seconds: int = config.get('acknowledge_suppress_seconds', 600)
        # 是否启用去重
        self.enabled: bool = config.get('enabled', True)
        # 最大同时显示的报警数（前端）
        self.max_visible_toasts: int = config.get('max_visible_toasts', 3)
        # 前端toast自动消失时间：严重报警（秒）
        self.critical_toast_duration: int = config.get('critical_toast_duration', 30)
        # 前端toast自动消失时间：普通警告（秒）
        self.warning_toast_duration: int = config.get('warning_toast_duration', 10)

    def to_dict(self) -> dict[str, Any]:
        return {
            'enabled': self.enabled,
            'emit_cooldown_seconds': self.emit_cooldown_seconds,
            'acknowledge_suppress_seconds': self.acknowledge_suppress_seconds,
            'max_visible_toasts': self.max_visible_toasts,
            'critical_toast_duration': self.critical_toast_duration,
            'warning_toast_duration': self.warning_toast_duration,
        }

    def update(self, data: dict[str, Any]):
        """从字典更新配置"""
        if 'emit_cooldown_seconds' in data:
            self.emit_cooldown_seconds = max(5, int(data['emit_cooldown_seconds']))
        if 'acknowledge_suppress_seconds' in data:
            self.acknowledge_suppress_seconds = max(30, int(data['acknowledge_suppress_seconds']))
        if 'enabled' in data:
            self.enabled = bool(data['enabled'])
        if 'max_visible_toasts' in data:
            self.max_visible_toasts = max(1, min(10, int(data['max_visible_toasts'])))
        if 'critical_toast_duration' in data:
            self.critical_toast_duration = max(5, int(data['critical_toast_duration']))
        if 'warning_toast_duration' in data:
            self.warning_toast_duration = max(5, int(data['warning_toast_duration']))


class AlarmShelveState:
    """报警旁路/搁置状态"""

    def __init__(self, alarm_key: tuple, reason: str, shelved_by: str,
                 shelved_until: datetime | None = None):
        self.alarm_key = alarm_key
        self.reason = reason
        self.shelved_by = shelved_by
        self.shelved_at = datetime.now()
        self.shelved_until = shelved_until  # None = 手动解除

    def is_expired(self) -> bool:
        if self.shelved_until is None:
            return False  # 手动旁路，不过期
        return datetime.now() > self.shelved_until

    def to_dict(self) -> dict:
        return {
            'alarm_key': ':'.join(self.alarm_key),
            'reason': self.reason,
            'shelved_by': self.shelved_by,
            'shelved_at': self.shelved_at.isoformat(),
            'shelved_until': self.shelved_until.isoformat() if self.shelved_until else None,
        }


class AlarmFloodDetector:
    """告警洪水检测器 - ISA-18.2

    当告警频率超过阈值时自动抑制低优先级告警，
    防止操作员被大量告警淹没（工业安全关键）
    """

    # 严重度分级表。规则配置实际使用三级命名（critical/warning/info），
    # 历史/第三方配置可能使用五级命名（critical/high/medium/low/info），
    # 两套命名必须归一到同一量纲。旧表只有五级命名且用 .get(severity, 0)
    # 兜底 —— `warning` 落到 0，被当成最低优先级，洪水期静默抑制 = 漏报。
    SEVERITY_ORDER = {
        'critical': 5,
        'high': 4,
        'warning': 4,   # ≡ high：告警级，洪水期不抑制
        'medium': 3,
        'low': 2,
        'info': 1,
    }

    # 未知严重度的兜底档：取最高档（fail-open）。
    # 抑制是"漏报"方向，遇到无法识别的级别宁可多报一次，也不能静默吞掉。
    UNKNOWN_SEVERITY_VALUE = 5

    def __init__(self,
                 window_seconds: int = 60,
                 threshold: int = 10,
                 suppress_below: str = 'high'):
        """
        Args:
            window_seconds: 滑动窗口时长（秒）
            threshold: 窗口内告警数达到此值即触发洪水抑制
            suppress_below: 抑制此严重度以下的告警
                （critical ≥ high ≡ warning > medium > low > info）
        """
        self.window = window_seconds
        self.threshold = threshold
        self.suppress_below = suppress_below
        self._timestamps: list = []
        self._lock = threading.Lock()
        self._flood_active = False
        self._suppressed_count = 0
        self._flood_start = None
        #: 窗口计时用的时钟。默认单调时钟；测试替换这个缝来模拟"时间流逝"
        #: （**不要** monkeypatch 全局 time.monotonic —— 会波及后台线程的
        #: `Event.wait(timeout)`，teardown 直接卡死）。
        self._clock = time.monotonic

    def record_alarm(self, severity: str) -> tuple[bool, str]:
        """记录一条告警，返回 (should_emit, reason)"""
        with self._lock:
            # round 258：滑动窗口用**单调时钟**（原来是 time.time()）。
            # 墙钟向后跳一次，窗口里的旧时间戳就永远算不出"过期"
            # （`now - t` 为负）→ 洪水抑制**永久卡在激活态**，
            # 所有低于 suppress_below 的报警被静默吞掉，直到窗口被新告警挤满。
            now = self._clock()
            self._timestamps.append(now)

            # 清理窗口外的时间戳
            cutoff = now - self.window
            self._timestamps = [t for t in self._timestamps if t > cutoff]

            count = len(self._timestamps)

            # 检查是否进入洪水状态
            if count >= self.threshold and not self._flood_active:
                self._flood_active = True
                self._flood_start = now
                logger.warning(f"告警洪水检测: {count}/{self.threshold}条/{self.window}秒, 激活抑制")

            # 洪水状态下的抑制逻辑
            if self._flood_active:
                sev_val = self._severity_value(severity)
                suppress_val = self.SEVERITY_ORDER.get(
                    str(self.suppress_below).strip().lower(),
                    self.SEVERITY_ORDER['high'])

                if sev_val >= suppress_val:
                    return True, "flood_active_but_critical"
                else:
                    self._suppressed_count += 1
                    return False, "suppressed_by_flood_detector"

            return True, "normal"

    @classmethod
    def _severity_value(cls, severity: Any) -> int:
        """把任意严重度写法归一到分级值。

        无法识别的严重度：留 WARNING 日志痕迹并按最高档处理（不抑制），
        绝不沿用旧实现的"默认 0 = 最低档 → 静默抑制"。
        """
        key = str(severity).strip().lower() if severity is not None else ''
        value = cls.SEVERITY_ORDER.get(key)
        if value is None:
            logger.warning("未知报警严重度 %r，洪水抑制按最高档处理（不抑制，避免漏报）",
                           severity)
            return cls.UNKNOWN_SEVERITY_VALUE
        return value

    def check_flood_end(self):
        """检查洪水是否结束（由后台定时器调用）"""
        with self._lock:
            if not self._flood_active:
                return

            # round 258：同 record_alarm，窗口计时用单调时钟
            now = self._clock()
            cutoff = now - self.window
            recent = [t for t in self._timestamps if t > cutoff]

            if len(recent) < self.threshold // 2:
                self._flood_active = False
                duration = now - self._flood_start if self._flood_start else 0
                logger.info(f"告警洪水结束: 持续{duration:.0f}秒, 抑制了{self._suppressed_count}条告警")
                self._suppressed_count = 0
                self._flood_start = None

    def get_status(self) -> dict:
        """获取洪水检测器状态"""
        with self._lock:
            return {
                'flood_active': self._flood_active,
                'recent_count': len(self._timestamps),
                'threshold': self.threshold,
                'suppressed_count': self._suppressed_count,
                'window': self.window,
            }


class AlarmPriorityMatrix:
    """
    ISA-18.2 报警优先级矩阵
    严重度(Severity) × 可能性(Likelihood) → 优先级(Priority)
    """

    # 严重度: 1=可忽略, 2=次要, 3=中等, 4=严重, 5=灾难
    # 可能性: 1=罕见, 2=不太可能, 3=可能, 4=很可能, 5=频繁
    MATRIX = {
        # (severity, likelihood) -> priority
        (5, 5): 'P1', (5, 4): 'P1', (5, 3): 'P1',
        (5, 2): 'P2', (5, 1): 'P2',
        (4, 5): 'P1', (4, 4): 'P2', (4, 3): 'P2',
        (4, 2): 'P3', (4, 1): 'P3',
        (3, 5): 'P2', (3, 4): 'P2', (3, 3): 'P3',
        (3, 2): 'P3', (3, 1): 'P4',
        (2, 5): 'P3', (2, 4): 'P3', (2, 3): 'P4',
        (2, 2): 'P4', (2, 1): 'P5',
        (1, 5): 'P4', (1, 4): 'P4', (1, 3): 'P5',
        (1, 2): 'P5', (1, 1): 'P5',
    }

    PRIORITY_ORDER = {'P1': 1, 'P2': 2, 'P3': 3, 'P4': 4, 'P5': 5}

    @classmethod
    def get_priority(cls, severity: int, likelihood: int) -> str:
        severity = max(1, min(5, severity))
        likelihood = max(1, min(5, likelihood))
        return cls.MATRIX.get((severity, likelihood), 'P3')

    @classmethod
    def is_higher(cls, p1: str, p2: str) -> bool:
        """判断 p1 是否比 p2 优先级更高"""
        return cls.PRIORITY_ORDER.get(p1, 99) < cls.PRIORITY_ORDER.get(p2, 99)


#: 输出通道**连续**失败多少次，就把"这个通道坏了"本身当成一条报警。
#: 为什么需要：邮件/短信投递失败只记 `output_errors` 计数与 ERROR 日志 ——
#: 运维不主动翻统计页就永远不知道"报警发不出去"。而通知通道坏了意味着
#: **报警到不了人**，这本身必须是报警（ISA-18.2 也要求监控报警系统自身）。
NOTIFY_FAILURE_ALARM_THRESHOLD = 3


class AlarmManager:
    """
    报警管理器（重写版）
    负责报警检测、记录、声光输出、语音广播、前端推送

    报警输出总线（三级联动）：
    1. 声光报警器（Modbus DO -> 报警灯塔 + 蜂鸣器）
    2. 语音广播系统（MQTT -> IP网络广播/现场音柱）
    3. 前端WebSocket推送（页面弹窗/音效/语音合成）

    去重机制：
    - 同一报警（alarm_id + device_id + register_name）在冷却窗口内只推送一次
    - 用户确认后，同一报警在抑制时间内不再弹窗
    - 前端可通过 dismissed 列表告知后端跳过推送
    """

    # 去重表容量上限。报警 key = (rule_id, device_id, register_name)，长期运行
    # 数量可无限增长——两张表若无上限就是内存泄漏。超过上限时先按 TTL 清掉
    # 已失效项（早于窗口的记录不可能再抑制任何推送），仍超限则淘汰最旧记录。
    _DEDUP_MAX_ENTRIES = 10000

    def __init__(self, database, config_path: str = '配置/alarms.yaml',
                 alarm_output=None, broadcast_system=None):
        """
        初始化报警管理器

        Args:
            database: 数据库实例
            config_path: 报警配置文件路径
            alarm_output: 声光报警输出实例（AlarmOutput，可选）
            broadcast_system: 广播系统实例（BroadcastSystem，可选）
        """
        self.database = database
        self.config_path = str(paths.resolve(config_path))

        # 报警规则
        self.rules: dict[str, dict] = {}  # rule_id -> rule_config

        # 报警状态
        # 键是 **(rule_id, device_id, register_name)**，不是 (device, register)。
        # 同一个寄存器经常同时配「警告 80 / 严重 100」两条规则；用两元组做键时
        # 两条规则抢同一个槽位：后触发的把先触发的状态覆盖掉，于是
        #   · 严重规则会借「已有报警」的迟滞分支，在**低于自己阈值**时触发；
        #   · 两条规则同时成立时每个数据点互相覆盖 → 声光反复触发；
        #   · 清除任意一条会把另一条一起从活动列表里抹掉。
        self.alarm_states: dict[tuple, dict] = {}  # (rule_id, device_id, register_name)

        # 输出总线（延迟注入，启动后绑定）
        self.alarm_output = alarm_output
        self.broadcast_system = broadcast_system

        # 通知模块（邮件/短信）。由 `load_config()` 按 yaml 的 `notification`
        # 段构造，也可以用 `set_notification()` 外部注入（测试/自定义实现）。
        #
        # 此前 `配置/alarms.yaml` 里那一整段 `notification:`（email/sms/sound）
        # **没有任何代码读**，`报警层/notification.py`（183 行，含 SMTP +
        # STARTTLS + HTML 转义）也**只在测试里被实例化过**，
        # `AlarmConfig.EMAIL_ENABLED` 与 4 个 `SMTP_*` 同样是零消费 ——
        # 配置齐了、代码写了、就是没接上，运维把 `enabled` 改 true 也不会有邮件。
        self.notification = None
        self._notify_executor: ThreadPoolExecutor | None = None
        self._notify_lock = Lock()

        # WebSocket回调（由外部注入，用于前端推送）
        self._websocket_emit: Callable[..., Any] | None = None

        # 去重配置
        self.dedup_config = AlarmDedupConfig()

        # ISA-18.2: 旁路/搁置的报警
        self._shelved_alarms: dict[tuple, AlarmShelveState] = {}

        # ISA-18.2: 死区配置 (rule_id -> deadband_value)
        self._deadbands: dict[str, float] = {}

        # ISA-18.2: 报警统计分析器（延迟初始化）
        self._alarm_statistics = None

        # ISA-18.2: 告警洪水检测器
        self._flood_detector = AlarmFloodDetector()

        # 去重状态：记录每个报警的最后推送时间
        # key: (alarm_id, device_id, register_name) -> last_emit_timestamp
        self._emit_history: dict[tuple, float] = {}

        # 确认记录：记录每个报警的确认时间
        # key: (alarm_id, device_id, register_name) -> acknowledge_timestamp
        self._acknowledge_history: dict[tuple, float] = {}

        #: 去重窗口的时钟缝（默认单调时钟）。测试替换它来模拟"时间流逝"，
        #: 详见 `_dedup_now()` 的 docstring。
        self._clock = time.monotonic

        # 线程锁（保护去重状态的并发访问）
        self._dedup_lock = Lock()

        # 输出通道失败计数（声光/广播/前端）。
        # 声光与广播是安全关键输出，"调用不通"绝不能静默 —— 每次失败都记账，
        # 并通过 get_alarm_statistics()['output']['errors'] / 日志暴露。
        # email / sms 两个通知通道（round 243/250）也走同一套计数。
        self._output_errors: dict[str, int] = {
            'alarm_output': 0, 'broadcast': 0, 'websocket': 0,
            'email': 0, 'sms': 0}
        self._output_error_lock = Lock()
        #: 各通道**连续**失败次数（成功即清零）。达到
        #: `NOTIFY_FAILURE_ALARM_THRESHOLD` 时把通道故障本身报成报警。
        self._output_consecutive: dict[str, int] = {}
        #: 已因"通道故障"报过警的通道（避免每次失败都重复登记）
        self._output_broken: set[str] = set()

        # 状态锁（保护 alarm_states, rules, _shelved_alarms, _deadbands）
        self._state_lock = Lock()

        # 报警升级配置
        self._escalation_timeout = 600  # 默认10分钟
        self._escalation_callbacks: list = []  # 升级回调函数列表

        # 告警升级管理器（多级升级，延迟到 load_config 后初始化）
        self._escalation_manager = None

        # 规则索引：(device_id, register_name) -> [(rule_id, rule_config), ...]
        self._rules_index: dict[tuple, list[tuple[str, dict]]] = {}

        # 上次清理过期旁路的时间戳
        self._last_shelf_cleanup: float = float('-inf')

        # 报警升级定时器（后台自动检查）
        self._escalation_timer: threading.Timer | None = None
        self._escalation_interval = 30  # 每30秒检查一次升级
        # 定时器停止标志：置位后 _tick 不再自我重排（防止永久泄漏）
        self._escalation_timer_stop = threading.Event()

        # 告警洪水检查定时器
        self._flood_timer: threading.Timer | None = None
        self._flood_check_interval = 15  # 每15秒检查一次洪水是否结束
        self._flood_timer_stop = threading.Event()

        # 配置文件热重载线程（可停止）
        self._config_watcher_thread: threading.Thread | None = None
        self._config_watch_interval = 10  # 每10秒检查一次文件修改时间
        self._config_watcher_stop = threading.Event()

        # 加载报警配置
        self.load_config()

        # 配置文件热重载：记录文件修改时间，定期检查变化
        self._config_mtime: float = 0
        try:
            self._config_mtime = Path(self.config_path).stat().st_mtime
        except Exception as e:
            # 首次启动时配置文件可能尚不存在，属预期内且无副作用
            # （mtime 保持 0，配置文件生成后会被热重载线程捕获）
            logger.debug("读取报警配置文件修改时间失败（文件可能尚不存在）%s: %s",
                         self.config_path, e)
        self._config_watcher_running = True
        self._start_config_watcher()

        # 启动报警升级自动检查定时器
        self._start_escalation_timer()

        # 启动告警洪水检查定时器
        self._start_flood_timer()

    def _start_config_watcher(self):
        """启动配置文件热重载后台线程（默认每10秒检查文件修改时间）"""
        if self._config_watcher_thread is not None and self._config_watcher_thread.is_alive():
            logger.warning("报警配置热重载监控已在运行，忽略重复启动")
            return

        self._config_watcher_stop.clear()
        self._config_watcher_running = True

        def _watch_loop():
            # 可中断等待：stop_config_watcher() 后立即退出，无需等满一个轮询周期
            while self._config_watcher_running and not self._config_watcher_stop.is_set():
                if self._config_watcher_stop.wait(self._config_watch_interval):
                    break
                try:
                    config_file = Path(self.config_path)
                    if config_file.exists():
                        current_mtime = config_file.stat().st_mtime
                        if current_mtime > self._config_mtime:
                            logger.info(f"检测到报警配置文件变更，自动重载...")
                            self.load_config()
                            self._config_mtime = current_mtime
                            logger.info("报警配置热重载完成")
                except Exception as e:
                    logger.debug(f"配置文件监控异常: {e}")

        self._config_watcher_thread = threading.Thread(
            target=_watch_loop, daemon=True, name="alarm-config-watcher")
        self._config_watcher_thread.start()
        logger.info(f"报警配置文件热重载监控已启动（每{self._config_watch_interval}秒检查）")

    def stop_config_watcher(self, timeout: float = 5) -> None:
        """停止配置文件热重载监控线程（幂等）"""
        self._config_watcher_running = False
        self._config_watcher_stop.set()

        thread = self._config_watcher_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._config_watcher_thread = None
        logger.info("报警配置文件热重载监控已停止")

    def _start_escalation_timer(self):
        """启动报警升级后台检查定时器"""
        self._escalation_timer_stop.clear()
        if self._escalation_timer is not None:
            self._escalation_timer.cancel()

        def _tick():
            try:
                self.check_escalation()
            except Exception as e:
                logger.error(f"报警升级定时检查异常: {e}")
            finally:
                # 停止后不再重排，否则定时器会自我复活、永久泄漏
                if not self._escalation_timer_stop.is_set():
                    # 在**局部变量**上构造-设daemon-启动，最后才发布到 self。
                    # 原先三步都走 `self._escalation_timer`：本回调运行在定时器线程里，
                    # 而 stop_escalation_timer() / _start_escalation_timer() 会在别的线程
                    # 改写同一个属性 —— 于是中间某步可能作用到另一个对象（甚至 None）上，
                    # 抛 `RuntimeError: Thread.__init__() not called` / AttributeError，
                    # 异常逃出 finally 直接杀掉该线程，报警升级定时器从此静默失效。
                    #
                    # 启动后必须**再查一次**停止标志：stop 可能刚好在上面的窗口里跑完，
                    # 那时得把新定时器撤掉，否则它会继续回调、且属性被写回非 None
                    # ——即测试里的"stop 之后自我复活"。
                    try:
                        timer = threading.Timer(self._escalation_interval, _tick)
                        timer.daemon = True
                        timer.start()
                        if self._escalation_timer_stop.is_set():
                            timer.cancel()
                        else:
                            self._escalation_timer = timer
                    except Exception as e:
                        logger.error(f"报警升级定时器重排失败，升级检查已停止: {e}")

        self._escalation_timer = threading.Timer(
            self._escalation_interval, _tick
        )
        self._escalation_timer.daemon = True
        self._escalation_timer.start()
        logger.info(f"报警升级定时器已启动（间隔{self._escalation_interval}秒）")

    def stop_escalation_timer(self):
        """停止报警升级定时器（幂等；停止后不会自我复活）"""
        self._escalation_timer_stop.set()
        if self._escalation_timer is not None:
            self._escalation_timer.cancel()
            self._escalation_timer = None
            logger.info("报警升级定时器已停止")

    def _start_flood_timer(self):
        """启动告警洪水检查后台定时器"""
        self._flood_timer_stop.clear()
        if self._flood_timer is not None:
            self._flood_timer.cancel()

        def _flood_tick():
            try:
                self._flood_detector.check_flood_end()
            except Exception as e:
                logger.error(f"告警洪水检查异常: {e}")
            finally:
                # 停止后不再重排，否则定时器会自我复活、永久泄漏。
                # 用局部变量构造-设daemon-启动，理由同 _tick（避免跨线程改写 self 属性）。
                if not self._flood_timer_stop.is_set():
                    try:
                        timer = threading.Timer(self._flood_check_interval, _flood_tick)
                        timer.daemon = True
                        timer.start()
                        # 启动后二次确认停止标志（理由同 _tick）
                        if self._flood_timer_stop.is_set():
                            timer.cancel()
                        else:
                            self._flood_timer = timer
                    except Exception as e:
                        logger.error(f"告警洪水定时器重排失败，洪水检查已停止: {e}")

        self._flood_timer = threading.Timer(
            self._flood_check_interval, _flood_tick
        )
        self._flood_timer.daemon = True
        self._flood_timer.start()
        logger.info(f"告警洪水检查定时器已启动（间隔{self._flood_check_interval}秒）")

    def stop_flood_timer(self):
        """停止告警洪水检查定时器（幂等；停止后不会自我复活）"""
        self._flood_timer_stop.set()
        if self._flood_timer is not None:
            self._flood_timer.cancel()
            self._flood_timer = None
            logger.info("告警洪水检查定时器已停止")

    def stop(self) -> None:
        """
        停止报警管理器全部后台资源（幂等）

        停止配置热重载线程、报警升级定时器、告警洪水检查定时器，
        并停止告警升级管理器。进程退出 / 模块重载时应调用，
        避免线程与定时器泄漏。
        """
        self.stop_config_watcher()
        self.stop_escalation_timer()
        self.stop_flood_timer()

        if self._escalation_manager is not None:
            try:
                self._escalation_manager.stop()
            except Exception as e:
                logger.debug(f"告警升级管理器停止跳过: {e}")

        # 邮件线程池：等已提交的邮件发完再关，避免停机时丢掉最后几条报警邮件
        with self._notify_lock:
            executor, self._notify_executor = self._notify_executor, None
        if executor is not None:
            try:
                executor.shutdown(wait=True)
            except Exception as e:
                logger.debug(f"邮件线程池关闭跳过: {e}")

        logger.info("报警管理器后台资源已停止")

    def shutdown(self) -> None:
        """停止报警管理器（stop() 的语义别名）"""
        self.stop()

    def set_websocket_emit(self, emit_func: Callable[..., Any]):
        """注入WebSocket emit函数（由run.py启动时调用）"""
        self._websocket_emit = emit_func

    def set_notification(self, notifier) -> None:
        """注入通知实现（默认由 `load_config()` 按 yaml 构造，这里供测试覆盖）"""
        self.notification = notifier

    def _setup_notification(self, notif_cfg: dict[str, Any]) -> None:
        """按 yaml 的 `notification` 段配置邮件/短信通知。

        配置不完整时**必须打 ERROR**：否则运维把 `notification.email.enabled`
        改成 true、填了半套 SMTP，界面一切正常，报警邮件一封都发不出去，
        日志里也什么都看不到。
        """
        try:
            from 报警层.notification import Notification
            notifier = Notification(notif_cfg)
        except Exception as e:
            logger.error("初始化通知模块失败（报警邮件/短信将不可用）: %s", e)
            return

        email_cfg = notif_cfg.get('email') or {}
        if getattr(notifier, 'email_enabled', False):
            missing = [k for k in ('smtp_server', 'username', 'password')
                       if not email_cfg.get(k)]
            if missing:
                logger.error(
                    "邮件通知已启用但缺少配置项 %s —— 报警邮件不会发出"
                    "（检查 配置/alarms.yaml 的 notification.email）", missing)
            elif not email_cfg.get('recipients'):
                logger.error(
                    "邮件通知已启用但没有收件人 —— 报警邮件不会发出"
                    "（notification.email.recipients）")
            else:
                logger.info(
                    "邮件报警通知已启用: %s:%s → %d 个收件人",
                    email_cfg.get('smtp_server'),
                    email_cfg.get('smtp_port', 587),
                    len(email_cfg['recipients']),
                )

        # 短信通道（round 250）：`Notification.send_sms` 是**占位实现**，
        # 一条都不会发出。这里把现状说清楚，并且**照常接入报警链路** ——
        # 启用它之后每条报警都会走一次「尝试 → 失败 → 记账 + ERROR」，
        # 而不是像此前那样：接口看着正常、日志一片安静、短信一封没有。
        # 静默的缺口比响亮的缺口更难发现。
        sms_cfg = notif_cfg.get('sms') or {}
        if getattr(notifier, 'sms_enabled', False):
            logger.error(
                "短信通知已启用，但 send_sms **尚未实现**（占位，恒返回 False）—— "
                "报警短信不会发出。每条报警会记一条 output_errors['sms']。"
                "实现清单见 报警层/notification.py:send_sms 的 docstring；"
                "在实现完成前请把 notification.sms.enabled 置为 false。")
        elif sms_cfg and sms_cfg.get('phone_numbers'):
            logger.info("短信通知未启用（notification.sms.enabled 不为真）")

        self.notification = notifier

    def _ensure_notify_executor(self) -> ThreadPoolExecutor | None:
        """邮件投递用单线程池：顺序发送、不并发轰炸 SMTP。"""
        with self._notify_lock:
            if self._notify_executor is None:
                self._notify_executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix='alarm-email')
            return self._notify_executor

    @staticmethod
    def _send_email_blocking(notifier, payload: dict[str, Any]) -> tuple[bool, str]:
        """在后台线程里真正发邮件；异常转成 (False, 原因)。"""
        try:
            ok = notifier.send_alarm_notification(payload)
            return bool(ok), '' if ok else 'send_alarm_notification 返回 False'
        except Exception as e:
            return False, f'邮件发送异常: {e}'

    def _on_email_done(self, future) -> None:
        try:
            ok, err = future.result()
        except Exception as e:
            ok, err = False, f'邮件线程异常: {e}'
        if ok:
            self._record_output_success('email')
        else:
            self._record_output_failure('email', err or '邮件发送失败')

    def _notify_by_email(self, alarm_data: dict[str, Any]) -> None:
        """邮件报警通知 —— 异步投递，失败留痕（`output_errors['email']`）。

        为什么必须异步：`Notification.send_email` 是**同步 SMTP**，
        连接 / STARTTLS / 登录 / 发送串起来，一个不通的 SMTP 服务器能让
        调用线程卡住十几秒。报警主链路（声光 / 广播 / 前端推送）不能被
        一个可选通道拖死 —— 声光不响比邮件晚到严重得多。

        字段映射：`Notification.send_alarm_notification` 读的是
        `level` / `message` / `value`，而这里上游的报警 payload 用的是
        `alarm_level` / `alarm_message` / `actual_value`。两套命名不一致，
        直接传过去会发出一封「[WARNING] 报警通知」的空壳邮件。
        """
        notifier = getattr(self, 'notification', None)
        if notifier is None or not getattr(notifier, 'email_enabled', False):
            return

        payload = {
            'level': alarm_data.get('alarm_level', 'warning'),
            'message': alarm_data.get('alarm_message', '报警通知'),
            'device_id': alarm_data.get('device_id'),
            'register_name': alarm_data.get('register_name'),
            'value': alarm_data.get('actual_value'),
            'threshold': alarm_data.get('threshold'),
            'timestamp': alarm_data.get('timestamp'),
        }

        executor = self._ensure_notify_executor()
        if executor is None:
            return
        try:
            executor.submit(self._send_email_blocking, notifier, payload) \
                    .add_done_callback(self._on_email_done)
        except Exception as e:
            self._record_output_failure('email', f"邮件任务提交失败: {e}")

    # ------------------------------------------------------------
    # 短信通知（round 250）
    # ------------------------------------------------------------

    @staticmethod
    def _send_sms_blocking(notifier, payload: dict[str, Any]) -> tuple[bool, str]:
        """在后台线程里尝试发短信；异常转成 (False, 原因)。

        与 `_send_email_blocking` 同构：同步 IO 不进报警主链路。
        """
        try:
            numbers = list(payload.get('phone_numbers') or [])
            if not numbers:
                return False, 'notification.sms.phone_numbers 为空'
            ok = notifier.send_sms(numbers, payload.get('message', ''))
            return bool(ok), '' if ok else 'send_sms 返回 False（占位实现，未真正发送）'
        except Exception as e:
            return False, f'短信发送异常: {e}'

    def _on_sms_done(self, future) -> None:
        try:
            ok, err = future.result()
        except Exception as e:
            ok, err = False, f'短信线程异常: {e}'
        if ok:
            self._record_output_success('sms')
        else:
            self._record_output_failure('sms', err or '短信发送失败')

    def _notify_by_sms(self, alarm_data: dict[str, Any]) -> None:
        """短信报警通知 —— 异步投递，失败留痕（`output_errors['sms']`）。

        round 250。`Notification.send_sms` 目前是**占位实现**（恒返回 False，
        见其 docstring 里的实现清单）。仍然接进链路是刻意的：

        - 启用 `notification.sms` 后，每条报警都会尝试一次并记一条
          `output_errors['sms']` + ERROR 日志 —— 缺口是**响亮的**；
        - 此前的状态是「接口正常、日志安静、短信一封没有」—— 缺口是**静默的**。
        静默的缺口更难发现，也更晚被发现。

        去重位置与邮件一致（去重点之后），报警风暴不会把短信网关打爆。
        """
        notifier = getattr(self, 'notification', None)
        if notifier is None or not getattr(notifier, 'sms_enabled', False):
            return

        sms_cfg = getattr(notifier, 'sms_config', None) or {}
        payload = {
            'phone_numbers': sms_cfg.get('phone_numbers') or [],
            'message': (
                f"[{alarm_data.get('alarm_level', 'warning')}] "
                f"{alarm_data.get('alarm_message', '报警通知')} "
                f"{alarm_data.get('device_id', '')}/{alarm_data.get('register_name', '')} "
                f"当前值 {alarm_data.get('actual_value')}"
            ),
        }

        executor = self._ensure_notify_executor()
        if executor is None:
            return
        try:
            executor.submit(self._send_sms_blocking, notifier, payload) \
                    .add_done_callback(self._on_sms_done)
        except Exception as e:
            self._record_output_failure('sms', f"短信任务提交失败: {e}")

    def _rebuild_rules_index(self):
        """构建规则索引：(device_id, register_name) -> [(rule_id, rule_config)]"""
        self._rules_index.clear()
        for rule_id, rule_config in self.rules.items():
            if not rule_config.get('enabled', True):
                continue
            key = (rule_config.get('device_id'), rule_config.get('register_name'))
            if key not in self._rules_index:
                self._rules_index[key] = []
            self._rules_index[key].append((rule_id, rule_config))
        logger.debug(f"规则索引已构建: {len(self._rules_index)} 个设备/寄存器组合")

    def add_escalation_callback(self, callback: Callable[..., Any]):
        """
        注册报警升级回调

        当报警超过escalation_timeout仍未确认时，调用此回调。
        回调签名: callback(alarm_info: dict)
        """
        self._escalation_callbacks.append(callback)

    def check_escalation(self):
        """
        检查报警升级（单一权威入口）

        升级策略只有一套：装配了 AlarmEscalationManager（多级规则，见
        alarm_escalation.py）时，以它的规则为唯一权威，由本方法所在的
        报警升级定时器驱动；未装配时才退化为单级超时（_escalation_timeout）。

        旧实现是"两套各跑一套、互不知情"：本方法按 _escalation_timeout 升级，
        管理器又按自己的 300/900/1800 秒规则升级，同一条报警被两套逻辑
        各自判定，谁先命中取决于定时器时序。
        """
        if self._escalation_manager is not None:
            self._escalation_manager.check_escalations()
            return

        now = datetime.now()
        escalation_timeout = self._escalation_timeout

        with self._state_lock:
            states_snapshot = list(self.alarm_states.items())

        for state_key, state in states_snapshot:
            if state.get('pending', False):
                continue  # 延迟确认中，报警尚未真正产生，不参与升级计时

            if state.get('acknowledged', False):
                continue  # 已确认，跳过

            if state.get('escalated', False):
                continue  # 已升级，跳过

            # 检查是否超时
            first_trigger = state.get('first_trigger_time')
            if first_trigger is None:
                continue

            if isinstance(first_trigger, str):
                first_trigger = datetime.fromisoformat(first_trigger)

            elapsed = (now - first_trigger).total_seconds()
            if elapsed >= escalation_timeout:
                # 标记为已升级
                with self._state_lock:
                    state['escalated'] = True
                    state['escalation_time'] = now.isoformat()

                rule_id, device_id, register_name = state_key
                rule_config = self.rules.get(rule_id, {})

                alarm_info = {
                    'alarm_id': rule_id,
                    'device_id': device_id,
                    'register_name': register_name,
                    'alarm_level': rule_config.get('level', 'warning'),
                    'alarm_message': rule_config.get('name', '未知报警'),
                    'first_trigger_time': first_trigger.isoformat() if hasattr(first_trigger, 'isoformat') else str(first_trigger),
                    'elapsed_seconds': elapsed,
                    'escalation_reason': f'报警超过{escalation_timeout}秒未确认',
                }

                logger.warning(f"报警升级: {rule_id} ({device_id}/{register_name}) "
                              f"已超时{elapsed:.0f}秒未确认")

                self._notify_escalation_callbacks(alarm_info)

    def _notify_escalation_callbacks(self, alarm_info: Dict[str, Any]):
        """按既有契约通知 add_escalation_callback() 注册的外部回调"""
        for callback in self._escalation_callbacks:
            try:
                callback(alarm_info)
            except Exception as e:
                logger.error(f"报警升级回调异常: {e}")

    def _on_manager_escalation(self, escalation_info: Dict[str, Any]):
        """把升级管理器的多级升级结果桥接给外部回调（统一升级出口）

        管理器通知的是"多级规则命中了第 N 级"；这里补齐规则信息、把报警状态
        标记为已升级（前端/API 读 alarm_states['escalated']），再按既有契约
        通知外部回调，保证 add_escalation_callback() 的调用方不受升级机制
        切换影响。
        """
        device_id = escalation_info.get('device_id')
        register_name = escalation_info.get('register_name')
        rule_id = escalation_info.get('alarm_id')
        rule_config = self.rules.get(rule_id, {})
        elapsed = escalation_info.get('elapsed_seconds', 0)
        level = escalation_info.get('level')
        now = datetime.now()

        with self._state_lock:
            state = self.alarm_states.get((rule_id, device_id, register_name))
            if state is not None:
                state['escalated'] = True
                state['escalation_time'] = now.isoformat()

        first_trigger = state.get('first_trigger_time') if state else None
        if hasattr(first_trigger, 'isoformat'):
            first_trigger = first_trigger.isoformat()
        elif first_trigger is not None:
            first_trigger = str(first_trigger)

        alarm_info = {
            'alarm_id': rule_id,
            'device_id': device_id,
            'register_name': register_name,
            'alarm_level': rule_config.get('level', 'warning'),
            'alarm_message': rule_config.get('name', '未知报警'),
            'first_trigger_time': first_trigger,
            'elapsed_seconds': elapsed,
            'escalation_level': level,
            'escalation_reason': f'报警超过{int(elapsed)}秒未确认（升级到级别{level}）',
        }

        logger.warning("报警升级: %s (%s/%s) 已超时%.0f秒未确认，升级到级别%s",
                       rule_id, device_id, register_name, elapsed, level)

        self._notify_escalation_callbacks(alarm_info)

    def _on_escalation(self, escalation_info: Dict[str, Any]):
        """升级回调：触发广播通知"""
        try:
            if self.broadcast_system and self.broadcast_system.enabled:
                level = escalation_info.get('level', 1)
                rule = escalation_info.get('rule')
                message = rule.message_template.format(
                    alarm_message=escalation_info.get('alarm_id', '未知')
                ) if rule else f"告警升级: {escalation_info.get('alarm_id')}"

                self._invoke_broadcast(
                    'critical' if level >= 3 else 'warning',
                    message,
                    escalation_info.get('device_id'),
                    'all')
        except Exception as e:
            self._record_output_failure('broadcast', f"升级广播异常: {e}")

    def set_alarm_statistics(self, alarm_statistics):
        """注入报警统计分析器"""
        self._alarm_statistics = alarm_statistics

    def load_config(self):
        """加载报警配置文件"""
        try:
            config_file = Path(self.config_path)
            if not config_file.exists():
                logger.error(f"报警配置文件不存在: {self.config_path}")
                return

            with open(config_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)

            # 解析报警规则
            rules_config = config.get('alarm_rules', [])
            for rule_config in rules_config:
                rule_id = rule_config.get('id')
                if rule_id:
                    self.rules[rule_id] = rule_config
                    # 从规则配置中读取死区
                    deadband = rule_config.get('deadband', 0)
                    if deadband:
                        self._deadbands[rule_id] = float(deadband)
                    logger.info(f"加载报警规则: {rule_id} - {rule_config.get('name')}")

            logger.info(f"共加载 {len(self.rules)} 条报警规则")

            # 构建规则索引
            self._rebuild_rules_index()

            # 加载去重配置
            dedup_cfg = config.get('dedup', {})
            if dedup_cfg:
                self.dedup_config = AlarmDedupConfig(dedup_cfg)
                logger.info(f"加载去重配置: 冷却{self.dedup_config.emit_cooldown_seconds}s, "
                            f"确认抑制{self.dedup_config.acknowledge_suppress_seconds}s")

            # 加载通知配置（邮件/短信）—— 这一段此前从来没人读
            notif_cfg = config.get('notification', {})
            if notif_cfg:
                self._setup_notification(notif_cfg)

            # 加载升级配置
            escalation_cfg = config.get('escalation', {})
            if escalation_cfg:
                self._escalation_timeout = escalation_cfg.get('timeout_seconds', 600)
                logger.info(f"加载报警升级配置: 超时{self._escalation_timeout}s")

                # 单套升级：管理器规则是策略的唯一来源。配置只给了
                # timeout_seconds 时补一条同超时的单级规则，否则会出现
                # "配置写 600s、管理器仍按默认 300/900/1800 升级"的隐性分歧。
                manager_cfg = dict(escalation_cfg)
                if not manager_cfg.get('rules'):
                    manager_cfg['rules'] = [{
                        'level': 1,
                        'timeout_seconds': self._escalation_timeout,
                        'actions': ['notify', 'broadcast'],
                        'notify_roles': ['operator'],
                        'message_template': '报警超时未确认: {alarm_message}',
                    }]

                try:
                    from 报警层.alarm_escalation import AlarmEscalationManager
                    if self._escalation_manager is None:
                        self._escalation_manager = AlarmEscalationManager(manager_cfg)
                        self._escalation_manager.add_callback(self._on_escalation)
                        # 多级升级结果桥接给外部回调（见 _on_manager_escalation）
                        self._escalation_manager.add_callback(self._on_manager_escalation)
                        # 不启动管理器自带线程：升级由报警升级定时器统一驱动，
                        # 否则又是"两套调度各跑一套"
                        logger.info(
                            "告警升级管理器初始化成功（多级规则，统一由报警升级定时器驱动）")
                    else:
                        # round 249：原实现是 `if self._escalation_manager is None:`
                        # 整段跳过 —— 于是**首次装配之后再改 yaml 里的 rules 永不生效**：
                        # 手工编辑、热重载监控、以及 PUT /api/alarms/escalation/rules
                        # 全都撞在这道 Guard 上。yaml 是真源，已装配就走热更新。
                        self._escalation_manager.update_rules(manager_cfg['rules'])
                        logger.info("告警升级规则已热更新: %d 条",
                                    len(manager_cfg['rules']))

                    rules = self._escalation_manager.get_rules()
                    if rules:
                        self._escalation_timeout = min(
                            r['timeout_seconds'] for r in rules)
                    logger.info("告警升级首级超时 %d 秒", self._escalation_timeout)
                except Exception as e:
                    # 装配/热更新失败：**不清空已装配的管理器**（那会让正在
                    # 生效的多级策略凭空消失），只留 warning。首次装配失败时
                    # 它本来就是 None，上层 check_escalation 会走单级兜底。
                    logger.warning(f"告警升级管理器装配失败（保留既有策略）: {e}")

        except Exception as e:
            logger.error(f"加载报警配置异常: {e}")

    def check_alarm(self, device_id: str, register_name: str,
                    value: float, timestamp: datetime):
        """
        检查报警（使用索引快速查找匹配规则）

        Args:
            device_id: 设备ID
            register_name: 寄存器名称
            value: 数据值
            timestamp: 时间戳
        """
        if value is None:
            return

        # 非有限值（NaN / ±Infinity）= 「读数无效 / 不知道」，**不能当成「恢复正常」**。
        #
        # 原实现直接往下走：`_check_condition` 里 `nan > 100` 恒为 False，
        # 于是 `triggered=False` → `_process_alarm_state` 走进清除分支 →
        # `del self.alarm_states[...]` —— **已激活的报警被静默清除**，
        # 还会连带撤销升级跟踪。运维看到报警消失，以为设备自己好了。
        # （实测：value=120 触发 → 喂 NaN → 状态变 CLEARED。）
        #
        # 也不能反过来「按触发处理」：那会在传感器故障时刷出一堆假报警。
        # 正确做法是**本条不更新状态**：既不清除、也不触发，留一条 warning。
        # 设备真的离线会有 failed_collections / 熔断器另一条链路兜底。
        numeric = try_finite_float(value)
        if numeric is None:
            logger.warning(
                "报警检查收到非有限值，跳过本条（不触发也不清除已有报警）: "
                "%s/%s value=%r", device_id, register_name, value)
            return
        value = numeric

        # 用索引快速查找匹配的规则
        rules_key = (device_id, register_name)
        matched_rules = self._rules_index.get(rules_key)
        if not matched_rules:
            return

        # 清理过期的旁路（低频执行）
        # round 258：节流用单调时钟。初值必须是 -inf 而不是 0 ——
        # `time.monotonic()` 在 Windows 上是**开机以来的秒数**，
        # 开机不足 60 秒时 `now - 0` 还不满 60，首次清理会被跳过；
        # 更糟的是墙钟回拨会让它整整一个跳变量都不再清理。
        now_ts = time.monotonic()
        if now_ts - self._last_shelf_cleanup > 60:
            self._cleanup_expired_shelves()
            self._last_shelf_cleanup = now_ts

        # 遍历匹配的规则
        for rule_id, rule_config in matched_rules:
            # ISA-18.2: 检查是否被旁路/搁置
            alarm_key = (rule_id, device_id, register_name)
            if alarm_key in self._shelved_alarms:
                continue

            # 检查报警条件（含非对称死区/迟滞）
            with self._state_lock:
                # 按**本规则**取状态：迟滞（死区）判断必须只看自己这条规则
                current_alarm_state = self.alarm_states.get((rule_id, device_id, register_name))
            alarm_triggered = self._check_condition(
                value=value,
                condition=rule_config.get('condition'),
                threshold=rule_config.get('threshold'),
                rule_id=rule_id,
                alarm_state=current_alarm_state
            )

            # 处理报警状态
            self._process_alarm_state(
                rule_id=rule_id,
                rule_config=rule_config,
                device_id=device_id,
                register_name=register_name,
                value=value,
                timestamp=timestamp,
                triggered=alarm_triggered
            )

    def _check_condition(self, value: float, condition: str, threshold: float,
                         rule_id: str = None, alarm_state: dict = None) -> bool:
        """检查报警条件（含非对称死区/迟滞）。

        round 262：判定逻辑已抽到 `报警层/alarm_engine.py`（纯函数，运行时/
        离线回放/测试共用同一份）。这里只做"从 self 里取死区"的适配，
        避免出现第二份实现 —— 项目历史上吃过"平行实现"的亏。
        """
        deadband = self._deadbands.get(rule_id, 0) if rule_id else 0
        is_active = bool(alarm_state) and alarm_state.get('alarm_id') is not None
        return evaluate_condition(
            {'condition': condition, 'threshold': threshold},
            value, active=is_active, deadband=deadband)

    @staticmethod
    def _normalize_delay(raw: Any, rule_id: str = '') -> float:
        """把规则的 delay 配置归一为秒数（float，非负）。

        判定逻辑已抽到 `alarm_engine.normalize_delay`（round 262），这里保留
        方法名作为适配层，避免调用方与测试大改。
        """
        normalized = normalize_delay(raw, rule_id)
        if raw is not None and normalized == 0.0:
            parsed = try_finite_float(raw)
            if parsed is None or parsed != 0:
                logger.warning("报警规则 %s 的 delay 配置非法(%r)，按 0（不延迟）处理",
                               rule_id, raw)
        return normalized

    @staticmethod
    def _elapsed_seconds(start, end) -> float | None:
        """计算两个时间戳之间的秒数（容忍 str/datetime 混用），无法计算返回 None"""
        def _norm(ts):
            if ts is None:
                return None
            if isinstance(ts, datetime):
                return ts
            if isinstance(ts, str):
                try:
                    return datetime.fromisoformat(ts)
                except ValueError:
                    return None
            return None

        start_dt, end_dt = _norm(start), _norm(end)
        if start_dt is None or end_dt is None:
            return None
        try:
            return (end_dt - start_dt).total_seconds()
        except TypeError:
            return None

    def _process_alarm_state(self, rule_id: str, rule_config: dict[str, Any],
                             device_id: str, register_name: str,
                             value: float, timestamp: datetime, triggered: bool):
        """
        处理报警状态（含去重逻辑）

        延迟确认（rule_config['delay'] > 0）语义：
        条件首次成立时只登记 pending 状态（不触发声光/弹窗），
        后续数据点若条件持续成立且已满 delay 秒，才真正产生报警；
        中途条件恢复则状态被清除，延迟计时重新开始。

        Args:
            rule_id: 规则ID
            rule_config: 规则配置
            device_id: 设备ID
            register_name: 寄存器名称
            value: 数据值
            timestamp: 时间戳
            triggered: 是否触发
        """
        # 每条规则独立一个状态槽位（见 __init__ 里 alarm_states 的说明）
        state_key = (rule_id, device_id, register_name)
        fire = False
        cleared = False
        decision = None

        # round 262：判定（触发/挂起/更新/清除）全部交给
        # `alarm_engine.decide()` —— 纯函数，运行时与离线回放共用同一份逻辑。
        # 这里只负责"拿锁 → 应用状态 → 锁外做副作用"。
        with self._state_lock:
            current = self.alarm_states.get(state_key)
            if current is not None and current.get('alarm_id') != rule_id:
                # 槽位被别的规则占着（历史上会出现），按新报警处理
                current = None

            decision = decide(rule_config, current, value, timestamp)

            if decision.action == _ACT_CLEAR:
                del self.alarm_states[state_key]
                cleared = True
            elif decision.action in (_ACT_FIRE, _ACT_PENDING, _ACT_UPDATE):
                if current is None:
                    self.alarm_states[state_key] = new_state(
                        rule_id, device_id, register_name, decision, timestamp)
                else:
                    current.update(decision.updates)
                fire = decision.action == _ACT_FIRE
                if decision.action == _ACT_PENDING and decision.elapsed is None:
                    # 计时不可用（时间戳缺失/类型或时区不一致）：按到期处理，
                    # 宁可多报一次也不静默漏报 —— 但必须留痕
                    logger.warning(
                        "延迟报警无法计算持续时间，按到期处理以规避静默漏报: "
                        "%s - %s/%s", rule_id, device_id, register_name)
                elif decision.action == _ACT_FIRE and decision.reason == '延迟到期':
                    logger.info("延迟报警到期触发: %s - %s/%s (持续%.1fs)",
                                rule_id, device_id, register_name,
                                decision.elapsed or 0.0)

        # 锁外副作用（避免回调死锁）
        if fire:
            # 去重记账只在 _trigger_alarm 内、且**确实推送成功后**发生。
            # 这里再记一次会把"被洪水/冷却抑制、根本没推送"的报警也记成已推送，
            # 于是冷却窗口内真正该推送的报警被压掉 —— 静默漏报。
            self._trigger_alarm(rule_config, device_id, register_name, value, timestamp)

        if cleared:
            logger.info(f"报警清除: {rule_id} - {device_id}/{register_name}")
            # 同步撤销升级跟踪：否则已恢复正常的报警仍会被升级管理器
            # 按超时升级（且其 _states 会无界增长）
            if self._escalation_manager:
                self._escalation_manager.remove_alarm(rule_id, device_id, register_name)
            # round 260：把"已恢复"落到数据库。
            self._clear_alarm_record(rule_id, device_id, register_name, timestamp)

    def _clear_alarm_record(self, rule_id: str, device_id: str,
                            register_name: str, cleared_at) -> None:
        """把「报警已恢复正常」写进数据库（失败只记日志，绝不影响报警链路）。"""
        try:
            ok = self.database.clear_alarm(rule_id, device_id, register_name,
                                           cleared_at=cleared_at)
            if ok:
                logger.info("报警恢复已落库: %s - %s/%s", rule_id, device_id, register_name)
            else:
                logger.debug(
                    "报警恢复没有对应记录可更新（可能进程重启后内存重建、"
                    "或重复清除）: %s - %s/%s", rule_id, device_id, register_name)
        except Exception as e:      # noqa: BLE001 —— 落库失败不能炸掉报警清除
            logger.error("报警恢复落库失败: %s - %s/%s: %s",
                         rule_id, device_id, register_name, e)

    def _dedup_now(self) -> float:
        """去重窗口用的时钟：**单调时钟**，不是墙钟。

        round 258：原来这里用 `time.time()`。去重窗口（冷却 300s / 确认抑制 600s）
        表达的是**时长**，而 `time.time()` 是**墙钟** —— 系统时间被改动一次，
        窗口就整体错位：
          * NTP 校时 / 现场机没电池开机后校时 / 手工改时间，向后跳 8 小时 →
            `now` 比 `ack_time` 还小，`suppress_until = ack_time + 600` 远在未来，
            **这些报警 key 会被静默抑制整整一个跳变量**；
            `_prune_dedup_history` 也算不出过期（`now - ts` 为负），连淘汰都做不到。
          * 向前跳 → 窗口瞬间失效，同一报警重复轰炸。

        后果最坏的一种是「操作员确认过的那条报警，校时之后再也没弹过」——
        在真实厂区里等于漏掉一条已经确认过、但仍未恢复的报警。

        单调时钟不受系统时间调整影响（Windows 上是开机以来秒数）。
        ⚠️ 因此这个值**只能用于算差值**，绝不能落库、也不能当时间戳展示。

        时钟走 `self._clock`（默认 `time.monotonic`）：测试要模拟"时间流逝"
        只能替换这个缝，**不能** monkeypatch 全局的 `time.monotonic` ——
        那是进程级的，会把后台线程里的 `Event.wait(timeout)` 一起改掉，
        导致 teardown join 卡死（round 258 实测踩过）。
        """
        return self._clock()

    def _should_emit(self, rule_id: str, device_id: str, register_name: str) -> bool:
        """
        检查是否应该推送报警通知（去重核心逻辑）

        Args:
            rule_id: 规则ID
            device_id: 设备ID
            register_name: 寄存器名称

        Returns:
            bool: 是否应该推送
        """
        if not self.dedup_config.enabled:
            return True

        alarm_key = (rule_id, device_id, register_name)
        now = self._dedup_now()

        with self._dedup_lock:
            # 检查确认后抑制
            ack_time = self._acknowledge_history.get(alarm_key)
            if ack_time is not None:
                suppress_until = ack_time + self.dedup_config.acknowledge_suppress_seconds
                if now < suppress_until:
                    logger.debug(f"报警被确认抑制: {rule_id} (剩余{suppress_until - now:.0f}s)")
                    return False

            # 检查冷却窗口
            last_emit = self._emit_history.get(alarm_key)
            if last_emit is not None:
                cooldown_until = last_emit + self.dedup_config.emit_cooldown_seconds
                if now < cooldown_until:
                    logger.debug(f"报警在冷却窗口内: {rule_id} (剩余{cooldown_until - now:.0f}s)")
                    return False

        return True

    def _record_emit(self, rule_id: str, device_id: str, register_name: str):
        """记录报警推送时间（仅在确实推送成功后调用）"""
        alarm_key = (rule_id, device_id, register_name)
        with self._dedup_lock:
            now = self._dedup_now()
            self._emit_history[alarm_key] = now
            self._prune_dedup_history(
                self._emit_history, self.dedup_config.emit_cooldown_seconds, now)

    def _record_acknowledge(self, rule_id: str, device_id: str, register_name: str):
        """记录报警确认时间"""
        alarm_key = (rule_id, device_id, register_name)
        with self._dedup_lock:
            now = self._dedup_now()
            self._acknowledge_history[alarm_key] = now
            self._prune_dedup_history(
                self._acknowledge_history,
                self.dedup_config.acknowledge_suppress_seconds, now)

    def _prune_dedup_history(self, history: dict, ttl: float, now: float) -> None:
        """把去重表压回有界（调用方须持有 _dedup_lock）。

        先清掉早于 TTL 的记录（已不可能再抑制任何推送，纯属占位），
        若仍超过容量上限，再按时间淘汰最旧记录。容量上限保证内存有界，
        不会随报警 key 的种类数无限增长。
        """
        if len(history) <= self._DEDUP_MAX_ENTRIES:
            return

        for key in [k for k, ts in history.items() if now - ts > ttl]:
            history.pop(key, None)

        overflow = len(history) - self._DEDUP_MAX_ENTRIES
        if overflow > 0:
            for key, _ in sorted(history.items(), key=lambda kv: kv[1])[:overflow]:
                history.pop(key, None)
            logger.warning("去重记录超出上限，已淘汰 %d 条最旧记录（表容量=%d）",
                           overflow, self._DEDUP_MAX_ENTRIES)

    def _trigger_alarm(self, rule_config: dict[str, Any], device_id: str,
                       register_name: str, value: float, timestamp: datetime,
                       notify: bool = True):
        """
        触发报警 → 三级联动输出

        1. 记录数据库（去重：冷却期内不重复插入）
        2. 声光报警器（Modbus DO控制灯塔+蜂鸣器）
        3. 语音广播（MQTT发布到现场音柱）
        4. 前端WebSocket推送（页面弹窗+音效+语音合成）

        Args:
            rule_config: 规则配置
            device_id: 设备ID
            register_name: 寄存器名称
            value: 数据值
            timestamp: 时间戳
        """
        rule_id = rule_config.get('id')

        # 记录到升级管理器 —— 必须在洪水检测**之前**。
        # round 249：原实现在洪水抑制的 early return 之后，于是被洪水检测器
        # 抑制（没弹toast/没广播）的报警**不进升级跟踪**，永远不会升级。
        # 而它此前的单级兜底读的是 alarm_states（报警已真实存在），
        # 照样会升级 —— 换到管理器后覆盖范围变小了。
        # 升级关心的是「这条报警多久没人确认」，与它的**输出**是否被抑制无关。
        if self._escalation_manager:
            self._escalation_manager.record_alarm(rule_id, device_id, register_name)

        # ISA-18.2: 告警洪水检测 — 抑制低优先级告警
        severity = rule_config.get('level', 'medium')
        should_emit, reason = self._flood_detector.record_alarm(severity)
        if not should_emit:
            logger.debug(f"告警被洪水检测器抑制: {rule_id} ({device_id}/{register_name})")
            return
        alarm_level = rule_config.get('level', 'warning')

        alarm_message = rule_config.get('name', '未知报警')
        threshold = rule_config.get('threshold', 0)
        alarm_area = rule_config.get('area', 'all')

        # 1. 记录数据库（同一报警未确认时只更新计数，不重复插入）
        self.database.insert_alarm(
            alarm_id=rule_id,
            device_id=device_id,
            register_name=register_name,
            alarm_level=alarm_level,
            alarm_message=alarm_message,
            threshold=threshold,
            actual_value=value,
            timestamp=timestamp
        )

        logger.warning(f"报警触发: {alarm_message} - {device_id}/{register_name} = {value}")

        # 记录到统计分析器
        if self._alarm_statistics:
            self._alarm_statistics.record_alarm_trigger(rule_id, device_id, register_name, timestamp)

        # 2. 声光报警器输出（Modbus DO -> 灯塔+蜂鸣器）
        if self.alarm_output and self.alarm_output.enabled:
            self._invoke_alarm_output(alarm_level, alarm_message, device_id)

        # 3. 语音广播系统（MQTT -> IP网络广播/现场音柱）
        if self.broadcast_system and self.broadcast_system.enabled:
            self._invoke_broadcast(
                alarm_level,
                f"{alarm_message}，当前值{value}，阈值{threshold}",
                device_id, alarm_area)

        # 4. 前端WebSocket推送（含去重标记 + 冷却/确认抑制检查）
        if not self._should_emit(rule_id, device_id, register_name):
            logger.debug(f"报警WebSocket推送被去重抑制: {rule_id} ({device_id}/{register_name})")
            return
        self._record_emit(rule_id, device_id, register_name)
        alarm_payload = {
            'alarm_id': rule_id,
            'device_id': device_id,
            'register_name': register_name,
            'alarm_level': alarm_level,
            'alarm_message': alarm_message,
            'threshold': threshold,
            'actual_value': value,
            'timestamp': timestamp.isoformat() if hasattr(timestamp, 'isoformat') else str(timestamp),
            'area': alarm_area,
            'dedup_key': f"{rule_id}:{device_id}:{register_name}",
        }
        self._emit_websocket_alarm(alarm_payload)

        # Prometheus：报警总数（round 253）。放在去重点**之后**、与推送同一位置，
        # 所以它统计的是「真正推出去的报警」—— 被冷却/确认抑制掉的重复报警不计入，
        # 否则一条抖动报警能把计数刷到天上，看板上的报警率就失去意义。
        try:
            from core.metrics import ALARMS_TOTAL
            ALARMS_TOTAL.labels(level=alarm_level, device_id=device_id).inc()
        except Exception as e:      # noqa: BLE001 —— 指标失败不影响报警
            logger.debug(f"报警指标打点失败: {e}")

        # 5/6. 邮件 + 短信（异步，不阻塞上面的声光/广播/前端推送）。
        #    放在去重点**之后**是刻意的：复用同一套冷却与确认抑制，
        #    否则报警风暴会把收件人邮箱打爆（同一条报警每 10 秒一封）。
        #
        #    round 267：`notify=False` 时**跳过**这两个通道 —— 用于
        #    「通知通道本身坏了」这类系统报警，否则会自指递归
        #    （发邮件失败 → 报警 → 再发邮件 → 再失败 …）。
        if notify:
            self._notify_by_email(alarm_payload)
            self._notify_by_sms(alarm_payload)

    def _record_output_failure(self, channel: str, detail: str) -> None:
        """输出通道失败必须可见：ERROR 日志 + 计数（绝不静默吞掉）。

        round 267：**连续**失败达到阈值时，把"这个通道坏了"本身报成一条报警。
        此前只计数 + 打日志 —— 运维不主动翻统计页就永远不知道"报警发不出去"，
        而通知通道坏了意味着**报警到不了人**，这本身就是必须报警的事。
        """
        with self._output_error_lock:
            self._output_errors[channel] = self._output_errors.get(channel, 0) + 1
            consecutive = self._output_consecutive.get(channel, 0) + 1
            self._output_consecutive[channel] = consecutive
            already = channel in self._output_broken
            if consecutive >= NOTIFY_FAILURE_ALARM_THRESHOLD and not already:
                self._output_broken.add(channel)
                should_alarm = True
            else:
                should_alarm = False

        logger.error("报警输出失败[%s]（累计%d次，连续%d次）: %s",
                     channel, self._output_errors.get(channel, 0), consecutive, detail)

        if should_alarm:
            self._raise_channel_broken_alarm(channel, detail, consecutive)

    def _record_output_success(self, channel: str) -> None:
        """输出成功：清零连续失败；若此前报过"通道故障"，现在把它清掉。"""
        with self._output_error_lock:
            had = self._output_consecutive.get(channel, 0)
            self._output_consecutive[channel] = 0
            was_broken = channel in self._output_broken
            self._output_broken.discard(channel)

        if was_broken:
            logger.warning("报警输出通道恢复[%s]（此前连续失败 %d 次）", channel, had)
            try:
                self.clear_external_alarm(f'notify_broken_{channel}',
                                          'alarm_notification', channel)
            except Exception as e:      # noqa: BLE001
                logger.debug("清除通道故障报警失败: %s", e)

    def _raise_channel_broken_alarm(self, channel: str, detail: str,
                                    consecutive: int) -> None:
        """把"输出通道连续失败"报成一条系统报警。

        ⚠️ 必须 `notify=False`：这条报警讲的就是"通知通道坏了"，
        再走邮件/短信会形成自指（失败 → 报警 → 再发邮件 → 再失败 …）。
        声光/广播/WebSocket 仍然会触发 —— 现场该知道。
        """
        label = {
            'email': '邮件通知', 'sms': '短信通知',
            'alarm_output': '声光报警器', 'broadcast': '语音广播',
            'websocket': '前端推送',
        }.get(channel, channel)
        try:
            self.raise_external_alarm(
                alarm_id=f'notify_broken_{channel}',
                message=f'{label}通道连续失败 {consecutive} 次，报警可能到不了人：{detail}',
                level='critical',
                device_id='alarm_notification',
                register_name=channel,
                notify=False,
            )
        except Exception as e:      # noqa: BLE001 —— 自检报警失败不能反过来炸掉主链路
            logger.error("通道故障报警登记失败[%s]: %s", channel, e)

    def _get_output_consecutive(self) -> dict[str, int]:
        """各通道当前连续失败次数（统计接口用）。"""
        with self._output_error_lock:
            return dict(self._output_consecutive)

    def _get_output_error_counts(self) -> dict[str, int]:
        """获取各输出通道累计失败次数"""
        with self._output_error_lock:
            return dict(self._output_errors)

    def _call_output_method(self, channel: str, target, method_name: str, **kwargs) -> bool:
        """调用输出实现上的方法，失败（缺方法/抛异常/显式返回失败）一律留痕。

        Returns:
            bool: 是否成功调用且实现未报告失败。
        """
        method = getattr(target, method_name, None)
        if not callable(method):
            self._record_output_failure(
                channel, f"{type(target).__name__} 缺少 {method_name}()，该通道输出丢失")
            return False
        try:
            result = method(**kwargs)
        except Exception as e:
            self._record_output_failure(channel, f"{method_name}() 调用异常: {e}")
            return False

        # 仅显式失败信号（False / {'success': False}）算失败；
        # 返回 None 多为实现内部的"同级不重复触发"等正常分支，不算失败。
        if result is False or (isinstance(result, dict) and result.get('success') is False):
            self._record_output_failure(channel, f"{method_name}() 报告失败: {result!r}")
            return False
        # round 267：成功即清零连续失败（否则"坏了又好了"的通道永远停在已报警状态）
        self._record_output_success(channel)
        return True

    def _invoke_alarm_output(self, level: str, message: str, device_id: str) -> bool:
        """触发声光报警输出（兼容两套实现命名）

        装配的是 `AlarmOutput`（trigger_alarm）/ 还是 `interfaces.IAlarmOutput`
        实现（activate_alarm）都要能工作。旧实现只调 trigger_alarm，遇到
        RealAlarmOutput/SimulatedAlarmOutput 直接 AttributeError 并被 except 吞掉
        —— 声光全线静默漏报。
        """
        target = self.alarm_output
        if target is None:
            return False
        if callable(getattr(target, 'trigger_alarm', None)):
            return self._call_output_method(
                'alarm_output', target, 'trigger_alarm',
                level=level, message=message, device_id=device_id)
        if callable(getattr(target, 'activate_alarm', None)):
            return self._call_output_method(
                'alarm_output', target, 'activate_alarm',
                level=level, message=message)
        self._record_output_failure(
            'alarm_output',
            f"{type(target).__name__} 既不提供 trigger_alarm 也不提供 activate_alarm，"
            f"声光输出无法触发")
        return False

    def _invoke_broadcast(self, level: str, text: str, device_id: str, area: str) -> bool:
        """触发语音广播（兼容两套实现命名）

        `BroadcastSystem` 有 speak_alarm（报警话术模板），而 interfaces.IBroadcastSystem
        实现（RealBroadcastSystem/SimulatedBroadcastSystem）只有 speak。
        旧实现硬调 speak_alarm → AttributeError 被吞 → 广播全线静默漏报。
        """
        target = self.broadcast_system
        if target is None:
            return False
        if callable(getattr(target, 'speak_alarm', None)):
            return self._call_output_method(
                'broadcast', target, 'speak_alarm',
                level=level, message=text, device_id=device_id, area=area)
        if callable(getattr(target, 'speak', None)):
            return self._call_output_method(
                'broadcast', target, 'speak',
                text=text, level=level, area=area, source='alarm')
        self._record_output_failure(
            'broadcast',
            f"{type(target).__name__} 既不提供 speak_alarm 也不提供 speak，广播无法发出")
        return False

    def _emit_websocket_alarm(self, alarm_data: dict[str, Any]):
        """通过WebSocket向前端推送报警"""
        if not self._websocket_emit:
            return
        try:
            self._websocket_emit(alarm_data)
            # Prometheus（round 255）：推送方向的消息计数。
            try:
                from core.metrics import WS_MESSAGES
                WS_MESSAGES.labels(direction='out').inc()
            except Exception as e:      # noqa: BLE001
                logger.debug(f"WebSocket 消息指标打点失败: {e}")
        except Exception as e:
            self._record_output_failure('websocket', f"前端推送异常: {e}")

    def _send_notification(self, rule_config: dict[str, Any], device_id: str,
                           register_name: str, value: float, timestamp: datetime):
        """【占位，全仓库零调用】只打一条 INFO 日志。

        ⚠️ 别把它当成「发通知」的入口：真正发邮件的是 `_notify_by_email()`，
        它在 `_trigger_alarm` 的去重点之后调用、走后台线程池。
        这个方法留着是为了不破坏可能存在的旧调用方，方法名很有迷惑性，
        所以在这里写清楚。
        """
        alarm_level = rule_config.get('level', 'warning')
        alarm_message = rule_config.get('name', '未知报警')
        logger.info(f"报警通知: [{alarm_level}] {alarm_message} - {device_id}/{register_name}")

    def raise_external_alarm(self, alarm_id: str, message: str, level: str = 'warning',
                             device_id: str = 'external',
                             register_name: str = 'event',
                             value: float = 0.0,
                             timestamp: datetime | None = None,
                             notify: bool = True) -> bool:
        """把**外部事件**（边缘决策、上位机联动、脚本）当作一等报警处理。

        round 261：`run.py` 的 `edge_set_alarm` 原来是直接
        `database.insert_alarm(...)`，**绕开整个报警管理层**：

        * 没有去重 —— 同一事件每秒都能落一条；
        * 不进升级跟踪 —— 没人确认也不会升级；
        * 不触发声光/广播/WebSocket —— 现场根本不知道；
        * `alarm_id` 用的是 `edge_{当前秒}`，**每秒一个新 ID**，
          `insert_alarm` 那句「同一条未确认记录」永远命中不了，
          于是 9 天里同一个「管道压力超限」堆了 **20931 条**同义记录
          （真实库 `data/scada.db` 实测，占全库报警的 44%）。

        这里给它一条正式入口：注册成一条规则（让活跃报警列表能显示名称/级别）、
        建立报警状态（进得了 `get_active_alarms`）、再走 `_trigger_alarm`
        的完整链路（去重 → 数据库 → 声光 → 广播 → WebSocket → 升级）。

        Args:
            alarm_id: 报警ID。**必须稳定**（同一类事件用同一个 ID），
                否则去重与计数全部失效 —— 用 `hashlib` 从消息内容派生即可。
            message: 报警文案（现场操作员要照着它动作）。
            level: 级别（critical/warning/...）。
            device_id / register_name: 归属（外部事件用 'external'/'event'）。
            value: 触发值（外部事件通常没有，默认 0）。
            timestamp: 触发时刻，缺省取当前时间。

        Returns:
            bool: 是否登记成功（重复/被抑制也算成功登记，返回 True）。
        """
        timestamp = timestamp or datetime.now()
        state_key = (alarm_id, device_id, register_name)

        # 注册成"规则"，否则 get_active_alarms 拿不到名称/级别
        with self._state_lock:
            if alarm_id not in self.rules:
                self.rules[alarm_id] = {
                    'id': alarm_id,
                    'name': message,
                    'level': level,
                    'device_id': device_id,
                    'register_name': register_name,
                    'condition': 'external',
                    'threshold': 0,
                    'enabled': True,
                    'external': True,
                }

            state = self.alarm_states.get(state_key)
            if state is None:
                self.alarm_states[state_key] = {
                    'alarm_id': alarm_id,
                    'device_id': device_id,
                    'register_name': register_name,
                    'first_trigger_time': timestamp,
                    'last_trigger_time': timestamp,
                    'trigger_count': 1,
                    'confirmed': False,
                    'acknowledged': False,
                    'pending': False,
                }
            else:
                state['last_trigger_time'] = timestamp
                state['trigger_count'] = state.get('trigger_count', 0) + 1

        self._trigger_alarm(self.rules[alarm_id], device_id, register_name,
                            value, timestamp, notify=notify)
        return True

    def clear_external_alarm(self, alarm_id: str, device_id: str = 'external',
                             register_name: str = 'event',
                             timestamp: datetime | None = None) -> bool:
        """外部事件恢复：清内存状态 + 落库标记已恢复（与阈值报警同一套语义）。"""
        state_key = (alarm_id, device_id, register_name)
        with self._state_lock:
            existed = self.alarm_states.pop(state_key, None) is not None
        if not existed:
            return False
        if self._escalation_manager:
            self._escalation_manager.remove_alarm(alarm_id, device_id, register_name)
        self._clear_alarm_record(alarm_id, device_id, register_name,
                                 timestamp or datetime.now())
        logger.info("外部报警已恢复: %s - %s/%s", alarm_id, device_id, register_name)
        return True

    def get_active_alarms(self) -> list[dict[str, Any]]:
        """
        获取活动报警

        Returns:
            list[dict[str, Any]]: 活动报警列表
        """
        active_alarms = []

        # 线程安全：快照遍历，避免RuntimeError: dictionary changed size
        with self._state_lock:
            snapshot = list(self.alarm_states.items())
            rules_snapshot = dict(self.rules)

        for state_key, state in snapshot:
            rule_id, device_id, register_name = state_key
            rule_config = rules_snapshot.get(rule_id, {})

            active_alarms.append({
                'alarm_id': rule_id,
                'device_id': device_id,
                'register_name': register_name,
                'alarm_level': rule_config.get('level', 'warning'),
                'alarm_message': rule_config.get('name', '未知报警'),
                'first_trigger_time': state.get('first_trigger_time'),
                'last_trigger_time': state.get('last_trigger_time'),
                'trigger_count': state.get('trigger_count', 0),
                'acknowledged': state.get('acknowledged', False),
                'pending': state.get('pending', False)
            })

        return active_alarms

    def acknowledge_alarm(self, alarm_id: str, device_id: str,
                          register_name: str, acknowledged_by: str) -> bool:
        """
        确认报警（含声光消音 + 去重抑制）

        操作员到场后确认报警，自动关闭蜂鸣器（灯保持闪烁）
        同时记录确认时间，触发去重抑制窗口

        Args:
            alarm_id: 报警ID
            device_id: 设备ID
            register_name: 寄存器名称
            acknowledged_by: 确认人

        Returns:
            bool: 确认是否成功
        """
        state_key = (alarm_id, device_id, register_name)

        with self._state_lock:
            state = self.alarm_states.get(state_key)
            if state is not None:
                state['acknowledged'] = True
                state['acknowledged_by'] = acknowledged_by
                state['acknowledged_at'] = datetime.now()

        # 更新数据库
        success = self.database.acknowledge_alarm(alarm_id, acknowledged_by, device_id, register_name)

        # 通知升级管理器
        if success and self._escalation_manager:
            self._escalation_manager.acknowledge_alarm(alarm_id, device_id, register_name)

        if success:
            logger.info(f"报警已确认: {alarm_id} by {acknowledged_by}")

            # Prometheus（round 255）：确认计数 + 报警持续时长。
            # 此前 `scada_alarms_acknowledged_total` 与
            # `scada_alarm_duration_seconds` 都是恒 0 的线 —— 看板上
            # 看不出"报警多久才被确认"，而这正是 ISA-18.2 最关心的指标之一。
            try:
                from core.metrics import ALARMS_ACKNOWLEDGED, ALARM_DURATION
                ALARMS_ACKNOWLEDGED.inc()
                started = state.get('first_trigger_time') if state else None
                if isinstance(started, datetime):
                    elapsed = max(0.0, (datetime.now() - started).total_seconds())
                    level = (self.rules.get(alarm_id) or {}).get('level', 'warning')
                    ALARM_DURATION.labels(level=level).observe(elapsed)
            except Exception as e:      # noqa: BLE001 —— 指标失败不影响确认
                logger.debug(f"报警确认指标打点失败: {e}")

            # 记录确认时间（用于去重抑制）
            #
            # round 279：**只有"当时还在报警"才开抑制窗口**。
            # 操作员在历史列表里确认一条**已恢复**的报警时，`alarm_states` 里
            # 已经没有它了（`state is None`）—— 这时再记抑制，会把**下一次
            # 真正的新报警**在 600 秒内静默压掉：设备又越限了，界面不弹、
            # 声光不响，而日志里只有一行 debug。
            #
            # 已恢复报警的确认仍然要落库（历史记录里 `acknowledged=1`，
            # ISA-18.2 要求"已恢复但未确认"能被单独统计，人工补确认是合法的），
            # 只是不该产生"未来 10 分钟不许再报"的效果。
            if state is not None:
                self._record_acknowledge(alarm_id, device_id, register_name)
            else:
                logger.info(
                    "确认的是**已恢复**的报警（不在活跃列表中）: %s - %s/%s by %s"
                    " —— 已落库，但不设去重抑制窗口",
                    alarm_id, device_id, register_name, acknowledged_by)

            # 声光消音（关蜂鸣器，灯保持）
            if self.alarm_output and self.alarm_output.enabled:
                self._call_output_method('alarm_output', self.alarm_output, 'acknowledge')
        return success

    def reset_alarm(self, device_id: str | None = None) -> bool:
        """
        复位报警（清除所有/指定设备的报警状态，恢复绿灯）

        Args:
            device_id: 设备ID（None=全部复位）

        Returns:
            bool: 复位是否**完整**成功。报警状态清空总是成功的；但如果配了声光
            输出、而 DO 写入失败，返回 False —— 此时软件侧已清零、硬件侧可能
            仍停在红灯/蜂鸣器状态。调用方（API/UI）必须把 False 报给操作员，
            否则就是「界面说绿灯、现场还在响」的假成功。
        """
        with self._state_lock:
            if device_id:
                # 键是 (rule_id, device_id, register_name) → 设备在 k[1]
                keys_to_remove = [k for k in self.alarm_states if k[1] == device_id]
                cleared_rules = [(k[0], k[1], k[2]) for k in keys_to_remove]
                for key in keys_to_remove:
                    del self.alarm_states[key]
            else:
                # 键是 (rule_id, device_id, register_name)，三个都要原样取出。
                # ⚠️ 这里曾经写成 `(state['alarm_id'], key[0], key[1])` ——
                # 加 rule_id 之后 key[0] 就是 rule_id，于是传给升级管理器的是
                # `remove_alarm(rule_id, rule_id, device_id)`：设备/寄存器全错位，
                # 已复位的报警会继续被升级跟踪（且 _states 里留下垃圾键）。
                cleared_rules = [(key[0], key[1], key[2]) for key in self.alarm_states]
                self.alarm_states.clear()

        # 复位即报警生命周期结束，撤销升级跟踪（避免已复位的报警继续升级）
        if self._escalation_manager:
            for rid, dev, reg in cleared_rules:
                if rid:
                    self._escalation_manager.remove_alarm(rid, dev, reg)

        # round 260：复位也要把「已恢复」落到数据库。否则运维点一次"复位"，
        # 界面上报警消失了，数据库里那行仍然活跃 —— 未确认数不降反升
        # （下次触发还会被当成同一条未结束记录）。
        for rid, dev, reg in cleared_rules:
            if rid:
                self._clear_alarm_record(rid, dev, reg, datetime.now())

        # 清除去重历史（复位后允许重新推送）
        with self._dedup_lock:
            if device_id:
                emit_keys = [k for k in self._emit_history if k[1] == device_id]
                ack_keys = [k for k in self._acknowledge_history if k[1] == device_id]
                for k in emit_keys:
                    del self._emit_history[k]
                for k in ack_keys:
                    del self._acknowledge_history[k]
            else:
                self._emit_history.clear()
                self._acknowledge_history.clear()

        # 声光复位（全部清零，绿灯恢复）
        output_ok = True
        if self.alarm_output and self.alarm_output.enabled:
            output_ok = self._call_output_method('alarm_output', self.alarm_output, 'reset')

        logger.info(f"报警已复位: {'全部' if not device_id else device_id}")
        return output_ok

    def get_alarm_statistics(self) -> dict[str, Any]:
        """
        获取报警统计信息（含输出总线状态 + 去重统计）

        Returns:
            dict[str, Any]: 统计信息
        """
        # 活动报警统计
        active_alarms = self.get_active_alarms()

        # 按级别统计
        level_stats = {}
        for alarm in active_alarms:
            level = alarm.get('alarm_level', 'unknown')
            level_stats[level] = level_stats.get(level, 0) + 1

        # 按设备统计
        device_stats = {}
        for alarm in active_alarms:
            device_id = alarm.get('device_id')
            device_stats[device_id] = device_stats.get(device_id, 0) + 1

        stats = {
            'total_active_alarms': len(active_alarms),
            'by_level': level_stats,
            'by_device': device_stats,
            'total_rules': len(self.rules),
            'enabled_rules': sum(1 for r in self.rules.values() if r.get('enabled', True))
        }

        # 输出总线状态
        stats['output'] = {
            'alarm_output': self.alarm_output.get_status() if self.alarm_output else None,
            'broadcast': self.broadcast_system.get_status() if self.broadcast_system else None,
            # 各通道累计调用失败次数（>0 即存在声光/广播/前端漏报，必须可观测）
            'errors': self._get_output_error_counts(),
            # 各通道**当前连续**失败次数（达到阈值会把"通道坏了"报成报警）
            'consecutive_failures': self._get_output_consecutive(),
            'failure_alarm_threshold': NOTIFY_FAILURE_ALARM_THRESHOLD,
            'broken_channels': sorted(self._output_broken),
        }

        # 去重统计
        with self._dedup_lock:
            stats['dedup'] = {
                'enabled': self.dedup_config.enabled,
                'emit_cooldown_seconds': self.dedup_config.emit_cooldown_seconds,
                'acknowledge_suppress_seconds': self.dedup_config.acknowledge_suppress_seconds,
                'tracked_alarms': len(self._emit_history),
                'acknowledged_alarms': len(self._acknowledge_history),
            }

        return stats

    def get_dedup_config(self) -> dict[str, Any]:
        """获取去重配置"""
        return self.dedup_config.to_dict()

    def update_dedup_config(self, data: dict[str, Any]) -> dict[str, Any]:
        """
        更新去重配置

        round 256：原来不管落盘成不成功都返回"更新后的配置"，接口照样回
        `success: true` —— 磁盘没写进去，重启就恢复旧值，用户以为改好了。
        现在**写失败就回滚内存**（否则内存与磁盘长期不一致，更难查）。

        Args:
            data: 新配置数据

        Returns:
            dict: 更新后的配置

        Raises:
            ConfigPersistError: 配置文件存在但写入失败（内存已回滚）
        """
        previous = self.dedup_config.to_dict()
        self.dedup_config.update(data)

        if not self._save_dedup_config():
            # 回滚：宁可让本次修改整体失败，也不要留下「内存改了、磁盘没改」
            self.dedup_config = AlarmDedupConfig(previous)
            raise ConfigPersistError(
                f'去重配置写入失败（已回滚）：{self.config_path}')

        logger.info(f"去重配置已更新: {self.dedup_config.to_dict()}")
        return self.dedup_config.to_dict()

    def _save_dedup_config(self) -> bool:
        """保存去重配置到 alarms.yaml。

        round 256：原来 `open(..., 'w')` + `yaml.dump` —— 先截断再序列化，
        dump 抛异常就把整份配置清成 0 字节（所有规则一起没），而异常被
        吞成一条日志。改走 `core.config_io.dump_yaml`：原子写 + 只重写
        `dedup` 这一段，文件里其它注释/段落逐字保留。
        """
        try:
            config_file = Path(self.config_path)
            if not config_file.exists():
                # 没有配置文件 = 这个部署本来就不从文件读去重配置（测试夹具 /
                # 嵌入式装配）。此时内存态就是唯一真源，不算失败；
                # 但也不凭空造一份残缺的配置出来。
                logger.warning("去重配置未落盘：配置文件不存在 %s", config_file)
                return True

            with open(config_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}

            config['dedup'] = self.dedup_config.to_dict()

            if not dump_yaml(config_file, config):
                return False

            logger.info("去重配置已保存到配置文件")
            return True

        except Exception as e:
            logger.error(f"保存去重配置异常: {e}")
            return False

    def add_rule(self, rule_config: dict[str, Any]) -> bool:
        """
        添加报警规则

        Args:
            rule_config: 规则配置

        Returns:
            bool: 添加是否成功
        """
        try:
            rule_id = rule_config.get('id')
            if not rule_id:
                logger.error("报警规则缺少id字段")
                return False

            # 检查是否已存在
            if rule_id in self.rules:
                logger.warning(f"报警规则 {rule_id} 已存在，将被覆盖")

            # 添加到内存 (线程安全)
            with self._state_lock:
                previous = self.rules.get(rule_id)
                self.rules[rule_id] = rule_config
            self._rebuild_rules_index()

            # 保存到配置文件。round 256：落盘失败必须回滚 —— 否则接口回
            # `success: true`、规则只在内存里活着，重启就没了（用户以为已生效）。
            if not self._save_config():
                with self._state_lock:
                    if previous is None:
                        self.rules.pop(rule_id, None)
                    else:
                        self.rules[rule_id] = previous
                self._rebuild_rules_index()
                logger.error("添加报警规则失败（配置落盘失败，已回滚）: %s", rule_id)
                return False

            logger.info(f"添加报警规则: {rule_id}")
            return True

        except Exception as e:
            logger.error(f"添加报警规则异常: {e}")
            return False

    def remove_rule(self, rule_id: str) -> bool:
        """
        移除报警规则

        Args:
            rule_id: 规则ID

        Returns:
            bool: 移除是否成功
        """
        try:
            if rule_id in self.rules:
                with self._state_lock:
                    removed = self.rules.pop(rule_id)
                self._rebuild_rules_index()

                # 保存到配置文件。round 256：落盘失败回滚，避免「内存删了、
                # 磁盘还在 → 重启后规则复活」这种更难查的不一致。
                if not self._save_config():
                    with self._state_lock:
                        self.rules[rule_id] = removed
                    self._rebuild_rules_index()
                    logger.error("移除报警规则失败（配置落盘失败，已回滚）: %s", rule_id)
                    return False

                logger.info(f"移除报警规则: {rule_id}")
                return True
            else:
                logger.warning(f"报警规则 {rule_id} 不存在")
                return False

        except Exception as e:
            logger.error(f"移除报警规则异常: {e}")
            return False

    def _save_config(self) -> bool:
        """保存报警配置到文件（保留 dedup/escalation 等非规则配置段）。

        Returns:
            bool: 是否成功写入。

        **数据安全**：配置文件已存在却读不出来（YAML 语法错、编码错、写到一半
        被截断）时，绝不能拿空 dict 直接覆盖 —— 那会把 dedup / escalation 等
        非规则配置段**不可逆地抹掉**。此时先把坏文件另存为 ``.corrupt-<时间戳>``，
        确认备份成功后再写新配置；连备份都做不出来就放弃本次写入。
        """
        try:
            config_file = Path(self.config_path)
            config_file.parent.mkdir(parents=True, exist_ok=True)

            # 读取已有配置，保留非规则段
            config: Dict[str, Any] = {}
            read_failed = False
            if config_file.exists():
                try:
                    with open(config_file, 'r', encoding='utf-8') as f:
                        config = yaml.safe_load(f) or {}
                    if not isinstance(config, dict):
                        read_failed = True
                        logger.error("已有报警配置不是 YAML 映射，内容类型为 %s: %s",
                                     type(config).__name__, config_file)
                        config = {}
                except Exception as e:
                    read_failed = True
                    config = {}
                    logger.error("读取已有报警配置失败（文件存在但无法解析）%s: %s",
                                 config_file, e)

            if read_failed:
                backup = config_file.with_name(
                    '%s.corrupt-%s' % (config_file.name,
                                       datetime.now().strftime('%Y%m%d-%H%M%S'))
                )
                try:
                    shutil.copy2(config_file, backup)
                    logger.error(
                        "报警配置已损坏，已另存备份再覆盖（避免静默丢失 dedup/escalation "
                        "等配置段）: %s -> %s", config_file, backup)
                except Exception as backup_err:
                    # 连备份都失败 → 宁可这次规则存不下，也不能毁掉整份配置
                    logger.error("报警配置损坏且备份失败，已放弃本次写入以防配置丢失 %s: %s",
                                 config_file, backup_err)
                    return False

            config['alarm_rules'] = list(self.rules.values())

            # round 256：原来 `open(..., 'w')` 先截断再 dump —— 序列化一抛异常
            # （规则里有不可序列化的值 / 磁盘满），文件就成 0 字节：上面刚做的
            # 损坏备份保护全部白费，规则连同 dedup/escalation 一起没了。
            # 改走原子写 + 就地合并：只重写 alarm_rules 这一段，其余段落
            # （含注释）逐字保留。
            if not dump_yaml(config_file, config):
                logger.error("报警配置写入失败（原文件保持不变）: %s", config_file)
                return False

            logger.info("报警配置已保存")
            return True

        except Exception as e:
            logger.error(f"保存报警配置异常: {e}")
            return False

    # ================================================================
    # ISA-18.2 报警管理扩展
    # ================================================================

    def shelve_alarm(self, alarm_id: str, device_id: str, register_name: str,
                     reason: str, shelved_by: str, duration_minutes: int = None) -> bool:
        """
        旁路/搁置报警（ISA-18.2 Alarm Shelving）

        维护期间临时屏蔽特定报警，避免误报干扰。
        支持定时自动解除或手动解除。

        Args:
            alarm_id: 报警规则ID
            device_id: 设备ID
            register_name: 寄存器名
            reason: 旁路原因
            shelved_by: 操作人
            duration_minutes: 旁路时长（分钟），None=手动解除

        Returns:
            bool: 是否成功
        """
        alarm_key = (alarm_id, device_id, register_name)
        shelved_until = None
        if duration_minutes:
            shelved_until = datetime.now() + timedelta(minutes=duration_minutes)

        with self._state_lock:
            self._shelved_alarms[alarm_key] = AlarmShelveState(
                alarm_key=alarm_key,
                reason=reason,
                shelved_by=shelved_by,
                shelved_until=shelved_until,
            )

        logger.warning(f"报警已旁路: {alarm_id} ({device_id}/{register_name}) "
                       f"by {shelved_by}, 原因: {reason}, "
                       f"时长: {'手动解除' if not duration_minutes else f'{duration_minutes}分钟'}")
        return True

    def unshelve_alarm(self, alarm_id: str, device_id: str, register_name: str) -> bool:
        """
        解除报警旁路

        Args:
            alarm_id: 报警规则ID
            device_id: 设备ID
            register_name: 寄存器名

        Returns:
            bool: 是否成功
        """
        alarm_key = (alarm_id, device_id, register_name)
        with self._state_lock:
            if alarm_key in self._shelved_alarms:
                del self._shelved_alarms[alarm_key]
                logger.info(f"报警旁路已解除: {alarm_id} ({device_id}/{register_name})")
                return True
        return False

    def get_shelved_alarms(self) -> list[dict]:
        """获取所有被旁路的报警"""
        with self._state_lock:
            return [state.to_dict() for state in self._shelved_alarms.values()]

    def _cleanup_expired_shelves(self):
        """清理过期的旁路"""
        with self._state_lock:
            expired = [k for k, v in self._shelved_alarms.items() if v.is_expired()]
            for key in expired:
                del self._shelved_alarms[key]
                logger.info(f"报警旁路已过期自动解除: {':'.join(key)}")

    def set_deadband(self, rule_id: str, deadband_value: float):
        """
        设置报警死区（ISA-18.2 Deadband）

        防止阈值附近信号抖动导致报警反复触发/清除。
        例如：阈值100，死区2，则 value>102 才触发，value<98 才清除。

        Args:
            rule_id: 报警规则ID
            deadband_value: 死区值
        """
        with self._state_lock:
            self._deadbands[rule_id] = deadband_value
        logger.info(f"报警死区已设置: {rule_id} = {deadband_value}")

    def get_deadbands(self) -> dict[str, float]:
        """获取所有死区配置"""
        with self._state_lock:
            return dict(self._deadbands)

    def get_priority_matrix(self) -> dict:
        """获取 ISA-18.2 优先级矩阵定义"""
        return {
            'severity_levels': {
                1: '可忽略',
                2: '次要',
                3: '中等',
                4: '严重',
                5: '灾难',
            },
            'likelihood_levels': {
                1: '罕见',
                2: '不太可能',
                3: '可能',
                4: '很可能',
                5: '频繁',
            },
            'priorities': {
                'P1': {'name': '紧急', 'response_time': '1分钟', 'color': 'red'},
                'P2': {'name': '高', 'response_time': '5分钟', 'color': 'orange'},
                'P3': {'name': '中', 'response_time': '15分钟', 'color': 'yellow'},
                'P4': {'name': '低', 'response_time': '1小时', 'color': 'blue'},
                'P5': {'name': '记录', 'response_time': '班次结束', 'color': 'gray'},
            }
        }
