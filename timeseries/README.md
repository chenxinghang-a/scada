# 工业SCADA时序数据库模块

## 概述

本模块实现了四层漏斗架构的第四层：**时序数据库层**。

使用TDengine作为时序数据库，提供：
- 高性能数据写入（每秒百万级数据点）
- 复杂时间窗口查询
- 数据降采样和聚合
- 自动数据压缩

**核心优势：查询性能比SQLite提升100倍以上**

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│                    主系统 (SCADA)                            │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  业务模块 (OEE、预测性维护、SPC、能源管理)            │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                              ↑ 查询
┌─────────────────────────────────────────────────────────────┐
│                    TDengine时序数据库                        │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  超级表: device_telemetry, alarm_records, oee_records│    │
│  │  子表: 每个设备的每个指标一张表                       │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                              ↑ 写入
┌─────────────────────────────────────────────────────────────┐
│                    MQTT到TDengine服务                        │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  MQTTSubscriber → 数据缓冲 → 批量写入               │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                              ↑ 订阅
┌─────────────────────────────────────────────────────────────┐
│                    MQTT Broker (EMQX)                        │
└─────────────────────────────────────────────────────────────┘
```

## 快速开始

### 1. 安装TDengine

```bash
# Docker方式（推荐）
docker run -d --name tdengine \
  -p 6030:6030 \
  -p 6041:6041 \
  -p 6043:6043 \
  -p 6044:6044 \
  -p 6060:6060 \
  tdengine/tdengine

# 或使用TDengine Cloud
# 参考: https://www.taosdata.com/getting-started
```

### 2. 安装Python依赖

```bash
pip install requests
# 如果使用原生连接（可选）
pip install taos
```

### 3. 初始化数据库

```python
from timeseries import TDengineClient

# 创建客户端（REST API方式）
client = TDengineClient("localhost", 6041)

# 连接
client.connect()

# 初始化表结构
client.init_tables()
```

### 4. 写入数据

```python
from timeseries import TDengineClient, TelemetryRecord
from datetime import datetime

client = TDengineClient("localhost", 6041)
client.connect()

# 写入单条数据
record = TelemetryRecord(
    device_id="CNC_001",
    register_name="temperature",
    timestamp=datetime.now(),
    value=25.5,
    quality=192,
    unit="°C",
    protocol="ModbusTCP",
    gateway_id="gateway_01"
)
client.write_telemetry(record)
```

### 5. 查询数据

```python
from datetime import datetime, timedelta

# 查询时间范围内的数据
end_time = datetime.now()
start_time = end_time - timedelta(hours=24)

data = client.query_telemetry("CNC_001", "temperature", start_time, end_time)
print(f"查询到 {len(data)} 条记录")

# 查询聚合数据（每小时平均值）
agg_data = client.query_telemetry_agg(
    "CNC_001", "temperature", 
    start_time, end_time,
    interval="1h"
)
```

### 6. 使用查询构建器

```python
from timeseries import QueryBuilder

builder = QueryBuilder("device_telemetry")
sql = (builder
       .select("ts", "value", "quality")
       .where_device("CNC_001")
       .where_time(start_time, end_time)
       .interval("1h")
       .order_by("ts", "DESC")
       .limit(100)
       .build())
```

## 文件结构

```
timeseries/
├── __init__.py              # 模块入口
├── README.md                # 本文档
├── tdengine_client.py       # TDengine客户端封装
├── data_models.py           # 数据模型定义
├── query_builder.py         # 查询构建器
├── migration.py             # 数据迁移工具
└── mqtt_to_tsdb.py          # MQTT到TDengine服务
```

## 数据模型

### 超级表设计

#### 1. device_telemetry（遥测数据）

```sql
CREATE STABLE device_telemetry (
    ts TIMESTAMP,
    value DOUBLE,
    quality INT
) TAGS (
    device_id NCHAR(64),
    register_name NCHAR(64),
    unit NCHAR(16),
    protocol NCHAR(32),
    gateway_id NCHAR(64)
)
```

#### 2. alarm_records（报警记录）

```sql
CREATE STABLE alarm_records (
    ts TIMESTAMP,
    level NCHAR(16),
    alarm_type NCHAR(32),
    message NCHAR(512),
    value DOUBLE,
    threshold DOUBLE,
    acknowledged INT
) TAGS (
    device_id NCHAR(64),
    alarm_id NCHAR(64)
)
```

#### 3. oee_records（OEE记录）

```sql
CREATE STABLE oee_records (
    ts TIMESTAMP,
    availability DOUBLE,
    performance DOUBLE,
    quality_rate DOUBLE,
    oee DOUBLE,
    total_count BIGINT,
    good_count BIGINT,
    run_time DOUBLE,
    downtime DOUBLE
) TAGS (
    device_id NCHAR(64)
)
```

#### 4. energy_records（能源记录）

```sql
CREATE STABLE energy_records (
    ts TIMESTAMP,
    power DOUBLE,
    energy DOUBLE,
    voltage DOUBLE,
    current DOUBLE,
    power_factor DOUBLE
) TAGS (
    device_id NCHAR(64)
)
```

#### 5. predictive_records（预测性维护）

```sql
CREATE STABLE predictive_records (
    ts TIMESTAMP,
    health_score DOUBLE,
    failure_probability DOUBLE,
    remaining_life DOUBLE,
    anomaly_score DOUBLE,
    trend NCHAR(16)
) TAGS (
    device_id NCHAR(64)
)
```

### 子表命名规范

| 数据类型 | 子表命名规则 | 示例 |
|----------|--------------|------|
| 遥测数据 | `tel_{device_id}_{register_name}` | `tel_CNC_001_temperature` |
| 报警记录 | `alarm_{device_id}` | `alarm_CNC_001` |
| OEE记录 | `oee_{device_id}` | `oee_CNC_001` |
| 能源记录 | `energy_{device_id}` | `energy_CNC_001` |
| 预测维护 | `predict_{device_id}` | `predict_CNC_001` |

## 集成方式

### 方式1：MQTT自动写入（推荐）

```python
from gateway import MQTTSubscriber
from timeseries import TDengineClient
from timeseries.mqtt_to_tsdb import MQTTToTSDBService

