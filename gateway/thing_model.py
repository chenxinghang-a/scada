"""
统一物模型定义 (Digital Twin / Thing Model)

所有设备数据必须转换为统一格式后才能发布到MQTT总线。
这是工业4.0系统的"世界语"，确保不同协议、不同厂商的设备数据可以无缝对接。

设计原则：
1. 设备无关性 — 不管底层是什么协议，数据格式统一
2. 自描述性 — 数据包含设备ID、时间戳、质量戳
3. 可扩展性 — Metrics字段支持任意键值对
4. 标准化 — 符合工业4.0物模型规范
"""

import json
import time
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict, field
from enum import Enum


class ProtocolType(Enum):
    """支持的协议类型"""
    MODBUS_RTU = "ModbusRTU"
    MODBUS_TCP = "ModbusTCP"
    S7 = "S7"
    OPC_UA = "OPCUA"
    MQTT = "MQTT"
    REST = "REST"
    MC = "MC"  # 三菱MC协议
    FINS = "FINS"  # 欧姆龙FINS协议


class DataQuality(Enum):
    """数据质量戳（OPC UA标准）"""
    GOOD = 192  # 0xC0 - 数据良好
    UNCERTAIN = 104  # 0x68 - 数据不确定
    BAD = 0  # 0x00 - 数据坏


@dataclass
class MetricValue:
    """单个指标值"""
    value: float
    unit: str = ""
    quality: int = DataQuality.GOOD.value
    description: str = ""
    
    def to_dict(self) -> Dict:
        return {
            "value": self.value,
            "unit": self.unit,
            "quality": self.quality,
            "description": self.description
        }


@dataclass
class DeviceTelemetry:
    """
    设备遥测数据 — 统一物模型
    
    这是整个系统的核心数据结构，所有设备数据必须转换为此格式。
    
    示例：
    {
        "DeviceID": "CNC_001",
        "Timestamp": 1715129400.123,
        "Protocol": "ModbusTCP",
        "GatewayID": "gateway_01",
        "Metrics": {
            "temperature": {"value": 45.5, "unit": "°C", "quality": 192},
            "pressure": {"value": 0.5, "unit": "MPa", "quality": 192},
            "status": {"value": 1, "unit": "enum", "quality": 192}
        }
    }
    """
    DeviceID: str
    Timestamp: float
    Protocol: str
    Metrics: Dict[str, Dict[str, Any]]
    GatewayID: str = ""
    Metadata: Dict[str, Any] = field(default_factory=dict)
    
    def to_json(self) -> str:
        """转换为JSON字符串"""
        return json.dumps(asdict(self), ensure_ascii=False)
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'DeviceTelemetry':
        """从字典创建"""
        return cls(**data)
    
    @classmethod
    def from_json(cls, json_str: str) -> 'DeviceTelemetry':
        """从JSON字符串创建"""
        return cls.from_dict(json.loads(json_str))
    
    def add_metric(self, name: str, value: float, unit: str = "", 
                   quality: int = DataQuality.GOOD.value, description: str = ""):
        """添加一个指标"""
        self.Metrics[name] = {
            "value": value,
            "unit": unit,
            "quality": quality,
            "description": description
        }
    
    def get_metric_value(self, name: str) -> Optional[float]:
        """获取指标值"""
        metric = self.Metrics.get(name)
        return metric["value"] if metric else None
    
    def get_metric_quality(self, name: str) -> Optional[int]:
        """获取指标质量"""
        metric = self.Metrics.get(name)
        return metric["quality"] if metric else None


@dataclass
class DeviceStatus:
    """
    设备状态数据
    
    用于表示设备的在线/离线/故障等状态信息。
    """
    DeviceID: str
    Timestamp: float
    Online: bool
    Status: str  # "running", "stopped", "fault", "maintenance"
    ErrorCode: int = 0
    ErrorMessage: str = ""
    Uptime: float = 0  # 运行时间（秒）
    
    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)
    
    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class AlarmMessage:
    """
    报警消息
    
    用于在MQTT总线上传输报警信息。
    """
    AlarmID: str
    DeviceID: str
    Timestamp: float
    Level: str  # "critical", "warning", "info"
    Type: str  # "threshold", "rate_of_change", "communication", "device"
    Message: str
    Value: float = 0
    Threshold: float = 0
    Acknowledged: bool = False
    
    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)
    
    def to_dict(self) -> Dict:
        return asdict(self)


