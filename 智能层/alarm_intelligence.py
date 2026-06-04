"""
报警智能降噪模块
基于统计分析的报警智能降噪，减少误报和重复报警

功能：
- 报警频率分析
- 报警模式识别
- 智能抑制策略
- 报警优先级调整
"""

import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from collections import defaultdict

logger = logging.getLogger(__name__)


class AlarmPattern:
    """报警模式"""

    def __init__(self, pattern_id: str, device_id: str, register_name: str):
        self.pattern_id = pattern_id
        self.device_id = device_id
        self.register_name = register_name
        self.timestamps: List[float] = []
        self.values: List[float] = []
        self.thresholds: List[float] = []
        self._lock = threading.Lock()

    def add_occurrence(self, timestamp: float, value: float, threshold: float):
        """记录一次报警发生"""
        with self._lock:
            self.timestamps.append(timestamp)
            self.values.append(value)
            self.thresholds.append(threshold)

            # 只保留最近1000条记录
            if len(self.timestamps) > 1000:
                self.timestamps = self.timestamps[-1000:]
                self.values = self.values[-1000:]
                self.thresholds = self.thresholds[-1000:]

    def get_frequency(self, window_seconds: int = 3600) -> float:
        """获取指定时间窗口内的报警频率（次/小时）"""
        with self._lock:
            if not self.timestamps:
                return 0.0

            now = time.time()
            cutoff = now - window_seconds
            recent_count = sum(1 for t in self.timestamps if t > cutoff)

            return recent_count * (3600 / window_seconds)

    def get_pattern_type(self) -> str:
        """识别报警模式类型"""
        with self._lock:
            if len(self.timestamps) < 3:
                return 'unknown'

            # 分析时间间隔
            intervals = []
            for i in range(1, len(self.timestamps)):
                intervals.append(self.timestamps[i] - self.timestamps[i-1])

            if not intervals:
                return 'unknown'

            avg_interval = sum(intervals) / len(intervals)

            # 高频抖动：间隔小于60秒
            if avg_interval < 60:
                return 'chattering'

            # 周期性：间隔标准差很小
            if len(intervals) > 5:
                std_dev = (sum((x - avg_interval) ** 2 for x in intervals) / len(intervals)) ** 0.5
                if std_dev < avg_interval * 0.1:
                    return 'periodic'

            # 持续性：一直报警
            if len(self.timestamps) > 10:
                recent = self.timestamps[-10:]
                if recent[-1] - recent[0] < 600:  # 10分钟内10次
                    return 'persistent'

            return 'normal'


class AlarmNoiseReducer:
    """报警智能降噪器"""

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.patterns: Dict[str, AlarmPattern] = {}
        self._lock = threading.Lock()

        # 降噪配置
        self.chattering_threshold = self.config.get('chattering_threshold', 10)  # 次/小时
        self.suppress_duration = self.config.get('suppress_duration', 300)  # 抑制时长（秒）
        self.pattern_window = self.config.get('pattern_window', 3600)  # 模式分析窗口（秒）

        # 抑制状态
        self._suppressed: Dict[str, float] = {}  # pattern_id -> suppress_until

    def _get_pattern_id(self, device_id: str, register_name: str, rule_id: str) -> str:
        """生成模式ID"""
        return f"{device_id}:{register_name}:{rule_id}"

    def should_emit(self, device_id: str, register_name: str, rule_id: str,
                    value: float, threshold: float) -> tuple[bool, str]:
        """判断是否应该发出报警

        Returns:
            (should_emit, reason)
        """
        pattern_id = self._get_pattern_id(device_id, register_name, rule_id)

        # 获取或创建模式
        with self._lock:
            if pattern_id not in self.patterns:
                self.patterns[pattern_id] = AlarmPattern(pattern_id, device_id, register_name)
            pattern = self.patterns[pattern_id]

        # 记录此次报警
        pattern.add_occurrence(time.time(), value, threshold)

        # 检查是否在抑制期
        with self._lock:
            if pattern_id in self._suppressed:
                if time.time() < self._suppressed[pattern_id]:
                    remaining = self._suppressed[pattern_id] - time.time()
                    return False, f"抑制期剩余{remaining:.0f}秒"
                else:
                    del self._suppressed[pattern_id]

        # 分析模式
        pattern_type = pattern.get_pattern_type()
        frequency = pattern.get_frequency(self.pattern_window)

        # 抖动模式：高频报警，延长抑制
        if pattern_type == 'chattering' or frequency > self.chattering_threshold:
            suppress_until = time.time() + self.suppress_duration * 2
            with self._lock:
                self._suppressed[pattern_id] = suppress_until
            return False, f"抖动模式({frequency:.1f}次/小时)，抑制{self.suppress_duration*2}秒"

        # 周期性模式：记录但降低优先级
        if pattern_type == 'periodic':
            return True, "周期性报警"

        # 持续性模式：首次触发后抑制一段时间
        if pattern_type == 'persistent':
            suppress_until = time.time() + self.suppress_duration
            with self._lock:
                self._suppressed[pattern_id] = suppress_until
            return False, f"持续性模式，抑制{self.suppress_duration}秒"

        # 正常模式：正常发出
        return True, "正常报警"

    def get_alarm_priority(self, device_id: str, register_name: str, rule_id: str,
                          base_priority: int) -> int:
        """获取调整后的报警优先级

        Args:
            base_priority: 基础优先级 (1-10)

        Returns:
            调整后的优先级 (1-10)
        """
        pattern_id = self._get_pattern_id(device_id, register_name, rule_id)

        with self._lock:
            if pattern_id not in self.patterns:
                return base_priority

            pattern = self.patterns[pattern_id]

        pattern_type = pattern.get_pattern_type()
        frequency = pattern.get_frequency(self.pattern_window)

        # 抖动模式：降低优先级
        if pattern_type == 'chattering':
            return max(1, base_priority - 3)

        # 高频模式：降低优先级
        if frequency > self.chattering_threshold / 2:
            return max(1, base_priority - 2)

        # 周期性模式：略微降低
        if pattern_type == 'periodic':
            return max(1, base_priority - 1)

        return base_priority

    def get_statistics(self) -> Dict[str, Any]:
        """获取降噪统计"""
        with self._lock:
            total_patterns = len(self.patterns)
            suppressed_count = len(self._suppressed)

            pattern_types = defaultdict(int)
            for pattern in self.patterns.values():
                pattern_types[pattern.get_pattern_type()] += 1

            return {
                'total_patterns': total_patterns,
                'suppressed_count': suppressed_count,
                'pattern_types': dict(pattern_types),
            }

    def cleanup(self, max_age_seconds: int = 86400):
        """清理过期的模式数据"""
        with self._lock:
            now = time.time()
            expired = []

            for pattern_id, pattern in self.patterns.items():
                if pattern.timestamps and now - pattern.timestamps[-1] > max_age_seconds:
                    expired.append(pattern_id)

            for pattern_id in expired:
                del self.patterns[pattern_id]

            # 清理过期的抑制状态
            expired_suppress = [k for k, v in self._suppressed.items() if now > v]
            for k in expired_suppress:
                del self._suppressed[k]

            if expired:
                logger.info(f"清理了 {len(expired)} 个过期的报警模式")


