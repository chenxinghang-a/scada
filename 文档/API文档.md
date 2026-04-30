# 工业数据采集与监控系统API文档

## 1. 概述

本系统提供RESTful API接口，用于设备管理、数据查询、报警管理等功能。

**基础URL**: `http://localhost:5000/api`

## 2. 设备管理API

### 2.1 获取所有设备
**请求**: `GET /api/devices`

**响应**:
```json
{
  "devices": [
    {
      "device_id": "temp_sensor_01",
      "name": "温度传感器1号",
      "description": "车间A温度监测点",
      "protocol": "modbus_tcp",
      "host": "192.168.1.100",
      "port": 502,
      "enabled": true,
      "connected": true,
      "registers": [...]
    }
  ]
}
```

### 2.2 获取单个设备
**请求**: `GET /api/devices/{device_id}`

**响应**:
```json
{
  "device_id": "temp_sensor_01",
  "name": "温度传感器1号",
  "description": "车间A温度监测点",
  "protocol": "modbus_tcp",
  "host": "192.168.1.100",
  "port": 502,
  "enabled": true,
  "connected": true,
  "registers": [
    {
      "name": "temperature",
      "description": "温度值",
      "address": 0,
      "data_type": "float32",
      "scale": 0.1,
      "unit": "°C"
    }
  ],
  "stats": {
    "total_reads": 100,
    "successful_reads": 98,
    "failed_reads": 2
  }
}
```

### 2.3 连接设备
**请求**: `POST /api/devices/{device_id}/connect`

**响应**:
```json
{
  "success": true,
  "message": "设备连接成功"
}
```

### 2.4 断开设备
**请求**: `POST /api/devices/{device_id}/disconnect`

**响应**:
```json
{
  "success": true,
  "message": "设备断开成功"
}
```

## 3. 数据查询API

### 3.1 获取实时数据
**请求**: `GET /api/data/realtime?limit=100`

**参数**:
- `limit`: 返回数据条数（默认100）

**响应**:
```json
{
  "data": [
    {
      "device_id": "temp_sensor_01",
      "register_name": "temperature",
      "value": 25.5,
      "unit": "°C",
      "timestamp": "2024-01-01T12:00:00"
    }
  ]
}
```

### 3.2 获取最新数据
**请求**: `GET /api/data/latest/{device_id}?register_name=temperature`

**参数**:
- `register_name`: 寄存器名称（可选）

**响应**:
```json
{
  "device_id": "temp_sensor_01",
  "register_name": "temperature",
  "value": 25.5,
  "unit": "°C",
  "timestamp": "2024-01-01T12:00:00"
}
```

### 3.3 获取历史数据
**请求**: `GET /api/data/history/{device_id}/{register_name}?start_time=...&end_time=...&interval=1min`

**参数**:
- `start_time`: 开始时间（ISO格式）
- `end_time`: 结束时间（ISO格式）
- `interval`: 时间间隔（1min/5min/1hour/1day）

**响应**:
```json
{
  "data": [
    {
      "timestamp": "2024-01-01T12:00:00",
      "avg_value": 25.5,
      "min_value": 25.0,
      "max_value": 26.0,
      "sample_count": 12
    }
  ]
}
```

## 4. 报警管理API

### 4.1 获取报警记录
**请求**: `GET /api/alarms?limit=100&device_id=...&alarm_level=...`

**参数**:
- `limit`: 返回数量（默认100）
- `device_id`: 设备ID（可选）
- `alarm_level`: 报警级别（可选）

**响应**:
```json
{
  "alarms": [
    {
      "alarm_id": "temp_high",
      "device_id": "temp_sensor_01",
      "register_name": "temperature",
      "alarm_level": "critical",
      "alarm_message": "温度过高",
      "threshold": 50.0,
      "actual_value": 55.0,
      "timestamp": "2024-01-01T12:00:00",
      "acknowledged": false
    }
  ]
}
```

### 4.2 获取活动报警
**请求**: `GET /api/alarms/active`

**响应**:
```json
{
  "alarms": [
    {
      "alarm_id": "temp_high",
      "device_id": "temp_sensor_01",
      "register_name": "temperature",
      "alarm_level": "critical",
      "alarm_message": "温度过高",
      "first_trigger_time": "2024-01-01T12:00:00",
      "last_trigger_time": "2024-01-01T12:05:00",
      "trigger_count": 10,
      "acknowledged": false
    }
  ]
}
```

### 4.3 确认报警
**请求**: `POST /api/alarms/{alarm_id}/acknowledge`

**请求体**:
```json
{
  "device_id": "temp_sensor_01",
  "register_name": "temperature",
  "acknowledged_by": "admin"
}
```

**响应**:
```json
{
  "success": true,
  "message": "报警已确认"
}
```

## 5. 系统信息API

### 5.1 获取系统状态
**请求**: `GET /api/system/status`

**响应**:
```json
{
  "devices": [...],
  "collector": {
    "total_collections": 1000,
    "successful_collections": 998,
    "failed_collections": 2,
    "last_collection_time": "2024-01-01T12:00:00"
  },
  "database": {
    "realtime_records": 10000,
    "history_records": 50000,
    "alarm_records": 100
  }
}
```

### 5.2 获取数据库统计
**请求**: `GET /api/system/database`

**响应**:
```json
{
  "realtime_records": 10000,
  "history_records": 50000,
  "alarm_records": 100,
  "database_size_mb": 15.5
}
```

## 6. 数据导出API

### 6.1 导出设备数据
**请求**: `POST /api/export/device/{device_id}`

**请求体**:
```json
{
  "start_time": "2024-01-01T00:00:00",
  "end_time": "2024-01-01T23:59:59",
  "format": "csv"
}
```

**响应**:
```json
{
  "success": true,
  "filepath": "exports/temp_sensor_01_20240101_120000.csv"
}
```

### 6.2 导出报警记录
**请求**: `POST /api/export/alarms`

**请求体**:
```json
{
  "start_time": "2024-01-01T00:00:00",
  "end_time": "2024-01-01T23:59:59",
  "format": "csv"
}
```

**响应**:
```json
{
  "success": true,
  "filepath": "exports/alarms_20240101_120000.csv"
}
```

## 7. WebSocket事件

### 7.1 客户端事件
- `connect`: 连接服务器
- `disconnect`: 断开连接
- `subscribe`: 订阅设备数据
- `unsubscribe`: 取消订阅

### 7.2 服务端事件
- `connected`: 连接成功
- `data_update`: 数据更新
- `alarm`: 报警通知
- `system_status`: 系统状态

## 8. 错误码

- `200`: 成功
- `400`: 请求参数错误
- `404`: 资源不存在
- `500`: 服务器内部错误

## 9. 示例代码

### 9.1 Python示例
```python
import requests

# 获取设备列表
r = requests.get('http://localhost:5000/api/devices')
devices = r.json()['devices']

# 获取实时数据
r = requests.get('http://localhost:5000/api/data/realtime?limit=10')
data = r.json()['data']

# 导出数据
r = requests.post('http://localhost:5000/api/export/device/temp_sensor_01', json={
    'start_time': '2024-01-01T00:00:00',
    'end_time': '2024-01-01T23:59:59',
    'format': 'csv'
})
result = r.json()
```

### 9.2 JavaScript示例
```javascript
// 获取设备列表
fetch('/api/devices')
  .then(r => r.json())
  .then(data => console.log(data.devices));

// 获取实时数据
fetch('/api/data/realtime?limit=10')
  .then(r => r.json())
  .then(data => console.log(data.data));
```
