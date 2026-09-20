"""
内存使用监控
追踪进程RSS/VMS内存使用，检测内存增长趋势。

使用方式:
    from core.memory_usage_monitor import memory_monitor
    stats = memory_monitor.get_stats()
"""

# ============================================================================
# 接线状态：未接线（WIRED = False）
# ============================================================================
# 本模块在生产代码（run.py / 各业务层 / 其它 core 模块）中**没有任何 import 引用**。
# 模块本身可用，但当前没有调用方 —— 也就是说它宣称的这项能力**当前并未生效**。
#
# 为什么保留而不删除：删掉即丢能力，模块本身有测试价值；这里只把「没接线」显式化、
# 可追踪，避免「代码在库里」被误读成「功能在跑」。
#
# 自动化复核（防止本标注过期）：
#   tests/test_core_regressions.py::test_unwired_marker_matches_reality
#   —— 该用例用 AST 扫描全仓库 import。一旦有人把本模块接进生产代码，
#      而这里仍写着 WIRED = False，用例即失败，强制文档与事实同步。
#
# 接线建议（需改 run.py / 各层，core 内部无权自行接线）：
#     在 run.py 健康检查/看板里暴露 `MemoryUsageMonitor.get_stats()`。
# ============================================================================
WIRED = False


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
        # 采样失败计数：空样本与"采集失败"必须可区分，计数暴露在 get_stats()
        self._sample_errors = 0

        if HAS_PSUTIL:
            try:
                self._process = psutil.Process(os.getpid())
                self._baseline = self._take_sample()
            except Exception as e:
                # 初始化失败后 _process/_baseline 保持为空 → 内存监控整体静默不可用（功能降级）
                logger.warning(f"内存监控初始化失败(将无法采集内存指标): pid={os.getpid()}: {e}")

    def _take_sample(self) -> Dict[str, float]:
        """采集内存样本

        采集失败返回 `{}`，但**必须留痕**：`{}` 与"没有进程对象"返回的空 dict
        完全同形，旧实现只 `return {}`，监控侧只会看到"采样为空"，
        无法区分"还没初始化"和"读内存指标一直失败"。
        """
        if not self._process:
            return {}

        try:
            mem = self._process.memory_info()
            return {
                'rss_mb': mem.rss / 1024 / 1024,
                'vms_mb': mem.vms / 1024 / 1024,
                'timestamp': time.time(),
            }
        except Exception as e:
            self._sample_errors += 1
            logger.warning("内存样本采集失败(累计 %d 次): %s", self._sample_errors, e)
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
            return {'available': False, 'reason': 'psutil not installed',
                    'sample_errors': self._sample_errors}

        current = self._take_sample()
        if not current:
            # 区分"尚未初始化出进程对象"与"采样一直失败"，不要一律报 process not found
            reason = 'process not found' if not self._process else 'sample failed'
            return {'available': False, 'reason': reason,
                    'sample_errors': self._sample_errors}

        with self._lock:
            history = list(self._history)

        result = {
            'available': True,
            'current': {
                'rss_mb': round(current['rss_mb'], 2),
                'vms_mb': round(current['vms_mb'], 2),
            },
            'samples': len(history),
            'sample_errors': self._sample_errors,
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
