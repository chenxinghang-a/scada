# 设备模拟改进总结

## 问题分析

用户反馈的问题：
1. 设备真实方面欠缺
2. 模拟效果要真实，要符合真实设备
3. 工业4.0的模拟也做好
4. 模拟可以自由添加和删除设备
5. 不能写个壳，不然跑模拟没办法出数据

## 改进方案

### 1. 创建设备行为模拟器

**文件**: `采集层/device_behavior_simulator.py`

核心特性：
- **物理模型** - 设备参数之间的关联性（温度→压力→流量）
- **设备状态机** - 运行/空闲/故障/维护/停机（ISA-95标准）
- **故障模拟** - 真实的故障场景和渐进式退化
- **数据连续性** - 确保数据流连续，支持历史回放
- **班次影响** - 白班/夜班对设备参数的影响

#### 设备状态模型
```python
class DeviceState(Enum):
    STOPPED = 0      # 停机
    IDLE = 1         # 空闲/待机
    RUNNING = 2      # 运行
    FAULT = 3        # 故障
    MAINTENANCE = 4  # 维护中
    SETUP = 5        # 换型/调试
```

#### 故障类型
```python
class FaultType(Enum):
    NONE = "none"
    SENSOR_DRIFT = "sensor_drift"        # 传感器漂移
    OVERHEATING = "overheating"          # 过热
    PRESSURE_LEAK = "pressure_leak"      # 压力泄漏
    MOTOR_WEAR = "motor_wear"            # 电机磨损
    COMMUNICATION = "communication"      # 通信故障
    POWER_FLUCTUATION = "power_fluctuation"  # 电源波动
```

#### 物理模型
- **温度** - 基础温度 + 运行升温 + 班次影响 + 故障影响
- **压力** - 与温度关联（温度升高→压力升高）
- **流量** - 与压力和温度关联
- **液位** - 与流量关联

### 2. 创建增强版模拟客户端

**文件**: `采集层/enhanced_simulated_client.py`

增强版客户端使用设备行为模拟器生成更真实的工业数据：
- `EnhancedSimulatedModbusClient` - 增强版Modbus客户端
- `EnhancedSimulatedOPCUAClient` - 增强版OPC UA客户端
- `EnhancedSimulatedMQTTClient` - 增强版MQTT客户端
- `EnhancedSimulatedRESTClient` - 增强版REST客户端

### 3. 更新设备管理器

**文件**: `采集层/device_manager.py`

添加 `use_enhanced_simulation` 参数，支持选择使用增强版或基础版模拟客户端。

### 4. 添加设备行为API

**文件**: `展示层/api/api_devices.py`

新增API端点：
- `GET /api/devices/<device_id>/behavior` - 获取设备行为模拟状态
- `POST /api/devices/<device_id>/inject-fault` - 注入设备故障（测试用）
- `POST /api/devices/<device_id>/force-state` - 强制设置设备状态（测试用）

## 数据流

```
设备行为模拟器
    ↓
增强版模拟客户端
    ↓
数据采集器 (DataCollector)
    ↓
工业4.0模块 (预测性维护/OEE/SPC/能源管理)
```

## 工业4.0数据兼容性

增强版模拟客户端生成的数据可直接用于：
1. **预测性维护** - 温度、压力、振动、电流等参数
2. **OEE计算** - 设备状态、生产计数、质量数据
3. **SPC分析** - 过程参数、质量指标
4. **能源管理** - 电力参数、能耗数据

## 使用方法

### 1. 启用增强版模拟

在 `配置/system.yaml` 中：
```yaml
system:
  simulation_mode: true
  use_enhanced_simulation: true  # 启用增强版模拟
```

### 2. 添加设备

通过API添加设备：
```bash
POST /api/devices
{
    "id": "new_device_01",
    "name": "新设备",
    "protocol": "modbus_tcp",
    "host": "192.168.1.100",
    "port": 502,
    "registers": [...]
}
```

### 3. 查看设备行为

```bash
GET /api/devices/new_device_01/behavior
```

### 4. 注入故障（测试）

```bash
POST /api/devices/new_device_01/inject-fault
{
    "fault_type": "overheating",
    "severity": 0.7
}
```

## 与原有系统的兼容性

- 保持向后兼容，可通过配置切换回基础版模拟
- 所有API接口保持不变
- 数据格式保持一致

## 测试建议

1. 启动系统，观察设备状态变化
2. 注入不同类型的故障，观察数据变化
3. 检查工业4.0模块是否能正确接收数据
4. 验证设备添加/删除功能

## 后续优化

1. 添加更多设备类型模板
2. 实现设备关联性模拟（如产线设备联动）
3. 添加历史数据回放功能
4. 优化故障预测算法
