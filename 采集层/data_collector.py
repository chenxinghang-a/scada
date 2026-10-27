"""
数据采集器模块
实现定时数据采集和数据处理
支持协议: Modbus TCP/RTU, OPC UA, MQTT, REST HTTP
"""

import json
import math
import os
import time
import queue
import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime, timedelta
from queue import Queue
from concurrent.futures import ThreadPoolExecutor

# paths 是仓库根模块。用别名 _paths 避免与局部变量 / 参数名冲突。
import paths as _paths
from core.safe_numeric import try_finite_float
from core.degradation_manager import degradation_manager

logger = logging.getLogger(__name__)

# 智能层模块 → 降级管理器里的功能名。
# 降级后这些模块**不再收数据**（这是降级唯一能真正省 CPU 的地方）。
# 右侧的名字必须出现在 core/degradation_manager.DISABLED_FEATURES 里，
# 否则降级永远关不掉它（tests/test_degradation_enforcement.py 会核对）。
#
# 注意：`device_control`（安全联锁）**故意不在表里** —— 联锁在任何降级等级
# 下都必须继续工作，EMERGENCY 的语义是「仅保留报警与联锁」，不是「什么都停」。
_INTEL_FEATURE_NAMES = {
    'predictive_maintenance': 'predictive_maintenance',
    'spc_analyzer': 'spc_analyzer',
    'oee_calculator': 'oee_calculator',
    'energy_manager': 'energy_optimizer',
    'edge_decision': 'edge_decision',
    'vibration_analyzer': 'vibration_analyzer',
}


def _normalize_timestamp(ts):
    """确保timestamp是datetime对象（DiskBackedQueue恢复时可能是字符串）"""
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return datetime.now()
    if isinstance(ts, datetime):
        return ts
    return datetime.now()


# --- 智能分发关键字匹配 ---
_power_kw = ('power', 'watt', 'kw', 'kwh', 'active_power', 'reactive_power', 'apparent_power')
_energy_kw = ('energy', 'kwh', 'consumption', 'electricity', 'water_flow', 'gas_flow')
_spc_kw = ('temperature', 'pressure', 'humidity', 'viscosity', 'ph', 'concentration')
_status_kw = ('status', 'state', 'running_status', 'boiler_status', 'packing_status', 'line_status', 'ahu_status', 'compressor_status', 'robot_status', 'welder_status', 'ups_status', 'cems_status')
_count_kw = ('_count', 'quantity', 'output', 'production', 'yield', 'shot_count', 'painted_count', 'label_count', 'palletizing_count', 'reject_count', 'weld_count', 'part_count', 'inbound_count', 'outbound_count')

# --- 命名常量（替代魔法数字） ---
BACKOFF_CEILING_S = 60              # 退避上限
#: 崩溃恢复清单（`pending_data.jsonl` + `_pending`）的硬上限。
#: 消费端停摆时如果不管，这个文件会一直涨到把磁盘写满（2026-09-20 实测 160MB）。
PENDING_COMPACT_EVERY = 1000        # 累计淘汰多少条后真正重写一次文件（摊薄 O(n) 重写）
#: 调度最小延迟：即使本轮采集已经超时，也不要 0 延迟忙等
SCHEDULE_MIN_DELAY_S = 0.5
#: 启动抖动的上限（秒）：25 台设备同时启动时把相位打散，避免同一秒一起打网关
STARTUP_JITTER_MAX_S = 5.0


def compute_next_delay(interval: float, elapsed: float = 0.0,
                       min_delay: float = SCHEDULE_MIN_DELAY_S) -> float:
    """算下一次采集该等多久（**含漂移补偿**）。

    round 272：原实现是 `threading.Timer(interval, ...)` 固定等 interval ——
    但本轮采集本身还要花时间（连设备、读寄存器、写队列），于是**实际周期
    = interval + 采集耗时**。设备慢一点（2s 采集 / 5s 周期）实际就是 7s，
    长期采样率比配置低 30%，历史曲线的间隔与配置不符，基于采样数的 KPI 也跟着偏。

    补偿后长期平均周期 ≈ interval：本轮花了多少，下次就少等多少。

    Args:
        interval: 目标周期（秒）
        elapsed: 本轮已经花掉的时间（秒）
        min_delay: 最小延迟（防止超时后 0 延迟忙等）

    Returns:
        下次调度延迟（秒），恒 >= `min_delay`。
    """
    # 用 safe_numeric 而不是裸 float()：`float('inf')` 不抛异常，
    # 会让"周期无穷大"静默通过（闸门 test_non_finite_input_guard 会拦）。
    from core.safe_numeric import try_finite_float

    period = try_finite_float(interval)
    if period is None or period <= 0:
        period = 5.0
    used = try_finite_float(elapsed)
    if used is None:
        used = 0.0
    used = max(0.0, used)
    floor = try_finite_float(min_delay)
    if floor is None or floor <= 0:
        floor = float(SCHEDULE_MIN_DELAY_S)
    return max(period - used, floor)


def startup_jitter(interval: float, rng=None) -> float:
    """首次调度的随机延迟：把各设备的相位打散。

    原实现所有设备都是 `Timer(0.1, ...)` —— 启动后 0.1 秒**一起**开始，
    之后每 5 秒**一起**打网关和数据库：25 台设备形成脉冲式负载，
    峰值是平均值的 25 倍，而低谷完全空闲。加 0~min(interval, 5s) 的随机
    偏移后，负载被摊平到整个周期里。
    """
    import random as _random

    from core.safe_numeric import try_finite_float

    period = try_finite_float(interval)
    if period is None or period <= 0:
        period = 5.0
    upper = min(max(period, 0.1), STARTUP_JITTER_MAX_S)
    generator = rng or _random
    return generator.uniform(0.0, upper)


def compute_backoff_interval(failures: int, base_interval: float) -> float:
    """连续失败 `failures` 次后，下一次采集该等多久（秒）。

    round 264：原实现是 `min(2 ** failures, BACKOFF_CEILING_S)` —— 第 1 次失败
    得到 **2 秒**，比正常采集间隔（默认 5 秒）**还短**：所谓"退避"反而让一台
    已经出问题的设备被更频繁地打。正确语义是：

    * 退避间隔**不得小于基础采集间隔**（不能比正常时更激进）；
    * 在基础间隔之上按 2^n 指数增长；
    * 封顶 `BACKOFF_CEILING_S`。

    例（base=5）：1 次 → 5s，2 次 → 5s，3 次 → 8s，4 次 → 16s，5 次 → 32s。
    """
    from core.safe_numeric import try_int

    parsed = try_int(failures)
    n = 1 if parsed is None else max(1, parsed)
    # `collection.default_interval: .inf` 是 YAML 合法字面量 → 基础间隔无穷大 →
    # 退避也无穷大 → 设备失败一次就再也不采了。统一用 safe_numeric 挡住。
    base = try_finite_float(base_interval)
    if base is None or base <= 0:
        base = 5.0
    return float(min(max(base, 2 ** n), BACKOFF_CEILING_S))
SENSOR_STUCK_WINDOW_S = 300         # 传感器卡死窗口
BATCH_MAX = 1000                    # 批处理最大条数（100设备场景下提高到1000）
BATCH_TIMEOUT_S = 0.5               # 批处理超时
CIRCUIT_BREAKER_THRESHOLD = 5       # 断路器：连续失败阈值（5次即可判定设备不可达）
CIRCUIT_BREAKER_COOLDOWN_S = 30     # 断路器：冷却时间（30秒后试探恢复，满足99.97%可用性）
# 故障降级：采集失败时是否生成模拟数据兜底。
# 默认关闭 —— 审计要求「失效数据必须标记为非 GOOD，假成功 = 0」，
# 伪造的随机数据绝不能被当作正常数据入库。需显式开启（如离线演示场景）
# 才允许生成兜底数据，且即便开启，其质量码也会被标为 BAD 并明确告警。
FALLBACK_SIMULATION_ENABLED = False

#: 「已取消设备」登记的最短保留时长（秒，**单调时钟**）。
#:
#: 为什么需要保留期而不是删完就扔：`remove_device_task()` 只能 cancel 还没触发的
#: Timer；正在线程池里跑的那一轮（最长 = 单次采集超时）结束后仍会走
#: `_schedule_next()`，必须让它看见「已取消」才不复活。所以登记不能删得太早。
#:
#: 为什么必须有上限（round 289）：这个集合此前**只在 `start()` 清空**，运行期
#: 每删一台设备（含 `sync_device_tasks` 因配置热重载停掉的）就 `.add()` 一个 id，
#: 永不移除 —— 长时间运行 + 频繁增删设备就是无界增长。单条 id 很小，但它同时
#: 是「幽灵数据」的判据（推送型设备的残留回调靠它拦截），无界增长意味着
#: 这个判据表永远只增不减。
#:
#: 取 300s：远大于任何一次在途采集（Modbus 1s × 寄存器轮次、REST 十几秒），
#: 又远小于「一天」这种运行尺度。裁剪条件见 `_prune_cancelled()`。
CANCELLED_RETENTION_S = 300.0


def _has_keyword(register_name: str, keywords: tuple) -> bool:
    """检查寄存器名是否包含指定关键字"""
    name_lower = register_name.lower()
    return any(kw in name_lower for kw in keywords)


def _read_is_stale(client) -> bool:
    """本次读取是否由客户端的陈旧缓存兜底返回。

    ``ModbusClient`` 在读取失败时会返回缓存中的最后已知良好值，此时会把
    ``last_read_source`` 置为 ``'cache'``。返回值本身无法区分新鲜数据与
    陈旧数据，所以采集侧必须显式查询该标记，并把数据质量码降级为
    ``UNCERTAIN_LAST_USABLE``。

    不支持该属性的客户端（如模拟客户端）视为新鲜数据。
    """
    return getattr(client, 'last_read_source', 'fresh') == 'cache'


def _stale_value_timestamp(client, address: int, count: int,
                           fallback: datetime) -> datetime:
    """陈旧缓存值**原本**的采集时刻。

    读取失败时 ``ModbusClient`` 会返回最后已知良好值，该值可能是几秒前、
    也可能是几天前的。原实现给这类值打上 ``now()`` 的时间戳入库，操作员
    会把几天前的旧值当成实时数据。这里用 ``get_cache_age()`` 反推真实时刻。

    拿不到缓存年龄（客户端不支持 / 该地址从未成功读过）时退回 ``fallback``。
    """
    getter = getattr(client, 'get_cache_age', None)
    if getter is None:
        return fallback
    try:
        age = getter(address, count)
    except Exception as e:
        # 缓存年龄查询失败不该影响采集主流程，退回本次时间戳并留痕
        logger.debug(f"查询缓存年龄失败 ({address},{count}): {e}")
        return fallback
    if age is None:
        return fallback
    seconds = try_finite_float(age)
    if seconds is None:
        return fallback
    return fallback - timedelta(seconds=seconds)


class DataQualityAssessor:
    """数据质量评估器 - OPC UA质量码"""

    # 质量码定义 (OPC UA标准)
    GOOD = 192           # 0xC0 - 好
    UNCERTAIN = 104      # 0x68 - 不确定
    BAD = 0              # 0x00 - 坏
    BAD_SENSOR_FAILURE = 4      # 传感器故障
    BAD_COMM_FAILURE = 6        # 通信故障
    BAD_OUT_OF_SERVICE = 8      # 停用
    UNCERTAIN_SENSOR_CAL = 80   # 传感器需要校准
    UNCERTAIN_LAST_USABLE = 64  # 最后可用值

    @staticmethod
    def assess(value: float, register_name: str, device_status: str,
               last_value: float = None, last_time: float = None) -> int:
        """评估数据质量

        Args:
            value: 数据值
            register_name: 寄存器名称
            device_status: 设备状态 ('offline', 'disconnected', 'fault', etc.)
            last_value: 上一次的值（用于检测传感器卡死）
            last_time: 上一次的时间戳（用于检测传感器卡死）

        Returns:
            OPC UA质量码 (int)
        """

        # 设备断开 = BAD_COMM_FAILURE
        if device_status in ('offline', 'disconnected'):
            return DataQualityAssessor.BAD_COMM_FAILURE

        # 设备故障 = BAD_SENSOR_FAILURE
        if device_status == 'fault':
            return DataQualityAssessor.BAD_SENSOR_FAILURE

        # 值为None/NaN = BAD
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return DataQualityAssessor.BAD

        # 值超出合理范围 = BAD_SENSOR_FAILURE
        if isinstance(value, (int, float)):
            if abs(value) > 1e10:  # 超大值可能是传感器故障
                return DataQualityAssessor.BAD_SENSOR_FAILURE

        # 值长时间不变 = UNCERTAIN（可能传感器卡死）
        if last_value is not None and last_time is not None:
            # round 264：卡死窗口是时长 → 单调时钟（墙钟回拨会让卡死永远检测不出来）
            if abs(value - last_value) < 0.001 and time.monotonic() - last_time > SENSOR_STUCK_WINDOW_S:
                return DataQualityAssessor.UNCERTAIN_LAST_USABLE

        # 正常
        return DataQualityAssessor.GOOD


