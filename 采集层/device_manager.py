"""
设备管理器模块
管理所有Modbus设备的连接和配置
"""

import logging
import yaml
from typing import Dict, List, Any, Optional
from pathlib import Path

from .modbus_client import ModbusClient
from .simulated_client import SimulatedModbusClient

logger = logging.getLogger(__name__)


class DeviceManager:
    """
    设备管理器
    负责管理所有Modbus设备的连接和配置
    """
    
    def __init__(self, config_path: str = None, simulation_mode: bool = True):
        """
        初始化设备管理器
        
        Args:
            config_path: 设备配置文件路径
            simulation_mode: 是否启用模拟模式（无真实设备时使用模拟数据）
        """
        self.config_path = config_path or '配置/devices.yaml'
        self.simulation_mode = simulation_mode
        self.devices = {}  # device_id -> device_config
        self.clients = {}  # device_id -> ModbusClient
        
        # 加载设备配置
        self.load_config()
    
    def load_config(self):
        """加载设备配置文件"""
        try:
            config_file = Path(self.config_path)
            if not config_file.exists():
                logger.error(f"配置文件不存在: {self.config_path}")
                return
            
            with open(config_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            
            # 解析设备配置
            devices_config = config.get('devices', [])
            for device_config in devices_config:
                device_id = device_config.get('id')
                if device_id:
                    self.devices[device_id] = device_config
                    logger.info(f"加载设备配置: {device_id} - {device_config.get('name')}")
            
            logger.info(f"共加载 {len(self.devices)} 个设备配置")
            
        except Exception as e:
            logger.error(f"加载配置文件异常: {e}")
    
    def get_device_config(self, device_id: str) -> Optional[Dict]:
        """
        获取设备配置
        
        Args:
            device_id: 设备ID
            
        Returns:
            Dict: 设备配置字典
        """
        return self.devices.get(device_id)
    
    def get_all_devices(self) -> Dict[str, Dict]:
        """
        获取所有设备配置
        
        Returns:
            Dict: 设备配置字典
        """
        return self.devices.copy()
    
    def get_client(self, device_id: str):
        """
        获取设备客户端
        
        Args:
            device_id: 设备ID
            
        Returns:
            ModbusClient 或 SimulatedModbusClient: 客户端实例
        """
        # 如果客户端不存在，创建新的
        if device_id not in self.clients:
            device_config = self.devices.get(device_id)
            if not device_config:
                logger.error(f"设备 {device_id} 配置不存在")
                return None
            
            if self.simulation_mode:
                client = SimulatedModbusClient(device_config)
                client.connect()
            else:
                client = ModbusClient(device_config)
            self.clients[device_id] = client
        
        return self.clients[device_id]
    
    def connect_device(self, device_id: str) -> bool:
        """
        连接设备
        
        Args:
            device_id: 设备ID
            
        Returns:
            bool: 连接是否成功
        """
        client = self.get_client(device_id)
        if not client:
            return False
        
        return client.connect()
    
    def disconnect_device(self, device_id: str):
        """
        断开设备连接
        
        Args:
            device_id: 设备ID
        """
        client = self.clients.get(device_id)
        if client:
            client.disconnect()
    
    def connect_all(self) -> Dict[str, bool]:
        """
        连接所有设备
        
        Returns:
            Dict: 设备连接状态
        """
        results = {}
        for device_id in self.devices:
            results[device_id] = self.connect_device(device_id)
        return results
    
    def disconnect_all(self):
        """断开所有设备连接"""
        for device_id in list(self.clients.keys()):
            self.disconnect_device(device_id)
    
    def get_device_status(self, device_id: str) -> Dict[str, Any]:
        """
        获取设备状态
        
        Args:
            device_id: 设备ID
            
        Returns:
            Dict: 设备状态信息
        """
        client = self.clients.get(device_id)
        device_config = self.devices.get(device_id)
        
        if not device_config:
            return {'error': '设备配置不存在'}
        
        status = {
            'device_id': device_id,
            'name': device_config.get('name'),
            'description': device_config.get('description'),
            'protocol': device_config.get('protocol'),
            'host': device_config.get('host'),
            'port': device_config.get('port'),
            'enabled': device_config.get('enabled', True),
            'connected': False,
            'registers': device_config.get('registers', []),
            'stats': {}
        }
        
        if client:
            status['connected'] = client.connected
            status['stats'] = client.get_stats()
        
        return status
    
    def get_all_status(self) -> List[Dict[str, Any]]:
        """
        获取所有设备状态
        
        Returns:
            List: 设备状态列表
        """
        status_list = []
        for device_id in self.devices:
            status = self.get_device_status(device_id)
            status_list.append(status)
        return status_list
    
    def add_device(self, device_config: Dict) -> bool:
        """
        添加设备
        
        Args:
            device_config: 设备配置
            
        Returns:
            bool: 添加是否成功
        """
        try:
            device_id = device_config.get('id')
            if not device_id:
                logger.error("设备配置缺少id字段")
                return False
            
            # 检查是否已存在
            if device_id in self.devices:
                logger.warning(f"设备 {device_id} 已存在，将被覆盖")
            
            # 添加到内存
            self.devices[device_id] = device_config
            
            # 保存到配置文件
            self._save_config()
            
            logger.info(f"添加设备: {device_id}")
            return True
            
        except Exception as e:
            logger.error(f"添加设备异常: {e}")
            return False
    
    def remove_device(self, device_id: str) -> bool:
        """
        移除设备
        
        Args:
            device_id: 设备ID
            
        Returns:
            bool: 移除是否成功
        """
        try:
            # 断开连接
            self.disconnect_device(device_id)
            
            # 从内存移除
            if device_id in self.devices:
                del self.devices[device_id]
            
            if device_id in self.clients:
                del self.clients[device_id]
            
            # 保存到配置文件
            self._save_config()
            
            logger.info(f"移除设备: {device_id}")
            return True
            
        except Exception as e:
            logger.error(f"移除设备异常: {e}")
            return False
    
    def _save_config(self):
        """保存设备配置到文件"""
        try:
            config = {
                'devices': list(self.devices.values())
            }
            
            config_file = Path(self.config_path)
            config_file.parent.mkdir(parents=True, exist_ok=True)
            
            with open(config_file, 'w', encoding='utf-8') as f:
                yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
            
            logger.info("设备配置已保存")
            
        except Exception as e:
            logger.error(f"保存配置文件异常: {e}")