class AlarmCorrelator:
    """报警关联分析器"""

    def __init__(self):
        self.correlations: Dict[str, List[str]] = {}  # alarm_id -> related_alarm_ids
        self._lock = threading.Lock()

    def record_correlation(self, alarm_id: str, related_alarm_id: str):
        """记录报警关联"""
        with self._lock:
            if alarm_id not in self.correlations:
                self.correlations[alarm_id] = []
            if related_alarm_id not in self.correlations[alarm_id]:
                self.correlations[alarm_id].append(related_alarm_id)

    def get_related_alarms(self, alarm_id: str) -> List[str]:
        """获取关联报警"""
        with self._lock:
            return self.correlations.get(alarm_id, [])

    def find_root_cause(self, alarm_ids: List[str]) -> Optional[str]:
        """尝试找到根因报警

        基于关联分析，找到可能的根因报警
        """
        if not alarm_ids:
            return None

        # 统计每个报警的关联度
        correlation_scores: Dict[str, int] = defaultdict(int)

        for alarm_id in alarm_ids:
            related = self.get_related_alarms(alarm_id)
            for r in related:
                if r in alarm_ids:
                    correlation_scores[alarm_id] += 1

        if not correlation_scores:
            return alarm_ids[0]  # 无法确定，返回第一个

        # 返回关联度最高的报警作为可能的根因
        return max(correlation_scores, key=correlation_scores.get)


class SmartAlarmManager:
    """智能报警管理器"""

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.noise_reducer = AlarmNoiseReducer(config)
        self.correlator = AlarmCorrelator()
        self._cleanup_interval = 3600  # 每小时清理一次
        self._running = False
        self._cleanup_thread: Optional[threading.Thread] = None

    def start(self):
        """启动智能报警管理"""
        self._running = True
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()
        logger.info("智能报警管理器已启动")

    def stop(self):
        """停止智能报警管理"""
        self._running = False
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=5)
        logger.info("智能报警管理器已停止")

    def _cleanup_loop(self):
        """清理循环"""
        while self._running:
            try:
                time.sleep(self._cleanup_interval)
                self.noise_reducer.cleanup()
            except Exception as e:
                logger.error(f"清理循环异常: {e}")

    def should_emit_alarm(self, device_id: str, register_name: str, rule_id: str,
                         value: float, threshold: float) -> tuple[bool, str]:
        """判断是否应该发出报警"""
        return self.noise_reducer.should_emit(
            device_id, register_name, rule_id, value, threshold
        )

    def get_alarm_priority(self, device_id: str, register_name: str, rule_id: str,
                          base_priority: int) -> int:
        """获取调整后的报警优先级"""
        return self.noise_reducer.get_alarm_priority(
            device_id, register_name, rule_id, base_priority
        )

    def record_alarm_correlation(self, alarm_id: str, related_alarm_id: str):
        """记录报警关联"""
        self.correlator.record_correlation(alarm_id, related_alarm_id)

    def get_statistics(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            'noise_reducer': self.noise_reducer.get_statistics(),
            'correlations': len(self.correlator.correlations),
        }
