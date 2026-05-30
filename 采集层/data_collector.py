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


# --- 智能分发关键字匹配 ---
_power_kw = ('power', 'watt', 'kw', 'kwh', 'active_power', 'reactive_power', 'apparent_power')
_energy_kw = ('energy', 'kwh', 'consumption', 'electricity', 'water_flow', 'gas_flow')
_spc_kw = ('temperature', 'pressure', 'humidity', 'viscosity', 'ph', 'concentration')
_status_kw = ('status', 'state', 'running', 'fault', 'alarm', 'speed', 'rpm')
_count_kw = ('count', 'quantity', 'output', 'production', 'yield', 'total')

# --- 命名常量（替代魔法数字） ---
BACKOFF_CEILING_S = 60              # 退避上限
SENSOR_STUCK_WINDOW_S = 300         # 传感器卡死窗口
BATCH_MAX = 200                     # 批处理最大条数
BATCH_TIMEOUT_S = 0.5               # 批处理超时


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
                            self._queue.put_nowait(item)
                            recovered += 1
                        except (json.JSONDecodeError, queue.Full):
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
        """停止数据采集（安全排空队列）"""
        self.running = False

        # 线程安全地取消所有定时器
        with self._tasks_lock:
            for device_id, timer in self.tasks.items():
                timer.cancel()
            self.tasks.clear()

        # 排空数据队列
        drained = 0
        while not self.data_queue.empty():
            try:
                self.data_queue.get_nowait()
                drained += 1
            except queue.Empty:
                break
        if drained > 0:
            logger.info(f"排空数据队列: {drained} 条数据已丢弃")

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

    def _setup_push_device(self, device_id: str, device_config: dict[str, Any]):
        """设置推送型设备（OPC UA / MQTT）"""
        protocol = device_config.get('protocol', 'modbus_tcp')
        client = self.device_manager.get_client(device_id)
        if not client:
            logger.error(f"设备 {device_id} 客户端创建失败")
            return

        def on_data(device_id, name, value, unit):
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

            success = self._collect_device_data(device_id, device_config, protocol)

            if not self.running:
                return

            # 失败退避：连续失败越多，间隔越长（1s→2s→4s→8s→...→60s）
            if success:
                self._failure_counts[device_id] = 0
                interval = self._get_dynamic_interval(device_id, base_interval)
            else:
                failures = self._failure_counts.get(device_id, 0) + 1
                self._failure_counts[device_id] = failures
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

            # 检查健康评分（如果预测性维护模块可用）
            if self.predictive_maintenance:
                health_scores = self.predictive_maintenance.get_health_scores()
                device_health = [
                    s for s in health_scores.values()
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

            if protocol in ('modbus_tcp', 'modbus_rtu'):
                self._collect_modbus(client, device_id, device_config, timestamp)
            elif protocol == 'rest':
                self._collect_rest(client, device_id, device_config, timestamp)
            elif protocol == 'opcua':
                self._collect_opcua(client, device_id, device_config, timestamp)
            elif protocol == 'mqtt':
                self._collect_mqtt(client, device_id, device_config, timestamp)

            self._inc_stat('successful_collections')
            with self._stats_lock:
                self.stats['last_collection_time'] = timestamp
            return True

        except Exception as e:
            logger.error(f"采集设备 {device_id} 数据异常: {e}")
            self._inc_stat('failed_collections')
            return False

    def _collect_modbus(self, client, device_id: str, device_config: dict[str, Any], timestamp):
        """
        采集Modbus设备的寄存器数据（批量读取优化）

        按 Modbus 规范：FC03 单次最多读 125 个寄存器。
        将连续地址范围合并为一次请求，减少网络往返。
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
                return
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
            for start in range(min_addr, max_end, 125):
                count = min(125, max_end - start)
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
        """数据处理线程 — 批量写DB + 报警检查，智能层异步分发"""
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

            try:
                # === 数据质量评估（OPC UA标准） ===
                for data in batch:
                    device_id = data.get('device_id', '')
                    register_name = data.get('register_name', '')
                    value = data.get('value')
                    key = f"{device_id}:{register_name}"

                    # 获取设备状态
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

                    quality = DataQualityAssessor.assess(
                        value=value,
                        register_name=register_name,
                        device_status=device_status,
                        last_value=self._last_values.get(key),
                        last_time=self._last_times.get(key)
                    )
                    data['quality'] = quality

                    # 更新跟踪状态
                    if value is not None and not (isinstance(value, float) and math.isnan(value)):
                        self._last_values[key] = value
                        self._last_times[key] = time.time()

                # === 批量写DB（单事务，比逐条快10-50倍） ===
                self.database.insert_data_batch(batch)

                # === 报警检查（每条都要检查） ===
                if self.alarm_manager:
                    for data in batch:
                        self.alarm_manager.check_alarm(
                            device_id=data['device_id'],
                            register_name=data['register_name'],
                            value=data['value'],
                            timestamp=data['timestamp']
                        )

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
                            logger.debug(f"智能分发异常: {e}")

                # === 智能层：扔进异步队列 ===
                for data in batch:
                    if not intel_queue.full():
                        intel_queue.put_nowait(data)

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
                status = status_map.get(int(value), 'stopped')
                self.oee_calculator.update_device_state(device_id, status)
            elif _has_keyword(name_lower, _count_kw):
                good_kw = frozenset(['good', 'ok', 'pass', 'qualified'])
                reject_kw = frozenset(['reject', 'ng', 'defect', 'scrap'])
                if _has_keyword(name_lower, good_kw):
                    self.oee_calculator.record_production(device_id, good_count=int(value))
                elif not _has_keyword(name_lower, reject_kw):
                    self.oee_calculator.record_production(device_id, count=int(value))

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
