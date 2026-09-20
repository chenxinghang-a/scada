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
from typing import Any, Callable, Dict
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
import paths

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

    def record_alarm(self, severity: str) -> tuple[bool, str]:
        """记录一条告警，返回 (should_emit, reason)"""
        with self._lock:
            now = time.time()
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

            now = time.time()
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
        self.alarm_states: dict[tuple, dict] = {}  # (device_id, register_name) -> alarm_state

        # 输出总线（延迟注入，启动后绑定）
        self.alarm_output = alarm_output
        self.broadcast_system = broadcast_system

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

        # 线程锁（保护去重状态的并发访问）
        self._dedup_lock = Lock()

        # 输出通道失败计数（声光/广播/前端）。
        # 声光与广播是安全关键输出，"调用不通"绝不能静默 —— 每次失败都记账，
        # 并通过 get_alarm_statistics()['output']['errors'] / 日志暴露。
        self._output_errors: dict[str, int] = {
            'alarm_output': 0, 'broadcast': 0, 'websocket': 0}
        self._output_error_lock = Lock()

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
        self._last_shelf_cleanup: float = 0

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

        logger.info("报警管理器后台资源已停止")

    def shutdown(self) -> None:
        """停止报警管理器（stop() 的语义别名）"""
        self.stop()

    def set_websocket_emit(self, emit_func: Callable[..., Any]):
        """注入WebSocket emit函数（由run.py启动时调用）"""
        self._websocket_emit = emit_func

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

                device_id, register_name = state_key
                rule_id = state.get('alarm_id')
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
            state = self.alarm_states.get((device_id, register_name))
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

            # 加载升级配置
            escalation_cfg = config.get('escalation', {})
            if escalation_cfg:
                self._escalation_timeout = escalation_cfg.get('timeout_seconds', 600)
                logger.info(f"加载报警升级配置: 超时{self._escalation_timeout}s")

                # 初始化告警升级管理器（多级升级）
                if self._escalation_manager is None:
                    try:
                        from 报警层.alarm_escalation import AlarmEscalationManager
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
                        self._escalation_manager = AlarmEscalationManager(manager_cfg)
                        self._escalation_manager.add_callback(self._on_escalation)
                        # 多级升级结果桥接给外部回调（见 _on_manager_escalation）
                        self._escalation_manager.add_callback(self._on_manager_escalation)
                        # 不启动管理器自带线程：升级由报警升级定时器统一驱动，
                        # 否则又是"两套调度各跑一套"
                        rules = self._escalation_manager.get_rules()
                        if rules:
                            self._escalation_timeout = min(
                                r['timeout_seconds'] for r in rules)
                        logger.info(
                            "告警升级管理器初始化成功（多级规则，统一由报警升级定时器驱动，"
                            "首级超时%d秒）", self._escalation_timeout)
                    except Exception as e:
                        logger.warning(f"告警升级管理器初始化失败: {e}")
                        self._escalation_manager = None

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
        # 用索引快速查找匹配的规则（避免遍历所有规则）
        rules_key = (device_id, register_name)
        matched_rules = self._rules_index.get(rules_key)
        if not matched_rules:
            return

        # 清理过期的旁路（低频执行）
        now_ts = time.time()
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
                current_alarm_state = self.alarm_states.get((device_id, register_name))
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
        """
        检查报警条件（含非对称死区/迟滞支持）

        死区（Deadband）防止阈值附近信号抖动导致报警反复触发/清除。
        非对称逻辑：
        - 触发（alarm not active）：精确阈值判断
        - 清除（alarm active）：阈值 ± deadband，需要更大偏移才能清除

        例如：阈值100，死区2，gt条件
        - 触发：value > 100
        - 清除：value < 98（需要降到 threshold - deadband 以下）

        Args:
            value: 实际值
            condition: 条件类型
            threshold: 阈值
            rule_id: 规则ID（用于查找死区配置）
            alarm_state: 当前告警状态（用于区分触发/清除逻辑）

        Returns:
            bool: 是否触发报警
        """
        deadband = self._deadbands.get(rule_id, 0) if rule_id else 0
        is_active = alarm_state and alarm_state.get('alarm_id') is not None

        if condition == 'greater_than':
            if is_active:
                return value > threshold - deadband  # 清除需降到 threshold - deadband 以下
            return value > threshold
        elif condition == 'less_than':
            if is_active:
                return value < threshold + deadband  # 清除需升到 threshold + deadband 以上
            return value < threshold
        elif condition == 'equal_to':
            return abs(value - threshold) <= deadband if deadband > 0 else value == threshold
        elif condition == 'not_equal_to':
            return abs(value - threshold) > deadband if deadband > 0 else value != threshold
        elif condition == 'greater_equal':
            if is_active:
                return value >= threshold - deadband
            return value >= threshold
        elif condition == 'less_equal':
            if is_active:
                return value <= threshold + deadband
            return value <= threshold
        else:
            logger.warning(f"未知的报警条件: {condition}")
            return False

    @staticmethod
    def _normalize_delay(raw: Any, rule_id: str = '') -> float:
        """把规则的 delay 配置归一为秒数（float，非负）。

        配置里写成字符串（`delay: "30"`）时，旧实现的 `delay > 0` 会直接抛
        TypeError，把整条报警检查路径打断（调用方 except 吞掉 → 报警丢失）。
        """
        if raw is None:
            return 0.0
        try:
            delay = float(raw)
        except (TypeError, ValueError):
            logger.warning("报警规则 %s 的 delay 配置非法(%r)，按 0（不延迟）处理",
                           rule_id, raw)
            return 0.0
        return max(0.0, delay)

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
        state_key = (device_id, register_name)

        with self._state_lock:
            current_state = self.alarm_states.get(state_key, {})

        # 获取延迟时间
        delay = self._normalize_delay(rule_config.get('delay', 0), rule_id)

        if triggered:
            # C1修复: 整个 read-modify-write 在同一锁块内，消除 TOCTOU 竞态
            fire = False
            with self._state_lock:
                live_state = self.alarm_states.get(state_key)
                if live_state and live_state.get('alarm_id') == rule_id:
                    if live_state.get('pending'):
                        # 延迟确认中：只有条件持续成立满 delay 秒才真正产生报警
                        anchor = live_state.get('confirm_time') or live_state.get('first_trigger_time')
                        elapsed = self._elapsed_seconds(anchor, timestamp)
                        if elapsed is None:
                            # 时间戳缺失/类型或时区不一致导致无法计时：旧实现会让
                            # 报警永远停在 pending —— 即"配置了 delay 的规则永不报警"。
                            # 计时不可用属数据问题，但必须报警（fail-open）且留痕。
                            logger.warning(
                                "延迟报警无法计算持续时间，按到期处理以规避静默漏报: "
                                "%s - %s/%s (anchor=%r timestamp=%r)",
                                rule_id, device_id, register_name, anchor, timestamp)
                            elapsed = delay
                        if elapsed >= delay:
                            live_state['pending'] = False
                            live_state['confirmed'] = True
                            live_state['first_trigger_time'] = timestamp
                            live_state['last_trigger_time'] = timestamp
                            live_state['last_value'] = value
                            fire = True
                            logger.info(f"延迟报警到期触发: {rule_id} - {device_id}/{register_name} "
                                        f"(持续{elapsed:.1f}s >= delay {delay}s)")
                        else:
                            # 未到期：只刷新数值，不触发声光/弹窗
                            live_state['last_trigger_time'] = timestamp
                            live_state['last_value'] = value
                    else:
                        # 已经在报警，只更新时间和数值，不重复触发声光/弹窗
                        live_state['last_trigger_time'] = timestamp
                        live_state['trigger_count'] = live_state.get('trigger_count', 0) + 1
                        live_state['last_value'] = value
                else:
                    logger.debug(f"新报警触发: rule={rule_id} device={device_id} reg={register_name} "
                               f"current_alarm_id={current_state.get('alarm_id')} state_key={state_key}")
                    alarm_state = {
                        'alarm_id': rule_id,
                        'device_id': device_id,
                        'register_name': register_name,
                        'first_trigger_time': timestamp,
                        'last_trigger_time': timestamp,
                        'trigger_count': 1,
                        'confirmed': False,
                        'acknowledged': False
                    }
                    if delay > 0:
                        # 延迟确认：先挂起，等条件持续满 delay 秒后由上面的分支消费
                        alarm_state['pending'] = True
                        alarm_state['confirm_time'] = timestamp
                    else:
                        alarm_state['pending'] = False
                    self.alarm_states[state_key] = alarm_state
                    fire = delay <= 0

            # 锁外触发（避免回调死锁）
            if fire:
                # 去重记账只在 _trigger_alarm 内、且**确实推送成功后**发生。
                # 这里再记一次会把"被洪水/冷却抑制、根本没推送"的报警也记成已推送，
                # 于是冷却窗口内真正该推送的报警被压掉 —— 静默漏报。
                self._trigger_alarm(rule_config, device_id, register_name, value, timestamp)
        else:
            # 未触发，检查是否需要清除报警（只清除自己规则的状态）
            cleared = False
            with self._state_lock:
                live_state = self.alarm_states.get(state_key)
                if live_state and live_state.get('alarm_id') == rule_id:
                    del self.alarm_states[state_key]
                    cleared = True
            if cleared:
                logger.info(f"报警清除: {rule_id} - {device_id}/{register_name}")
                # 同步撤销升级跟踪：否则已恢复正常的报警仍会被升级管理器
                # 按超时升级（且其 _states 会无界增长）
                if self._escalation_manager:
                    self._escalation_manager.remove_alarm(rule_id, device_id, register_name)

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
        now = time.time()

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
            now = time.time()
            self._emit_history[alarm_key] = now
            self._prune_dedup_history(
                self._emit_history, self.dedup_config.emit_cooldown_seconds, now)

    def _record_acknowledge(self, rule_id: str, device_id: str, register_name: str):
        """记录报警确认时间"""
        alarm_key = (rule_id, device_id, register_name)
        with self._dedup_lock:
            now = time.time()
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
                       register_name: str, value: float, timestamp: datetime):
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

        # ISA-18.2: 告警洪水检测 — 抑制低优先级告警
        severity = rule_config.get('level', 'medium')
        should_emit, reason = self._flood_detector.record_alarm(severity)
        if not should_emit:
            logger.debug(f"告警被洪水检测器抑制: {rule_id} ({device_id}/{register_name})")
            return
        alarm_level = rule_config.get('level', 'warning')

        # 记录到升级管理器
        if self._escalation_manager:
            self._escalation_manager.record_alarm(rule_id, device_id, register_name)
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
        self._emit_websocket_alarm({
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
        })

    def _record_output_failure(self, channel: str, detail: str) -> None:
        """输出通道失败必须可见：ERROR 日志 + 计数（绝不静默吞掉）"""
        with self._output_error_lock:
            self._output_errors[channel] = self._output_errors.get(channel, 0) + 1
        logger.error("报警输出失败[%s]（第%d次）: %s",
                     channel, self._output_errors.get(channel, 0), detail)

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
        except Exception as e:
            self._record_output_failure('websocket', f"前端推送异常: {e}")

    def _send_notification(self, rule_config: dict[str, Any], device_id: str,
                           register_name: str, value: float, timestamp: datetime):
        """
        发送报警通知（保留兼容，具体实现在 _trigger_alarm 中）
        """
        alarm_level = rule_config.get('level', 'warning')
        alarm_message = rule_config.get('name', '未知报警')
        logger.info(f"报警通知: [{alarm_level}] {alarm_message} - {device_id}/{register_name}")

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
            device_id, register_name = state_key
            rule_id = state.get('alarm_id')
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
        state_key = (device_id, register_name)

        with self._state_lock:
            state = self.alarm_states.get(state_key)
            if state and state.get('alarm_id') == alarm_id:
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

            # 记录确认时间（用于去重抑制）
            self._record_acknowledge(alarm_id, device_id, register_name)

            # 声光消音（关蜂鸣器，灯保持）
            if self.alarm_output and self.alarm_output.enabled:
                self._call_output_method('alarm_output', self.alarm_output, 'acknowledge')
        return success

    def reset_alarm(self, device_id: str | None = None) -> bool:
        """
        复位报警（清除所有/指定设备的报警状态，恢复绿灯）

        Args:
            device_id: 设备ID（None=全部复位）
        """
        with self._state_lock:
            if device_id:
                keys_to_remove = [k for k in self.alarm_states if k[0] == device_id]
                cleared_rules = [(self.alarm_states[k].get('alarm_id'), k[0], k[1])
                                 for k in keys_to_remove]
                for key in keys_to_remove:
                    del self.alarm_states[key]
            else:
                cleared_rules = [(state.get('alarm_id'), key[0], key[1])
                                 for key, state in self.alarm_states.items()]
                self.alarm_states.clear()

        # 复位即报警生命周期结束，撤销升级跟踪（避免已复位的报警继续升级）
        if self._escalation_manager:
            for rid, dev, reg in cleared_rules:
                if rid:
                    self._escalation_manager.remove_alarm(rid, dev, reg)

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
        if self.alarm_output and self.alarm_output.enabled:
            self._call_output_method('alarm_output', self.alarm_output, 'reset')

        logger.info(f"报警已复位: {'全部' if not device_id else device_id}")
        return True

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

        Args:
            data: 新配置数据

        Returns:
            dict: 更新后的配置
        """
        self.dedup_config.update(data)

        # 持久化到配置文件
        self._save_dedup_config()

        logger.info(f"去重配置已更新: {self.dedup_config.to_dict()}")
        return self.dedup_config.to_dict()

    def _save_dedup_config(self):
        """保存去重配置到alarms.yaml"""
        try:
            config_file = Path(self.config_path)
            if not config_file.exists():
                return

            with open(config_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}

            config['dedup'] = self.dedup_config.to_dict()

            with open(config_file, 'w', encoding='utf-8') as f:
                yaml.dump(config, f, allow_unicode=True, default_flow_style=False)

            logger.info("去重配置已保存到配置文件")

        except Exception as e:
            logger.error(f"保存去重配置异常: {e}")

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
                self.rules[rule_id] = rule_config
            self._rebuild_rules_index()

            # 保存到配置文件
            self._save_config()

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
                    del self.rules[rule_id]
                self._rebuild_rules_index()

                # 保存到配置文件
                self._save_config()

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

            with open(config_file, 'w', encoding='utf-8') as f:
                yaml.dump(config, f, allow_unicode=True, default_flow_style=False)

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
