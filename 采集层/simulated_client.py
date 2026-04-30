"""
模拟数据源模块
在没有真实Modbus设备时提供模拟数据
"""

import math
import time
import random
import logging
import threading
from datetime import datetime
from typing import Dict, List, Any

logger = logging.getLogger(__name__)


class SimulatedModbusClient:
    """
    模拟Modbus客户端
    生成仿真工业数据
    """
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.device_id = config.get('id')
        self.device_name = config.get('name')
        self.connected = False
        self.start_time = time.time()
        
        # 统计信息
        self.stats = {
            'total_reads': 0,
            'successful_reads': 0,
            'failed_reads': 0,
            'last_read_time': None,
            'last_error': None
        }
    
    def connect(self) -> bool:
        """模拟连接（始终成功）"""
        self.connected = True
        logger.info(f"[模拟] 设备 {self.device_name} 连接成功")
        return True
    
    def disconnect(self):
        """断开连接"""
        self.connected = False
        logger.info(f"[模拟] 设备 {self.device_name} 已断开")
    
    def read_holding_registers(self, address: int, count: int, 
                               slave_id: int = None) -> List[int]:
        """读取保持寄存器（返回模拟数据）"""
        if not self.connected:
            return None
        
        self.stats['total_reads'] += 1
        self.stats['successful_reads'] += 1
        self.stats['last_read_time'] = time.time()
        
        t = time.time() - self.start_time
        
        # 根据设备类型生成不同的模拟数据
        # 注意：返回的是原始寄存器值，scale由采集层应用
        if 'temp' in self.device_id:
            # 温度: 20~35°C，scale=0.1，所以寄存器值=200~350
            base = 275 + 75 * math.sin(t / 60)
            noise = random.gauss(0, 3)
            value = base + noise
            if count == 2:
                # float32（寄存器值，未缩放）
                import struct
                raw = struct.pack('>f', value)
                return [struct.unpack('>H', raw[0:2])[0], struct.unpack('>H', raw[2:4])[0]]
            return [int(value)]
        
        elif 'pressure' in self.device_id:
            # 压力: 0.3~0.7 MPa，scale=0.01，所以寄存器值=30~70
            base = 50 + 20 * math.sin(t / 45)
            noise = random.gauss(0, 1)
            value = base + noise
            if count == 2:
                import struct
                raw = struct.pack('>f', value)
                return [struct.unpack('>H', raw[0:2])[0], struct.unpack('>H', raw[2:4])[0]]
            return [int(value)]
        
        elif 'power' in self.device_id:
            # 电力仪表: 电压/电流/功率/电量
            # 根据地址判断寄存器类型
            if address == 0:  # 电压，scale=0.1，寄存器值=2200
                value = 2200 + random.gauss(0, 20)
                if count == 2:
                    import struct
                    raw = struct.pack('>f', value)
                    return [struct.unpack('>H', raw[0:2])[0], struct.unpack('>H', raw[2:4])[0]]
                return [int(value)]
            elif address == 2:  # 电流，scale=0.01，寄存器值=5000~7000
                value = 5000 + 2000 * math.sin(t / 30) + random.gauss(0, 100)
                if count == 2:
                    import struct
                    raw = struct.pack('>f', value)
                    return [struct.unpack('>H', raw[0:2])[0], struct.unpack('>H', raw[2:4])[0]]
                return [int(value)]
            elif address == 4:  # 功率，scale=0.001，寄存器值=10000~15000
                value = 10000 + 5000 * math.sin(t / 20) + random.gauss(0, 500)
                if count == 2:
                    import struct
                    raw = struct.pack('>f', value)
                    return [struct.unpack('>H', raw[0:2])[0], struct.unpack('>H', raw[2:4])[0]]
                return [int(value)]
            elif address == 6:  # 电量
                value = 1000 + t * 0.1  # 累计电量随时间增加
                if count == 4:  # float64需要4个寄存器
                    import struct
                    raw = struct.pack('>d', value)
                    return [struct.unpack('>H', raw[i:i+2])[0] for i in range(0, 8, 2)]
                elif count == 2:
                    import struct
                    raw = struct.pack('>f', value)
                    return [struct.unpack('>H', raw[0:2])[0], struct.unpack('>H', raw[2:4])[0]]
                return [int(value * 100)]
        
        # 默认随机值
        return [random.randint(0, 1000)]
    
    def decode_float32(self, registers: List[int]) -> float:
        """解码32位浮点数"""
        import struct
        raw = (registers[0] << 16) | registers[1]
        return struct.unpack('>f', struct.pack('>I', raw))[0]
    
    def decode_uint16(self, register: int) -> int:
        return register & 0xFFFF
    
    def decode_int16(self, register: int) -> int:
        if register & 0x8000:
            return register - 0x10000
        return register
    
    def write_single_register(self, address: int, value: int, slave_id: int = None) -> bool:
        """写入单个寄存器（模拟成功）"""
        if not self.connected:
            return False
        
        logger.info(f"[模拟] 设备 {self.device_name} 写入寄存器: address={address}, value={value}")
        return True
    
    def write_single_coil(self, address: int, value: bool, slave_id: int = None) -> bool:
        """写入单个线圈（模拟成功）"""
        if not self.connected:
            return False
        
        logger.info(f"[模拟] 设备 {self.device_name} 写入线圈: address={address}, value={value}")
        return True
    
    def get_stats(self) -> Dict[str, Any]:
        return {
            'device_id': self.device_id,
            'device_name': self.device_name,
            'connected': self.connected,
            **self.stats
        }
