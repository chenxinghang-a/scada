"""
模拟协议转换网关
用于演示和测试协议转换功能
"""

import logging
import time
import threading
from typing import Any, Dict, List, Optional
from datetime import datetime
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class ProtocolType(Enum):
    """支持的协议类型"""
    MODBUS_TCP = "modbus_tcp"
    MODBUS_RTU = "modbus_rtu"
    OPC UA = "opcua"
    MQTT = "mqtt"
    REST = "rest"


@dataclass
class ConversionRule:
    """协议转换规则"""
    source_protocol: ProtocolType
    source_address: str
    target_protocol: ProtocolType
    target_address: str
    data_type: str = "float"
    scale: float = 1.0
    offset: float = 0.0


class SimulatedProtocolGateway:
    """
    模拟协议转换网关

    功能：
    - 支持多种协议之间的转换
    - 模拟数据转换和路由
    - 提供转换统计和日志
    """

    def __init__(self, config: Dict[str, Any]):
        """
        初始化模拟网关

        Args:
            config: 网关配置
        """
        self.gateway_id = config.get('gateway_id', 'sim_gateway_001')
        self.name = config.get('name', '模拟协议网关')
        self.description = config.get('description', '用于演示的协议转换网关')

        # 支持的协议
        self.supported_protocols = [
            ProtocolType.MODBUS_TCP,
            ProtocolType.MODBUS_RTU,
            ProtocolType.OPC UA,
            ProtocolType.MQTT,
            ProtocolType.REST
        ]

        # 转换规则
        self.conversion_rules: List[ConversionRule] = []

        # 连接的设备
        self.connected_devices: Dict[str, Dict[str, Any]] = {}

        # 转换统计
        self.stats = {
            'total_conversions': 0,
            'successful_conversions': 0,
            'failed_conversions': 0,
            'bytes_translated': 0,
            'uptime_seconds': 0,
            'last_conversion_time': None
        }

        # 运行状态
        self.running = False
        self._start_time = None
        self._lock = threading.Lock()

        logger.info(f"模拟协议网关初始化: {self.gateway_id}")

    def start(self):
        """启动网关"""
        if self.running:
            logger.warning(f"网关 {self.gateway_id} 已在运行")
            return

        self.running = True
        self._start_time = time.time()
        logger.info(f"模拟协议网关 {self.gateway_id} 已启动")

    def stop(self):
        """停止网关"""
        self.running = False
        logger.info(f"模拟协议网关 {self.gateway_id} 已停止")

    def add_conversion_rule(self, rule: ConversionRule):
        """
        添加转换规则

        Args:
            rule: 转换规则
        """
        self.conversion_rules.append(rule)
        logger.info(f"添加转换规则: {rule.source_protocol.value} -> {rule.target_protocol.value}")

    def convert_data(self, source_protocol: ProtocolType, source_address: str,
                     value: Any, target_protocol: ProtocolType) -> Optional[Dict[str, Any]]:
        """
        转换数据

        Args:
            source_protocol: 源协议
            source_address: 源地址
            value: 源值
            target_protocol: 目标协议

        Returns:
            转换后的数据，如果转换失败返回None
        """
        with self._lock:
            self.stats['total_conversions'] += 1

        try:
            # 查找匹配的转换规则
            rule = self._find_conversion_rule(source_protocol, source_address, target_protocol)

            if rule:
                # 应用转换规则
                converted_value = self._apply_conversion_rule(rule, value)
            else:
                # 默认转换
                converted_value = self._default_conversion(value)

            # 更新统计
            with self._lock:
                self.stats['successful_conversions'] += 1
                self.stats['last_conversion_time'] = datetime.now().isoformat()
                self.stats['bytes_translated'] += len(str(value)) + len(str(converted_value))

            return {
                'success': True,
                'source': {
                    'protocol': source_protocol.value,
                    'address': source_address,
                    'value': value
                },
                'target': {
                    'protocol': target_protocol.value,
                    'address': source_address,  # 默认使用相同地址
                    'value': converted_value
                },
                'timestamp': datetime.now().isoformat(),
                'gateway_id': self.gateway_id
            }

        except Exception as e:
            logger.error(f"数据转换失败: {e}")
            with self._lock:
                self.stats['failed_conversions'] += 1
            return None

    def _find_conversion_rule(self, source_protocol: ProtocolType,
                              source_address: str, target_protocol: ProtocolType) -> Optional[ConversionRule]:
        """查找匹配的转换规则"""
        for rule in self.conversion_rules:
            if (rule.source_protocol == source_protocol and
                rule.target_protocol == target_protocol and
                rule.source_address == source_address):
                return rule
        return None

    def _apply_conversion_rule(self, rule: ConversionRule, value: Any) -> Any:
        """应用转换规则"""
        try:
            # 数据类型转换
            if rule.data_type == 'float':
                converted = float(value)
            elif rule.data_type == 'int':
                converted = int(value)
            elif rule.data_type == 'bool':
                converted = bool(value)
            else:
                converted = value

            # 应用缩放和偏移
            if isinstance(converted, (int, float)):
                converted = converted * rule.scale + rule.offset

            return converted

        except Exception as e:
            logger.error(f"应用转换规则失败: {e}")
            return value

    def _default_conversion(self, value: Any) -> Any:
        """默认转换（直接传递）"""
        return value

    def get_stats(self) -> Dict[str, Any]:
        """获取网关统计信息"""
        with self._lock:
            stats = dict(self.stats)
            if self._start_time:
                stats['uptime_seconds'] = int(time.time() - self._start_time)
            stats['gateway_id'] = self.gateway_id
            stats['name'] = self.name
            stats['running'] = self.running
            stats['conversion_rules_count'] = len(self.conversion_rules)
            stats['connected_devices_count'] = len(self.connected_devices)
            stats['supported_protocols'] = [p.value for p in self.supported_protocols]
            return stats

    def get_status(self) -> Dict[str, Any]:
        """获取网关状态"""
        return {
            'gateway_id': self.gateway_id,
            'name': self.name,
            'description': self.description,
            'running': self.running,
            'supported_protocols': [p.value for p in self.supported_protocols],
            'conversion_rules_count': len(self.conversion_rules),
            'connected_devices_count': len(self.connected_devices),
            'stats': self.get_stats()
        }