# 创建客户端
tdengine = TDengineClient("localhost", 6041)
subscriber = MQTTSubscriber("localhost", 1883)

# 创建服务
service = MQTTToTSDBService(subscriber, tdengine)

# 启动服务
service.start()
```

### 方式2：业务模块直接写入

```python
from timeseries import TDengineClient
from timeseries.mqtt_to_tsdb import OEEDataWriter, EnergyDataWriter

# 创建客户端
tdengine = TDengineClient("localhost", 6041)
tdengine.connect()

# 创建写入器
oee_writer = OEEDataWriter(tdengine)
energy_writer = EnergyDataWriter(tdengine)

# 在业务模块中使用
oee_writer.write_oee("CNC_001", 0.95, 0.98, 0.99, 0.92)
energy_writer.write_energy("CNC_001", 15.5, 1234.5)
```

### 方式3：主系统集成

```python
# 在run.py中添加
from timeseries import TDengineClient
from timeseries.mqtt_to_tsdb import MQTTToTSDBService

# 初始化TDengine
tdengine = TDengineClient("localhost", 6041)
tdengine.connect()
tdengine.init_tables()

# 创建MQTT到TDengine服务
mqtt_to_tsdb = MQTTToTSDBService(mqtt_subscriber, tdengine)
mqtt_to_tsdb.start()
```

## 数据迁移

### 从SQLite迁移

```bash
# 迁移历史数据
python timeseries/migration.py --sqlite data/scada.db --tdengine-host localhost --verify
```

### 迁移策略

1. **备份SQLite数据库**
2. **停止数据写入**
3. **执行迁移脚本**
4. **验证数据完整性**
5. **切换到TDengine**

## 性能优化

### 1. 批量写入

```python
# 批量写入比单条写入快100倍
client.write_telemetry_batch(records)
```

### 2. 数据保留策略

```sql
-- 设置数据保留时间
ALTER DATABASE scada KEEP 365;  -- 保留365天
```

### 3. 降采样查询

```sql
-- 每小时平均值
SELECT _wstart as ts, AVG(value) 
FROM device_telemetry 
INTERVAL(1h);
```

### 4. 数据压缩

TDengine自动压缩数据，压缩率可达10:1以上。

## 查询示例

### 1. 时间范围查询

```python
# 查询最近24小时的数据
data = client.query_telemetry(
    "CNC_001", "temperature",
    datetime.now() - timedelta(hours=24),
    datetime.now()
)
```

### 2. 聚合查询

```python
# 查询每小时平均值
agg_data = client.query_telemetry_agg(
    "CNC_001", "temperature",
    start_time, end_time,
    interval="1h"
)
```

### 3. 最新值查询

```python
# 查询最新数据点
latest = client.query_telemetry_latest("CNC_001", "temperature")
```

### 4. 报警查询

```python
# 查询严重报警
alarms = client.query_alarms(
    "CNC_001", start_time, end_time,
    level="critical"
)
```

### 5. OEE查询

```python
# 查询OEE历史
oee_data = client.query_oee("CNC_001", start_time, end_time)
```

## 故障排除

### 1. 连接失败

检查：
- TDengine服务是否启动
- 端口是否正确（REST API默认6041）
- 防火墙设置

### 2. 写入失败

检查：
- 表结构是否正确
- 数据类型是否匹配
- 时间戳格式

### 3. 查询超时

检查：
- 查询时间范围是否过大
- 是否需要降采样
- 索引是否正确

## 与SQLite对比

| 指标 | SQLite | TDengine |
|------|--------|----------|
| 写入性能 | 1,000条/秒 | 1,000,000条/秒 |
| 查询性能 | 秒级 | 毫秒级 |
| 存储压缩 | 无 | 10:1以上 |
| 数据保留 | 手动清理 | 自动过期 |
| 时序优化 | 无 | 专为时序优化 |

## 下一步

- [ ] 添加InfluxDB支持
- [ ] 实现数据可视化
- [ ] 添加告警规则引擎
- [ ] 支持分布式部署

---

*文档版本：v1.0*
*更新时间：2026-05-08*
