"""
内存使用监控
追踪进程RSS/VMS内存使用，检测内存增长趋势。

使用方式:
    from core.memory_usage_monitor import memory_monitor
    stats = memory_monitor.get_stats()
"""

import os
import time
import logging
import threading
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

# 尝试导入psutil
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


class MemoryUsageMonitor:
    """内存使用监控器"""

    def __init__(self, history_size: int = 360):
        self._history_size = history_size
        self._history: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._process: Optional[psutil.Process] = None
        self._baseline: Optional[Dict[str, float]] = None

        if HAS_PSUTIL:
            try:
                self._process = psutil.Process(os.getpid())
                self._baseline = self._take_sample()
            except Exception:
                pass

    def _take_sample(self) -> Dict[str, float]:
        """采集内存样本"""
        if not self._process:
            return {}

        try:
            mem = self._process.memory_info()
            return {
                'rss_mb': mem.rss / 1024 / 1024,
                'vms_mb': mem.vms / 1024 / 1024,
                'timestamp': time.time(),
            }
        except Exception:
            return {}

    def sample(self):
        """手动采集一次"""
        sample = self._take_sample()
        if sample:
            with self._lock:
                self._history.append(sample)
                if len(self._history) > self._history_size:
                    self._history.pop(0)

    def get_stats(self) -> Dict[str, Any]:
        """获取内存统计"""
        if not HAS_PSUTIL:
            return {'available': False, 'reason': 'psutil not installed'}

        current = self._take_sample()
        if not current:
            return {'available': False, 'reason': 'process not found'}

        with self._lock:
            history = list(self._history)

        result = {
            'available': True,
            'current': {
                'rss_mb': round(current['rss_mb'], 2),
                'vms_mb': round(current['vms_mb'], 2),
            },
            'samples': len(history),
        }

        if self._baseline:
            result['baseline'] = {
                'rss_mb': round(self._baseline['rss_mb'], 2),
                'vms_mb': round(self._baseline['vms_mb'], 2),
            }
            result['growth'] = {
                'rss_mb': round(current['rss_mb'] - self._baseline['rss_mb'], 2),
                'vms_mb': round(current['vms_mb'] - self._baseline['vms_mb'], 2),
            }

        if history:
            rss_values = [s['rss_mb'] for s in history]
            result['min_rss_mb'] = round(min(rss_values), 2)
            result['max_rss_mb'] = round(max(rss_values), 2)
            result['avg_rss_mb'] = round(sum(rss_values) / len(rss_values), 2)

        return result

    def get_trend(self, minutes: int = 10) -> str:
        """获取内存趋势（增长/稳定/下降）"""
        with self._lock:
            history = list(self._history)

        if len(history) < 2:
            return 'unknown'

        cutoff = time.time() - minutes * 60
        recent = [s for s in history if s['timestamp'] >= cutoff]
        if len(recent) < 2:
            return 'unknown'

        first_half = recent[:len(recent) // 2]
        second_half = recent[len(recent) // 2:]

        avg_first = sum(s['rss_mb'] for s in first_half) / len(first_half)
        avg_second = sum(s['rss_mb'] for s in second_half) / len(second_half)

        diff_pct = (avg_second - avg_first) / max(0.1, avg_first) * 100

        if diff_pct > 5:
            return 'growing'
        elif diff_pct < -5:
            return 'shrinking'
        return 'stable'


# 全局实例
memory_monitor = MemoryUsageMonitor()
