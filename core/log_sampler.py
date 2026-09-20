"""
日志采样器
对高频日志进行降采样，避免日志风暴淹没重要信息。

使用方式:
    from core.log_sampler import sampled_logger

    # 每100次只记录1次
    sampled_logger.warning("设备断连", sample_rate=100, device_id=device_id)
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
#     在 run.py 的日志初始化处用 `sampled_logger` 包装高频日志点（如设备断连）。
# ============================================================================
WIRED = False


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
        # 每个 key 的"被抑制条数"：必须在**锁内按 key 维护**。
        # 旧实现把 self._suppressed_count 写在实例上（单个标量）、且赋值在 with 块
        # 之外，两个后果：① 并发下 A key 的抑制数会被 B key 覆盖（串台）；
        # ② get_suppressed_count() 读到的是"最后一次任意 key"的值，日志里写的
        # "已抑制N条"和实际被抑制的日志根本不是同一路。现在按 key 分桶。
        self._suppressed_by_key: dict[str, int] = defaultdict(int)
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

            def _flush(suppressed: int) -> bool:
                # 调用方必须持锁进入；重置计数并把本次被抑制的条数记到该 key 名下
                self._counters[key] = 0
                self._last_logged[key] = time.time()
                self._suppressed_by_key[key] = max(0, suppressed - 1)
                return True

            # 达到采样率（含采样率 == 1 已提前返回）：本次要记，其余 count-1 条被抑制
            if count % sample_rate == 0:
                return _flush(count)

            # 超过最小间隔
            last = self._last_logged.get(key, 0)
            if time.time() - last >= self._min_interval:
                return _flush(count)

            return False

    def get_suppressed_count(self, key: str = None) -> int:
        """获取被抑制的日志数量

        Args:
            key: 指定 key 时返回该 key 的抑制数（正确用法）。
                 不传 key 时返回所有 key 的累计值，仅为兼容旧调用方，**不要**
                 用它去标注某一条具体日志的抑制数。

        Returns:
            被抑制条数
        """
        with self._lock:
            if key is not None:
                return self._suppressed_by_key.get(key, 0)
            return sum(self._suppressed_by_key.values())


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
            # 必须按 key 取抑制数：取全局值会在多路日志并发时把别的 key 的抑制数
            # 写进本条消息，运维据此判断"丢了多少"会得到错误结论。
            suppressed = self._sampler.get_suppressed_count(key)
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
            'suppressed_by_key': dict(self._sampler._suppressed_by_key),
            'suppressed_total': self._sampler.get_suppressed_count(),
        }


# 全局实例
sampled_logger = SampledLogger(logger)
