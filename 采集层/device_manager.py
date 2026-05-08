"""
设备管理器模块
管理所有设备的连接和配置
支持协议: Modbus TCP/RTU, OPC UA, MQTT, REST HTTP
面向现代黑灯工厂的多协议统一管理
"""

import logging
import yaml
import socket
from typing import Any
from pathlib import Path

logger = logging.getLogger(__name__)


# ---- 协议工厂：根据protocol字段自动选择客户端 ----
def _create_client(config: dict[str, Any], simulation_mode: bool, use_enhanced: bool = True):
    """
    根据协议类型创建对应的客户端实例

    模拟模式下所有协议都使用模拟客户端，无需真实设备。
    真实模式下按协议创建对应的真实客户端。

    Args:
        config: 设备配置字典
        simulation_mode: 是否模拟模式
        use_enhanced: 是否使用增强版模拟客户端（默认True）

    Returns:
        协议客户端实例
    """
    protocol = config.get('protocol', 'modbus_tcp')

    if protocol in ('modbus_tcp', 'modbus_rtu'):
        if simulation_mode:
            if use_enhanced:
                from .enhanced_simulated_client import EnhancedSimulatedModbusClient
                return EnhancedSimulatedModbusClient(config)
            else:
                from .simulated_client import SimulatedModbusClient
                return SimulatedModbusClient(config)
        else:
            from .modbus_client import ModbusClient
            return ModbusClient(config)

    elif protocol == 'opcua':
        if simulation_mode:
            if use_enhanced:
                from .enhanced_simulated_client import EnhancedSimulatedOPCUAClient
                return EnhancedSimulatedOPCUAClient(config)
            else:
                from .simulated_client import SimulatedOPCUAClient
                return SimulatedOPCUAClient(config)
        else:
            from .opcua_client import OPCUAClient
            return OPCUAClient(config)

    elif protocol == 'mqtt':
        if simulation_mode:
            if use_enhanced:
                from .enhanced_simulated_client import EnhancedSimulatedMQTTClient
                return EnhancedSimulatedMQTTClient(config)
            else:
                from .simulated_client import SimulatedMQTTClient
                return SimulatedMQTTClient(config)
        else:
            from .mqtt_client import MQTTClient
            return MQTTClient(config)

    elif protocol == 'rest':
        if simulation_mode:
            if use_enhanced:
                from .enhanced_simulated_client import EnhancedSimulatedRESTClient
                return EnhancedSimulatedRESTClient(config)
            else:
                from .simulated_client import SimulatedRESTClient
                return SimulatedRESTClient(config)
        else:
            from .rest_client import RESTDeviceClient
            return RESTDeviceClient(config)

    else:
        logger.error(f"不支持的协议类型: {protocol}")
        return None