class SimulatedModbusToMQTTGateway(SimulatedProtocolGateway):
    """
    模拟Modbus到MQTT转换网关

    将Modbus数据转换为MQTT消息格式
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.name = config.get('name', 'Modbus-MQTT转换网关')
        self.description = config.get('description', '将Modbus设备数据转换为MQTT消息')

        # MQTT主题模板
        self.mqtt_topic_template = config.get('mqtt_topic_template', 'factory/{device_id}/{register_name}')

        # 添加默认转换规则
        self._add_default_rules()

    def _add_default_rules(self):
        """添加默认转换规则"""
        # Modbus TCP -> MQTT
        self.add_conversion_rule(ConversionRule(
            source_protocol=ProtocolType.MODBUS_TCP,
            source_address='*',
            target_protocol=ProtocolType.MQTT,
            target_address='*',
            data_type='float'
        ))

        # Modbus RTU -> MQTT
        self.add_conversion_rule(ConversionRule(
            source_protocol=ProtocolType.MODBUS_RTU,
            source_address='*',
            target_protocol=ProtocolType.MQTT,
            target_address='*',
            data_type='float'
        ))

    def convert_to_mqtt(self, device_id: str, register_name: str,
                        value: Any, source_protocol: ProtocolType) -> Dict[str, Any]:
        """
        转换为MQTT消息格式

        Args:
            device_id: 设备ID
            register_name: 寄存器名称
            value: 值
            source_protocol: 源协议

        Returns:
            MQTT消息格式
        """
        topic = self.mqtt_topic_template.format(
            device_id=device_id,
            register_name=register_name
        )

        payload = {
            'device_id': device_id,
            'register_name': register_name,
            'value': value,
            'timestamp': datetime.now().isoformat(),
            'source_protocol': source_protocol.value,
            'gateway_id': self.gateway_id,
            'quality': 'good'
        }

        return {
            'topic': topic,
            'payload': payload,
            'qos': 1
        }


class SimulatedModbusToOPCUAGateway(SimulatedProtocolGateway):
    """
    模拟Modbus到OPC UA转换网关

    将Modbus数据转换为OPC UA节点格式
    """

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.name = config.get('name', 'Modbus-OPC UA转换网关')
        self.description = config.get('description', '将Modbus设备数据转换为OPC UA节点')

        # OPC UA命名空间
        self.namespace = config.get('namespace', 'ns=2;s=Factory')

        # 添加默认转换规则
        self._add_default_rules()

    def _add_default_rules(self):
        """添加默认转换规则"""
        # Modbus TCP -> OPC UA
        self.add_conversion_rule(ConversionRule(
            source_protocol=ProtocolType.MODBUS_TCP,
            source_address='*',
            target_protocol=ProtocolType.OPC UA,
            target_address='*',
            data_type='float'
        ))

        # Modbus RTU -> OPC UA
        self.add_conversion_rule(ConversionRule(
            source_protocol=ProtocolType.MODBUS_RTU,
            source_address='*',
            target_protocol=ProtocolType.OPC UA,
            target_address='*',
            data_type='float'
        ))

    def convert_to_opcua(self, device_id: str, register_name: str,
                         value: Any, source_protocol: ProtocolType) -> Dict[str, Any]:
        """
        转换为OPC UA节点格式

        Args:
            device_id: 设备ID
            register_name: 寄存器名称
            value: 值
            source_protocol: 源协议

        Returns:
            OPC UA节点格式
        """
        node_id = f"{self.namespace}.{device_id}.{register_name}"

        return {
            'node_id': node_id,
            'value': value,
            'data_type': 'Double',
            'timestamp': datetime.now().isoformat(),
            'source_protocol': source_protocol.value,
            'gateway_id': self.gateway_id,
            'status_code': 'Good'
        }


# 便捷函数
def create_simulated_gateway(gateway_type: str, config: Dict[str, Any]) -> SimulatedProtocolGateway:
    """
    创建模拟网关实例

    Args:
        gateway_type: 网关类型 (modbus_mqtt, modbus_opcua, generic)
        config: 网关配置

    Returns:
        网关实例
    """
    gateway_map = {
        'modbus_mqtt': SimulatedModbusToMQTTGateway,
        'modbus_opcua': SimulatedModbusToOPCUAGateway,
        'generic': SimulatedProtocolGateway
    }

    gateway_class = gateway_map.get(gateway_type, SimulatedProtocolGateway)
    return gateway_class(config)


# 测试代码
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    # 创建Modbus到MQTT网关
    config = {
        'gateway_id': 'test_gateway_001',
        'name': '测试网关',
        'mqtt_topic_template': 'factory/{device_id}/{register_name}'
    }

    gateway = create_simulated_gateway('modbus_mqtt', config)
    gateway.start()

    # 测试转换
    result = gateway.convert_data(
        source_protocol=ProtocolType.MODBUS_TCP,
        source_address='0',
        value=25.5,
        target_protocol=ProtocolType.MQTT
    )

    print(f"转换结果: {result}")

    # 测试MQTT格式转换
    mqtt_msg = gateway.convert_to_mqtt(
        device_id='siemens_1500_01',
        register_name='boiler_temperature',
        value=155.0,
        source_protocol=ProtocolType.MODBUS_TCP
    )

    print(f"MQTT消息: {mqtt_msg}")

    # 获取统计
    stats = gateway.get_stats()
    print(f"网关统计: {stats}")

    gateway.stop()
