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

logger = logging.getLogger(__name__)


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
    try:
        return fallback - timedelta(seconds=float(age))
    except (TypeError, ValueError):
        return fallback


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
            if abs(value - last_value) < 0.001 and time.time() - last_time > SENSOR_STUCK_WINDOW_S:
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

    def __init__(self, maxsize: int = 50000, persist_dir: str | None = None):
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

    def _persist_item(self, item: Dict[str, Any]) -> None:
        """持久化单条数据"""
        try:
            with self._lock:
                with open(self._persist_file, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(item, default=str, ensure_ascii=False) + '\n')
        except Exception as e:
            logger.debug(f"数据持久化失败: {e}")  # 持久化失败不影响主流程

    def _recover_from_disk(self) -> None:
        """从磁盘恢复未处理的数据"""
        if not self._persist_file.exists():
            return

        recovered = 0
        queue_full = False
        try:
            with open(self._persist_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            item = json.loads(line)
                            if item.get('value') is not None:
                                self._queue.put_nowait(item)
                                recovered += 1
                            else:
                                logger.warning(f"磁盘恢复: 跳过无value字段的记录 keys={list(item.keys())}")
                        except json.JSONDecodeError as e:
                            # 安全忽略并跳过该行：崩溃时可能留下半行不完整 JSON，
                            # 只丢这一条，不影响后续行恢复
                            logger.debug(f"磁盘恢复: 跳过损坏记录行: {e} | {line[:200]}")
                            continue
                        except queue.Full:
                            queue_full = True
                            break
        except Exception as e:
            logger.warning(f"磁盘恢复失败: {e}")

        # 只有全部恢复成功才删文件；队列满时保留文件供下次恢复
        if recovered > 0 and not queue_full:
            try:
                self._persist_file.unlink()
            except Exception as e:
                # 删除失败 → 持久化文件残留，下次启动会把这些记录再恢复一遍（重复数据）
                logger.warning(
                    f"磁盘恢复后删除持久化文件失败，下次启动可能重复恢复 "
                    f"{self._persist_file}: {e}"
                )

        if recovered > 0:
            logger.info(f"从磁盘恢复 {recovered} 条未处理数据" +
                       (f"（队列满，剩余数据待下次恢复）" if queue_full else ""))

    def clear_persistence(self) -> None:
        """清除持久化文件（正常关闭时调用）"""
        try:
            if self._persist_file.exists():
                self._persist_file.unlink()
        except Exception as e:
            # 删除失败 → 文件残留，下次启动会重复恢复这批已处理的数据
            logger.warning(
                f"清除持久化文件失败，下次启动可能重复恢复数据 "
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
        self._pool = ThreadPoolExecutor(max_workers=20, thread_name_prefix="collector")

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
            'queue_size': 0,
            # 队列满时被丢弃的数据项数（含"丢最旧"与被拒两种）
            'dropped_items': 0,
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
            except queue.Empty:
                # 安全忽略：full() 与 get_nowait() 之间消费线程可能已把队列取空，
                # 此时无需再丢弃，直接走下面的入队即可
                pass
        try:
            self.data_queue.put_nowait(item)
            return True
        except queue.Full:
            self._inc_stat('dropped_items')
            return False

    def start(self) -> None:
        """启动数据采集"""
        if self.running:
            logger.warning("数据采集器已在运行")
            return

        self.running = True

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

    def remove_device_task(self, device_id: str):
        """删除设备的采集任务（设备被删除时调用）"""
        with self._tasks_lock:
            timer = self.tasks.pop(device_id, None)
        if timer:
            timer.cancel()
            logger.info(f"已停止设备 {device_id} 的采集任务")
        # 清理追踪数据，防止内存泄漏。
        # **必须在 `_tracking_lock` 内** —— 这几个字典被采集线程并发写
        # （见 _handle_normal_collection 里的 `self._last_values[key] = value`）。
        # 不加锁时，这里对 `_last_values` 的迭代会和采集线程的写入撞上，
        # 抛 `RuntimeError: dictionary changed size during iteration`，
        # 导致**设备删除失败、追踪数据残留**（也就是这段代码本来要防的内存泄漏）。
        with self._tracking_lock:
            prefix = f"{device_id}:"
            stale_keys = [k for k in self._last_values if k.startswith(prefix)]
            for k in stale_keys:
                self._last_values.pop(k, None)
                self._last_times.pop(k, None)
            # 清理断路器和失败计数
            self._circuit_state.pop(device_id, None)
            self._failure_counts.pop(device_id, None)

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
            try:
                value = float(value)
            except (ValueError, TypeError):
                return
            if math.isnan(value) or math.isinf(value):
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

        采集工作提交到线程池执行（max_workers=20），避免100设备创建
        100+线程。调度延迟仍用 threading.Timer，但实际采集在池中运行。
        """
        base_interval = device_config.get('collection_interval', 5)
        protocol = device_config.get('protocol', 'modbus_tcp')

        def _run_collection():
            """在线程池中执行的采集工作"""
            if not self.running:
                return
            return self._collect_device_data(device_id, device_config, protocol)

        def _schedule_next(interval):
            """安全地调度下一次采集"""
            with self._tasks_lock:
                if self.running:
                    timer = threading.Timer(interval, collect_task)
                    timer.daemon = True
                    timer.start()
                    self.tasks[device_id] = timer

        def _handle_circuit_breaker():
            """处理断路器逻辑：冷却期内兜底 + 半开试探"""
            cb = self._circuit_state.get(device_id)
            if not cb or not cb.get('opened_at'):
                return None  # 无断路器，继续正常采集

            elapsed = time.time() - cb['opened_at']

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
                self._circuit_state[device_id] = {'opened_at': time.time()}
                interval = CIRCUIT_BREAKER_COOLDOWN_S
                logger.warning(f"设备 {device_id} 试探读失败，断路器重新打开")

            _schedule_next(interval)
            return True  # 已处理

        def _handle_normal_collection():
            """正常采集路径：提交到线程池 + 失败退避"""
            try:
                future = self._pool.submit(_run_collection)
                success = future.result(timeout=30)
            except Exception as e:
                logger.debug(f"设备 {device_id} 线程池采集异常: {e}")
                success = False

            if not self.running:
                return

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
                    self._circuit_state[device_id] = {'opened_at': time.time()}
                    interval = CIRCUIT_BREAKER_COOLDOWN_S
                    logger.warning(f"设备 {device_id} 连续失败 {failures} 次，断路器打开，{CIRCUIT_BREAKER_COOLDOWN_S}s 后重试")
                else:
                    interval = min(2 ** failures, BACKOFF_CEILING_S)
                    logger.debug(f"设备 {device_id} 连续失败 {failures} 次，{interval}s 后重试")

            _schedule_next(interval)

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

        with self._tasks_lock:
            self.tasks[device_id] = threading.Timer(0.1, collect_task)
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
                return self._dynamic_interval_config.get('fault', 1)

            # 检查健康评分（如果预测性维护模块可用，带缓存避免O(N²)）
            if self.predictive_maintenance:
                now = time.time()
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
                        return self._dynamic_interval_config.get('fault', 1)
                    elif avg_health < 60:
                        return self._dynamic_interval_config.get('warning', 2)

            # 检查设备运行状态
            stats = status.get('stats', {})
            if hasattr(stats, 'get'):
                state = stats.get('state', 'idle')
            else:
                state = 'idle'

            # 根据状态返回对应的采集间隔
            interval = self._dynamic_interval_config.get(state, base_interval)
            self._device_intervals[device_id] = interval
            return interval

        except Exception as e:
            logger.debug(f"计算动态采集间隔失败 {device_id}: {e}")
            return base_interval

    def _collect_device_data(self, device_id: str, device_config: dict[str, Any], protocol: str) -> bool:
        """采集单个轮询型设备的数据，返回是否成功"""

        try:
            client = self.device_manager.get_client(device_id)
            if not client:
                logger.debug(f"设备 {device_id} 客户端不存在，跳过采集")
                self._inc_stat('failed_collections')
                return False

            if not getattr(client, 'connected', False):
                if not self.device_manager.connect_device(device_id):
                    self._inc_stat('failed_collections')
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
                return False

            self._inc_stat('successful_collections')
            with self._stats_lock:
                self.stats['last_collection_time'] = timestamp
            return True

        except Exception as e:
            logger.error(f"采集设备 {device_id} 数据异常: {e}")
            self._inc_stat('failed_collections')
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
            return round(value * scale + offset, 4)
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
                try:
                    item = {
                        'device_id': device_id,
                        'register_name': name,
                        'value': float(value) if value is not None else 0,
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
            deadline = time.time() + BATCH_TIMEOUT_S
            while len(batch) < BATCH_MAX and time.time() < deadline:
                try:
                    batch.append(self.data_queue.get_nowait())
                except queue.Empty:
                    # 队列空了，等一小会再试
                    remaining = deadline - time.time()
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
                            self._last_times[key] = time.time()

                # === 批量写DB（单事务，比逐条快10-50倍） ===
                db_write_ok = True
                try:
                    self.database.insert_data_batch(batch)
                except Exception as e:
                    db_write_ok = False
                    logger.warning(f"批量写入数据库失败 ({len(batch)} 条): {e}")
                    # 失败数据重新入队（标记 _db_retry 防止无限重试）
                    retried = 0
                    dropped = 0
                    requeue_full = 0
                    for item in batch:
                        if item.get('_db_retry'):
                            # 已重试过一次仍失败 → 丢弃。
                            # **必须计数并报出来** —— 这是真正的数据丢失点，
                            # 原实现直接 continue，运维完全看不到丢了什么、丢了多少。
                            dropped += 1
                            continue
                        item['_db_retry'] = True
                        try:
                            if not self.data_queue.full():
                                self.data_queue.put_nowait(item)
                                retried += 1
                            else:
                                requeue_full += 1
                                break
                        except queue.Full:
                            requeue_full += 1
                            break
                    if retried:
                        self._inc_stat('requeued_db_retry', retried)
                        logger.info(f"已重新入队 {retried} 条数据等待重试")
                    if dropped:
                        # 计数落进 stats，让「丢了多少」在 /api/metrics 之类
                        # 的地方可查 —— 光有日志不足以做监控告警。
                        self._inc_stat('dropped_db_retry', dropped)
                        logger.error(
                            f"丢弃 {dropped} 条重试后仍写库失败的数据"
                            f"（device/reg 见上一条告警；这是数据丢失，请检查数据库状态）"
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
                    self.data_queue.clear_persistence()

                with self._stats_lock:
                    self.stats['queue_size'] = self.data_queue.qsize()
                    self.stats['total_collections'] = self.stats.get('total_collections', 0) + len(batch)

            except Exception as e:
                if self.running:
                    logger.error(f"批量数据处理异常: {e}")

    def _dispatch_intelligence(self, data: dict):
        """智能层数据分发（在独立线程中运行）"""
        device_id = data['device_id']
        register_name = data['register_name']
        value = data['value']
        timestamp = data['timestamp']
        name_lower = register_name.lower()

        # 预测性维护
        if self.predictive_maintenance:
            self.predictive_maintenance.feed_data(device_id, register_name, value, timestamp)

        # 边缘决策
        if self.edge_decision:
            self.edge_decision.update_data(f"{device_id}:{register_name}", value)

        # 能源管理
        if self.energy_manager:
            if _has_keyword(name_lower, _power_kw):
                power_value = value
                if 'w' in name_lower and 'kw' not in name_lower:
                    power_value = value / 1000
                self.energy_manager.feed_power_data(device_id, power_value, timestamp=timestamp)
            elif _has_keyword(name_lower, _energy_kw):
                self.energy_manager.feed_power_data(device_id, 0, energy_kwh=value, timestamp=timestamp)

        # SPC
        if self.spc_analyzer and _has_keyword(name_lower, _spc_kw):
            self.spc_analyzer.feed_data(device_id, register_name, value)

        # 振动分析
        if self.vibration_analyzer:
            self.vibration_analyzer.feed_data(device_id, register_name, value, timestamp)

        # OEE
        if self.oee_calculator:
            if _has_keyword(name_lower, _status_kw):
                status_map = {0: 'stopped', 1: 'idle', 2: 'running', 3: 'fault', 4: 'maintenance', 5: 'setup'}
                try:
                    status_int = int(value)
                except (ValueError, OverflowError, TypeError):
                    status_int = 0
                status = status_map.get(status_int, 'stopped')
                self.oee_calculator.update_device_state(device_id, status)
            elif _has_keyword(name_lower, _count_kw):
                good_kw = frozenset(['good', 'ok', 'pass', 'qualified'])
                reject_kw = frozenset(['reject', 'ng', 'defect', 'scrap'])
                if _has_keyword(name_lower, good_kw):
                    self.oee_calculator.record_production(device_id, good_count=int(value))
                elif _has_keyword(name_lower, reject_kw):
                    # 缺陷数：从总产量中扣除
                    self.oee_calculator.record_production(device_id, count=int(value))
                else:
                    # 总产量：同时记录合格品（98%合格率模拟）
                    total = int(value)
                    good = max(0, total - round(total * 0.02))  # 用round避免小批量截断为0
                    self.oee_calculator.record_production(device_id, count=total, good_count=good)

        # 安全联锁
        if self.device_control:
            self.device_control.check_interlocks(device_id, register_name, value)

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        with self._stats_lock:
            return {
                'running': self.running,
                'queue_size': self.data_queue.qsize(),
                **self.stats
            }
