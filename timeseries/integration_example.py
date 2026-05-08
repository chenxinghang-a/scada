"""
TDengine集成示例

展示如何将TDengine时序数据库集成到现有SCADA系统。

架构说明：
1. 网关服务通过MQTT发布标准化数据
2. MQTT到TDengine服务订阅并写入TDengine
3. 业务模块从TDengine查询数据
4. API层提供数据查询接口

优势：
- 查询性能提升100倍以上
- 支持复杂的时间窗口聚合
- 自动数据压缩和过期
"""

import sys
import logging
from pathlib import Path
from datetime import datetime, timedelta

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from timeseries import TDengineClient, QueryBuilder
from timeseries.mqtt_to_tsdb import MQTTToTSDBService, OEEDataWriter, EnergyDataWriter
from gateway import MQTTSubscriber


def demonstrate_basic_usage():
    """演示基本用法"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger("IntegrationExample")
    
    # 1. 创建TDengine客户端
    logger.info("创建TDengine客户端...")
    client = TDengineClient("localhost", 6041)
    
    # 2. 连接
    if not client.connect():
        logger.error("TDengine连接失败，请确保TDengine已启动")
        logger.info("启动TDengine: docker run -d --name tdengine -p 6041:6041 tdengine/tdengine")
        return
    
    # 3. 初始化表
    logger.info("初始化表结构...")
    client.init_tables()
    
    # 4. 写入测试数据
    logger.info("写入测试数据...")
    from timeseries.data_models import TelemetryRecord
    
    for i in range(10):
        record = TelemetryRecord(
            device_id="CNC_001",
            register_name="temperature",
            timestamp=datetime.now() - timedelta(minutes=i),
            value=25.0 + i * 0.5,
            quality=192,
            unit="°C",
            protocol="ModbusTCP",
            gateway_id="gateway_01"
        )
        client.write_telemetry(record)
    
    logger.info("写入完成")
    
    # 5. 查询数据
    logger.info("查询数据...")
    end_time = datetime.now()
    start_time = end_time - timedelta(hours=1)
    
    data = client.query_telemetry("CNC_001", "temperature", start_time, end_time)
    logger.info(f"查询到 {len(data)} 条记录")
    
    for record in data[:5]:  # 只显示前5条
        logger.info(f"  {record['timestamp']}: {record['value']}")
    
    # 6. 查询聚合数据
    logger.info("查询聚合数据...")
    agg_data = client.query_telemetry_agg(
        "CNC_001", "temperature",
        start_time, end_time,
        interval="5m"
    )
    logger.info(f"聚合结果: {len(agg_data)} 条")
    
    # 7. 打印统计
    logger.info(f"统计信息: {client.get_stats()}")


def demonstrate_mqtt_integration():
    """演示MQTT集成"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger("MQTTIntegration")
    
    # 1. 创建客户端
    logger.info("创建客户端...")
    tdengine = TDengineClient("localhost", 6041)
    subscriber = MQTTSubscriber("localhost", 1883)
    
    # 2. 创建服务
    logger.info("创建MQTT到TDengine服务...")
    service = MQTTToTSDBService(subscriber, tdengine)
    
    # 3. 启动服务
    logger.info("启动服务...")
    service.start()
    
    logger.info("=" * 50)
    logger.info("服务已启动")
    logger.info("等待MQTT数据...")
    logger.info("按 Ctrl+C 停止")
    logger.info("=" * 50)
    
    try:
        import time
        while True:
            time.sleep(10)
            stats = service.get_stats()
            logger.info(f"统计: {stats}")
    except KeyboardInterrupt:
        logger.info("正在停止...")
    finally:
        service.stop()


def demonstrate_query_builder():
    """演示查询构建器"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger("QueryBuilder")
    
    # 创建查询构建器
    builder = QueryBuilder("device_telemetry")
    
    # 构建查询
    end_time = datetime.now()
    start_time = end_time - timedelta(hours=24)
    
    sql = (builder
           .select("ts", "value", "quality")
           .where_device("CNC_001")
           .where_time(start_time, end_time)
           .where_quality(192)
           .interval("1h")
           .order_by("ts", "DESC")
           .limit(100)
           .build())
    
    logger.info("构建的SQL:")
    logger.info(sql)
    
    # 构建聚合查询
    builder2 = QueryBuilder("device_telemetry")
    sql2 = (builder2
            .select_agg("AVG", "value", "avg_value")
            .select_agg("MAX", "value", "max_value")
            .select_agg("MIN", "value", "min_value")
            .where_device("CNC_001")
            .where_time(start_time, end_time)
            .interval("1h")
            .build())
    
    logger.info("\n聚合查询SQL:")
    logger.info(sql2)


def demonstrate_business_integration():
    """演示业务模块集成"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger("BusinessIntegration")
    
    # 创建客户端
    tdengine = TDengineClient("localhost", 6041)
    
    if not tdengine.connect():
        logger.error("TDengine连接失败")
        return
    
    # 创建业务写入器
    oee_writer = OEEDataWriter(tdengine)
    energy_writer = EnergyDataWriter(tdengine)
    
    # 模拟OEE计算结果
    logger.info("写入OEE数据...")
    oee_writer.write_oee(
        device_id="CNC_001",
        availability=0.95,
        performance=0.98,
        quality_rate=0.99,
        oee=0.92,
        total_count=1000,
        good_count=990,
        run_time=28800,
        downtime=1440
    )
    
    # 模拟能源数据
    logger.info("写入能源数据...")
    energy_writer.write_energy(
        device_id="CNC_001",
        power=15.5,
        energy=1234.5,
        voltage=220.0,
        current=70.5,
        power_factor=0.95
    )
    
    logger.info("写入完成")
    
    # 查询OEE数据
    logger.info("查询OEE数据...")
    end_time = datetime.now()
    start_time = end_time - timedelta(hours=1)
    
    oee_data = tdengine.query_oee("CNC_001", start_time, end_time)
    logger.info(f"OEE数据: {len(oee_data)} 条")
    
    # 查询能源数据
    logger.info("查询能源数据...")
    energy_data = tdengine.query_energy("CNC_001", start_time, end_time, interval="1h")
    logger.info(f"能源数据: {len(energy_data)} 条")


