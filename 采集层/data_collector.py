"""
数据采集器模块
实现定时数据采集和数据处理
"""

import time
import logging
import threading
from typing import Dict, List, Any, Optional
from datetime import datetime
from queue import Queue

logger = logging.getLogger(__name__)


class DataCollector:
    """
    数据采集器
    负责定时从Modbus设备采集数据
    """
    
    def __init__(self, device_manager, database, alarm_manager=None):
        """
        初始化数据采集器
        
        Args:
            device_manager: 设备管理器实例
            database: 数据库实例
            alarm_manager: 报警管理器实例（可选）
        """
        self.device_manager = device_manager
        self.database = database
        self.alarm_manager = alarm_manager
        
        # 采集任务
        self.tasks = {}  # device_id -> threading.Timer
        self.running = False
        
        # 数据队列
        self.data_queue = Queue(maxsize=10000)
        
        # 统计信息
        self.stats = {
            'total_collections': 0,
            'successful_collections': 0,
            'failed_collections': 0,
            'last_collection_time': None,
            'queue_size': 0
        }
        
        # 数据处理线程
        self.process_thread = None
    
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
        for device_id, device_config in devices.items():
            if device_config.get('enabled', True):
                self._start_device_collection(device_id, device_config)
        
        logger.info(f"数据采集器已启动，共 {len(devices)} 个设备")
    
    def stop(self):
        """停止数据采集"""
        self.running = False
        
        # 取消所有采集任务
        for device_id, timer in self.tasks.items():
            timer.cancel()
        self.tasks.clear()
        
        logger.info("数据采集器已停止")
    
    def _start_device_collection(self, device_id: str, device_config: Dict):
        """
        启动单个设备的采集任务
        
        Args:
            device_id: 设备ID
            device_config: 设备配置
        """
        interval = device_config.get('collection_interval', 5)
        
        def collect_task():
            if not self.running:
                return
            
            # 执行采集
            self._collect_device_data(device_id, device_config)
            
            # 安排下次采集
            if self.running:
                timer = threading.Timer(interval, collect_task)
                timer.daemon = True
                timer.start()
                self.tasks[device_id] = timer
        
        # 立即开始第一次采集
        collect_task()
    
    def _collect_device_data(self, device_id: str, device_config: Dict):
        """
        采集单个设备的数据
        
        Args:
            device_id: 设备ID
            device_config: 设备配置
        """
        self.stats['total_collections'] += 1
        
        try:
            # 获取设备客户端
            client = self.device_manager.get_client(device_id)
            if not client:
                logger.error(f"设备 {device_id} 客户端不存在")
                self.stats['failed_collections'] += 1
                return
            
            # 确保连接
            if not client.connected:
                if not client.connect():
                    logger.error(f"设备 {device_id} 连接失败")
                    self.stats['failed_collections'] += 1
                    return
            
            # 采集所有寄存器数据
            registers = device_config.get('registers', [])
            timestamp = datetime.now()
            
            for register in registers:
                data = self._read_register(client, register)
                if data is not None:
                    # 放入数据队列
                    self.data_queue.put({
                        'device_id': device_id,
                        'register_name': register['name'],
                        'value': data,
                        'timestamp': timestamp,
                        'unit': register.get('unit', '')
                    })
            
            self.stats['successful_collections'] += 1
            self.stats['last_collection_time'] = timestamp
            
        except Exception as e:
            logger.error(f"采集设备 {device_id} 数据异常: {e}")
            self.stats['failed_collections'] += 1
    
    def _read_register(self, client, register: Dict) -> Optional[float]:
        """
        读取单个寄存器数据
        
        Args:
            client: Modbus客户端
            register: 寄存器配置
            
        Returns:
            float: 读取的值，失败返回None
        """
        try:
            address = register['address']
            data_type = register.get('data_type', 'uint16')
            scale = register.get('scale', 1)
            offset = register.get('offset', 0)
            
            # 根据数据类型读取
            if data_type == 'float32':
                # 浮点数需要读取2个寄存器
                raw_values = client.read_holding_registers(address, 2)
                if raw_values is None or len(raw_values) < 2:
                    return None
                value = client.decode_float32(raw_values)
            elif data_type == 'float64':
                # 双精度浮点需要读取4个寄存器
                raw_values = client.read_holding_registers(address, 4)
                if raw_values is None or len(raw_values) < 4:
                    return None
                value = client.decode_float32(raw_values[:2])  # 简化处理
            else:
                # 整数读取1个寄存器
                raw_values = client.read_holding_registers(address, 1)
                if raw_values is None:
                    return None
                
                if data_type == 'uint16':
                    value = client.decode_uint16(raw_values[0])
                elif data_type == 'int16':
                    value = client.decode_int16(raw_values[0])
                else:
                    value = raw_values[0]
            
            # 应用缩放和偏移
            value = value * scale + offset
            
            return round(value, 4)
            
        except Exception as e:
            logger.error(f"读取寄存器异常: {e}")
            return None
    
    def _process_data(self):
        """数据处理线程"""
        import queue
        while self.running:
            try:
                # 从队列获取数据
                data = self.data_queue.get(timeout=1)
                
                # 存储到数据库
                self.database.insert_data(
                    device_id=data['device_id'],
                    register_name=data['register_name'],
                    value=data['value'],
                    timestamp=data['timestamp'],
                    unit=data['unit']
                )
                
                # 检查报警
                if self.alarm_manager:
                    self.alarm_manager.check_alarm(
                        device_id=data['device_id'],
                        register_name=data['register_name'],
                        value=data['value'],
                        timestamp=data['timestamp']
                    )
                
                self.stats['queue_size'] = self.data_queue.qsize()
                
            except queue.Empty:
                # 队列超时，继续循环
                continue
            except Exception as e:
                if self.running:
                    logger.error(f"数据处理异常: {e}")
    
    def get_stats(self) -> Dict[str, Any]:
        """
        获取统计信息
        
        Returns:
            Dict: 统计信息字典
        """
        return {
            'running': self.running,
            'queue_size': self.data_queue.qsize(),
            **self.stats
        }