class DeviceManager:
    """
    设备管理器
    统一管理 Modbus / OPC UA / MQTT / REST 四种协议设备

    黑灯工厂核心能力：
    1. 多协议统一接入 - 工厂内不同品牌/型号设备一网打尽
    2. 配置热加载 - 运行时新增/删除设备无需重启
    3. 模拟模式 - 无真实设备时自动切换模拟数据
    4. 自动重连 - 设备断开后自动恢复连接
    """

    # 支持的协议列表
    SUPPORTED_PROTOCOLS = ['modbus_tcp', 'modbus_rtu', 'opcua', 'mqtt', 'rest']

    def __init__(self, config_path: str | None = None, simulation_mode: bool = True, use_enhanced_simulation: bool = True):
        """
        初始化设备管理器

        Args:
            config_path: 设备配置文件路径
            simulation_mode: 是否启用模拟模式（无真实设备时使用模拟数据）
            use_enhanced_simulation: 是否使用增强版模拟（物理模型驱动）
        """
        self.config_path = config_path or '配置/devices.yaml'
        self.simulation_mode = simulation_mode
        self.use_enhanced_simulation = use_enhanced_simulation
        self.devices = {}  # device_id -> device_config
        self.clients = {}  # device_id -> protocol client

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
                    protocol = device_config.get('protocol', 'modbus_tcp')
                    logger.info(f"加载设备配置: {device_id} [{protocol}] - {device_config.get('name')}")

            # 按协议统计
            proto_count = {}
            for d in self.devices.values():
                p = d.get('protocol', 'modbus_tcp')
                proto_count[p] = proto_count.get(p, 0) + 1
            summary = ', '.join(f"{k}:{v}" for k, v in proto_count.items())
            logger.info(f"共加载 {len(self.devices)} 个设备 ({summary})")

        except Exception as e:
            logger.error(f"加载配置文件异常: {e}")

    def reload_config(self):
        """热重载设备配置（运行时调用，不会中断已有连接）"""
        old_ids = set(self.devices.keys())
        self.devices.clear()
        self.load_config()
        new_ids = set(self.devices.keys())

        added = new_ids - old_ids
        removed = old_ids - new_ids
        if added:
            logger.info(f"新增设备: {added}")
        if removed:
            for did in removed:
                self.disconnect_device(did)
            logger.info(f"移除设备: {removed}")

    def get_device_config(self, device_id: str) -> dict[str, Any] | None:
        """获取设备配置"""
        return self.devices.get(device_id)

    def get_all_devices(self) -> dict[str, dict[str, Any]]:
        """获取所有设备配置"""
        return self.devices.copy()

    def get_client(self, device_id: str):
        """
        获取设备客户端（懒创建）

        Returns:
            协议客户端实例（ModbusClient / OPCUAClient / MQTTClient / RESTDeviceClient）
        """
        if device_id not in self.clients:
            device_config = self.devices.get(device_id)
            if not device_config:
                logger.error(f"设备 {device_id} 配置不存在")
                return None

            client = _create_client(device_config, self.simulation_mode, self.use_enhanced_simulation)
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

    def switch_simulation_mode(self, new_mode: bool) -> dict[str, Any]:
        """
        运行时切换模拟/真实模式（热切换，无需重启）

        Args:
            new_mode: True=模拟模式, False=真实模式

        Returns:
            切换结果字典
        """
        old_mode = self.simulation_mode
        if old_mode == new_mode:
            return {
                'success': True,
                'message': f'已经是{"模拟" if new_mode else "真实"}模式，无需切换',
                'simulation_mode': new_mode,
                'reconnected': 0
            }

        logger.info(f"切换模式: {'模拟' if old_mode else '真实'} → {'模拟' if new_mode else '真实'}")

        # 1. 断开所有现有连接
        self.disconnect_all()

        # 2. 清除客户端缓存（下次get_client时会用新模式创建）
        self.clients.clear()

        # 3. 更新模式标志
        self.simulation_mode = new_mode

        # 4. 重新连接所有设备
        results = self.connect_all()
        connected = sum(1 for v in results.values() if v is True)
        failed = sum(1 for v in results.values() if v is False)

        logger.info(f"模式切换完成: {connected}个设备连接成功, {failed}个失败")

        return {
            'success': True,
            'message': f'已切换到{"模拟" if new_mode else "真实"}模式',
            'simulation_mode': new_mode,
            'reconnected': connected,
            'failed': failed
        }

    def get_device_status(self, device_id: str) -> dict[str, Any]:
        """获取设备状态"""
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
            'stats': {}
        }

        # 使用 get_client() 实现懒创建，确保客户端存在
        client = self.get_client(device_id)
        if client:
            # 模拟模式下，确保设备始终处于连接状态
            if self.simulation_mode:
                if not getattr(client, 'connected', False):
                    if device_config.get('enabled', True):
                        try:
                            client.connect()
                        except Exception:
                            pass
                    # 模拟模式最终保障：即使connect()失败也强制标记为已连接
                    if not getattr(client, 'connected', False) and device_config.get('enabled', True):
                        client.connected = True
            else:
                # 真实模式下，尝试自动重连
                if not getattr(client, 'connected', False):
                    if device_config.get('enabled', True):
                        try:
                            client.connect()
                        except Exception:
                            pass

            status['connected'] = getattr(client, 'connected', False)
            status['stats'] = getattr(client, 'stats', {})

        return status

    def get_all_status(self) -> list[dict[str, Any]]:
        """获取所有设备状态"""
        return [self.get_device_status(did) for did in self.devices]

    def add_device(self, device_config: dict[str, Any]) -> bool:
        """
        添加设备（运行时热添加）

        Args:
            device_config: 设备配置，必须包含 id 和 protocol 字段
        """
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

            # 热添加后自动连接设备
            if device_config.get('enabled', True):
                try:
                    connected = self.connect_device(device_id)
                    if connected:
                        logger.info(f"添加设备并连接成功: {device_id} [{protocol}]")
                    else:
                        # 模拟模式下连接失败，强制标记为已连接
                        if self.simulation_mode:
                            client = self.clients.get(device_id)
                            if client:
                                client.connected = True
                                logger.info(f"添加设备并强制连接（模拟模式）: {device_id} [{protocol}]")
                            else:
                                logger.warning(f"添加设备成功但客户端未创建: {device_id} [{protocol}]")
                        else:
                            logger.warning(f"添加设备成功但连接失败: {device_id} [{protocol}]")
                except Exception as conn_e:
                    # 模拟模式下连接异常，强制标记为已连接
                    if self.simulation_mode:
                        client = self.clients.get(device_id)
                        if client:
                            client.connected = True
                            logger.info(f"添加设备并强制连接（模拟模式，忽略异常）: {device_id} [{protocol}]")
                    else:
                        logger.warning(f"添加设备成功但连接异常: {device_id} [{conn_e}]")
            else:
                logger.info(f"添加设备（已禁用）: {device_id} [{protocol}]")

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
            logger.info(f"移除设备: {device_id}")
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
            logger.info("设备配置已保存")
        except Exception as e:
            logger.error(f"保存配置文件异常: {e}")
