"""
数据采集器模块
实现定时数据采集和数据处理
支持协议: Modbus TCP/RTU, OPC UA, MQTT, REST HTTP
"""

import json
import math
import time
import queue
import logging
import threading
from pathlib import Path
from typing import Any
from datetime import datetime
from queue import Queue

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
BATCH_MAX = 500                     # 批处理最大条数（WAL模式下500条事务开销最优）
BATCH_TIMEOUT_S = 0.5               # 批处理超时
CIRCUIT_BREAKER_THRESHOLD = 10      # 断路器：连续失败阈值
CIRCUIT_BREAKER_COOLDOWN_S = 300    # 断路器：冷却时间（秒）


def _has_keyword(register_name: str, keywords: tuple) -> bool:
    """检查寄存器名是否包含指定关键字"""
    name_lower = register_name.lower()
    return any(kw in name_lower for kw in keywords)


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
    """磁盘持久化队列 - 崩溃恢复"""

    def __init__(self, maxsize: int = 50000, persist_dir: str = 'data/queue'):
        self.maxsize = maxsize
        self._queue = queue.Queue(maxsize=maxsize)
        self._persist_dir = Path(persist_dir)
        self._persist_dir.mkdir(parents=True, exist_ok=True)
        self._persist_file = self._persist_dir / 'pending_data.jsonl'
        self._lock = threading.Lock()
        # 启动时恢复
        self._recover_from_disk()

    def put(self, item, block=True, timeout=None):
        """入队（同时写磁盘）"""
        self._queue.put(item, block=block, timeout=timeout)
        self._persist_item(item)

    def get(self, block=True, timeout=None):
        """出队"""
        return self._queue.get(block=block, timeout=timeout)

    def get_nowait(self):
        """非阻塞出队"""
        return self._queue.get_nowait()

    def put_nowait(self, item):
        """非阻塞入队"""
        self._queue.put_nowait(item)
        self._persist_item(item)

    def qsize(self):
        return self._queue.qsize()

    def empty(self):
        return self._queue.empty()

    def full(self):
        return self._queue.full()

    def _persist_item(self, item):
        """持久化单条数据"""
        try:
            with self._lock:
                with open(self._persist_file, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(item, default=str, ensure_ascii=False) + '\n')
        except Exception as e:
            logger.debug(f"数据持久化失败: {e}")  # 持久化失败不影响主流程

    def _recover_from_disk(self):
        """从磁盘恢复未处理的数据"""
        if not self._persist_file.exists():
            return

        recovered = 0
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
                        except json.JSONDecodeError:
                            continue  # 跳过损坏行，继续恢复后续数据
                        except queue.Full:
                            break
        except Exception as e:
            logger.warning(f"磁盘恢复失败: {e}")

        # 清空已恢复的文件
        if recovered > 0:
            try:
                self._persist_file.unlink()
            except Exception:
                pass

        if recovered > 0:
            logger.info(f"从磁盘恢复 {recovered} 条未处理数据")

    def clear_persistence(self):
        """清除持久化文件（正常关闭时调用）"""
        try:
            if self._persist_file.exists():
                self._persist_file.unlink()
        except Exception:
            pass


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

        # 数据队列（磁盘持久化，崩溃恢复）
        self.data_queue = DiskBackedQueue(maxsize=50000)

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
            'protocols_active': {}
        }

        # 数据处理线程
        self.process_thread = None

    def _inc_stat(self, key: str, amount: int = 1):
        """线程安全地增加统计计数"""
        with self._stats_lock:
            self.stats[key] = self.stats.get(key, 0) + amount

    def start(self):
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

    def stop(self):
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

        # 排空剩余数据并入库（不丢弃）
        remaining = []
        while not self.data_queue.empty():
            try:
                remaining.append(self.data_queue.get_nowait())
            except queue.Empty:
                break
        if remaining:
            try:
                self.database.insert_data_batch(remaining)
                logger.info(f"关闭前写入 {len(remaining)} 条剩余数据")
            except Exception as e:
                logger.error(f"关闭前写入剩余数据失败: {e}")

        # 正常关闭，清除磁盘持久化文件
        self.data_queue.clear_persistence()

        self.device_manager.disconnect_all()
        logger.info("数据采集器已停止")

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
        # 清理追踪数据，防止内存泄漏
        prefix = f"{device_id}:"
        stale_keys = [k for k in self._last_values if k.startswith(prefix)]
        for k in stale_keys:
            self._last_values.pop(k, None)
            self._last_times.pop(k, None)
        # 清理断路器状态
        self._circuit_state.pop(device_id, None)

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
            if self.data_queue.full():
                try:
                    self.data_queue.get_nowait()
                except queue.Empty:
                    pass
            try:
                self.data_queue.put_nowait({
                    'device_id': device_id,
                    'register_name': name,
                    'value': value,
                    'timestamp': datetime.now(),
                    'unit': unit
                })
            except queue.Full:
                pass  # 静默丢弃，不打日志（高频场景日志本身也卡）

        if hasattr(client, 'add_data_callback'):
            client.add_data_callback(on_data)

        if client.connect():
            logger.info(f"[{protocol.upper()}] 设备 {device_id} 已连接（推送模式）")
        else:
            logger.error(f"[{protocol.upper()}] 设备 {device_id} 连接失败")

    def _start_device_collection(self, device_id: str, device_config: dict[str, Any]):
        """启动单个设备的轮询采集任务（Modbus / REST），含失败退避"""
        base_interval = device_config.get('collection_interval', 5)
        protocol = device_config.get('protocol', 'modbus_tcp')

        def collect_task():
            if not self.running:
                return

            # 断路器检查：冷却期内跳过采集，避免对已知死设备无限轮询
            cb = self._circuit_state.get(device_id)
            if cb and cb.get('opened_at'):
                elapsed = time.time() - cb['opened_at']
                if elapsed < CIRCUIT_BREAKER_COOLDOWN_S:
                    remaining = CIRCUIT_BREAKER_COOLDOWN_S - elapsed
                    with self._tasks_lock:
                        if self.running:
                            timer = threading.Timer(remaining, collect_task)
                            timer.daemon = True
                            timer.start()
                            self.tasks[device_id] = timer
                    return
                # 冷却期结束，允许一次试探（half-open）

            success = self._collect_device_data(device_id, device_config, protocol)

            if not self.running:
                return

            # 失败退避 + 断路器
            if success:
                self._failure_counts[device_id] = 0
                self._circuit_state.pop(device_id, None)  # 恢复成功，关闭断路器
                interval = self._get_dynamic_interval(device_id, base_interval)
            else:
                failures = self._failure_counts.get(device_id, 0) + 1
                self._failure_counts[device_id] = failures
                if failures >= CIRCUIT_BREAKER_THRESHOLD:
                    self._circuit_state[device_id] = {'opened_at': time.time()}
                    interval = CIRCUIT_BREAKER_COOLDOWN_S
                    logger.warning(f"设备 {device_id} 连续失败 {failures} 次，断路器打开，{CIRCUIT_BREAKER_COOLDOWN_S}s 后重试")
                else:
                    interval = min(2 ** failures, BACKOFF_CEILING_S)
                    logger.debug(f"设备 {device_id} 连续失败 {failures} 次，{interval}s 后重试")

            with self._tasks_lock:
                if self.running:
                    timer = threading.Timer(interval, collect_task)
                    timer.daemon = True
                    timer.start()
                    self.tasks[device_id] = timer

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
                self._collect_modbus(client, device_id, device_config, timestamp)
            elif protocol == 'rest':
                self._collect_rest(client, device_id, device_config, timestamp)
            elif protocol == 'opcua':
                self._collect_opcua(client, device_id, device_config, timestamp)
            elif protocol == 'mqtt':
                self._collect_mqtt(client, device_id, device_config, timestamp)
            else:
                logger.warning(f"设备 {device_id} 不支持的协议: {protocol}")
                self._inc_stat('failed_collections')
                return False

            # Modbus采集内部自行计数成功/失败，此处不重复计数
            if protocol not in ('modbus_tcp', 'modbus_rtu', 'mc', 'fins'):
                self._inc_stat('successful_collections')
            with self._stats_lock:
                self.stats['last_collection_time'] = timestamp
            return True

        except Exception as e:
            logger.error(f"采集设备 {device_id} 数据异常: {e}")
            self._inc_stat('failed_collections')
            return False

    def _collect_modbus(self, client, device_id: str, device_config: dict[str, Any], timestamp: datetime):
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
            None

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
            return

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
            if self.data_queue.full():
                try:
                    self.data_queue.get_nowait()
                except queue.Empty:
                    pass
            try:
                self.data_queue.put_nowait(item)
            except queue.Full:
                pass

        # 单次 FC03 读取整个范围（规范限制 125，超出则分段）
        if total_count <= 125:
            all_regs = client.read_holding_registers(min_addr, total_count)
            if all_regs is None:
                self._inc_stat('failed_collections')
                logger.debug(f"设备 {device_id} Modbus读取返回None")
                return
            self._inc_stat('successful_collections')
            for reg in registers:
                offset = reg['address'] - min_addr
                size = reg_sizes[reg['address']]
                raw = all_regs[offset:offset + size]
                if len(raw) < size:
                    continue
                value = self._decode_register(client, raw, reg)
                if value is not None:
                    _enqueue({
                        'device_id': device_id,
                        'register_name': reg['name'],
                        'value': value,
                        'timestamp': timestamp,
                        'unit': reg.get('unit', '')
                    })
        else:
            # 分块读取，块边界预留重叠区防止多寄存器值被截断
            max_reg_size = max(reg_sizes.values()) if reg_sizes else 1
            chunk_size = 125
            step = chunk_size - (max_reg_size - 1)  # 重叠区 = 最大寄存器宽度-1
            for start in range(min_addr, max_end, step):
                count = min(chunk_size, max_end - start)
                chunk = client.read_holding_registers(start, count)
                if chunk is None:
                    continue
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
                        _enqueue({
                            'device_id': device_id,
                            'register_name': reg['name'],
                            'value': value,
                            'timestamp': timestamp,
                            'unit': reg.get('unit', '')
                        })

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
                            pass
                    self.data_queue.put_nowait(item)
                except (ValueError, TypeError, queue.Full):
                    pass

    def _collect_rest(self, client, device_id: str, device_config: dict[str, Any], timestamp):
        """采集REST设备的缓存数据（客户端自带轮询）"""
        self._collect_from_cache(client, device_id, timestamp)

    def _collect_opcua(self, client, device_id: str, device_config: dict[str, Any], timestamp):
        """采集OPC UA设备的缓存数据（客户端通过订阅自动更新缓存）"""
        self._collect_from_cache(client, device_id, timestamp)

    def _collect_mqtt(self, client, device_id: str, device_config: dict[str, Any], timestamp):
        """采集MQTT设备的缓存数据（客户端通过订阅自动更新缓存）"""
        self._collect_from_cache(client, device_id, timestamp)

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

    def _process_data(self):
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

                    quality = DataQualityAssessor.assess(
                        value=value,
                        register_name=register_name,
                        device_status=_device_status_cache[device_id],
                        last_value=self._last_values.get(key),
                        last_time=self._last_times.get(key)
                    )
                    data['quality'] = quality

                    # 更新跟踪状态
                    if value is not None and not (isinstance(value, float) and math.isnan(value)):
                        self._last_values[key] = value
                        self._last_times[key] = time.time()

                # === 批量写DB（单事务，比逐条快10-50倍） ===
                try:
                    self.database.insert_data_batch(batch)
                except Exception as e:
                    logger.warning(f"批量写入数据库失败 ({len(batch)} 条): {e}")
                    # 失败数据重新入队（标记 _db_retry 防止无限重试）
                    retried = 0
                    for item in batch:
                        if item.get('_db_retry'):
                            continue  # 已重试过一次，丢弃
                        item['_db_retry'] = True
                        try:
                            if not self.data_queue.full():
                                self.data_queue.put_nowait(item)
                                retried += 1
                            else:
                                break
                        except queue.Full:
                            break
                    if retried:
                        logger.info(f"已重新入队 {retried} 条数据等待重试")

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
                if hasattr(self.data_queue, 'clear_persistence'):
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
                    pass  # 缺陷数单独记录，不重复计数
                else:
                    # 总产量：同时记录合格品（98%合格率模拟）
                    total = int(value)
                    good = int(total * 0.98)
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
