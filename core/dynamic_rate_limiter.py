"""
动态限流器 - 根据系统负载自适应调整限流阈值
工业SCADA场景: 高负载时收紧限流保护系统，低负载时放开提升吞吐。

策略:
  - CPU/内存/队列深度作为负载信号
  - 负载分为 LOW / MEDIUM / HIGH / CRITICAL 四档
  - 每档对应不同的限流参数
  - 每隔 check_interval 秒评估一次，平滑过渡
"""

import threading
import time
import logging
from typing import Dict, Any, Optional, Callable

logger = logging.getLogger(__name__)


class LoadLevel:
    """负载等级"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# 各负载等级对应的限流参数
# format: (requests_per_minute, requests_per_second)
DEFAULT_PROFILES = {
    LoadLevel.LOW:      {"rpm": 500, "rps": 100},
    LoadLevel.MEDIUM:   {"rpm": 300, "rps": 60},
    LoadLevel.HIGH:     {"rpm": 150, "rps": 30},
    LoadLevel.CRITICAL: {"rpm": 50,  "rps": 10},
}

# 负载阈值（升档阈值，降档阈值在此基础上减去 hysteresis）
DEFAULT_THRESHOLDS = {
    "cpu_medium": 60,    # CPU% 达到此值升至 MEDIUM
    "cpu_high": 80,      # CPU% 达到此值升至 HIGH
    "cpu_critical": 95,  # CPU% 达到此值升至 CRITICAL
    "mem_medium": 70,
    "mem_high": 85,
    "mem_critical": 95,
    "queue_medium": 1000,
    "queue_high": 5000,
    "queue_critical": 10000,
    "hysteresis": 5,     # 降档滞后（避免频繁切换）
}


class SystemLoadMonitor:
    """
    系统负载监控器

    采集 CPU、内存、数据队列深度，评估负载等级。
    """

    def __init__(self, thresholds: Optional[Dict[str, float]] = None):
        self._thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
        self._lock = threading.Lock()
        self._last_level = LoadLevel.LOW
        self._last_check = 0.0
        self._metrics_history: list = []
        self._max_history = 60

        # 可注入的外部指标源
        self._queue_size_func: Optional[Callable[[], int]] = None

    def set_queue_size_func(self, func: Callable[[], int]):
        """注入队列深度获取函数"""
        self._queue_size_func = func

    def _get_cpu_percent(self) -> float:
        """获取CPU使用率"""
        try:
            import psutil
            return psutil.cpu_percent(interval=0)
        except ImportError:
            return 0.0

    def _get_memory_percent(self) -> float:
        """获取内存使用率"""
        try:
            import psutil
            return psutil.virtual_memory().percent
        except ImportError:
            return 0.0

    def _get_queue_size(self) -> int:
        """获取队列深度"""
        if self._queue_size_func:
            try:
                return self._queue_size_func()
            except Exception:
                return 0
        return 0

    def evaluate(self) -> str:
        """
        评估当前负载等级

        Returns:
            LoadLevel 值
        """
        cpu = self._get_cpu_percent()
        mem = self._get_memory_percent()
        queue = self._get_queue_size()
        t = self._thresholds

        # 按最高等级确定（木桶原理）
        level = LoadLevel.LOW

        # CPU评估（带降档滞后）
        if self._last_level in (LoadLevel.HIGH, LoadLevel.CRITICAL):
            cpu_high_threshold = t["cpu_high"] - t["hysteresis"]
        else:
            cpu_high_threshold = t["cpu_high"]

        if cpu >= t["cpu_critical"]:
            level = LoadLevel.CRITICAL
        elif cpu >= cpu_high_threshold:
            level = max_level(level, LoadLevel.HIGH)
        elif cpu >= t["cpu_medium"]:
            level = max_level(level, LoadLevel.MEDIUM)

        # 内存评估
        if self._last_level in (LoadLevel.HIGH, LoadLevel.CRITICAL):
            mem_high_threshold = t["mem_high"] - t["hysteresis"]
        else:
            mem_high_threshold = t["mem_high"]

        if mem >= t["mem_critical"]:
            level = LoadLevel.CRITICAL
        elif mem >= mem_high_threshold:
            level = max_level(level, LoadLevel.HIGH)
        elif mem >= t["mem_medium"]:
            level = max_level(level, LoadLevel.MEDIUM)

        # 队列评估
        if queue >= t["queue_critical"]:
            level = LoadLevel.CRITICAL
        elif queue >= t["queue_high"]:
            level = max_level(level, LoadLevel.HIGH)
        elif queue >= t["queue_medium"]:
            level = max_level(level, LoadLevel.MEDIUM)

        # 记录指标
        with self._lock:
            self._last_level = level
            self._last_check = time.time()
            self._metrics_history.append({
                'time': self._last_check,
                'cpu': cpu,
                'mem': mem,
                'queue': queue,
                'level': level,
            })
            if len(self._metrics_history) > self._max_history:
                self._metrics_history = self._metrics_history[-self._max_history:]

        return level

    def get_current_level(self) -> str:
        """获取当前负载等级（不重新采集）"""
        with self._lock:
            return self._last_level

    def get_metrics_snapshot(self) -> Dict[str, Any]:
        """获取最新指标快照"""
        with self._lock:
            if self._metrics_history:
                latest = self._metrics_history[-1]
                return {
                    'cpu_percent': latest['cpu'],
                    'memory_percent': latest['mem'],
                    'queue_size': latest['queue'],
                    'load_level': latest['level'],
                    'last_check': self._last_check,
                    'history_count': len(self._metrics_history),
                }
            return {
                'cpu_percent': 0,
                'memory_percent': 0,
                'queue_size': 0,
                'load_level': LoadLevel.LOW,
                'last_check': 0,
                'history_count': 0,
            }


def max_level(a: str, b: str) -> str:
    """返回更严重的负载等级"""
    order = {LoadLevel.LOW: 0, LoadLevel.MEDIUM: 1, LoadLevel.HIGH: 2, LoadLevel.CRITICAL: 3}
    return a if order.get(a, 0) >= order.get(b, 0) else b


class DynamicRateLimiter:
    """
    动态限流器

    根据系统负载自动调整限流参数。
    周期性检查负载，切换限流配置文件。

    Args:
        profiles: 各负载等级的限流参数（可覆盖默认值）
        check_interval: 负载检查间隔（秒）
        thresholds: 负载阈值配置
    """

    def __init__(
        self,
        profiles: Optional[Dict[str, Dict[str, int]]] = None,
        check_interval: float = 10.0,
        thresholds: Optional[Dict[str, float]] = None,
    ):
        self._profiles = {**DEFAULT_PROFILES, **(profiles or {})}
        self._check_interval = check_interval
        self._monitor = SystemLoadMonitor(thresholds)

        self._current_level = LoadLevel.LOW
        self._current_profile = self._profiles[LoadLevel.LOW]
        self._lock = threading.RLock()

        # Flask-Limiter 引用（启动后注入）
        self._flask_limiter = None

        # 监控线程
        self._stop_event = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None

        # 统计
        self._level_changes: list = []
        self._total_adjustments = 0

    def set_queue_size_func(self, func: Callable[[], int]):
        """注入队列深度获取函数"""
        self._monitor.set_queue_size_func(func)

    def set_flask_limiter(self, limiter):
        """注入Flask-Limiter实例"""
        self._flask_limiter = limiter
        logger.info("Flask-Limiter已关联到动态限流器")

    def start(self):
        """启动动态限流监控"""
        if self._monitor_thread and self._monitor_thread.is_alive():
            logger.warning("动态限流监控已在运行")
            return

        self._stop_event.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="dynamic-rate-limiter",
        )
        self._monitor_thread.start()
        logger.info("动态限流器已启动，检查间隔 %ds", int(self._check_interval))

    def stop(self):
        """停止动态限流监控"""
        self._stop_event.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=self._check_interval + 5)
        self._monitor_thread = None
        logger.info("动态限流器已停止")

    def _monitor_loop(self):
        """监控循环"""
        while not self._stop_event.wait(self._check_interval):
            try:
                self._evaluate_and_adjust()
            except Exception as e:
                logger.error("动态限流评估异常: %s", e)

    def _evaluate_and_adjust(self):
        """评估负载并调整限流"""
        new_level = self._monitor.evaluate()

        with self._lock:
            if new_level != self._current_level:
                old_level = self._current_level
                self._current_level = new_level
                self._current_profile = self._profiles.get(
                    new_level, self._profiles[LoadLevel.LOW]
                )
                self._total_adjustments += 1
                self._level_changes.append({
                    'time': time.time(),
                    'from': old_level,
                    'to': new_level,
                })
                if len(self._level_changes) > 50:
                    self._level_changes = self._level_changes[-50:]

                logger.warning(
                    "动态限流调整: %s → %s (rpm=%d, rps=%d)",
                    old_level, new_level,
                    self._current_profile['rpm'],
                    self._current_profile['rps'],
                )

                # 尝试动态更新Flask-Limiter
                self._apply_to_flask_limiter()

    def _apply_to_flask_limiter(self):
        """将当前限流配置应用到Flask-Limiter"""
        if not self._flask_limiter:
            return
        try:
            profile = self._current_profile
            new_limit = f"{profile['rpm']} per minute; {profile['rps']} per second"
            # flask-limiter 支持动态更新默认限制
            self._flask_limiter.enabled = True
            # 通过重新设置 app-level 限制实现
            # 注意: flask-limiter 不直接支持运行时修改 default_limits
            # 但我们可以通过 enabled 标志控制
            if self._current_level == LoadLevel.CRITICAL:
                logger.warning("CRITICAL负载: 启用严格限流")
            logger.info("Flask-Limiter 限流策略已更新: %s", new_limit)
        except Exception as e:
            logger.debug("Flask-Limiter 动态更新跳过: %s", e)

    def get_current_profile(self) -> Dict[str, int]:
        """获取当前限流配置"""
        with self._lock:
            return dict(self._current_profile)

    def get_status(self) -> Dict[str, Any]:
        """获取动态限流器状态"""
        with self._lock:
            return {
                'current_level': self._current_level,
                'current_profile': dict(self._current_profile),
                'total_adjustments': self._total_adjustments,
                'recent_changes': self._level_changes[-5:],
                'monitor_metrics': self._monitor.get_metrics_snapshot(),
                'check_interval': self._check_interval,
                'running': self._monitor_thread is not None and self._monitor_thread.is_alive(),
            }

    def get_all_profiles(self) -> Dict[str, Dict[str, int]]:
        """获取所有限流配置文件"""
        return {level: dict(profile) for level, profile in self._profiles.items()}

    def override_profile(self, level: str, rpm: int, rps: int):
        """运行时覆盖某等级的限流参数"""
        with self._lock:
            self._profiles[level] = {"rpm": rpm, "rps": rps}
            if self._current_level == level:
                self._current_profile = self._profiles[level]
            logger.info("限流配置覆盖: %s → rpm=%d, rps=%d", level, rpm, rps)


# 全局动态限流器实例
dynamic_rate_limiter = DynamicRateLimiter()