class ThingModelValidator:
    """
    物模型验证器
    
    验证数据是否符合统一物模型规范。
    """
    
    REQUIRED_FIELDS = ["DeviceID", "Timestamp", "Metrics"]
    
    @classmethod
    def validate_telemetry(cls, data: Dict) -> tuple[bool, List[str]]:
        """
        验证遥测数据
        
        Returns:
            (is_valid, errors)
        """
        errors = []
        
        # 检查必需字段
        for field in cls.REQUIRED_FIELDS:
            if field not in data:
                errors.append(f"缺少必需字段: {field}")
        
        # 检查DeviceID类型
        if "DeviceID" in data and not isinstance(data["DeviceID"], str):
            errors.append("DeviceID必须是字符串")
        
        # 检查Timestamp类型
        if "Timestamp" in data and not isinstance(data["Timestamp"], (int, float)):
            errors.append("Timestamp必须是数字")
        
        # 检查Metrics结构
        if "Metrics" in data:
            if not isinstance(data["Metrics"], dict):
                errors.append("Metrics必须是字典")
            else:
                for key, metric in data["Metrics"].items():
                    if not isinstance(metric, dict):
                        errors.append(f"Metrics.{key}必须是字典")
                    elif "value" not in metric:
                        errors.append(f"Metrics.{key}缺少value字段")
                    elif not isinstance(metric["value"], (int, float)):
                        errors.append(f"Metrics.{key}.value必须是数字")
        
        return (len(errors) == 0, errors)
    
    @classmethod
    def validate_status(cls, data: Dict) -> tuple[bool, List[str]]:
        """验证状态数据"""
        errors = []
        
        required = ["DeviceID", "Timestamp", "Online", "Status"]
        for field in required:
            if field not in data:
                errors.append(f"缺少必需字段: {field}")
        
        if "Status" in data and data["Status"] not in ["running", "stopped", "fault", "maintenance"]:
            errors.append(f"无效的Status值: {data['Status']}")
        
        return (len(errors) == 0, errors)


class ThingModelConverter:
    """
    物模型转换器
    
    将不同协议的原始数据转换为统一物模型。
    """
    
    @staticmethod
    def from_modbus_registers(device_id: str, registers: Dict[str, float], 
                              gateway_id: str = "") -> DeviceTelemetry:
        """
        从Modbus寄存器数据转换
        
        Args:
            device_id: 设备ID
            registers: 寄存器数据 {register_name: value}
            gateway_id: 网关ID
        """
        metrics = {}
        for name, value in registers.items():
            # 根据寄存器名称推断单位
            unit = ThingModelConverter._infer_unit(name)
            metrics[name] = {
                "value": value,
                "unit": unit,
                "quality": DataQuality.GOOD.value,
                "description": ""
            }
        
        return DeviceTelemetry(
            DeviceID=device_id,
            Timestamp=time.time(),
            Protocol=ProtocolType.MODBUS_TCP.value,
            Metrics=metrics,
            GatewayID=gateway_id
        )
    
    @staticmethod
    def from_opcua_node(device_id: str, node_id: str, value: float, 
                        unit: str = "", quality: int = DataQuality.GOOD.value) -> DeviceTelemetry:
        """从OPC UA节点数据转换"""
        return DeviceTelemetry(
            DeviceID=device_id,
            Timestamp=time.time(),
            Protocol=ProtocolType.OPC_UA.value,
            Metrics={
                node_id: {
                    "value": value,
                    "unit": unit,
                    "quality": quality,
                    "description": ""
                }
            }
        )
    
    @staticmethod
    def from_mqtt_payload(device_id: str, payload: Dict) -> DeviceTelemetry:
        """从MQTT载荷转换"""
        metrics = {}
        for key, value in payload.items():
            if isinstance(value, (int, float)):
                metrics[key] = {
                    "value": value,
                    "unit": ThingModelConverter._infer_unit(key),
                    "quality": DataQuality.GOOD.value,
                    "description": ""
                }
        
        return DeviceTelemetry(
            DeviceID=device_id,
            Timestamp=time.time(),
            Protocol=ProtocolType.MQTT.value,
            Metrics=metrics
        )
    
    @staticmethod
    def _infer_unit(register_name: str) -> str:
        """根据寄存器名称推断单位"""
        name_lower = register_name.lower()
        
        unit_map = {
            "temperature": "°C",
            "temp": "°C",
            "pressure": "MPa",
            "voltage": "V",
            "current": "A",
            "power": "kW",
            "energy": "kWh",
            "frequency": "Hz",
            "speed": "RPM",
            "flow": "m³/h",
            "level": "%",
            "humidity": "%",
            "ph": "pH",
            "torque": "N·m",
            "vibration": "mm/s",
            "thickness": "mm",
            "weight": "kg",
            "count": "pcs",
        }
        
        for keyword, unit in unit_map.items():
            if keyword in name_lower:
                return unit
        
        return ""


