"""
模拟设备管理器
完全独立的模拟实现，不依赖真实设备
"""

import logging
import yaml
from typing import Any
from pathlib import Path

from .interfaces import IDeviceManager, IDeviceClient
from .simulated_client import (
    SimulatedModbusClient,
    SimulatedOPCUAClient,
    SimulatedMQTTClient,
    SimulatedRESTClient
)

logger = logging.getLogger(__name__)


class SimulatedDeviceManager(IDeviceManager):
    """
    模拟设备管理器
    
    特点：
    - 完全独立，不依赖任何真实硬件
    - 所有设备使用模拟客户端
    - 生成仿真数据用于演示和测试
    - 可以独立运行，无需切换模式
    """

    SUPPORTED_PROTOCOLS = ['modbus_tcp', 'modbus_rtu', 'opcua', 'mqtt', 'rest']

    def __init__(self, config_path: str | None = None):
        """
        初始化模拟设备管理器
        
        Args:
            config_path: 设备配置文件路径
        """
        self.config_path = config_path or '配置/devices.yaml'
        self.simulation_mode = True  # 模拟设备管理器始终为模拟模式
        self.devices: dict[str, dict[str, Any]] = {}
        self.clients: dict[str, IDeviceClient] = {}
        
        # 加载设备配置
        self.load_config()
        
        logger.info("模拟设备管理器初始化完成")

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
                    protocol = device_config.get('protocol', 'modbus_tcp')
                    logger.info(f"[模拟] 加载设备配置: {device_id} [{protocol}] - {device_config.get('name')}")

            # 按协议统计
            proto_count = {}
            for d in self.devices.values():
                p = d.get('protocol', 'modbus_tcp')
                proto_count[p] = proto_count.get(p, 0) + 1
            summary = ', '.join(f"{k}:{v}" for k, v in proto_count.items())
            logger.info(f"[模拟] 共加载 {len(self.devices)} 个设备 ({summary})")

        except Exception as e:
            logger.error(f"加载配置文件异常: {e}")

    def _create_simulated_client(self, config: dict[str, Any]) -> IDeviceClient | None:
        """
        根据协议类型创建模拟客户端
        
        Args:
            config: 设备配置
            
        Returns:
            模拟客户端实例
        """
        protocol = config.get('protocol', 'modbus_tcp')

        if protocol in ('modbus_tcp', 'modbus_rtu'):
            return SimulatedModbusClient(config)
        elif protocol == 'opcua':
            return SimulatedOPCUAClient(config)
        elif protocol == 'mqtt':
            return SimulatedMQTTClient(config)
        elif protocol == 'rest':
            return SimulatedRESTClient(config)
        else:
            logger.error(f"不支持的协议类型: {protocol}")
            return None

    def get_client(self, device_id: str) -> IDeviceClient | None:
        """获取设备客户端（懒创建）"""
        if device_id not in self.clients:
            device_config = self.devices.get(device_id)
            if not device_config:
                logger.error(f"设备 {device_id} 配置不存在")
                return None

            client = self._create_simulated_client(device_config)
            if client is None:
                return None

            self.clients[device_id] = client

        return self.clients[device_id]

    def connect_device(self, device_id: str) -> bool:
        """连接设备"""
        client = self.get_client(device_id)
        if not client:
            return False
        return client.connect()

    def disconnect_device(self, device_id: str):
        """断开设备连接"""
        client = self.clients.get(device_id)
        if client:
            client.disconnect()

    def connect_all(self) -> dict[str, bool]:
        """连接所有设备"""
        results = {}
        for device_id in self.devices:
            if self.devices[device_id].get('enabled', True):
                results[device_id] = self.connect_device(device_id)
            else:
                results[device_id] = None  # 跳过禁用设备
        return results

    def disconnect_all(self):
        """断开所有设备连接"""
        for device_id in list(self.clients.keys()):
            self.disconnect_device(device_id)

    def get_device_status(self, device_id: str) -> dict[str, Any]:
        """获取设备状态"""
        client = self.clients.get(device_id)
        device_config = self.devices.get(device_id)

        if not device_config:
            return {'error': '设备配置不存在'}

        status = {
            'device_id': device_id,
            'name': device_config.get('name'),
            'description': device_config.get('description'),
            'protocol': device_config.get('protocol', 'modbus_tcp'),
            'host': device_config.get('host', device_config.get('endpoint', '')),
            'port': device_config.get('port'),
            'enabled': device_config.get('enabled', True),
            'connected': False,
            'registers': device_config.get('registers', device_config.get('nodes', [])),
            'stats': {},
            'mode': 'simulated'  # 标记为模拟模式
        }

        if client:
            status['connected'] = getattr(client, 'connected', False)
            status['stats'] = getattr(client, 'stats', {})

        return status

    def get_all_devices(self) -> dict[str, dict[str, Any]]:
        """获取所有设备配置"""
        return self.devices.copy()

    def get_all_status(self) -> list[dict[str, Any]]:
        """获取所有设备状态"""
        return [self.get_device_status(did) for did in self.devices]

    def add_device(self, device_config: dict[str, Any]) -> bool:
        """添加设备"""
        try:
            device_id = device_config.get('id')
            protocol = device_config.get('protocol', 'modbus_tcp')

            if not device_id:
                logger.error("设备配置缺少id字段")
                return False

            if protocol not in self.SUPPORTED_PROTOCOLS:
                logger.error(f"不支持的协议: {protocol}，支持: {self.SUPPORTED_PROTOCOLS}")
                return False

            if device_id in self.devices:
                logger.warning(f"设备 {device_id} 已存在，将被覆盖")
                self.disconnect_device(device_id)
                if device_id in self.clients:
                    del self.clients[device_id]

            self.devices[device_id] = device_config
            self._save_config()

            logger.info(f"[模拟] 添加设备: {device_id} [{protocol}]")
            return True

        except Exception as e:
            logger.error(f"添加设备异常: {e}")
            return False

    def remove_device(self, device_id: str) -> bool:
        """移除设备"""
        try:
            self.disconnect_device(device_id)
            self.devices.pop(device_id, None)
            self.clients.pop(device_id, None)
            self._save_config()
            logger.info(f"[模拟] 移除设备: {device_id}")
            return True
        except Exception as e:
            logger.error(f"移除设备异常: {e}")
            return False

    def get_protocol_summary(self) -> dict[str, int]:
        """获取各协议设备数量统计"""
        summary = {}
        for d in self.devices.values():
            p = d.get('protocol', 'modbus_tcp')
            summary[p] = summary.get(p, 0) + 1
        return summary

    def _save_config(self):
        """保存设备配置到文件"""
        try:
            config = {'devices': list(self.devices.values())}
            config_file = Path(self.config_path)
            config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(config_file, 'w', encoding='utf-8') as f:
                yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
            logger.info("[模拟] 设备配置已保存")
        except Exception as e:
            logger.error(f"保存配置文件异常: {e}")
