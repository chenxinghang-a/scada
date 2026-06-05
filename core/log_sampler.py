"""
日志采样器
对高频日志进行降采样，避免日志风暴淹没重要信息。

使用方式:
    from core.log_sampler import sampled_logger

    # 每100次只记录1次
    sampled_logger.warning("设备断连", sample_rate=100, device_id=device_id)
"""

import time
import logging
import threading
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)


class LogSampler:
    """日志采样器"""

    def __init__(self):
        # 计数器: {key: count}
        self._counters: dict[str, int] = defaultdict(int)
        # 上次记录时间: {key: timestamp}
        self._last_logged: dict[str, float] = {}
        # 锁
        self._lock = threading.Lock()
        # 最小间隔（秒）：即使未达到采样率，也至少每30秒记录一次
        self._min_interval = 30.0

    def should_log(self, key: str, sample_rate: int = 100) -> bool:
        """
        判断是否应该记录日志

        Args:
            key: 日志标识（通常用消息模板或调用位置）
            sample_rate: 采样率（每N次记录1次）

        Returns:
            是否应该记录
        """
        if sample_rate <= 1:
            return True

        with self._lock:
            self._counters[key] += 1
            count = self._counters[key]

            # 达到采样率
            if count % sample_rate == 0:
                self._counters[key] = 0
                self._last_logged[key] = time.time()
                return True

            # 超过最小间隔
            last = self._last_logged.get(key, 0)
            if time.time() - last >= self._min_interval:
                suppressed = count
                self._counters[key] = 0
                self._last_logged[key] = time.time()
                # 在消息中附加被抑制的数量
                self._suppressed_count = suppressed
                return True

            return False

    def get_suppressed_count(self) -> int:
        """获取被抑制的日志数量"""
        return getattr(self, '_suppressed_count', 0)


class SampledLogger:
    """
    采样日志包装器

    使用方式:
        slog = SampledLogger(logger)
        slog.warning("设备断连: %s", device_id, sample_rate=100)
    """

    def __init__(self, logger: logging.Logger):
        self._logger = logger
        self._sampler = LogSampler()

    def _log(self, level: int, msg: str, sample_rate: int, *args, **kwargs):
        key = f"{level}:{msg}"
        if self._sampler.should_log(key, sample_rate):
            suppressed = self._sampler.get_suppressed_count()
            if suppressed > 0:
                msg = f"{msg} (已抑制{suppressed}条同类日志)"
            self._logger.log(level, msg, *args, **kwargs)

    def debug(self, msg: str, *args, sample_rate: int = 1, **kwargs):
        self._log(logging.DEBUG, msg, sample_rate, *args, **kwargs)

    def info(self, msg: str, *args, sample_rate: int = 1, **kwargs):
        self._log(logging.INFO, msg, sample_rate, *args, **kwargs)

    def warning(self, msg: str, *args, sample_rate: int = 10, **kwargs):
        self._log(logging.WARNING, msg, sample_rate, *args, **kwargs)

    def error(self, msg: str, *args, sample_rate: int = 1, **kwargs):
        self._log(logging.ERROR, msg, sample_rate, *args, **kwargs)

    def get_stats(self) -> dict:
        """获取采样统计"""
        return {
            'counters': dict(self._sampler._counters),
            'active_keys': len(self._sampler._counters),
        }


# 全局实例
sampled_logger = SampledLogger(logger)