# MQTT Topic规范
class MQTTTopics:
    """
    MQTT Topic规范
    
    所有模块必须使用这些标准Topic进行通信。
    """
    
    # 设备遥测数据
    DEVICE_TELEMETRY = "scada/devices/{device_id}/telemetry"
    
    # 设备状态
    DEVICE_STATUS = "scada/devices/{device_id}/status"
    
    # 报警信息
    ALARM = "scada/alarms/{level}"
    
    # OEE计算结果
    OEE = "scada/oee/{device_id}"
    
    # 预测性维护结果
    PREDICTIVE = "scada/predictive/{device_id}"
    
    # 能源数据
    ENERGY = "scada/energy/{area}"
    
    # SPC分析结果
    SPC = "scada/spc/{device_id}/{register}"
    
    # 边缘决策
    EDGE_DECISION = "scada/edge/{device_id}"
    
    # 系统命令
    COMMAND = "scada/commands/{target}"
    
    @classmethod
    def get_telemetry_topic(cls, device_id: str) -> str:
        return cls.DEVICE_TELEMETRY.format(device_id=device_id)
    
    @classmethod
    def get_status_topic(cls, device_id: str) -> str:
        return cls.DEVICE_STATUS.format(device_id=device_id)
    
    @classmethod
    def get_alarm_topic(cls, level: str) -> str:
        return cls.ALARM.format(level=level)
    
    @classmethod
    def get_oee_topic(cls, device_id: str) -> str:
        return cls.OEE.format(device_id=device_id)
    
    @classmethod
    def get_predictive_topic(cls, device_id: str) -> str:
        return cls.PREDICTIVE.format(device_id=device_id)


# 测试代码
if __name__ == "__main__":
    # 测试物模型创建
    telemetry = DeviceTelemetry(
        DeviceID="CNC_001",
        Timestamp=time.time(),
        Protocol=ProtocolType.MODBUS_TCP.value,
        Metrics={
            "temperature": {"value": 45.5, "unit": "°C", "quality": 192},
            "pressure": {"value": 0.5, "unit": "MPa", "quality": 192},
            "status": {"value": 1, "unit": "enum", "quality": 192}
        },
        GatewayID="gateway_01"
    )
    
    print("=== 物模型测试 ===")
    print(telemetry.to_json())
    
    # 测试验证器
    is_valid, errors = ThingModelValidator.validate_telemetry(telemetry.to_dict())
    print(f"\n验证结果: {'通过' if is_valid else '失败'}")
    if errors:
        for error in errors:
            print(f"  - {error}")
    
    # 测试转换器
    modbus_data = {
        "temperature": 25.5,
        "pressure": 0.3,
        "running_status": 1,
        "product_count": 100
    }
    converted = ThingModelConverter.from_modbus_registers("PLC_001", modbus_data)
    print(f"\n转换后的物模型:")
    print(converted.to_json())
    
    # 测试MQTT Topic
    print(f"\nMQTT Topic示例:")
    print(f"  遥测: {MQTTTopics.get_telemetry_topic('CNC_001')}")
    print(f"  状态: {MQTTTopics.get_status_topic('CNC_001')}")
    print(f"  报警: {MQTTTopics.get_alarm_topic('critical')}")
