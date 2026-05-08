"""
Modbus网关 (Modbus Gateway)

独立的Modbus协议网关服务，负责：
1. 通过Modbus TCP/RTU连接PLC设备
2. 读取寄存器数据
3. 转换为统一物模型
4. 通过MQTT发布标准化数据

支持：
- Modbus TCP
- Modbus RTU (串口)
- 自动重连
- 多设备并发采集
"""

import time
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime

from pymodbus.client import ModbusTcpClient, ModbusSerialClient
from pymodbus.exceptions import ModbusException
from pymodbus.constants import Endian
from pymodbus.payload import BinaryPayloadDecoder

from .base_gateway import BaseGateway
from .thing_model import (
    DeviceTelemetry, ThingModelConverter, 
    ProtocolType, DataQuality
)


class ModbusGateway(BaseGateway):
    """
    Modbus网关
    
    配置示例：
    {
        "gateway_id": "modbus_gateway_01",
        "mqtt_broker": "localhost",
        "mqtt_port": 1883,
        "poll_interval": 5.0,
        "devices": [
            {
                "device_id": "PLC_001",
                "protocol": "tcp",  # tcp 或 rtu
                "host": "192.168.1.100",
                "port": 502,
                "slave_id": 1,
                "registers": [
                    {"name": "temperature", "address": 0, "count": 2, "type": "float32"},
                    {"name": "pressure", "address": 2, "count": 2, "type": "float32"},
                    {"name": "status", "address": 10, "count": 1, "type": "uint16"}
                ]
            }
        ]
    }
    """
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        
        # Modbus客户端缓存
        self._clients: Dict[str, Any] = {}
        
        # 寄存器配置缓存
        self._register_configs: Dict[str, List[Dict]] = {}
        
        # 解析设备配置
        for device_config in self.devices_config:
            device_id = device_config.get('device_id')
            if device_id:
                self._register_configs[device_id] = device_config.get('registers', [])
    
    def connect(self) -> bool:
        """连接所有Modbus设备"""
        all_connected = True
        
        for device_config in self.devices_config:
            device_id = device_config.get('device_id')
            if not device_id:
                continue
            
            try:
                client = self._create_client(device_config)
                if client and client.connect():
                    self._clients[device_id] = client
                    self.connected_devices[device_id] = True
                    self.logger.info(f"设备 {device_id} 连接成功")
                else:
                    self.connected_devices[device_id] = False
                    self.logger.error(f"设备 {device_id} 连接失败")
                    all_connected = False
            except Exception as e:
                self.logger.error(f"设备 {device_id} 连接异常: {e}")
                self.connected_devices[device_id] = False
                all_connected = False
        
        return all_connected
    
    def disconnect(self):
        """断开所有Modbus设备"""
        for device_id, client in self._clients.items():
            try:
                client.close()
                self.logger.info(f"设备 {device_id} 已断开")
            except Exception as e:
                self.logger.error(f"设备 {device_id} 断开异常: {e}")
        
        self._clients.clear()
        self.connected_devices.clear()
    
    def _create_client(self, device_config: Dict) -> Any:
        """创建Modbus客户端"""
        protocol = device_config.get('protocol', 'tcp')
        
        if protocol == 'tcp':
            host = device_config.get('host', 'localhost')
            port = device_config.get('port', 502)
            return ModbusTcpClient(host, port=port, timeout=10)
        
        elif protocol == 'rtu':
            port = device_config.get('serial_port', 'COM1')
            baudrate = device_config.get('baudrate', 9600)
            parity = device_config.get('parity', 'N')
            stopbits = device_config.get('stopbits', 1)
            bytesize = device_config.get('bytesize', 8)
            
            return ModbusSerialClient(
                port=port,
                baudrate=baudrate,
                parity=parity,
                stopbits=stopbits,
                bytesize=bytesize,
                timeout=1
            )
        
        else:
            self.logger.error(f"不支持的协议: {protocol}")
            return None
    
    def read_device_data(self, device_id: str) -> Optional[Dict[str, float]]:
        """
        读取单个设备的寄存器数据
        
        Returns:
            Dict[str, float]: {register_name: value}
        """
        client = self._clients.get(device_id)
        if not client:
            self.logger.error(f"设备 {device_id} 未连接")
            return None
        
        register_configs = self._register_configs.get(device_id, [])
        if not register_configs:
            self.logger.warning(f"设备 {device_id} 无寄存器配置")
            return None
        
        # 获取slave_id
        device_config = next(
            (d for d in self.devices_config if d.get('device_id') == device_id), 
            {}
        )
        slave_id = device_config.get('slave_id', 1)
        
        result = {}
        
        for reg_config in register_configs:
            name = reg_config.get('name')
            address = reg_config.get('address', 0)
            count = reg_config.get('count', 1)
            data_type = reg_config.get('type', 'uint16')
            
            try:
                value = self._read_register(client, slave_id, address, count, data_type)
                if value is not None:
                    result[name] = value
                else:
                    self.logger.warning(f"设备 {device_id} 寄存器 {name} 读取失败")
            except Exception as e:
                self.logger.error(f"设备 {device_id} 寄存器 {name} 读取异常: {e}")
        
        return result if result else None
    
    def _read_register(self, client, slave_id: int, address: int, 
                       count: int, data_type: str) -> Optional[float]:
        """
        读取单个寄存器
        
        Args:
            client: Modbus客户端
            slave_id: 从站地址
            address: 寄存器地址
            count: 寄存器数量
            data_type: 数据类型 (uint16, int16, float32, float64, uint32, int32)
        """
        try:
            # 读取保持寄存器
            response = client.read_holding_registers(address, count, slave=slave_id)
            
            if response.isError():
                self.logger.error(f"Modbus错误: {response}")
                return None
            
            registers = response.registers
            
            # 根据数据类型解码
            if data_type == 'uint16':
                return float(registers[0])
            
            elif data_type == 'int16':
                value = registers[0]
                if value > 32767:
                    value -= 65536
                return float(value)
            
            elif data_type == 'float32':
                if len(registers) < 2:
                    return None
                decoder = BinaryPayloadDecoder.fromRegisters(
                    registers, byteorder=Endian.BIG, wordorder=Endian.BIG
                )
                return decoder.decode_32bit_float()
            
            elif data_type == 'float64':
                if len(registers) < 4:
                    return None
                decoder = BinaryPayloadDecoder.fromRegisters(
                    registers, byteorder=Endian.BIG, wordorder=Endian.BIG
                )
                return decoder.decode_64bit_float()
            
            elif data_type == 'uint32':
                if len(registers) < 2:
                    return None
                return float((registers[0] << 16) | registers[1])
            
            elif data_type == 'int32':
                if len(registers) < 2:
                    return None
                value = (registers[0] << 16) | registers[1]
                if value > 2147483647:
                    value -= 4294967296
                return float(value)
            
            else:
                self.logger.error(f"不支持的数据类型: {data_type}")
                return None
        
        except ModbusException as e:
            self.logger.error(f"Modbus异常: {e}")
            return None
        except Exception as e:
            self.logger.error(f"解码异常: {e}")
            return None
    
    def convert_to_telemetry(self, device_id: str, raw_data: Dict[str, float]) -> DeviceTelemetry:
        """将原始Modbus数据转换为统一物模型"""
        return ThingModelConverter.from_modbus_registers(
            device_id=device_id,
            registers=raw_data,
            gateway_id=self.gateway_id
        )
    
    def reconnect_device(self, device_id: str) -> bool:
        """重连单个设备"""
        device_config = next(
            (d for d in self.devices_config if d.get('device_id') == device_id),
            None
        )
        
        if not device_config:
            return False
        
        # 关闭旧连接
        old_client = self._clients.get(device_id)
        if old_client:
            try:
                old_client.close()
            except:
                pass
        
        # 创建新连接
        try:
            client = self._create_client(device_config)
            if client and client.connect():
                self._clients[device_id] = client
                self.connected_devices[device_id] = True
                self.logger.info(f"设备 {device_id} 重连成功")
                return True
        except Exception as e:
            self.logger.error(f"设备 {device_id} 重连失败: {e}")
        
        self.connected_devices[device_id] = False
        return False
    
    def write_register(self, device_id: str, address: int, value: int, 
                       slave_id: int = None) -> bool:
        """
        写入单个寄存器（用于设备控制）
        
        Args:
            device_id: 设备ID
            address: 寄存器地址
            value: 写入值
            slave_id: 从站地址（可选）
        """
        client = self._clients.get(device_id)
        if not client:
            return False
        
        if slave_id is None:
            device_config = next(
                (d for d in self.devices_config if d.get('device_id') == device_id),
                {}
            )
            slave_id = device_config.get('slave_id', 1)
        
        try:
            response = client.write_register(address, value, slave=slave_id)
            return not response.isError()
        except Exception as e:
            self.logger.error(f"写入失败: {e}")
            return False
    
    def write_coil(self, device_id: str, address: int, value: bool, 
                   slave_id: int = None) -> bool:
        """
        写入线圈（用于开关控制）
        """
        client = self._clients.get(device_id)
        if not client:
            return False
        
        if slave_id is None:
            device_config = next(
                (d for d in self.devices_config if d.get('device_id') == device_id),
                {}
            )
            slave_id = device_config.get('slave_id', 1)
        
        try:
            response = client.write_coil(address, value, slave=slave_id)
            return not response.isError()
        except Exception as e:
            self.logger.error(f"写入线圈失败: {e}")
            return False


# 测试代码
if __name__ == "__main__":
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # 测试配置
    config = {
        "gateway_id": "modbus_gateway_test",
        "mqtt_broker": "localhost",
        "mqtt_port": 1883,
        "poll_interval": 5.0,
        "devices": [
            {
                "device_id": "PLC_001",
                "protocol": "tcp",
                "host": "192.168.1.100",
                "port": 502,
                "slave_id": 1,
                "registers": [
                    {"name": "temperature", "address": 0, "count": 2, "type": "float32"},
                    {"name": "pressure", "address": 2, "count": 2, "type": "float32"},
                    {"name": "status", "address": 10, "count": 1, "type": "uint16"}
                ]
            }
        ]
    }
    
    # 创建并启动网关
    gateway = ModbusGateway(config)
    
    try:
        gateway.start()
        
        # 保持运行
        while True:
            time.sleep(1)
            stats = gateway.get_stats()
            print(f"统计: {stats}")
    
    except KeyboardInterrupt:
        print("正在停止...")
    finally:
        gateway.stop()
