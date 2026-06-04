# SmartSCADA API 文档

## 概述

SmartSCADA 后端提供 RESTful API 用于工业数据采集、设备管理、报警控制等功能。

**基础URL**: `http://localhost:5000/api`

**认证方式**: JWT Bearer Token
```
Authorization: Bearer <token>
```

## 通用响应格式

### 成功响应
```json
{
  "success": true,
  "data": { ... },
  "message": "操作成功"
}
```

### 错误响应
```json
{
  "error": "错误描述"
}
```

---

## 认证 API

### POST /auth/login
用户登录

**请求体**:
```json
{
  "username": "admin",
  "password": "password123"
}
```

**响应**:
```json
{
  "success": true,
  "token": "eyJ...",
  "refresh_token": "abc...",
  "user": {
    "username": "admin",
    "role": "admin",
    "display_name": "管理员"
  }
}
```

### POST /auth/logout
用户登出

### POST /auth/refresh
刷新Token

### POST /auth/register
注册新用户（管理员）

### POST /auth/force-change-password
强制修改密码

---

## 设备管理 API

### GET /devices
获取所有设备列表

**响应**:
```json
{
  "devices": [
    {
      "device_id": "motor_01",
      "name": "电机01",
      "protocol": "modbus_tcp",
      "host": "192.168.1.100",
      "port": 502,
      "connected": true,
      "registers": [...]
    }
  ]
}
```

### GET /devices/{device_id}
获取单个设备详情

### POST /devices
添加新设备

### PUT /devices/{device_id}
更新设备配置

### DELETE /devices/{device_id}
删除设备

### GET /devices/protocols
获取支持的协议列表

### POST /devices/{device_id}/write-register
写入寄存器

**请求体**:
```json
{
  "address": 100,
  "value": 50.5
}
```

### POST /devices/{device_id}/write-coil
写入线圈

### POST /devices/{device_id}/test
测试设备连接

---

## 数据 API

### GET /data/realtime
获取实时数据

### GET /data/latest/{device_id}
获取设备最新数据

### GET /data/history/{device_id}/{register_name}
获取历史数据

**查询参数**:
- `start_time`: 开始时间 (ISO格式)
- `end_time`: 结束时间 (ISO格式)
- `interval`: 聚合间隔 (1min, 5min, 1hour, 1day)
- `limit`: 返回条数限制

### POST /export/device/{device_id}
导出设备数据

---

## 报警 API

### GET /alarms
获取报警记录

**查询参数**:
- `device_id`: 设备ID筛选
- `alarm_level`: 报警级别筛选
- `acknowledged`: 确认状态筛选
- `limit`: 返回条数限制

### GET /alarms/active
获取活动报警

### POST /alarms/{alarm_id}/acknowledge
确认报警

### GET /alarms/statistics
获取报警统计

---

## 系统 API

### GET /system/status
获取系统状态

### GET /system/database
获取数据库统计

### GET /system/simulation-mode
获取模拟模式状态

### POST /system/simulation-mode
切换模拟模式

### GET /config
获取系统配置

### PUT /config
更新系统配置

---

## 健康检查 API

### GET /health/status
获取健康状态

### GET /health/modules
获取所有模块状态

### GET /health/modules/{module_name}
获取指定模块状态

### GET /health/checks
获取所有健康检查结果

---

## 控制 API

### POST /control/estop
紧急停机

### POST /control/estop/reset
复位急停

### GET /control/interlocks
获取安全联锁列表

### POST /control/interlocks/{rule_id}/bypass
旁路联锁

### POST /control/batch
批量控制

---

## 工业4.0 API

### GET /industry40/health
获取设备健康评分

### GET /industry40/oee
获取OEE数据

### GET /industry40/spc/{device_id}/{register_name}
获取SPC控制图数据

### GET /industry40/energy
获取能源数据

### GET /industry40/edge/status
获取边缘决策状态

---

## WebSocket 事件

### 连接
```javascript
const socket = io('http://localhost:5000', {
  auth: { token: 'jwt_token' }
});
```

### 订阅设备数据
```javascript
socket.emit('subscribe', { device_id: 'motor_01' });
```

### 接收数据更新
```javascript
socket.on('data_update', (data) => {
  // data: { device_id, register_name, value, quality, timestamp }
});
```

### 接收报警通知
```javascript
socket.on('alarm', (data) => {
  // data: { alarm_id, device_id, register_name, alarm_level, alarm_message }
});
```

---

## 错误码

| 状态码 | 含义 |
|--------|------|
| 200 | 成功 |
| 400 | 请求参数错误 |
| 401 | 未认证 |
| 403 | 权限不足 |
| 404 | 资源不存在 |
| 500 | 服务器内部错误 |

---

## 速率限制

- 默认: 200次/分钟
- 登录: 5次/分钟

---

## 注意事项

1. 所有时间字段使用ISO 8601格式
2. 设备ID区分大小写
3. 写入操作需要工程师或管理员权限
4. WebSocket连接需要有效的JWT Token