class DiskBackedQueue:
    """磁盘持久化队列 - 崩溃恢复

    持久化目录可用 ``persist_dir`` 显式指定，或用环境变量
    ``SCADA_QUEUE_PERSIST_DIR`` 覆盖（测试会话用它指向临时目录，
    避免多个测试共用同一个 pending_data.jsonl 造成相互污染）。
    """

    DEFAULT_PERSIST_DIR = 'data/queue'

    def __init__(self, maxsize: int = 50000, persist_dir: str | None = None,
                 pending_cap: int | None = None):
        self.maxsize = maxsize
        self._queue = queue.Queue(maxsize=maxsize)
        if persist_dir is None:
            persist_dir = os.environ.get(
                'SCADA_QUEUE_PERSIST_DIR', self.DEFAULT_PERSIST_DIR
            )
        # 绝对路径化：默认值 'data/queue' 是相对路径，从服务 / 计划任务 /
        # 冻结产物启动（CWD 不是项目根）时，会在启动目录下**悄悄另建一个
        # data/queue**，于是"待发数据"分裂成两处 —— 表现为重启后
        # pending_data.jsonl 莫名其妙为空、数据对不上，日志里毫无异常。
        # 环境变量 SCADA_QUEUE_PERSIST_DIR 若已是绝对路径则原样返回
        # （paths.resolve 对绝对路径是 passthrough），测试传 tmp 目录不受影响。
        self._persist_dir = _paths.resolve(persist_dir)
        self._persist_dir.mkdir(parents=True, exist_ok=True)
        self._persist_file = self._persist_dir / 'pending_data.jsonl'
        self._lock = threading.Lock()
        # 与磁盘文件内容一一对应的「待恢复」清单（顺序 = 文件行顺序）。
        #
        # 存在的理由：确认入库时必须只移除**本批**对应的记录，而不是清空整个文件。
        # 无脑清空会有一个真实的丢数据窗口 —— 消费线程取走本批之后、清空文件之前，
        # 采集线程可能刚 `put()` 进新数据：新数据已经落盘，但不在本批里，
        # 于是它的磁盘副本被这次清空抹掉，而它还在内存队列里等下一批。
        # 此刻进程崩溃 → 这条数据在内存与磁盘上**同时消失**。
        self._pending: list[Dict[str, Any]] = []
        self._pending_ids: set[int] = set()
        # 落盘失败计数：>0 表示「崩溃保护已失效」（数据只在内存里）。
        # 必须可观测 —— 见 _persist_item 的说明。
        self._persist_failures = 0
        #: 崩溃恢复清单的硬上限。**默认比内存队列大**：
        #: 内存队列是"待写库的缓冲"，磁盘文件是"崩溃后还能救回来的底账"，
        #: 后者本来就该容纳更多。取 `max(maxsize, 1000)` 保证小容量场景
        #: （测试里的 maxsize=1）不会一落盘就被自己的上限淘汰。
        self._pending_cap = int(pending_cap) if pending_cap else max(int(maxsize), 1000)
        #: 因为"清单满了"而被淘汰的条数（这些数据彻底丢失，必须可观测）
        self._dropped_persisted = 0
        #: 自上次重写文件以来淘汰/丢弃了多少条（用于摊薄重写成本）
        self._dirty_since_rewrite = 0
        # 启动时恢复
        self._recover_from_disk()

    def put(self, item: Dict[str, Any], block: bool = True, timeout: Optional[float] = None) -> None:
        """入队（先持久化再入队，崩溃时不丢数据）"""
        self._persist_item(item)
        self._queue.put(item, block=block, timeout=timeout)

    def get(self, block: bool = True, timeout: Optional[float] = None) -> Dict[str, Any]:
        """出队"""
        return self._queue.get(block=block, timeout=timeout)

    def get_nowait(self) -> Dict[str, Any]:
        """非阻塞出队"""
        return self._queue.get_nowait()

    def put_nowait(self, item: Dict[str, Any]) -> None:
        """非阻塞入队"""
        self._queue.put_nowait(item)
        self._persist_item(item)

    def qsize(self) -> int:
        return self._queue.qsize()

    def empty(self) -> bool:
        return self._queue.empty()

    def full(self) -> bool:
        return self._queue.full()

    def _persist_item(self, item: Dict[str, Any]) -> bool:
        """持久化单条数据（幂等）。返回**是否已确认落盘**。

        幂等的必要性：写库失败后 `_process_data` 会把整批 `put_nowait()` 重新入队。
        若不去重，文件里会留下同一批数据的**第二份**，崩溃恢复时就会重复入库
        （history_data 出现重复行，聚合/报表随之偏大）。同一条数据的对象身份
        在 `_pending` 里已经存在时直接跳过。

        ⚠️ 落盘失败**必须可见**（2026-10-13 修）：
        历史实现只打 `logger.debug`（默认 INFO 级别下完全隐形），也不计数。
        后果是磁盘满 / 权限错误 / 只读挂载时，每一条数据的**崩溃保护静默消失**
        —— 数据只在内存队列里，进程一崩就真丢，而日志上什么也看不到。
        现在：返回 False + 计数（`persist_failures`，经 `DataCollector.get_stats()`
        暴露给 /api/system/status 与健康检查）+ ERROR 日志（按 1/每100 条限流，
        避免磁盘满时刷爆日志把别的线索淹掉）。
        """
        try:
            with self._lock:
                if id(item) in self._pending_ids:
                    return True
                # round 265：清单有硬上限。消费端停摆时不清的话，
                # `pending_data.jsonl` 会一直涨到把磁盘写满（2026-09-20 实测 160MB），
                # 而磁盘满之后连数据库都写不进去 —— 从"数据积压"升级成"整机不可用"。
                trimmed = self._trim_if_over_cap()
                with open(self._persist_file, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(item, default=str, ensure_ascii=False) + '\n')
                self._pending.append(item)
                self._pending_ids.add(id(item))
                if trimmed or self._dirty_since_rewrite >= PENDING_COMPACT_EVERY:
                    # 淘汰只发生在内存清单里，磁盘文件要靠重写才会变小
                    self._rewrite_persist_file()
                    self._dirty_since_rewrite = 0
                return True
        except Exception as e:
            with self._lock:
                self._persist_failures += 1
                n = self._persist_failures
            if n == 1 or n % 100 == 0:
                logger.error(
                    "数据持久化失败（累计 %d 条未落盘，目录 %s）：%s —— "
                    "这些数据在进程崩溃时会丢失，请检查磁盘空间/权限",
                    n, self._persist_dir, e,
                )
            return False

    def _trim_if_over_cap(self) -> bool:
        """清单超上限时淘汰最旧的一批（调用方须持有 `_lock`）。返回是否发生了淘汰。"""
        if len(self._pending) < self._pending_cap:
            return False
        trim = max(1, self._pending_cap // 10)      # 一次淘汰 10%，避免每条都重写
        dropped = self._pending[:trim]
        self._pending[:] = self._pending[trim:]
        self._pending_ids = {id(x) for x in self._pending}
        self._dropped_persisted += len(dropped)
        self._dirty_since_rewrite += len(dropped)
        if self._dropped_persisted == len(dropped) or self._dropped_persisted % 1000 < len(dropped):
            logger.error(
                "崩溃恢复清单已满（上限 %d 条），淘汰最旧 %d 条 —— "
                "这些数据永久丢失。累计淘汰 %d 条。请检查数据库写入是否停滞。",
                self._pending_cap, len(dropped), self._dropped_persisted)
        return True

    @property
    def pending_count(self) -> int:
        """当前崩溃恢复清单条数（>上限 说明淘汰逻辑没生效）。"""
        with self._lock:
            return len(self._pending)

    @property
    def dropped_persisted(self) -> int:
        """因清单满而被淘汰、永久丢失的条数。"""
        return self._dropped_persisted

    @property
    def persist_failures(self) -> int:
        """累计落盘失败条数（>0 说明崩溃保护已失效，数据只在内存里）。"""
        return self._persist_failures

    def _recover_from_disk(self) -> None:
        """从磁盘恢复未处理的数据。

        与旧实现的区别：**恢复后不再清空文件**。

        旧实现是「恢复进内存 → 立刻把磁盘副本删掉」，那等于把数据的唯一崩溃保护
        提前交出去：恢复完还没落库就再崩一次，这批数据同样没了。
        现在文件里的记录只会在**确认写库成功后**被逐批移除（见 clear_persistence），
        所以恢复出来的数据在落库之前始终有磁盘副本兜底。
        代价是「至少一次」语义：写库成功与移除记录之间若崩溃，重启会重复入库一条。
        """
        if not self._persist_file.exists():
            return

        recovered = 0
        queue_full = False
        invalid = 0
        try:
            with open(self._persist_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError as e:
                        # 安全忽略并跳过该行：崩溃时可能留下半行不完整 JSON，
                        # 只丢这一条，不影响后续行恢复
                        logger.debug(f"磁盘恢复: 跳过损坏记录行: {e} | {line[:200]}")
                        continue

                    if item.get('value') is None:
                        # 这条记录永远无法入库，不进待恢复清单 —— 免得它被反复
                        # 重写回文件、每次启动都刷同一条告警
                        invalid += 1
                        logger.warning(f"磁盘恢复: 跳过无value字段的记录 keys={list(item.keys())}")
                        continue

                    # 该行有效 → 纳入待恢复清单，与文件内容保持一一对应
                    with self._lock:
                        if id(item) not in self._pending_ids:
                            self._pending.append(item)
                            self._pending_ids.add(id(item))

                    if queue_full:
                        continue
                    try:
                        self._queue.put_nowait(item)
                        recovered += 1
                    except queue.Full:
                        # 队列满 → 剩余记录留在文件里，下次启动继续恢复。
                        # 这里**继续读**而不是 break：要让 _pending 与文件行数
                        # 保持一致，否则后续按批移除时会算错行。
                        queue_full = True
        except Exception as e:
            logger.warning(f"磁盘恢复失败: {e}")

        if recovered > 0:
            logger.info(
                f"从磁盘恢复 {recovered} 条未处理数据（已落盘待入库，"
                f"写库成功后才会从文件移除）"
                + ("；队列已满，剩余记录留待下次启动" if queue_full else "")
            )
        if invalid:
            logger.warning(f"磁盘恢复: 丢弃 {invalid} 条结构无效的记录（无 value 字段）")

    def _rewrite_persist_file(self) -> None:
        """把持久化文件重写为当前 ``_pending`` 的内容（调用方必须已持有 ``_lock``）。

        注意：**刻意不使用 ``unlink()`` 删除文件**。

        原因（2026-09-20 实机排查）：消费线程每落库一批就确认一次，
        即每秒数次 unlink。当运行环境存在「同一轮内批量删除保护」时
        （工作台/安全策略会对删除调用计数，超过阈值即拦截），
        unlink 累积到阈值后会被挂起 —— 表现为：

          - 采集线程照常运行（last_collection_time 持续更新）
          - 消费线程卡住，**不再落库**（history_data 长时间不增长）
          - 队列文件持续膨胀（无人清理）
          - **日志完全静默**（线程被挂起，既没抛异常也没有任何输出）

        改成「截断 + 重写」后：文件本体保留、内容与待恢复清单一致，
        恢复语义等价，且不会触发删除保护。
        """
        with open(self._persist_file, 'w', encoding='utf-8') as f:
            for item in self._pending:
                # `_db_retry` 是本次进程内的重试标记，不该持久化：
                # 否则崩溃恢复出来的数据带着它，第一次写库失败就被直接丢弃
                # （等于把「还能再试一次」的机会提前用掉）。
                payload = {k: v for k, v in item.items() if k != '_db_retry'}
                f.write(json.dumps(payload, default=str, ensure_ascii=False) + '\n')

    def clear_persistence(self, flushed_items: Optional[list] = None) -> None:
        """确认已入库：把这些记录从持久化文件里移除。

        - ``flushed_items`` 传具体列表 → 只移除这批（消费线程每批落库成功后调用）
        - ``flushed_items`` 为 None → 全部确认（仅用于 ``stop()`` 收尾：
          此时生产者线程已停、队列已排空）

        **为什么不能无脑清空整个文件**：消费线程取走本批之后、清空文件之前，
        采集线程可能刚 ``put()`` 进新数据。新数据已经落盘、但不在本批里，
        无脑清空会把它的磁盘副本一起抹掉 —— 而它还在内存队列里等下一批，
        此刻进程崩溃，这条数据在内存与磁盘上同时消失。按批移除就没有这个窗口。
        """
        try:
            with self._lock:
                if flushed_items is None:
                    self._pending.clear()
                    self._pending_ids.clear()
                else:
                    acked = {id(it) for it in flushed_items}
                    if not acked:
                        return
                    self._pending[:] = [p for p in self._pending if id(p) not in acked]
                    self._pending_ids.difference_update(acked)
                self._rewrite_persist_file()
        except Exception as e:
            # 移除失败 → 文件残留，下次启动会重复恢复这批已处理的数据
            logger.warning(
                f"清空持久化文件失败，下次启动可能重复恢复数据 "
                f"{self._persist_file}: {e}"
            )


class DataCollector:
    """
    数据采集器
    统一定时从多协议设备采集数据（Modbus/OPC UA/MQTT/REST）
    OPC UA和MQTT有自带的推送机制，仅对Modbus/REST做轮询
    """

    def __init__(self, device_manager, database, alarm_manager=None,
                 predictive_maintenance=None, oee_calculator=None,
                 spc_analyzer=None, energy_manager=None, edge_decision=None,
                 device_control=None, realtime_bridge=None, vibration_analyzer=None):
        self.device_manager = device_manager
        self.database = database
        self.alarm_manager = alarm_manager

        # 工业4.0智能层模块（可选注入）
        self.predictive_maintenance = predictive_maintenance
        self.oee_calculator = oee_calculator
        self.spc_analyzer = spc_analyzer
        self.energy_manager = energy_manager
        self.edge_decision = edge_decision
        self.device_control = device_control
        self.vibration_analyzer = vibration_analyzer

        # TDengine实时数据桥接器（可选）
        self.realtime_bridge = realtime_bridge

        # 采集任务（线程安全）
        self._tasks_lock = threading.Lock()
        self.tasks = {}  # device_id -> threading.Timer
        # 已取消的设备：删除设备任务时登记，调度时据此**拒绝复活**。
        #
        # 为什么必须有这个集合（2026-10-20 实测的竞态）：
        #   `remove_device_task` 只能 cancel **还没触发**的 Timer。若删除发生时
        #   该设备的 `collect_task` 正在线程池里跑（最长 30s），它结束后仍会走
        #   `_schedule_next()` 新建一个 Timer 并写回 `self.tasks[device_id]` ——
        #   于是出现两个后果：
        #     ① 僵尸任务：设备已删，采集器仍按间隔轮询它，`failed_collections`
        #        每轮 +1，健康指标被一个不存在的设备污染；
        #     ② 更隐蔽：`start_device_task` 看到 `device_id in self.tasks` 就
        #        「已有任务，跳过」→ **删掉再重新添加同一台设备，采集永远不启动**，
        #        必须重启进程才恢复。
        self._cancelled_devices: set[str] = set()
        # 登记时刻（单调时钟），供 `_prune_cancelled()` 判过期。
        # 与 `_cancelled_devices` 同生同灭：mark 时写入，discard/clear/prune 时删除。
        self._cancelled_at: dict[str, float] = {}
        self.running = False

        # 失败计数器（用于退避）
        self._failure_counts = {}  # device_id -> consecutive_failures

        # 断路器状态（防止对已知死设备无限轮询）
        self._circuit_state = {}  # device_id -> {'opened_at': float}

        # 数据质量跟踪（用于检测传感器卡死等）
        self._last_values = {}   # "device_id:register_name" -> last_value
        self._last_times = {}   # "device_id:register_name" -> last_time

        # 共享字典锁（_failure_counts/_circuit_state/_last_values/_last_times 多线程访问）
        self._tracking_lock = threading.Lock()

        # 线程池（限制并发采集连接数，支持100+设备）
        #
        # ⚠️ 上限来自 `配置/system.yaml` 的 `collection.max_concurrent`（round 210）。
        #    此前是**硬编码 20**，而配置里写的是 **10** —— 两边对不上，且配置那侧
        #    **零读取点**，所以一直没人发现。现在这个旋钮真的生效了。
        #    `load_collection_settings()` 读不到配置时回退 `COLLECTION_DEFAULTS`
        #    （其值必须与配置文件一致，有守卫钉着）。
        from core.config_manager import load_collection_settings
        _coll = load_collection_settings()
        self._max_concurrent = _coll['max_concurrent']
        #: 设备配置**没写** `collection_interval` 时的兜底间隔（秒）。
        #: 此前是 `device_config.get('collection_interval', 5)` 的硬编码 5。
        self._default_interval = _coll['default_interval']
        self._pool = ThreadPoolExecutor(max_workers=self._max_concurrent,
                                        thread_name_prefix="collector")

        # 数据队列（磁盘持久化，崩溃恢复）
        #
        # 这里**刻意不传 persist_dir**，让 DiskBackedQueue 走
        # 「环境变量 SCADA_QUEUE_PERSIST_DIR → 默认 'data/queue'（经 paths.resolve
        # 解析成仓库根下的绝对路径）」这条链。生产环境这条链是对的：
        # 无论从哪个 CWD 启动，落点都是固定的数据目录。
        #
        # ⚠️ 对测试的影响（踩过一次，记在这免得再踩）：
        # 凡是构造 DataCollector 的测试，**必须**由 autouse fixture
        # `tests/conftest.py::isolate_queue_persistence` 把环境变量指向 tmp。
        # 万一漏了，落点就是仓库真实的 `data/queue/pending_data.jsonl`：
        #   - 构造时 `_recover_from_disk()` 若发现该文件非空，会 read + put_nowait，
        #     然后 **unlink()** → 直接删掉生产数据文件
        #   - put 数据后 `_persist_item()` 会往生产文件追加
        # 症状是一批与队列无关的测试集体飘红且单跑变绿。
        # conftest 里另有 `guard_repo_queue_persist_dir` 会话级哨兵兜底。
        self.data_queue = DiskBackedQueue(maxsize=200000)

        # 动态采集频率配置
        self._dynamic_interval_config = {
            'fault': 1,        # 故障状态：1秒
            'warning': 2,      # 警告状态：2秒
            'running': 5,      # 正常运行：5秒
            'idle': 10,        # 空闲状态：10秒
            'stopped': 30,     # 停机状态：30秒
        }
        self._device_intervals = {}  # device_id -> 当前采集间隔

        # 统计信息（用锁保护，多线程安全）
        self._stats_lock = threading.Lock()
        self.stats: dict[str, Any] = {
            'total_collections': 0,
            'successful_collections': 0,
            'failed_collections': 0,
            'last_collection_time': None,
            # ⚠️ 这里**不能**放 'queue_size'。
            #    get_stats() 的返回是 `{'queue_size': self.data_queue.qsize(), **self.stats}`，
            #    而 `**self.stats` 在**后面** —— dict 字面量里后出现的键覆盖前面的，
            #    所以 self.stats 里只要有 'queue_size'，实时算出来的那个就被丢掉，
            #    返回的永远是这里的陈旧值（初始 0）。
            #    实测（2026-10-08 round 194）：队列里放 5 条，
            #    data_queue.qsize() == 5 而 get_stats()['queue_size'] == 0。
            #    **队列深度是派生量，不该被存下来。**
            #    由 tests/test_derived_metrics_not_shadowed.py 看着。
            # 队列满时被丢弃的数据项数（含"丢最旧"与被拒两种，两者都是**真实丢失**）。
            # round 214 修正：原先只计"被拒的新数据"，**丢最旧那条（先丢的、
            # 更旧的数据）根本没计数** —— 与这行注释说的"含两种"不符，
            # 于是「到底丢了多少数据」在监控上是少报的。
            'dropped_items': 0,
            #: 细分：为腾位置而淘汰的**最旧一条**（真实数据丢失）
            'dropped_oldest': 0,
            #: 细分：连丢最旧都腾不出位置、本条被拒
            'dropped_rejected': 0,
            # 写库失败后的重试相关计数。
            # 这几个键让「数据有没有丢」在监控侧可查 ——
            # 光靠日志不足以做告警，而 `dropped_db_retry` 是**真实的数据丢失量**。
            'requeued_db_retry': 0,      # 写库失败后成功重新入队、等待重试的条数
            'dropped_db_retry': 0,       # 重试后仍失败、被丢弃的条数（= 数据丢失）
            'requeue_full_db_retry': 0,  # 队列满导致没能重新入队的条数
            'protocols_active': {}
        }

        # 数据处理线程
        self.process_thread = None

    def _inc_stat(self, key: str, amount: int = 1) -> None:
        """线程安全地增加统计计数"""
        with self._stats_lock:
            self.stats[key] = self.stats.get(key, 0) + amount

    def _protocol_of(self, device_id: str) -> str:
        """设备协议（带缓存）。指标标签用，避免每次读数都查设备配置。

        Prometheus 标签基数必须可控：协议只有 modbus_tcp/rest/opcua/mqtt/mc/fins
        这几种，按 device_id 缓存一次即可。
        """
        cache = getattr(self, '_protocol_cache', None)
        if cache is None:
            cache = self._protocol_cache = {}
        proto = cache.get(device_id)
        if proto is None:
            try:
                devices = getattr(self.device_manager, 'devices', None) or {}
                cfg = devices.get(device_id) or {}
                proto = cfg.get('protocol', 'unknown')
            except Exception:      # noqa: BLE001 —— 取不到就 unknown，不影响采集
                proto = 'unknown'
            cache[device_id] = proto
        return proto

    def _enqueue_drop_oldest(self, item: dict) -> bool:
        """非阻塞入队；队列满时丢弃最旧的一条，仍满则丢弃本条。

        **绝不能用阻塞式 ``data_queue.put()``** —— 队列有上限（默认 200000），
        一旦消费端卡住，``put()`` 会永久阻塞。采集链路是"采集完 → 在回调里
        ``_schedule_next()``"的串行结构，阻塞在那里意味着**该设备从此再也不采集**，
        而且不报错、不告警（典型的"静默假死"）。

        Returns:
            bool: 是否成功入队（False 表示本条被丢弃）
        """
        if self.data_queue.full():
            try:
                self.data_queue.get_nowait()
                # ⚠️ 被淘汰的最旧一条同样是**真实的数据丢失**（内存里没了），必须计数。
                # round 214 前这里不计数（只计"被拒"那一半）→ dropped_items 少报。
                self._inc_stat('dropped_items')
                self._inc_stat('dropped_oldest')
                # round 265：**刻意不删它的磁盘副本**。
                # 内存队列扔掉只是"这一轮送不进库"，磁盘副本还在
                # `pending_data.jsonl` 里 —— 下次启动可以恢复，数据不丢。
                # （曾试图"丢弃即删磁盘副本"，被
                #   tests/test_collector_db_failure.py 当场拦下：那条用例明确要求
                #   「队列满而未能重新入队的数据，磁盘副本必须仍在」。）
                # 文件不会因此无界增长 —— 由 `_persist_item` 里的**硬上限**兜住，
                # 超限淘汰是显式策略：计 `dropped_persisted` + ERROR 日志。
            except queue.Empty:
                # 安全忽略：full() 与 get_nowait() 之间消费线程可能已把队列取空，
                # 此时无需再丢弃，直接走下面的入队即可
                pass
        try:
            self.data_queue.put_nowait(item)
            # Prometheus：采集读数计数（round 253）。
            # 只统计**真实采集**的读数 —— 断路器冷却期生成的兜底数据
            # quality 是 BAD（合成数据），计进去会让"采集速率"看起来正常，
            # 而实际设备已经失联。那正是最需要看出来的场景。
            try:
                if item.get('quality') != 'BAD':
                    from core.metrics import DATA_COLLECTED
                    DATA_COLLECTED.labels(
                        device_id=item.get('device_id', 'unknown'),
                        protocol=self._protocol_of(item.get('device_id', '')),
                    ).inc()
            except Exception as e:      # noqa: BLE001 —— 指标失败不能影响采集
                logger.debug(f"采集指标打点失败: {e}")
            return True
        except queue.Full:
            self._inc_stat('dropped_items')
            self._inc_stat('dropped_rejected')
            return False

    def start(self) -> None:
        """启动数据采集"""
        if self.running:
            logger.warning("数据采集器已在运行")
            return

        self.running = True

        # 新的一轮启动：清空「已取消」登记，否则上一轮删过的设备 id
        # 会被永久挡住调度（见 __init__ 里 _cancelled_devices 的说明）。
        with self._tasks_lock:
            self._cancelled_devices.clear()
            self._cancelled_registry().clear()

        # 启动数据处理线程
        self.process_thread = threading.Thread(target=self._process_data, daemon=True)
        self.process_thread.start()

        # 为每个设备启动采集任务
        devices = self.device_manager.get_all_devices()
        proto_count = {}
        for device_id, device_config in devices.items():
            if device_config.get('enabled', True):
                protocol = device_config.get('protocol', 'modbus_tcp')
                proto_count[protocol] = proto_count.get(protocol, 0) + 1

                if protocol in ('opcua', 'mqtt'):
                    self._setup_push_device(device_id, device_config)
                else:
                    self._start_device_collection(device_id, device_config)

        with self._stats_lock:
            self.stats['protocols_active'] = proto_count

        summary = ', '.join(f"{k}:{v}" for k, v in proto_count.items())
        logger.info(f"数据采集器已启动，共 {len(devices)} 个设备 ({summary})")

    def stop(self) -> None:
        """停止数据采集（安全排空队列并入库）"""
        self.running = False

        # 等待处理线程结束
        if self.process_thread and self.process_thread.is_alive():
            self.process_thread.join(timeout=10)

        # 线程安全地取消所有定时器
        with self._tasks_lock:
            for device_id, timer in self.tasks.items():
                timer.cancel()
            self.tasks.clear()

        # 关闭线程池（等待正在执行的采集完成）
        if hasattr(self, '_pool'):
            self._pool.shutdown(wait=True)

        # 排空剩余数据并入库（不丢弃）
        remaining = []
        while not self.data_queue.empty():
            try:
                remaining.append(self.data_queue.get_nowait())
            except queue.Empty:
                break

        # 只有**确认入库成功**才允许清除磁盘持久化文件。
        #
        # 原先这里无条件 `clear_persistence()`：一旦上面的写库失败
        # （DB 被锁 / 磁盘满 / 进程正在退出），内存里的 remaining 已经从队列取走
        # （丢了），磁盘上的持久化副本又被删掉 —— **两处同时消失，数据彻底丢**。
        # 而 `DiskBackedQueue` 存在的意义正是防这种丢数据：
        # `put()` 是「先落盘再入队」，所以只要保留持久化文件，
        # 下次启动 `_recover_from_disk()` 就能把这批数据捞回来。
        flushed = True
        if remaining:
            try:
                self.database.insert_data_batch(remaining)
                logger.info(f"关闭前写入 {len(remaining)} 条剩余数据")
            except Exception as e:
                flushed = False
                logger.error(
                    f"关闭前写入剩余数据失败: {e} —— 保留磁盘持久化文件，"
                    f"下次启动将恢复这 {len(remaining)} 条数据（不丢弃）"
                )

        if flushed:
            self.data_queue.clear_persistence()
        else:
            logger.warning(
                "存在未能入库的数据，已保留持久化文件供下次启动恢复: "
                f"{getattr(self.data_queue, '_persist_file', '(未知路径)')}"
            )

        self.device_manager.disconnect_all()
        logger.info("数据采集器已停止")

    def _generate_fallback_data(self, device_id: str, device_config: dict[str, Any]) -> list:
        """故障降级：为失败的设备生成模拟数据兜底

        基于设备寄存器配置生成合理的模拟值，确保监控面板不空白。
        """
        import random
        fallback_data = []
        registers = device_config.get('registers', [])
        for reg in registers:
            name = reg.get('name', '')
            data_type = reg.get('data_type', 'int16')
            # 根据寄存器类型生成合理的模拟值
            if 'temp' in name.lower():
                value = round(random.uniform(20.0, 80.0), 1)
            elif 'pressure' in name.lower():
                value = round(random.uniform(0.5, 10.0), 2)
            elif 'flow' in name.lower():
                value = round(random.uniform(0.0, 100.0), 1)
            elif 'level' in name.lower():
                value = round(random.uniform(0.0, 100.0), 1)
            elif 'speed' in name.lower() or 'rpm' in name.lower():
                value = random.randint(0, 3000)
            elif data_type == 'float32':
                value = round(random.uniform(0.0, 100.0), 2)
            else:
                value = random.randint(0, 100)
            fallback_data.append({
                'device_id': device_id,
                'register_name': name,
                'value': value,
                'timestamp': datetime.now(),
                'quality': 'simulated',  # 标记为模拟数据
                'unit': reg.get('unit', ''),
            })
        return fallback_data

    def start_device_task(self, device_id: str, device_config: dict[str, Any]):
        """为指定设备启动采集任务（运行时添加设备时调用）"""
        if not self.running:
            logger.warning(f"数据采集器未运行，跳过设备 {device_id} 的采集启动")
            return

        if not device_config.get('enabled', True):
            logger.info(f"设备 {device_id} 已禁用，跳过采集启动")
            return

        with self._tasks_lock:
            if device_id in self.tasks:
                logger.debug(f"设备 {device_id} 已有采集任务，跳过")
                return
            # 重新添加同一台设备（此前被删除）时，必须解除「已取消」标记，
            # 否则 _schedule_next 会一直拒绝调度 → 采集中断且无任何报错。
            self._cancelled_devices.discard(device_id)
            self._cancelled_registry().pop(device_id, None)

        # 确保客户端已创建（device_manager.add_device 只存配置，不创建客户端）
        client = self.device_manager.get_client(device_id)
        if client is None:
            logger.info(f"设备 {device_id} 客户端不存在，尝试创建...")
            if not self.device_manager.connect_device(device_id):
                logger.warning(f"设备 {device_id} 客户端创建失败，采集任务仍会启动（等待重连）")

        protocol = device_config.get('protocol', 'modbus_tcp')
        if protocol in ('opcua', 'mqtt'):
            self._setup_push_device(device_id, device_config)
        else:
            self._start_device_collection(device_id, device_config)
        logger.info(f"已启动设备 {device_id} [{protocol}] 的采集任务")

    def _cancelled_registry(self) -> dict[str, float]:
        """返回 `device_id → 登记时刻(monotonic)`，必要时补建。

        用 `getattr` 而不是直接 `self._cancelled_at`：一批测试用
        `DataCollector.__new__` 绕过 `__init__` 只装配需要的字段（与
        `get_stats()` 的 `_device_intervals` 同一种兼容方式）。
        """
        registry = getattr(self, '_cancelled_at', None)
        if registry is None:
            registry = {}
            self._cancelled_at = registry
        return registry

    def _mark_cancelled(self, device_id: str) -> None:
        """登记「已取消」并打时间戳（须在 `_tasks_lock` 内调用）。

        round 289：原来只有 `self._cancelled_devices.add()`，没有时刻信息，
        于是「什么时候能安全地把它删掉」无从判断 → 这个集合只增不减。
        """
        self._cancelled_devices.add(device_id)
        now = time.monotonic()
        self._cancelled_registry()[device_id] = now
        # 每次登记顺便裁一次：集合规模的上界因此是「保留期内的删除次数」，
        # 而不是「进程生命周期内的删除次数」。
        self._prune_cancelled(now=now)

    def _prune_cancelled(self, now: float | None = None) -> int:
        """裁掉过期的「已取消」登记，返回裁掉的条数（须在 `_tasks_lock` 内调用）。

        保留条件（满足其一就不裁）：
          * 年龄 < `CANCELLED_RETENTION_S` —— 在途采集/残留回调可能还需要它挡着；
          * `device_id` 仍在 `self.tasks` 里 —— 理论上不该发生（登记即摘 Timer），
            但它一旦发生就说明还有活的定时器，此时删登记等于放它复活。

        裁掉之后的行为差异**只在「设备已删且早就不可能还有在途动作」这一种情形下**
        才可能出现，此时登记已无保护对象；而它挡住的「重新添加」路径由
        `start_device_task()` 的 discard 负责，不依赖过期与否。
        """
        if not self._cancelled_devices:
            return 0
        registry = self._cancelled_registry()
        now = time.monotonic() if now is None else now
        stale = [
            device_id for device_id in self._cancelled_devices
            if now - registry.get(device_id, now) >= CANCELLED_RETENTION_S
            and device_id not in self.tasks
        ]
        for device_id in stale:
            self._cancelled_devices.discard(device_id)
            registry.pop(device_id, None)
        if stale:
            logger.debug("已裁剪过期的「已取消」设备登记 %d 条: %s",
                         len(stale), stale[:10])
        return len(stale)

    def remove_device_task(self, device_id: str):
        """删除设备的采集任务（设备被删除时调用）"""
        with self._tasks_lock:
            timer = self.tasks.pop(device_id, None)
            # 登记「已取消」：正在线程池里跑的那一轮结束后会走 _schedule_next，
            # 必须让它看到这个标记而不再复活（否则删了还在采，见 __init__ 注释）。
            self._mark_cancelled(device_id)
        if timer:
            timer.cancel()
            logger.info(f"已停止设备 {device_id} 的采集任务")
        # 清理追踪数据，防止内存泄漏。
        # **必须在 `_tracking_lock` 内** —— 这几个字典被采集线程并发写
        # （见 _handle_normal_collection 里的 `self._last_values[key] = value`）。
        # 不加锁时，这里对 `_last_values` 的迭代会和采集线程的写入撞上，
        # 抛 `RuntimeError: dictionary changed size during iteration`，
        # 导致**设备删除失败、追踪数据残留**（也就是这段代码本来要防的内存泄漏）。
        self._forget_device_state(device_id)

    def sync_device_tasks(self) -> dict[str, Any]:
        """把正在跑的采集任务对齐到**当前设备配置**（配置热重载用）。

        round 283：`DeviceManager.reload_config()` 早就存在，但它只替换配置表 ——
        采集器完全不知道，于是：

        * 配置里**删掉 / 禁用**的设备：定时器照旧每 N 秒跑一次，而
          `get_client()` 已经找不到它的配置 → 每次采集都失败 → 退避 →
          **无限重试**。日志刷 `设备 xxx 配置不存在`，还给一台不存在的设备
          累计失败计数、可能触发「设备离线」报警；
        * 配置里**新增**的设备：没有定时器，**永远不采集** ——
          界面上有配置、没有数据，看起来像设备坏了，实际是没人拉它。

        本方法按 `get_all_devices()`（只算 `enabled` 的）做一次差集：
        缺的补 `start_device_task`，多的调 `remove_device_task`。

        Returns:
            dict: `{'started': [...], 'stopped': [...], 'unchanged': [...],
                   'skipped': bool}`
            `skipped=True` 表示采集器没在跑（启动流程自己会全量拉起，
            这里什么都不该做）。
        """
        if not self.running:
            logger.info("采集器未运行，跳过设备任务同步（启动时会全量拉起）")
            return {'started': [], 'stopped': [], 'unchanged': [], 'skipped': True}

        manager = getattr(self, 'device_manager', None)
        if manager is None:
            return {'started': [], 'stopped': [], 'unchanged': [], 'skipped': True}

        devices = manager.get_all_devices() or {}
        desired = {
            dev_id for dev_id, cfg in devices.items()
            if (cfg or {}).get('enabled', True)
        }

        # ⚠️ 差集在锁内算完就放锁：`start_device_task` / `remove_device_task`
        # 自己也会拿 `_tasks_lock`，而它是**非可重入**的 Lock ——
        # 持锁调用会直接把自己锁死（表现为整个采集器静默停摆）。
        with self._tasks_lock:
            running = set(self.tasks)

        to_start = sorted(desired - running)
        to_stop = sorted(running - desired)
        unchanged = sorted(desired & running)

        for device_id in to_stop:
            self.remove_device_task(device_id)
            logger.info("设备配置已移除/禁用，停止采集任务: %s", device_id)

        for device_id in to_start:
            self.start_device_task(device_id, devices.get(device_id) or {})
            logger.info("设备配置新增，启动采集任务: %s", device_id)

        if to_start or to_stop:
            logger.warning("设备采集任务已按配置同步: 启动 %s，停止 %s",
                           to_start, to_stop)

        return {
            'started': to_start,
            'stopped': to_stop,
            'unchanged': unchanged,
            'skipped': False,
        }

    #: 所有「按设备索引」的运行期状态容器 —— 删除设备时必须逐个清理。    #: round 274：`_device_intervals` 之前**漏了**，于是 `get_stats()` 里
    #: `device_intervals` 会一直列出已删除的设备（运维在界面上看到一台
    #: 不存在的设备"间隔 5s"），重新添加同名设备时也会短暂继承旧值。
    #: `tests/test_device_state_cleanup.py` 会扫源码里的
    #: `self._xxx[device_id] = ...` 赋值，发现新容器没登记就红。
    PER_DEVICE_STATE_ATTRS = (
        '_last_values', '_last_times',
        '_circuit_state', '_failure_counts',
        '_device_intervals',
    )

    #: 「已取消设备」登记表 —— 与上面**反向**的生命周期，必须分开登记。
    #:
    #: `PER_DEVICE_STATE_ATTRS` 里的容器是「设备活着时积累、删除时清空」；
    #: 这两个恰好相反：**删除时才写入**，靠 `_prune_cancelled()` 到期回收。
    #: 若把它们塞进 `_forget_device_state()`，删除瞬间就会把刚写的登记抹掉 ——
    #: 在途采集重新挂上定时器（round 219 的僵尸采集全部回归）。
    CANCELLED_REGISTRY_ATTRS = ('_cancelled_devices', '_cancelled_at')

    def _forget_device_state(self, device_id: str) -> None:
        """清掉某台设备的**全部**运行期状态（删除设备时调用）。

        `_last_values` / `_last_times` 的键是 `"{device_id}:{register}"`，
        其余是 `device_id` 本身 —— 两种都要清。
        """
        prefix = f"{device_id}:"
        with self._tracking_lock:
            stale_keys = [k for k in self._last_values if k.startswith(prefix)]
            for k in stale_keys:
                self._last_values.pop(k, None)
                self._last_times.pop(k, None)
            self._circuit_state.pop(device_id, None)
            self._failure_counts.pop(device_id, None)
            self._device_intervals.pop(device_id, None)

    def _setup_push_device(self, device_id: str, device_config: dict[str, Any]):
        """设置推送型设备（OPC UA / MQTT）的回调和连接。

        为推送型协议（OPC UA、MQTT）注册数据回调函数，当设备主动推送
        数据时自动入队。队列满时丢弃最旧数据以避免阻塞回调线程。

        Args:
            device_id: 设备唯一标识符。
            device_config: 设备配置字典，至少包含 ``protocol`` 字段。

        Returns:
            None

        Side Effects:
            - 调用 ``client.add_data_callback()`` 注册回调。
            - 调用 ``client.connect()`` 建立连接。
            - 向 ``self.data_queue`` 写入数据项。
            - 记录连接成功或失败的日志。

        Exceptions:
            不会主动抛出异常；连接失败时记录错误日志并静默返回。
        """
        protocol = device_config.get('protocol', 'modbus_tcp')
        client = self.device_manager.get_client(device_id)
        if not client:
            logger.error(f"设备 {device_id} 客户端创建失败")
            return

        def on_data(device_id, name, value, unit):
            if value is None:
                return
            # 设备被删除 / 采集器已停止时，回调可能还在（客户端没来得及断开），
            # 此时必须丢弃 —— 否则已删除设备的数据仍会入库，且会绕过
            # `remove_device_task` 的追踪清理。
            if not self.running or device_id in self._cancelled_devices:
                return
            value = try_finite_float(value)
            if value is None:
                return
            # 队列满时丢弃最旧数据，绝不阻塞回调线程
            self._enqueue_drop_oldest({
                'device_id': device_id,
                'register_name': name,
                'value': value,
                'timestamp': datetime.now(),
                'unit': unit
            })

        if hasattr(client, 'add_data_callback'):
            client.add_data_callback(on_data)

        if client.connect():
            logger.info(f"[{protocol.upper()}] 设备 {device_id} 已连接（推送模式）")
        else:
            logger.error(f"[{protocol.upper()}] 设备 {device_id} 连接失败")

    def _start_device_collection(self, device_id: str, device_config: dict[str, Any]):
        """启动单个设备的轮询采集任务（Modbus / REST），含失败退避。

        采集工作提交到线程池执行（上限见 `self._max_concurrent`，来自
        `配置/system.yaml` 的 `collection.max_concurrent`），避免100设备创建
        100+线程。调度延迟仍用 threading.Timer，但实际采集在池中运行。
        """
        # 设备自己没写间隔时，用 `配置/system.yaml` 的 `collection.default_interval`
        # （此前是硬编码 5，而配置里恰好也是 5，所以这个旋钮是死的也没人发现）。
        base_interval = device_config.get('collection_interval', self._default_interval)
        protocol = device_config.get('protocol', 'modbus_tcp')

        def _run_collection():
            """在线程池中执行的采集工作"""
            if not self.running:
                return
            # Prometheus（round 255）：采集耗时直方图。
            # 此前 `scada_collection_duration_seconds` 恒无数据 —— 看板上
            # 看不出"采集变慢了"，而那往往是设备开始不稳定的第一信号。
            _t0 = time.monotonic()
            try:
                return self._collect_device_data(device_id, device_config, protocol)
            finally:
                try:
                    from core.metrics import COLLECTION_DURATION
                    COLLECTION_DURATION.labels(device_id=device_id).observe(
                        max(0.0, time.monotonic() - _t0))
                except Exception as e:      # noqa: BLE001
                    logger.debug(f"采集耗时指标打点失败: {e}")

        def _schedule_next(interval, elapsed: float = 0.0):
            """安全地调度下一次采集。

            除 `self.running` 外还要检查「已取消」标记：设备被删除时，
            当前这一轮可能正跑在线程池里（`remove_device_task` 取消不到它），
            它结束后若无条件 `_schedule_next`，就会把刚删掉的设备**重新挂上定时器**。

            round 272：`elapsed` 传本轮已耗时，做**漂移补偿**（见
            `compute_next_delay`）。不传则按固定 interval（退避/断路器路径用）。
            """
            delay = compute_next_delay(interval, elapsed)
            if elapsed > interval > 0:
                logger.warning(
                    "设备 %s 采集耗时 %.2fs 超过周期 %.2fs —— 采样率已低于配置",
                    device_id, elapsed, interval)
            with self._tasks_lock:
                if self.running and device_id not in self._cancelled_devices:
                    timer = threading.Timer(delay, collect_task)
                    timer.daemon = True
                    timer.start()
                    self.tasks[device_id] = timer

        def _handle_circuit_breaker():
            """处理断路器逻辑：冷却期内兜底 + 半开试探"""
            cb = self._circuit_state.get(device_id)
            if not cb or not cb.get('opened_at'):
                return None  # 无断路器，继续正常采集

            # round 264：冷却窗口是**时长**，必须用单调时钟。
            # 用墙钟时一次校时回拨就会让 elapsed 变负 → 永远处于冷却期 →
            # 该设备**再也不被探测**（采集永久停摆，只能重启进程）。
            elapsed = time.monotonic() - cb['opened_at']

            if elapsed < CIRCUIT_BREAKER_COOLDOWN_S:
                # 冷却期内：生成模拟数据兜底（默认关闭，见 FALLBACK_SIMULATION_ENABLED）
                if FALLBACK_SIMULATION_ENABLED:
                    try:
                        fallback = self._generate_fallback_data(device_id, device_config)
                        # 审计合规：兜底数据是「假数据」，质量码必须为 BAD，
                        # 绝不能被下游当作 GOOD 正常数据使用。
                        for item in fallback:
                            item['quality'] = 'BAD'
                        accepted = 0
                        for item in fallback:
                            # 必须非阻塞：这里用阻塞式 put() 会让整条采集链
                            # 永久挂起（队列满时），该设备从此静默停止采集
                            if self._enqueue_drop_oldest(item):
                                accepted += 1
                        logger.warning(
                            f"设备 {device_id} 通信/解析失败，断路器冷却期内生成 {len(fallback)} 条"
                            f" BAD 质量兜底数据（非真实采集，仅供界面不空白；"
                            f"真实采集成功率不受影响，失败已计入统计）。入队 {accepted} 条")
                    except Exception as fe:
                        logger.debug(f"设备 {device_id} 降级数据生成失败: {fe}")
                remaining = CIRCUIT_BREAKER_COOLDOWN_S - elapsed
                _schedule_next(remaining)
                return True  # 已处理

            # 冷却期结束 → 半开试探
            try:
                probe_future = self._pool.submit(_run_collection)
                probe_ok = probe_future.result(timeout=15)
            except Exception as e:
                logger.debug(f"设备 {device_id} 试探读异常: {e}")
                probe_ok = False

            if probe_ok:
                self._circuit_state.pop(device_id, None)
                self._failure_counts[device_id] = 0
                interval = self._get_dynamic_interval(device_id, base_interval)
                logger.info(f"设备 {device_id} 试探读成功，断路器关闭，恢复采集")
            else:
                self._circuit_state[device_id] = {
                    'opened_at': time.monotonic(),      # 窗口计时
                    'opened_at_wall': time.time(),      # 展示用
                }
                interval = CIRCUIT_BREAKER_COOLDOWN_S
                logger.warning(f"设备 {device_id} 试探读失败，断路器重新打开")

            _schedule_next(interval)
            return True  # 已处理

        def _handle_normal_collection():
            """正常采集路径：提交到线程池 + 失败退避"""
            cycle_started = time.monotonic()
            try:
                future = self._pool.submit(_run_collection)
                success = future.result(timeout=30)
            except Exception as e:
                logger.debug(f"设备 {device_id} 线程池采集异常: {e}")
                success = False

            if not self.running:
                return

            elapsed = time.monotonic() - cycle_started

            if success:
                with self._tracking_lock:
                    self._failure_counts[device_id] = 0
                    self._circuit_state.pop(device_id, None)
                interval = self._get_dynamic_interval(device_id, base_interval)
            else:
                with self._tracking_lock:
                    failures = self._failure_counts.get(device_id, 0) + 1
                    self._failure_counts[device_id] = failures
                if failures >= CIRCUIT_BREAKER_THRESHOLD:
                    self._circuit_state[device_id] = {
                        'opened_at': time.monotonic(),      # 窗口计时
                        'opened_at_wall': time.time(),      # 展示用
                    }
                    interval = CIRCUIT_BREAKER_COOLDOWN_S
                    logger.warning(f"设备 {device_id} 连续失败 {failures} 次，断路器打开，{CIRCUIT_BREAKER_COOLDOWN_S}s 后重试")
                else:
                    # round 264：原来写的是 `min(2 ** failures, CEILING)` ——
                    # 第 1 次失败得到 2s，**比正常采集间隔（5s）还短**：
                    # 「退避」反而让一台已经出问题的设备被**更频繁**地打。
                    # 正确语义：退避间隔不得小于基础间隔，再按 2^n 指数增长。
                    interval = compute_backoff_interval(failures, base_interval)
                    logger.debug(f"设备 {device_id} 连续失败 {failures} 次，"
                                 f"{interval}s 后重试（基础间隔 {base_interval}s）")

            # 只有成功路径做漂移补偿：失败/退避路径的间隔是"额外等待"，
            # 不该因为本轮耗时而被压缩。
            _schedule_next(interval, elapsed if success else 0.0)

        def collect_task():
            try:
                if not self.running:
                    return

                # 断路器检查
                if _handle_circuit_breaker():
                    return

                # 正常采集
                _handle_normal_collection()

            except Exception as crash_err:
                logger.error(f"设备 {device_id} 采集任务崩溃（已恢复）: {crash_err}", exc_info=True)
                with self._tracking_lock:
                    crash_failures = self._failure_counts.get(device_id, 0) + 1
                    self._failure_counts[device_id] = crash_failures
                recovery_interval = min(2 ** crash_failures, BACKOFF_CEILING_S)
                _schedule_next(recovery_interval)

        # round 272：首次调度**随机抖动**，把各设备相位打散。
        # 原来固定 0.1s → 所有设备启动后同时开始，之后每 N 秒一起打网关/DB，
        # 25 台设备就是 25 倍的脉冲负载、其余时间全空。
        initial_delay = startup_jitter(base_interval)
        logger.debug("设备 %s 首次采集在 %.2fs 后（抖动打散相位）", device_id, initial_delay)
        with self._tasks_lock:
            self.tasks[device_id] = threading.Timer(initial_delay, collect_task)
            self.tasks[device_id].daemon = True
            self.tasks[device_id].start()

    def _get_dynamic_interval(self, device_id: str, base_interval: float) -> float:
        """
        根据设备状态动态计算采集间隔

        Args:
            device_id: 设备ID
            base_interval: 基础采集间隔

        Returns:
            动态采集间隔（秒）
        """
        try:
            # 获取设备状态
            status = self.device_manager.get_device_status(device_id)

            # 检查是否在故障状态
            if status.get('stopped') or status.get('error'):
                return self._apply_degradation_multiplier(
                    self._dynamic_interval_config.get('fault', 1))

            # 检查健康评分（如果预测性维护模块可用，带缓存避免O(N²)）
            # 降级把 predictive_maintenance 关掉后，get_health_scores() 会一直返回
            # 降级前的旧快照 —— 拿陈旧健康分去判定「设备故障 → 1 秒快采」，
            # 恰好和降级「降低轮询压力」的目的相反，所以这里也要跟着闸门走。
            if self.predictive_maintenance and self._intel_enabled('predictive_maintenance'):
                # round 264：5 秒缓存也是时长 → 单调时钟（墙钟回拨会让缓存
                # 永远不刷新，拿着几小时前的健康分去决定采集频率）
                now = time.monotonic()
                if not hasattr(self, '_health_cache') or now - getattr(self, '_health_cache_ts', 0) > 5:
                    self._health_cache = self.predictive_maintenance.get_health_scores()
                    self._health_cache_ts = now
                device_health = [
                    s for s in self._health_cache.values()
                    if s.get('device_id') == device_id
                ]
                if device_health:
                    avg_health = sum(s.get('health_score', 100) for s in device_health) / len(device_health)
                    if avg_health < 40:
                        return self._apply_degradation_multiplier(
                            self._dynamic_interval_config.get('fault', 1))
                    elif avg_health < 60:
                        return self._apply_degradation_multiplier(
                            self._dynamic_interval_config.get('warning', 2))

            # 检查设备运行状态
            stats = status.get('stats', {})
            if hasattr(stats, 'get'):
                state = stats.get('state', 'idle')
            else:
                state = 'idle'

            # 根据状态返回对应的采集间隔
            interval = self._apply_degradation_multiplier(
                self._dynamic_interval_config.get(state, base_interval))
            # 记录**实际生效**的间隔（已含降级倍数），否则这张表记的是
            # 「没降级时该多久采一次」，运维拿它对不上真实轮询周期。
            self._device_intervals[device_id] = interval
            return interval

        except Exception as e:
            logger.debug(f"计算动态采集间隔失败 {device_id}: {e}")
            return base_interval

    @staticmethod
    def _apply_degradation_multiplier(interval: float) -> float:
        """按当前降级等级拉长采集间隔。

        `get_frequency_multiplier()` 以前**没有任何消费方** —— 降级后采集频率
        一点没降，等于降级只改了个显示数字。这里把它接上。

        故障分支（1 秒快采）同样乘倍数：降级是运维显式选择的「用采样密度
        换系统存活」，降级状态下继续 1 秒轮询每台故障设备会加剧总线与 CPU
        压力。若确实需要 1 秒快采，正确做法是恢复 NORMAL 等级，而不是指望
        降级状态下还保持快采。
        """
        try:
            multiplier = float(degradation_manager.get_frequency_multiplier())
        except Exception:
            return interval
        if not math.isfinite(multiplier) or multiplier <= 1.0:
            return interval
        return interval * multiplier

    def _collect_device_data(self, device_id: str, device_config: dict[str, Any], protocol: str) -> bool:
        """采集单个轮询型设备的数据，返回是否成功"""

        def _metric_error(kind: str):
            """Prometheus：采集错误计数（round 253）。

            只在**失败分支**调用，按 (device_id, error_type) 打点 ——
            `scada_data_errors_total` 此前从未被递增，看板上恒 0，
            等于"采集一直在失败但监控看不出来"。
            """
            try:
                from core.metrics import DATA_ERRORS
                DATA_ERRORS.labels(device_id=device_id, error_type=kind).inc()
            except Exception as e:      # noqa: BLE001
                logger.debug(f"采集错误指标打点失败: {e}")

        try:
            client = self.device_manager.get_client(device_id)
            if not client:
                logger.debug(f"设备 {device_id} 客户端不存在，跳过采集")
                self._inc_stat('failed_collections')
                _metric_error('no_client')
                return False

            if not getattr(client, 'connected', False):
                if not self.device_manager.connect_device(device_id):
                    self._inc_stat('failed_collections')
                    _metric_error('connect_failed')
                    return False

            timestamp = datetime.now()

            if protocol in ('modbus_tcp', 'modbus_rtu', 'mc', 'fins'):
                # **必须把 _collect_modbus 的返回值透传出去。**
                # 原实现丢弃返回值、在函数末尾无条件 `return True`：
                # 于是「读取失败 → 连续失败 → 断路器打开 → 退避」整段逻辑
                # 是死代码 —— 熔断器永不打开、已死的设备被全速轮询。
                # Modbus采集内部自行计数成功/失败，此处不重复计数。
                ok = self._collect_modbus(client, device_id, device_config, timestamp)
                with self._stats_lock:
                    self.stats['last_collection_time'] = timestamp
                return ok
            elif protocol in ('rest', 'opcua', 'mqtt'):
                # REST/OPC-UA/MQTT都是缓存型协议，统一从缓存采集
                self._collect_from_cache(client, device_id, timestamp)
            else:
                logger.warning(f"设备 {device_id} 不支持的协议: {protocol}")
                self._inc_stat('failed_collections')
                _metric_error('unsupported_protocol')
                return False

            self._inc_stat('successful_collections')
            with self._stats_lock:
                self.stats['last_collection_time'] = timestamp
            return True

        except Exception as e:
            logger.error(f"采集设备 {device_id} 数据异常: {e}")
            self._inc_stat('failed_collections')
            _metric_error(type(e).__name__)
            return False

    def _collect_modbus(self, client, device_id: str, device_config: dict[str, Any],
                        timestamp: datetime) -> bool:
        """采集Modbus设备的寄存器数据（批量读取优化）。

        按 GB/T 19582 Modbus 规范：FC03 单次最多读 125 个寄存器。
        将连续地址范围合并为一次请求，减少网络往返。对于超过 125
        个寄存器的设备自动分段读取，并在块边界预留重叠区防止多寄存器
        值被截断。

        Args:
            client: Modbus 客户端实例，需支持 ``read_holding_registers()``
                和 ``decode_*()`` 方法。
            device_id: 设备唯一标识符。
            device_config: 设备配置字典，需包含 ``registers`` 列表，
                每个寄存器需有 ``address``、``name`` 字段，可选
                ``data_type``、``scale``、``offset``、``unit``。
            timestamp: 本次采集的时间戳。

        Returns:
            bool: 本次读取是否成功。
                - ``True``：读到新鲜数据（或设备未配置寄存器，无读取动作）；
                - ``False``：读取失败（含"只有陈旧缓存兜底"的情况 —— 数据仍然
                  入队并降级为 UNCERTAIN，但底层读取确实失败了，必须让上层
                  的退避/熔断逻辑看到失败）。

        Side Effects:
            - 通过 ``client.read_holding_registers()`` 发起网络请求。
            - 将解码后的数据项写入 ``self.data_queue``。
            - 更新 ``self.stats['successful_collections']`` 或
              ``self.stats['failed_collections']`` 计数器。

        Exceptions:
            不会主动抛出异常。读取失败时记录调试日志并更新失败计数。
        """
        registers = device_config.get('registers', [])
        if not registers:
            # 没有配置寄存器 = 无读取动作，不是"读取失败"，
            # 否则会给空配置设备误开断路器
            return True

        # 计算每个寄存器需要的寄存器数
        reg_sizes = {}
        for reg in registers:
            dt = reg.get('data_type', 'uint16')
            if dt in ('float32', 'float64', 'int32', 'uint32'):
                reg_sizes[reg['address']] = 2
            else:
                reg_sizes[reg['address']] = 1

        # 计算整体地址范围
        min_addr = min(r['address'] for r in registers)
        max_end = max(r['address'] + reg_sizes[r['address']] for r in registers)
        total_count = max_end - min_addr

        def _enqueue(item):
            """非阻塞入队，满则丢最旧"""
            return self._enqueue_drop_oldest(item)

        def _mark_stale(item, address, count):
            """陈旧缓存兜底值：打上缓存值原本的时间戳 + 标记降级质量码"""
            item['timestamp'] = _stale_value_timestamp(
                client, address, count, timestamp)
            item['stale'] = True

        # 单次 FC03 读取整个范围（规范限制 125，超出则分段）
        if total_count <= 125:
            all_regs = client.read_holding_registers(min_addr, total_count)
            if all_regs is None:
                self._inc_stat('failed_collections')
                logger.debug(f"设备 {device_id} Modbus读取返回None")
                return False
            stale = _read_is_stale(client)
            # 陈旧缓存 = 底层读取失败（只是客户端用旧值兜了底），
            # 必须计失败，否则熔断器对"有缓存的死设备"永不打开
            self._inc_stat('failed_collections' if stale else 'successful_collections')
            for reg in registers:
                offset = reg['address'] - min_addr
                size = reg_sizes[reg['address']]
                raw = all_regs[offset:offset + size]
                if len(raw) < size:
                    continue
                value = self._decode_register(client, raw, reg)
                if value is not None:
                    item = {
                        'device_id': device_id,
                        'register_name': reg['name'],
                        'value': value,
                        'timestamp': timestamp,
                        'unit': reg.get('unit', '')
                    }
                    if stale:
                        # 值是陈旧缓存兜底，交给质量评估降级为 UNCERTAIN_LAST_USABLE
                        _mark_stale(item, min_addr, total_count)
                    _enqueue(item)
            return not stale
        else:
            # 分块读取，块边界预留重叠区防止多寄存器值被截断
            max_reg_size = max(reg_sizes.values()) if reg_sizes else 1
            chunk_size = 125
            step = chunk_size - (max_reg_size - 1)  # 重叠区 = 最大寄存器宽度-1
            any_chunk_ok = False
            any_chunk_fresh = False
            for start in range(min_addr, max_end, step):
                count = min(chunk_size, max_end - start)
                chunk = client.read_holding_registers(start, count)
                if chunk is None:
                    continue
                stale = _read_is_stale(client)
                any_chunk_ok = True
                if not stale:
                    any_chunk_fresh = True
                for reg in registers:
                    if reg['address'] < start or reg['address'] >= start + count:
                        continue
                    offset = reg['address'] - start
                    size = reg_sizes[reg['address']]
                    raw = chunk[offset:offset + size]
                    if len(raw) < size:
                        continue
                    value = self._decode_register(client, raw, reg)
                    if value is not None:
                        item = {
                            'device_id': device_id,
                            'register_name': reg['name'],
                            'value': value,
                            'timestamp': timestamp,
                            'unit': reg.get('unit', '')
                        }
                        if stale:
                            _mark_stale(item, start, count)
                        _enqueue(item)

            # 所有分块都读失败 → 本次采集失败
            if not any_chunk_ok:
                self._inc_stat('failed_collections')
                logger.debug(f"设备 {device_id} Modbus分块读取全部失败")
                return False
            # 没有任何一块读到新鲜数据（全是陈旧缓存兜底）→ 设备实际不可达，
            # 同样按失败上报给退避/熔断；只要有任意一块是新鲜的就仍算成功，
            # 避免给"部分降级但还活着"的设备误开断路器。
            if not any_chunk_fresh:
                self._inc_stat('failed_collections')
                return False
            self._inc_stat('successful_collections')
            return True

    def _decode_register(self, client, raw_regs: list[int], register: dict) -> float | None:
        """从原始寄存器值解码为工程值"""
        try:
            data_type = register.get('data_type', 'uint16')
            scale = register.get('scale', 1)
            offset = register.get('offset', 0)

            if data_type == 'float32':
                value = client.decode_float32(raw_regs)
            elif data_type == 'float64':
                value = client.decode_float64(raw_regs)
            elif data_type == 'int32':
                value = client.decode_int32(raw_regs)
            elif data_type == 'uint32':
                value = client.decode_uint32(raw_regs)
            elif data_type == 'int16':
                value = client.decode_int16(raw_regs[0])
            else:
                value = client.decode_uint16(raw_regs[0])

            if value is None:
                return None
            # 缩放/偏移**之后**也必须过一遍非有限值检查。
            # `base_client.decode_float32` 只过滤了原始解码值；`value * scale + offset`
            # 在 scale 配置过大或异常数据下会溢出成 ±inf，NaN 也可能由此产生。
            # 缓存采集路径（`_collect_from_cache`）早就有这道检查，Modbus 路径没有 ——
            # 两条采集路径对同一个问题不一致，就是漏网的口子。
            scaled = value * scale + offset
            numeric = try_finite_float(scaled)
            if numeric is None:
                logger.warning(
                    f"寄存器解码缩放后得到非有限值，丢弃 device={register['name']} "
                    f"value={value!r} scale={scale!r} offset={offset!r} -> {scaled!r}"
                )
                return None
            return round(numeric, 4)
        except Exception:
            return None

    def _collect_from_cache(self, client, device_id: str, timestamp):
        """通用方法：从客户端缓存采集数据（适用于REST/OPC UA/MQTT）"""
        if not getattr(client, 'connected', False):
            return
        latest = client.get_latest_data()
        for name, data in latest.items():
            value = data.get('value')
            if value is not None:
                # 设备可能吐出 NaN/Infinity（float32 溢出、寄存器位型异常）。
                # 原实现 `float(value)` 会让它们原样入库，污染趋势图与统计。
                numeric = try_finite_float(value)
                if numeric is None:
                    logger.warning(
                        f"缓存采集数据丢弃 device={device_id} register={name} "
                        f"value={value!r}: 不是有限数值（NaN/Infinity）"
                    )
                    continue
                try:
                    item = {
                        'device_id': device_id,
                        'register_name': name,
                        'value': numeric,
                        'timestamp': timestamp,
                        'unit': data.get('unit', '')
                    }
                    if self.data_queue.full():
                        try:
                            self.data_queue.get_nowait()
                        except queue.Empty:
                            # 安全忽略：full() 与 get_nowait() 之间消费线程可能已取空队列
                            pass
                    self.data_queue.put_nowait(item)
                except (ValueError, TypeError, queue.Full) as e:
                    # 本条数据被丢弃：value 非数值(float()失败) 或 队列满。
                    # 属于"丢数据"，必须留痕，否则表现为数据缺口却查不到原因
                    logger.warning(
                        f"缓存采集数据丢弃 device={device_id} register={name} "
                        f"value={value!r}: {type(e).__name__}: {e}"
                    )

    def _read_register(self, client, register: dict[str, Any]) -> float | None:
        """读取单个Modbus寄存器数据"""
        try:
            address = register['address']
            data_type = register.get('data_type', 'uint16')
            scale = register.get('scale', 1)
            offset = register.get('offset', 0)

            if data_type == 'float32':
                raw_values = client.read_holding_registers(address, 2)
                if raw_values is None or len(raw_values) < 2:
                    return None
                value = client.decode_float32(raw_values)
            elif data_type == 'float64':
                raw_values = client.read_holding_registers(address, 4)
                if raw_values is None or len(raw_values) < 4:
                    return None
                value = client.decode_float64(raw_values)
            elif data_type in ('int32', 'uint32'):
                raw_values = client.read_holding_registers(address, 2)
                if raw_values is None or len(raw_values) < 2:
                    return None
                if data_type == 'int32':
                    value = client.decode_int32(raw_values)
                else:
                    value = client.decode_uint32(raw_values)
            else:
                raw_values = client.read_holding_registers(address, 1)
                if raw_values is None:
                    return None
                if data_type == 'uint16':
                    value = client.decode_uint16(raw_values[0])
                elif data_type == 'int16':
                    value = client.decode_int16(raw_values[0])
                else:
                    value = raw_values[0]

            value = value * scale + offset
            return round(value, 4)

        except Exception as e:
            logger.error(f"读取寄存器异常 ({register.get('name', '?')}): {e}", exc_info=True)
            return None

    def _process_data(self) -> None:
        """数据处理主循环（在独立守护线程中运行）。

        从 ``self.data_queue`` 中批量取出数据，依次执行以下操作：
        1. 数据质量评估（OPC UA 质量码标准）；
        2. 批量写入数据库（单事务，比逐条快 10-50 倍）；
        3. 报警规则检查；
        4. TDengine 实时桥接（非阻塞）；
        5. 智能层异步分发（预测性维护、SPC、OEE 等）。

        Args:
            无（使用实例属性 ``self.data_queue``、``self.database`` 等）。

        Returns:
            None

        Side Effects:
            - 批量插入数据库记录（``self.database.insert_data_batch``）。
            - 触发报警检查（``self.alarm_manager.check_alarm``）。
            - 向 TDengine 桥接器推送数据。
            - 更新 ``self.stats`` 统计信息。
            - 清除磁盘持久化文件（``self.data_queue.clear_persistence``）。

        Exceptions:
            主循环内部捕获所有异常并记录错误日志，不会终止线程。
        """
        # 智能层分发队列（独立线程处理，不阻塞主循环）
        intel_queue = Queue(maxsize=10000)

        def _intel_worker():
            """智能层数据分发工作线程"""
            while self.running:
                try:
                    data = intel_queue.get(timeout=1)
                except queue.Empty:
                    # 安全忽略：1秒轮询超时，队列暂时没数据属正常，继续下一轮
                    continue
                try:
                    self._dispatch_intelligence(data)
                except Exception as e:
                    logger.error(f"数据处理异常: {e}")

        intel_thread = threading.Thread(target=_intel_worker, daemon=True, name="intel_dispatch")
        intel_thread.start()

        # 批量参数（使用模块级常量）

        while self.running:
            # === 批量收集：从队列取一批数据 ===
            batch = []
            try:
                # 阻塞等第一条
                first = self.data_queue.get(timeout=1)
                batch.append(first)
            except queue.Empty:
                # 安全忽略：1秒内没有新数据属正常空转，继续下一轮批量
                continue

            # 非阻塞取剩余（凑满批次或等超时）
            deadline = time.monotonic() + BATCH_TIMEOUT_S
            while len(batch) < BATCH_MAX and time.monotonic() < deadline:
                try:
                    batch.append(self.data_queue.get_nowait())
                except queue.Empty:
                    # 队列空了，等一小会再试
                    remaining = deadline - time.monotonic()
                    if remaining > 0.01:
                        time.sleep(min(remaining, 0.05))
                        continue
                    break

            if not batch:
                continue

            # 修复从磁盘恢复的时间戳（字符串→datetime）
            for item in batch:
                if 'timestamp' in item:
                    item['timestamp'] = _normalize_timestamp(item['timestamp'])

            try:
                # === 数据质量评估（OPC UA标准） ===
                # 按设备缓存状态，避免同一批次内重复查询（500条/10设备 → 10次而非500次）
                _device_status_cache = {}
                for data in batch:
                    device_id = data.get('device_id', '')
                    register_name = data.get('register_name', '')
                    value = data.get('value')
                    key = f"{device_id}:{register_name}"

                    # 获取设备状态（缓存）
                    if device_id not in _device_status_cache:
                        try:
                            device_info = self.device_manager.get_device_status(device_id)
                            device_status = 'unknown'
                            if isinstance(device_info, dict):
                                if not device_info.get('connected', True):
                                    device_status = 'offline'
                                elif device_info.get('stopped'):
                                    device_status = 'stopped'
                                elif device_info.get('error'):
                                    device_status = 'fault'
                        except Exception:
                            device_status = 'unknown'
                        _device_status_cache[device_id] = device_status

                    # 读-用必须在同一临界区内：否则「读到 last_value」与
                    # 「它被别人改掉」之间会出现撕裂，质量判定会基于不一致的快照。
                    with self._tracking_lock:
                        _prev_value = self._last_values.get(key)
                        _prev_time = self._last_times.get(key)

                    quality = DataQualityAssessor.assess(
                        value=value,
                        register_name=register_name,
                        device_status=_device_status_cache[device_id],
                        last_value=_prev_value,
                        last_time=_prev_time
                    )

                    # 读取虽"成功"返回，但内容来自客户端陈旧缓存（设备实际
                    # 已读不到），必须降级为 UNCERTAIN_LAST_USABLE 而不是 GOOD，
                    # 否则操作员会把几天前的旧值当成实时数据。
                    if data.get('stale'):
                        quality = DataQualityAssessor.UNCERTAIN_LAST_USABLE

                    data['quality'] = quality

                    # 更新跟踪状态（与其他访问 _last_values/_last_times 的路径共用
                    # 同一把 `_tracking_lock`；这两个键必须一起更新，不能只写一半）
                    if value is not None and not (isinstance(value, float) and math.isnan(value)):
                        with self._tracking_lock:
                            self._last_values[key] = value
                            # 与 QualityAssessor 的卡死判断同一时钟（单调）
                            self._last_times[key] = time.monotonic()

                # === 批量写DB（单事务，比逐条快10-50倍） ===
                db_write_ok = True
                try:
                    self.database.insert_data_batch(batch)
                except Exception as e:
                    db_write_ok = False
                    logger.warning(f"批量写入数据库失败 ({len(batch)} 条): {e}")
                    # 失败数据重新入队（标记 _db_retry 防止无限重试）。
                    # 计数不变式：retried + dropped + requeue_full == len(batch)
                    counts = self._requeue_failed_batch(batch)
                    retried = counts['retried']
                    dropped = counts['dropped']
                    requeue_full = counts['requeue_full']
                    if retried:
                        self._inc_stat('requeued_db_retry', retried)
                        logger.info(f"已重新入队 {retried} 条数据等待重试")
                    if dropped:
                        # 计数落进 stats，让「本进程放弃了多少条」在 /api/metrics 之类
                        # 的地方可查 —— 光有日志不足以做监控告警。
                        self._inc_stat('dropped_db_retry', dropped)
                        logger.error(
                            f"本进程放弃重试 {dropped} 条写库失败的数据"
                            f"（磁盘副本仍在 pending_data.jsonl，重启会再恢复一次；"
                            f"若同时有落盘失败，那部分才不可恢复）请检查数据库状态"
                        )
                    if requeue_full:
                        self._inc_stat('requeue_full_db_retry', requeue_full)
                        logger.error(
                            f"队列已满，{requeue_full} 条数据未能重新入队 —— "
                            f"已保留磁盘持久化文件，下次启动可恢复"
                        )

                # === 报警检查（每条都要检查） ===
                if self.alarm_manager:
                    for data in batch:
                        try:
                            self.alarm_manager.check_alarm(
                                device_id=data['device_id'],
                                register_name=data['register_name'],
                                value=data['value'],
                                timestamp=data['timestamp']
                            )
                        except Exception as e:
                            logger.error(f"报警检查异常: {e}")

                # === TDengine桥接（非阻塞） ===
                if self.realtime_bridge:
                    for data in batch:
                        try:
                            self.realtime_bridge.feed(
                                device_id=data['device_id'],
                                register_name=data['register_name'],
                                value=data['value'],
                                timestamp=data['timestamp'],
                                unit=data.get('unit', ''),
                                protocol=data.get('protocol', ''),
                                gateway_id=data.get('gateway_id', '')
                            )
                        except Exception as e:
                            logger.error(f"TDengine桥接异常: {e}")

                # === 智能层：扔进异步队列 ===
                for data in batch:
                    try:
                        if not intel_queue.full():
                            intel_queue.put_nowait(data)
                        else:
                            logger.warning(f"智能分发队列已满({intel_queue.qsize()}), 丢弃数据: device={data.get('device_id')}, reg={data.get('register_name')}")
                    except Exception as e:
                        logger.error(f"智能分发异常: {e}")

                # === 所有处理完成后，清除持久化文件防崩溃恢复重复 ===
                #
                # 但**只有这批真的写库成功**才能清：写失败时数据刚被重新入队，
                # 此刻它们**只存在于内存队列**里，磁盘上的副本是唯一的崩溃保护。
                # 无条件清除 = 把它们的保护也一起抹掉（崩溃就真丢了）。
                if db_write_ok and hasattr(self.data_queue, 'clear_persistence'):
                    # 只确认本批 —— 不要清空整个文件，否则会把「已落盘但还没被
                    # 本批取走」的新数据一起抹掉（崩溃即真丢）。
                    self.data_queue.clear_persistence(batch)

                with self._stats_lock:
                    # 不再往 self.stats 里写 'queue_size'：它是**派生量**，
                    # 由 get_stats() 实时算（qsize()）。写进来只会把实时值覆盖掉
                    # （`**self.stats` 展开在后面）—— 见 __init__ 里的说明。
                    self.stats['total_collections'] = self.stats.get('total_collections', 0) + len(batch)

            except Exception as e:
                if self.running:
                    logger.error(f"批量数据处理异常: {e}")

    def _requeue_failed_batch(self, batch: list[Dict[str, Any]]) -> Dict[str, int]:
        """写库失败后把整批重新入队，返回 ``{'retried','dropped','requeue_full'}``。

        规则：
        * 已带 ``_db_retry``（本次进程内已经重试过一次）→ 记为 **dropped**（真丢失点）；
        * 其余按**原顺序**尝试重新入队；队列满则记 **requeue_full**（数据仍留在磁盘，
          下次启动可恢复，不是丢失）。

        ⚠️ 这里刻意**不 break**：队列一满就跳出循环，会让「本批剩余条目」既没入队、
        也没进任何计数 —— 日志只说「1 条未能重新入队」，实际可能是几百条。
        账目不准比数据丢失更难发现（数据还在磁盘上，但运维看不出规模）。
        不变式：``retried + dropped + requeue_full == len(batch)``。

        ⚠️ **`dropped` 的准确语义（2026-10-13 对端复核后修正）**：它是
        「**本进程内**放弃重试」，**不等于永久丢失**。写库失败时不会调用
        `clear_persistence`，这些条目的磁盘副本仍在 `pending_data.jsonl` 里，
        重启后 `_recover_from_disk` 会重新恢复（跳过条件只有「损坏行」与「value 为 None」）。
        真正不可恢复的只有**落盘也失败**的那部分 —— 那由 `persist_failures` 单独计数并告警。
        """
        retried = 0
        dropped = 0
        requeue_full = 0

        for item in batch:
            if item.get('_db_retry'):
                # 本进程内已重试过一次仍失败 → 放弃在本进程内重试。
                # 磁盘副本仍在（未 clear_persistence）→ 重启会再恢复一次，**不是永久丢失**。
                # 仍然必须计数：原实现直接 continue，运维看不到「本进程放弃了多少条」。
                dropped += 1
                continue

            if self.data_queue.full():
                # 队列满：本条与后续都进不去。数据仍在磁盘持久化文件里，下次启动恢复。
                requeue_full += 1
                continue

            item['_db_retry'] = True
            try:
                self.data_queue.put_nowait(item)
                retried += 1
            except queue.Full:
                requeue_full += 1

        return {'retried': retried, 'dropped': dropped, 'requeue_full': requeue_full}

    def _intel_enabled(self, module_attr: str) -> bool:
        """该智能模块在当前降级等级下是否还应该继续收数据。

        未登记在 `_INTEL_FEATURE_NAMES` 的模块（安全联锁等）一律返回 True。
        """
        feature = _INTEL_FEATURE_NAMES.get(module_attr)
        if feature is None:
            return True
        return degradation_manager.is_feature_enabled(feature)

    def _safe_intel(self, module_name: str, fn, *args, **kwargs) -> None:
        """执行单个智能模块的喂数；一个模块抛异常不牵连其它模块。

        原实现把 6 个模块串在同一个 try 里，外层 `_intel_worker` 只兜了一层，
        所以任一模块抛异常（例如 inf 产量让 `int()` 抛 OverflowError）会让
        **其后所有模块**整批跳过。而排在最后的恰好是安全联锁 ——
        业务分析算崩了把安全功能带走，是典型的静默失效。
        """
        try:
            fn(*args, **kwargs)
        except Exception as e:
            logger.error("智能模块 %s 处理数据失败（其余模块继续）: %s", module_name, e)

    def _dispatch_intelligence(self, data: dict):
        """智能层数据分发（在独立线程中运行）。

        执行顺序是有意为之：**安全联锁第一个跑**。联锁属于安全功能，不能
        因为前面某个分析模块算崩了就被跳过；它也不在任何降级等级的禁用
        清单里（EMERGENCY 的语义是「仅保留报警与联锁」）。
        """
        device_id = data['device_id']
        register_name = data['register_name']
        value = data['value']
        timestamp = data['timestamp']
        name_lower = register_name.lower()

        # 安全联锁 —— 永远第一个，且不受降级影响
        if self.device_control:
            self._safe_intel(
                'device_control', self.device_control.check_interlocks,
                device_id, register_name, value,
            )

        # 预测性维护
        if self.predictive_maintenance and self._intel_enabled('predictive_maintenance'):
            self._safe_intel(
                'predictive_maintenance', self.predictive_maintenance.feed_data,
                device_id, register_name, value, timestamp,
            )

        # 边缘决策
        if self.edge_decision and self._intel_enabled('edge_decision'):
            self._safe_intel(
                'edge_decision', self.edge_decision.update_data,
                f"{device_id}:{register_name}", value,
            )

        # 能源管理
        if self.energy_manager and self._intel_enabled('energy_manager'):
            # round 298：**顺序与词边界都得对**。
            # `_power_kw` 里含 'kwh'，而它原来排在 `_energy_kw` 之前 ——
            # 所有 `*_kwh` 电量寄存器都被当成**瞬时功率 kW** 做梯形积分：
            # 仿真预设 energy_meter_01 的 5 个 kWh 寄存器（120/450/180/50/320）
            # 每 60s 被当作功率积分 → 约 1120 kWh/h，而真实总功率只有 320 kW，
            # 电费/碳排放约 3.5 倍；同时 realtime_power 被轮流覆盖，
            # 页面上「总功率 320 kW」与「每小时 1120 kWh」并存且互相矛盾。
            # 判据：**kWh 类必须走 energy_kwh 差分路径**，不能只靠子串包含。
            if _has_keyword(name_lower, _energy_kw):
                self._safe_intel(
                    'energy_manager', self.energy_manager.feed_power_data,
                    device_id, 0, energy_kwh=value, timestamp=timestamp,
                )
            elif _has_keyword(name_lower, _power_kw):
                power_value = value
                if 'w' in name_lower and 'kw' not in name_lower:
                    power_value = value / 1000
                self._safe_intel(
                    'energy_manager', self.energy_manager.feed_power_data,
                    device_id, power_value, timestamp=timestamp,
                )

        # SPC
        if self.spc_analyzer and self._intel_enabled('spc_analyzer') and _has_keyword(name_lower, _spc_kw):
            self._safe_intel(
                'spc_analyzer', self.spc_analyzer.feed_data,
                device_id, register_name, value,
            )

        # 振动分析
        if self.vibration_analyzer and self._intel_enabled('vibration_analyzer'):
            self._safe_intel(
                'vibration_analyzer', self.vibration_analyzer.feed_data,
                device_id, register_name, value, timestamp,
            )

        # OEE
        if self.oee_calculator and self._intel_enabled('oee_calculator'):
            self._safe_intel('oee_calculator', self._dispatch_oee, device_id, name_lower, value)

    def _dispatch_oee(self, device_id: str, name_lower: str, value) -> None:
        """OEE 喂数（状态 / 产量）。

        非有限值（NaN / ±Infinity）一律**不记录**，而不是当成 0：
        - 产量：`int(float('inf'))` 抛 OverflowError、`int(float('nan'))` 抛
          ValueError，异常被外层接住后这条样本静默丢失；
        - 状态：原实现把解析失败的值兜底成 `status_int = 0`，而 0 映射到
          `'stopped'` —— 一台正在运行的设备只要状态寄存器读到 NaN，
          就会被记成「停机」，直接压低 OEE 可用率。这是**编造数据**，
          比不记录更糟。
        """
        if _has_keyword(name_lower, _status_kw):
            status_map = {0: 'stopped', 1: 'idle', 2: 'running', 3: 'fault', 4: 'maintenance', 5: 'setup'}
            status_int = try_finite_float(value)
            if status_int is None:
                logger.warning(
                    "OEE 设备状态非有限值（%r），本条不记录状态 device=%s reg=%s",
                    value, device_id, name_lower,
                )
                return
            status = status_map.get(int(status_int))
            if status is None:
                logger.warning(
                    "OEE 设备状态码未定义（%r），本条不记录状态 device=%s reg=%s",
                    value, device_id, name_lower,
                )
                return
            self.oee_calculator.update_device_state(device_id, status)
            return

        if not _has_keyword(name_lower, _count_kw):
            return

        count = try_finite_float(value)
        if count is None:
            logger.warning(
                "OEE 产量计数非有限值（%r），本条不计入产量统计 device=%s reg=%s",
                value, device_id, name_lower,
            )
            return
        count = int(count)

        good_kw = frozenset(['good', 'ok', 'pass', 'qualified'])
        reject_kw = frozenset(['reject', 'ng', 'defect', 'scrap'])
        if _has_keyword(name_lower, good_kw):
            self.oee_calculator.record_production(device_id, good_count=count)
        elif _has_keyword(name_lower, reject_kw):
            # 缺陷数：从总产量中扣除
            self.oee_calculator.record_production(device_id, count=count)
        else:
            # 总产量：**只记总数，不推算合格数**（round 298）。
            # 原实现 `good = count - round(count*0.02)`，注释写着「98%合格率模拟」——
            # 这个假值会进 TDengine 的 OEERecord、再经 OEE_SCORE 导出到 Grafana：
            # 真实良率 80% 的产线看板显示 98%，不合格品混在 total_count 里无标记。
            # 判据：没有独立合格品计数时，合格数就是"不可用"，
            # 不能编一个 0.98 —— 由 `calculate_oee` 把 quality 置 None。
            self.oee_calculator.record_production(device_id, count=count)

    def get_circuit_breaker_states(self) -> dict[str, Any]:
        """各设备**真实生效**的熔断器状态（round 251）。

        为什么需要它：采集链的熔断保护是 `_handle_circuit_breaker()` 里的
        内联实现（`_circuit_state` / `_failure_counts`，按设备、带冷却与
        半开试探）。而 `core/circuit_breaker.py` 那套通用熔断器
        **生产环境从未创建过实例** —— `/api/resilience/circuit-breaker`
        和健康详情的 `circuit_breakers` 段读的是它，于永远是**空**。

        净效果：运维在韧性看板上看到「0 个熔断器、一切正常」，
        而某台设备可能早已熔断打开、正在按 30 秒周期空转。
        **真实状态没有任何接口能看到**，这是本轮要修的。

        Returns:
            ``{'summary': {...}, 'devices': {device_id: {...}}}``。
            只包含「已熔断」或「有连续失败」的设备 —— 全健康时
            ``devices`` 为空、``summary.open == 0``，调用方可以据此判断。
        """
        now = time.monotonic()
        open_states = dict(getattr(self, '_circuit_state', None) or {})
        failure_counts = dict(getattr(self, '_failure_counts', None) or {})

        devices: dict[str, Any] = {}
        for device_id in set(open_states) | {d for d, n in failure_counts.items() if n}:
            cb = open_states.get(device_id) or {}
            opened_at = cb.get('opened_at')
            failures = int(failure_counts.get(device_id, 0) or 0)
            entry: dict[str, Any] = {
                'state': 'open' if opened_at else 'closed',
                'consecutive_failures': failures,
                'threshold': CIRCUIT_BREAKER_THRESHOLD,
            }
            if opened_at:
                elapsed = max(0.0, now - opened_at)
                # `opened_at` 对外保持"墙钟时刻"的语义（前端/接口契约），
                # 内部计时用单调值；老状态没有 wall 字段时退回单调值。
                entry['opened_at'] = cb.get('opened_at_wall', opened_at)
                entry['opened_for_sec'] = round(elapsed, 1)
                entry['cooldown_remaining_sec'] = round(
                    max(0.0, CIRCUIT_BREAKER_COOLDOWN_S - elapsed), 1)
                entry['phase'] = ('cooldown' if elapsed < CIRCUIT_BREAKER_COOLDOWN_S
                                  else 'half_open_probe')
            devices[device_id] = entry

        opened = sum(1 for d in devices.values() if d['state'] == 'open')
        return {
            'summary': {
                'total_tracked': len(devices),
                'open': opened,
                'cooldown': sum(1 for d in devices.values()
                                if d.get('phase') == 'cooldown'),
                'threshold': CIRCUIT_BREAKER_THRESHOLD,
                'cooldown_sec': CIRCUIT_BREAKER_COOLDOWN_S,
            },
            'devices': devices,
        }

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        with self._stats_lock:
            stats = {
                'running': self.running,
                'queue_size': self.data_queue.qsize(),
                **self.stats
            }
        # 落盘失败计数必须从这里出去（/api/system/status 的 collector 段 +
        # 健康检查 _check_collector 都读 get_stats）—— 只算不报等于没算。
        failures = getattr(self.data_queue, 'persist_failures', 0)
        if failures:
            stats['queue_persist_failures'] = failures
        # round 265：崩溃恢复清单的规模与"因满而淘汰"的条数。
        # 后者是**真实的数据丢失**，不报出来就等于静默丢数据。
        pending = getattr(self.data_queue, 'pending_count', None)
        if pending is not None:
            stats['queue_pending_count'] = pending
        dropped_persisted = getattr(self.data_queue, 'dropped_persisted', 0)
        if dropped_persisted:
            stats['queue_dropped_persisted'] = dropped_persisted
        # 各设备当前**实际生效**的采集间隔（含降级倍数）。
        # `_device_intervals` 原来只写不读 —— 降级后运维想确认「到底降频了没有」
        # 在界面上找不到任何依据，只能靠猜。
        # 用 getattr 兜底：部分测试会绕过 __init__ 构造采集器。
        intervals = dict(getattr(self, '_device_intervals', None) or {})
        if intervals:
            stats['device_intervals'] = intervals
        # round 289：「已取消」登记的规模。
        # 这个集合此前只在 `start()` 清空，运行期每删一台设备就 +1 —— 无界增长；
        # 而它同时是推送型设备「幽灵数据」的拦截判据，涨到多大没人知道。
        # 读统计时顺带裁剪一次：即使长期没有新的删除动作，过期登记也会被回收。
        # 计数直接看集合本身（不依赖 `_cancelled_at` 是否存在，兼容绕过 __init__ 的对象）。
        cancelled = 0
        if getattr(self, '_cancelled_devices', None):
            with self._tasks_lock:
                if getattr(self, '_cancelled_at', None) is not None:
                    try:
                        self._prune_cancelled()
                    except Exception as e:      # noqa: BLE001 —— 统计不该因裁剪失败而 500
                        logger.debug(f"裁剪「已取消」登记失败: {e}")
                cancelled = len(self._cancelled_devices)
        if cancelled:
            stats['cancelled_devices'] = cancelled
        # 真实熔断状态（round 251）。此前 `/api/resilience/circuit-breaker`
        # 读的是从未被实例化的通用熔断器管理器，恒为空 —— 看板显示"一切正常"，
        # 而设备可能早已熔断。这里把内联熔断器的真实状态暴露出来。
        try:
            cb = self.get_circuit_breaker_states()
            stats['circuit_breakers'] = cb['summary']
            if cb['devices']:
                stats['circuit_breaker_devices'] = cb['devices']
        except Exception as e:      # noqa: BLE001 —— 统计不该因探测失败而 500
            logger.debug(f"读取熔断器状态失败: {e}")
        return stats
