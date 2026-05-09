"""
数据采集器模块
实现定时数据采集和数据处理
支持协议: Modbus TCP/RTU, OPC UA, MQTT, REST HTTP
"""

import time
import queue
import logging
import threading
from typing import Any
from datetime import datetime
from queue import Queue

logger = logging.getLogger(__name__)


class DataCollector:
    """
    数据采集器
    统一定时从多协议设备采集数据（Modbus/OPC UA/MQTT/REST）
    OPC UA和MQTT有自带的推送机制，仅对Modbus/REST做轮询
    """

    def __init__(self, device_manager, database, alarm_manager=None,
                 predictive_maintenance=None, oee_calculator=None,
                 spc_analyzer=None, energy_manager=None, edge_decision=None,
                 device_control=None, realtime_bridge=None):
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

        # TDengine实时数据桥接器（可选）
        self.realtime_bridge = realtime_bridge

        # 采集任务
        self.tasks = {}  # device_id -> threading.Timer
        self.running = False

        # 数据队列
        self.data_queue = Queue(maxsize=10000)

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
        """停止数据采集"""
        self.running = False

        for device_id, timer in self.tasks.items():
            timer.cancel()
        self.tasks.clear()

        self.device_manager.disconnect_all()
        logger.info("数据采集器已停止")

    def start_device_task(self, device_id: str, device_config: dict[str, Any]):
        """为指定设备启动采集任务（运行时添加设备时调用）

        设备存在 ⟹ 有采集任务 ⟹ 有数据
        设备不存在 ⟹ 无采集任务 ⟹ 无数据
        """
        if not self.running:
            logger.warning(f"数据采集器未运行，跳过设备 {device_id} 的采集启动")
            return

        if not device_config.get('enabled', True):
            logger.info(f"设备 {device_id} 已禁用，跳过采集启动")
            return

        if device_id in self.tasks:
            logger.debug(f"设备 {device_id} 已有采集任务，跳过")
            return

        protocol = device_config.get('protocol', 'modbus_tcp')
        if protocol in ('opcua', 'mqtt'):
            self._setup_push_device(device_id, device_config)
        else:
            self._start_device_collection(device_id, device_config)
        logger.info(f"已启动设备 {device_id} [{protocol}] 的采集任务")

    def remove_device_task(self, device_id: str):
        """删除设备的采集任务（设备被删除时调用）

        取消定时器，不再采集该设备数据。历史数据保留。
        """
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
            # 队列满时丢弃最旧数据，避免阻塞
            if self.data_queue.full():
                try:
                    self.data_queue.get_nowait()
                except queue.Empty:
                    pass
            self.data_queue.put({
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
        """启动单个设备的轮询采集任务（Modbus / REST）"""
        interval = device_config.get('collection_interval', 5)
        protocol = device_config.get('protocol', 'modbus_tcp')

        def collect_task():
            if not self.running:
                return

            self._collect_device_data(device_id, device_config, protocol)

            if self.running:
                timer = threading.Timer(interval, collect_task)
                timer.daemon = True
                timer.start()
                self.tasks[device_id] = timer

        collect_task()

    def _collect_device_data(self, device_id: str, device_config: dict[str, Any], protocol: str):
        """采集单个轮询型设备的数据"""
        self._inc_stat('total_collections')

        try:
            client = self.device_manager.get_client(device_id)
            if not client:
                # 设备已被删除，静默跳过（定时器会在 remove_device_task 中被取消）
                logger.debug(f"设备 {device_id} 客户端不存在，跳过采集")
                self._inc_stat('failed_collections')
                return

            if not getattr(client, 'connected', False):
                # 使用 device_manager.connect_device() 以便退避逻辑生效
                if not self.device_manager.connect_device(device_id):
                    self._inc_stat('failed_collections')
                    return

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

        except Exception as e:
            logger.error(f"采集设备 {device_id} 数据异常: {e}")
            self._inc_stat('failed_collections')

    def _collect_modbus(self, client, device_id: str, device_config: dict[str, Any], timestamp):
        """采集Modbus设备的寄存器数据"""
        registers = device_config.get('registers', [])
        for register in registers:
            data = self._read_register(client, register)
            if data is not None:
                self.data_queue.put({
                    'device_id': device_id,
                    'register_name': register['name'],
                    'value': data,
                    'timestamp': timestamp,
                    'unit': register.get('unit', '')
                })

    def _collect_from_cache(self, client, device_id: str, timestamp):
        """通用方法：从客户端缓存采集数据（适用于REST/OPC UA/MQTT）"""
        # 设备已断开时不去读取缓存（避免读取到断开前的旧数据）
        if not getattr(client, 'connected', False):
            logger.debug(f"设备 {device_id} 已断开，跳过缓存采集")
            return
        latest = client.get_latest_data()
        for name, data in latest.items():
            value = data.get('value')
            if value is not None:
                try:
                    self.data_queue.put({
                        'device_id': device_id,
                        'register_name': name,
                        'value': float(value) if value is not None else 0,
                        'timestamp': timestamp,
                        'unit': data.get('unit', '')
                    })
                except (ValueError, TypeError):
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
        """数据处理线程"""
        while self.running:
            try:
                data = self.data_queue.get(timeout=1)

                self.database.insert_data(
                    device_id=data['device_id'],
                    register_name=data['register_name'],
                    value=data['value'],
                    timestamp=data['timestamp'],
                    unit=data['unit']
                )

                # 同时写入TDengine（如果配置了桥接器）
                if self.realtime_bridge:
                    self.realtime_bridge.feed(
                        device_id=data['device_id'],
                        register_name=data['register_name'],
                        value=data['value'],
                        timestamp=data['timestamp'],
                        unit=data.get('unit', ''),
                        protocol=data.get('protocol', ''),
                        gateway_id=data.get('gateway_id', '')
                    )

                if self.alarm_manager:
                    self.alarm_manager.check_alarm(
                        device_id=data['device_id'],
                        register_name=data['register_name'],
                        value=data['value'],
                        timestamp=data['timestamp']
                    )

                # ===== 工业4.0智能层数据分发 =====
                device_id = data['device_id']
                register_name = data['register_name']
                value = data['value']
                timestamp = data['timestamp']

                # 预测性维护 — 喂入所有数值数据
                if self.predictive_maintenance:
                    self.predictive_maintenance.feed_data(
                        device_id, register_name, value, timestamp)

                # 边缘决策引擎 — 更新数据快照
                if self.edge_decision:
                    self.edge_decision.update_data(
                        f"{device_id}:{register_name}", value)

                # 能源管理 — 电力数据喂入
                if self.energy_manager:
                    power_keywords = ['power', 'watt', 'kw', 'active_power', 'reactive_power', 'apparent_power']
                    energy_keywords = ['energy', 'kwh', 'mwh', 'electricity', 'consumption']
                    
                    if any(kw in register_name.lower() for kw in power_keywords):
                        # 功率数据（kW）
                        power_value = value
                        if 'w' in register_name.lower() and 'kw' not in register_name.lower():
                            power_value = value / 1000  # W转kW
                        self.energy_manager.feed_power_data(
                            device_id, power_value, timestamp=timestamp)
                    elif any(kw in register_name.lower() for kw in energy_keywords):
                        # 累积电量数据（kWh）
                        self.energy_manager.feed_power_data(
                            device_id, 0, energy_kwh=value, timestamp=timestamp)

                # SPC — 质量相关数据喂入（扩展关键词范围）
                if self.spc_analyzer:
                    spc_keywords = [
                        'temperature', 'pressure', 'ph', 'quality', 'dimension',
                        'voltage', 'current', 'speed', 'flow', 'level',
                        'humidity', 'torque', 'frequency', 'thickness',
                        'viscosity', 'density', 'concentration', 'turbidity',
                        'conductivity', 'oxygen', 'vibration', 'force',
                        'position', 'distance', 'cycle_time', 'injection',
                        'mold', 'dryer', 'distill', 'boiler', 'heat_exchanger',
                        'spray', 'coating', 'sealing', 'conveyor'
                    ]
                    if any(kw in register_name.lower() for kw in spc_keywords):
                        self.spc_analyzer.feed_data(device_id, register_name, value)

                # OEE — 设备状态和产量数据喂入
                if self.oee_calculator:
                    # 设备运行状态（兼容多种寄存器名称）
                    status_keywords = ['status', 'state', 'running', 'line_status', 'boiler_status', 'packing_status']
                    if any(kw in register_name.lower() for kw in status_keywords):
                        status_map = {0: 'stopped', 1: 'idle', 2: 'running', 3: 'fault', 4: 'maintenance', 5: 'setup'}
                        status = status_map.get(int(value), 'stopped')
                        self.oee_calculator.update_device_state(device_id, status)
                    
                    # 产品计数（兼容多种寄存器名称）
                    count_keywords = ['count', 'product', 'shot', 'label', 'palletizing', 'batch', 'painted', 'quantity']
                    if any(kw in register_name.lower() for kw in count_keywords):
                        # 区分合格品和总产量
                        good_keywords = ['good', 'ok', 'pass', 'qualified']
                        reject_keywords = ['reject', 'ng', 'defect', 'scrap']
                        
                        if any(kw in register_name.lower() for kw in good_keywords):
                            self.oee_calculator.record_production(device_id, good_count=int(value))
                        elif any(kw in register_name.lower() for kw in reject_keywords):
                            # 不良品不计入合格品
                            pass
                        else:
                            # 默认作为总产量
                            self.oee_calculator.record_production(device_id, count=int(value))

                # 安全联锁检查（每次数据采集时触发）
                if self.device_control:
                    self.device_control.check_interlocks(
                        device_id, register_name, value)

                with self._stats_lock:
                    self.stats['queue_size'] = self.data_queue.qsize()

            except queue.Empty:
                continue
            except Exception as e:
                if self.running:
                    logger.error(f"数据处理异常: {e}", exc_info=True)
                    # 打印更多调试信息
                    logger.error(f"异常数据: {data if 'data' in locals() else 'N/A'}")

    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        with self._stats_lock:
            return {
                'running': self.running,
                'queue_size': self.data_queue.qsize(),
                **self.stats
            }
