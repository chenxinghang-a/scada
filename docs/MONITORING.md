# SmartSCADA 监控告警配置指南

## 概述

SmartSCADA 提供多层监控机制，确保工业系统稳定运行。

---

## 1. 健康检查

### API端点
```
GET /api/health/status
```

### 响应示例
```json
{
  "success": true,
  "data": {
    "global_status": "healthy",
    "modules": {
      "data_collector": { "status": "running" },
      "alarm_manager": { "status": "running" },
      "device_manager": { "status": "running" }
    },
    "checks": {
      "database": true,
      "websocket": true,
      "device_connection": true
    },
    "unhealthy_modules": []
  }
}
```

### 健康状态
- `healthy`: 所有模块正常
- `degraded`: 部分模块异常
- `unhealthy`: 系统不可用

---

## 2. Prometheus指标

### 端点
```
GET /metrics
```

### 关键指标

#### 系统指标
```
# 设备总数
scada_devices_total{status="online"} 25
scada_devices_total{status="offline"} 3

# 报警统计
scada_alarms_active{level="critical"} 2
scada_alarms_active{level="warning"} 5

# 数据采集
scada_collections_total 12345
scada_collections_success 12300
scada_collections_failed 45

# WebSocket连接
scada_websocket_connections 8
```

#### 性能指标
```
# API响应时间
scada_api_request_duration_seconds{endpoint="/api/devices"} 0.05

# 数据库查询时间
scada_db_query_duration_seconds{table="history_data"} 0.1

# 采集延迟
scada_collection_delay_seconds{device="motor_01"} 0.2
```

---

## 3. Prometheus配置

### prometheus.yml
```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: 'smartscada'
    static_configs:
      - targets: ['localhost:5000']
    metrics_path: '/metrics'
    scheme: 'http'
```

### 启动Prometheus
```bash
prometheus --config.file=prometheus.yml
```

---

## 4. Grafana仪表盘

### 安装Grafana
```bash
# Windows
choco install grafana

# 或下载: https://grafana.com/grafana/download
```

### 配置数据源
1. 打开 http://localhost:3000
2. 添加Prometheus数据源
3. URL: http://localhost:9090

### 推荐仪表盘
- 设备状态概览
- 报警趋势
- 数据采集统计
- 系统性能

---

## 5. 告警规则

### 设备离线告警
```yaml
groups:
  - name: device_alerts
    rules:
      - alert: DeviceOffline
        expr: scada_devices_total{status="offline"} > 0
        for: 1m
        labels:
          severity: warning
        annotations:
          summary: "设备离线"
          description: "{{ $value }} 个设备离线超过1分钟"
```

### 报警堆积告警
```yaml
      - alert: AlarmBacklog
        expr: scada_alarms_active{level="critical"} > 10
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "报警堆积"
          description: "{{ $value }} 个严重报警未处理"
```

### 采集失败告警
```yaml
      - alert: CollectionFailures
        expr: rate(scada_collections_failed[5m]) > 0.1
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "采集失败率过高"
          description: "过去5分钟采集失败率 {{ $value | humanizePercentage }}"
```

---

## 6. 邮件通知

### 配置SMTP
在 `.env` 中添加：
```env
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=alert@example.com
SMTP_PASSWORD=password
SMTP_FROM=SmartSCADA <alert@example.com>
```

### 配置通知规则
```yaml
receivers:
  - name: 'email'
    email_configs:
      - to: 'operator@example.com'
        from: 'alert@example.com'
        smarthost: 'smtp.example.com:587'
        auth_username: 'alert@example.com'
        auth_password: 'password'
```

---

## 7. 短信通知

### 阿里云短信
```python
# 配置短信服务
SMS_ACCESS_KEY=your_access_key
SMS_ACCESS_SECRET=your_access_secret
SMS_SIGN_NAME=SmartSCADA
SMS_TEMPLATE_CODE=SMS_123456
```

### 通知模板
```
【SmartSCADA】告警: {device_id} {register_name} {level} - {message}
```

---

## 8. 声光报警器

### Patlite LR7配置
```yaml
signal_tower:
  enabled: true
  host: 192.168.1.70
  port: 502
  slave_id: 1
  do_mapping:
    red_light: 0
    yellow_light: 1
    green_light: 2
    buzzer: 5
```

### 报警级别映射
| 报警级别 | 红灯 | 黄灯 | 绿灯 | 蜂鸣器 |
|----------|------|------|------|--------|
| critical | 闪烁 | 灭 | 灭 | 响 |
| warning | 灭 | 闪烁 | 灭 | 响 |
| info | 灭 | 灭 | 闪烁 | 灭 |
| normal | 灭 | 灭 | 亮 | 灭 |

---

## 9. 广播系统

### MQTT配置
```yaml
broadcast:
  enabled: true
  mqtt:
    broker: 192.168.1.200
    port: 1883
    topic_prefix: pa/
    username: broadcast_user
    password: broadcast_pass
  areas:
    - 车间A
    - 车间B
    - 仓库
```

### 报警播报
```
[车间A] 高温报警: 电机01 温度 85°C 超过阈值 80°C
```

---

## 10. 日志监控

### ELK Stack配置

#### Filebeat
```yaml
filebeat.inputs:
  - type: log
    paths:
      - /path/to/scada/logs/*.log
    json.keys_under_root: true

output.elasticsearch:
  hosts: ["localhost:9200"]
```

#### Logstash
```ruby
input {
  beats {
    port => 5044
  }
}
filter {
  json {
    source => "message"
  }
}
output {
  elasticsearch {
    hosts => ["localhost:9200"]
    index => "smartscada-%{+YYYY.MM.dd}"
  }
}
```

---

## 11. 性能监控

### 关键性能指标
- API响应时间 < 100ms
- 数据采集延迟 < 1s
- WebSocket推送延迟 < 500ms
- 数据库查询时间 < 500ms

### 告警阈值
| 指标 | 警告阈值 | 严重阈值 |
|------|----------|----------|
| API响应时间 | > 500ms | > 2s |
| 采集延迟 | > 5s | > 30s |
| CPU使用率 | > 70% | > 90% |
| 内存使用率 | > 80% | > 95% |
| 磁盘使用率 | > 80% | > 95% |

---

## 12. 定期检查

### 每日检查
- [ ] 设备连接状态
- [ ] 报警统计
- [ ] 数据采集成功率
- [ ] 系统日志

### 每周检查
- [ ] 数据库大小
- [ ] 历史数据归档
- [ ] 性能指标趋势
- [ ] 告警规则有效性

### 每月检查
- [ ] 安全审计日志
- [ ] 用户权限审查
- [ ] 备份恢复测试
- [ ] 系统容量规划