def show_architecture():
    """展示架构图"""
    print("""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    工业4.0 SCADA系统 — 四层漏斗架构                          ║
║                    第四层：时序数据库层                                       ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  ┌─────────────────────────────────────────────────────────────────────┐    ║
║  │                    主系统 (SCADA)                                    │    ║
║  │  ┌─────────────────────────────────────────────────────────────┐    │    ║
║  │  │  API层 → 业务模块 (OEE、预测性维护、SPC、能源管理)           │    │    ║
║  │  │         ↓ 查询                                              │    │    ║
║  │  │  TDengineClient → 查询构建器 → SQL执行                      │    │    ║
║  │  └─────────────────────────────────────────────────────────────┘    │    ║
║  └─────────────────────────────────────────────────────────────────────┘    ║
║                                  ↑ 查询                                      ║
║  ┌─────────────────────────────────────────────────────────────────────┐    ║
║  │                    TDengine时序数据库                                │    ║
║  │  ┌─────────────────────────────────────────────────────────────┐    │    ║
║  │  │  超级表: device_telemetry (遥测数据)                        │    │    ║
║  │  │  超级表: alarm_records (报警记录)                           │    │    ║
║  │  │  超级表: oee_records (OEE记录)                              │    │    ║
║  │  │  超级表: energy_records (能源记录)                          │    │    ║
║  │  │  超级表: predictive_records (预测维护)                      │    │    ║
║  │  └─────────────────────────────────────────────────────────────┘    │    ║
║  │  优势: 查询性能提升100倍 | 自动压缩 | 数据过期 | 降采样聚合       │    ║
║  └─────────────────────────────────────────────────────────────────────┘    ║
║                                  ↑ 写入                                      ║
║  ┌─────────────────────────────────────────────────────────────────────┐    ║
║  │                    MQTT到TDengine服务                                │    ║
║  │  ┌─────────────────────────────────────────────────────────────┐    │    ║
║  │  │  MQTTSubscriber → 数据缓冲 → 批量写入 → TDengine            │    │    ║
║  │  └─────────────────────────────────────────────────────────────┘    │    ║
║  └─────────────────────────────────────────────────────────────────────┘    ║
║                                  ↑ 订阅                                      ║
║  ┌─────────────────────────────────────────────────────────────────────┐    ║
║  │                    MQTT Broker (EMQX)                                │    ║
║  │  ┌─────────────────────────────────────────────────────────────┐    │    ║
║  │  │  Topic: scada/devices/{device_id}/telemetry                 │    │    ║
║  │  │  Topic: scada/alarms/{level}                                │    │    ║
║  │  └─────────────────────────────────────────────────────────────┘    │    ║
║  └─────────────────────────────────────────────────────────────────────┘    ║
║                                  ↑ 发布                                      ║
║  ┌─────────────────────────────────────────────────────────────────────┐    ║
║  │                    协议网关服务                                       │    ║
║  │  ┌─────────────┬─────────────┬─────────────┬─────────────┐          │    ║
║  │    │ Modbus网关  │   S7网关    │  OPC UA网关 │  MQTT网关   │          │    ║
║  │    └─────────────┴─────────────┴─────────────┴─────────────┘          │    ║
║  │         ↑              ↑              ↑              ↑               │    ║
║  │    ┌─────────┐    ┌─────────┐    ┌─────────┐    ┌─────────┐        │    ║
║  │    │  PLC    │    │  CNC    │    │  SCADA  │    │ Sensor  │        │    ║
║  │    └─────────┘    └─────────┘    └─────────┘    └─────────┘        │    ║
║  └─────────────────────────────────────────────────────────────────────┘    ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
    """)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='TDengine集成示例')
    parser.add_argument('--demo', type=str, 
                        choices=['basic', 'mqtt', 'query', 'business'],
                        help='运行指定演示')
    parser.add_argument('--arch', action='store_true', help='显示架构图')
    
    args = parser.parse_args()
    
    if args.arch:
        show_architecture()
    elif args.demo == 'basic':
        demonstrate_basic_usage()
    elif args.demo == 'mqtt':
        demonstrate_mqtt_integration()
    elif args.demo == 'query':
        demonstrate_query_builder()
    elif args.demo == 'business':
        demonstrate_business_integration()
    else:
        show_architecture()
        print("\n可用的演示:")
        print("  --demo basic     基本用法演示")
        print("  --demo mqtt      MQTT集成演示")
        print("  --demo query     查询构建器演示")
        print("  --demo business  业务模块集成演示")
