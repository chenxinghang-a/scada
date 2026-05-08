# 工业SCADA协议网关服务

## 概述

本模块实现了四层漏斗架构的第一层：**边缘网关层**。

协议网关是一个独立运行的服务，负责：
1. 与工业设备通信（Modbus、S7、OPC UA等）
2. 将原始数据转换为统一物模型
3. 通过MQTT发布标准化数据

**核心优势：故障隔离** — 网关崩溃不影响主系统。

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│                    主系统 (SCADA)                            │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  MQTTSubscriber → 数据分发器 → 业务模块              │    │
│  │  (OEE、预测性维护、SPC、能源管理、报警)               │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                              ↑ 订阅
┌─────────────────────────────────────────────────────────────┐
│                    MQTT Broker (EMQX)                        │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  Topic: scada/devices/{device_id}/telemetry         │    │
│  │  Topic: scada/devices/{device_id}/status            │    │
│  │  Topic: scada/alarms/{level}                        │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                              ↑ 发布
┌─────────────────────────────────────────────────────────────┐
│                    协议网关服务                               │
│  ┌─────────────┬─────────────┬─────────────┬─────────────┐  │
│  │ Modbus网关  │   S7网关    │  OPC UA网关 │  MQTT网关   │  │
│  └─────────────┴─────────────┴─────────────┴─────────────┘  │
│         ↑              ↑              ↑              ↑       │
│    ┌─────────┐    ┌─────────┐    ┌─────────┐    ┌─────────┐ │
│    │  PLC    │    │  CNC    │    │  SCADA  │    │ Sensor  │ │
│    └─────────┘    └─────────┘    └─────────┘    └─────────┘ │
└─────────────────────────────────────────────────────────────┘
```

## 快速开始

### 1. 安装依赖

```bash
pip install paho-mqtt pymodbus pyyaml
```

### 2. 启动MQTT Broker

```bash
# 使用EMQX（推荐）
docker run -d --name emqx -p 1883:1883 -p 8083:8083 emqx/emqx

# 或使用Mosquitto
docker run -d --name mosquitto -p 1883:1883 eclipse-mosquitto
```

### 3. 配置网关

编辑 `gateway/config.yaml`：

```yaml
gateways:
  - type: "modbus"
    gateway_id: "modbus_gateway_01"
    mqtt_broker: "localhost"
    mqtt_port: 1883
    poll_interval: 5.0
    
    devices:
      - device_id: "PLC_001"
        protocol: "tcp"
        host: "192.168.1.100"
        port: 502
        slave_id: 1
        
        registers:
          - name: "temperature"
            address: 0
            count: 2
            type: "float32"
```

### 4. 启动网关

```bash
# 启动Modbus网关
python gateway/run_gateway.py --type modbus --config gateway/config.yaml

# 启动所有网关
python gateway/run_gateway.py --all --config gateway/config.yaml
```

### 5. 主系统集成

```python
from gateway import MQTTSubscriber, MQTTDataDistributor

# 创建订阅客户端
subscriber = MQTTSubscriber("localhost", 1883)

# 创建数据分发器
distributor = MQTTDataDistributor(subscriber)

# 注册业务模块
distributor.register_module('oee', oee_calculator)
distributor.register_module('predictive', predictive_maintenance)

# 订阅所有主题
subscriber.subscribe_all()

# 启动
subscriber.start()
```

## 文件结构

```
gateway/
├── __init__.py              # 模块入口
├── README.md                # 本文档
├── config.yaml              # 网关配置
├── thing_model.py           # 统一物模型定义
├── base_gateway.py          # 网关基类
├── modbus_gateway.py        # Modbus网关实现
├── mqtt_subscriber.py       # MQTT订阅客户端
├── run_gateway.py           # 网关启动脚本
└── integration_example.py   # 集成示例
```

## 统一物模型

所有设备数据必须转换为统一格式：

```json
{
  "DeviceID": "CNC_001",
  "Timestamp": 1715129400.123,
  "Protocol": "ModbusTCP",
  "GatewayID": "gateway_01",
  "Metrics": {
    "temperature": {
      "value": 45.5,
      "unit": "°C",
      "quality": 192,
      "description": "温度传感器"
    },
    "pressure": {
      "value": 0.5,
      "unit": "MPa",
      "quality": 192,
      "description": "压力传感器"
    }
  }
}
```

## MQTT Topic规范

| 主题 | 用途 | 示例 |
|------|------|------|
| `scada/devices/{device_id}/telemetry` | 设备遥测数据 | `scada/devices/CNC_001/telemetry` |
| `scada/devices/{device_id}/status` | 设备状态 | `scada/devices/CNC_001/status` |
| `scada/alarms/{level}` | 报警信息 | `scada/alarms/critical` |
| `scada/oee/{device_id}` | OEE计算结果 | `scada/oee/CNC_001` |
| `scada/predictive/{device_id}` | 预测性维护 | `scada/predictive/CNC_001` |

## 扩展指南

### 添加新协议网关

1. 继承 `BaseGateway` 基类
2. 实现以下方法：
   - `connect()`: 连接设备
   - `disconnect()`: 断开连接
   - `read_device_data()`: 读取数据
   - `convert_to_telemetry()`: 转换为物模型

3. 在 `run_gateway.py` 中注册新网关

### 示例：S7网关

```python
from gateway import BaseGateway, DeviceTelemetry

class S7Gateway(BaseGateway):
    def connect(self) -> bool:
        # 连接S7设备
        pass
    
    def disconnect(self):
        # 断开连接
        pass
    
    def read_device_data(self, device_id: str) -> Optional[Dict[str, float]]:
        # 读取S7数据
        pass
    
    def convert_to_telemetry(self, device_id: str, raw_data: Dict[str, float]) -> DeviceTelemetry:
        # 转换为物模型
        pass
```

## 故障排除

### 1. MQTT连接失败

检查：
- MQTT Broker是否启动
- 端口是否正确（默认1883）
- 防火墙设置

### 2. Modbus连接超时

检查：
- PLC IP地址是否正确
- 端口是否开放（默认502）
- 从站ID是否匹配

### 3. 数据格式错误

检查：
- 寄存器地址是否正确
- 数据类型是否匹配
- 字节序设置

## 性能优化

1. **轮询间隔**：根据设备响应速度调整
2. **并发采集**：多线程读取不同设备
3. **数据压缩**：MQTT支持压缩传输
4. **缓存策略**：本地缓存减少网络请求

## 安全建议

1. **MQTT认证**：启用用户名/密码认证
2. **TLS加密**：使用MQTT over TLS
3. **访问控制**：限制Topic访问权限
4. **网络隔离**：工业网络与IT网络隔离

## 下一步

- [ ] 添加S7网关支持
- [ ] 添加OPC UA网关支持
- [ ] 集成时序数据库（TDengine）
- [ ] 添加Web管理界面
- [ ] 支持Docker容器化部署

---

*文档版本：v1.0*
*更新时间：2026-05-08*
