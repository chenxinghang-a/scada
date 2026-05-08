"""
网关集成示例

展示如何将协议网关集成到主系统中，实现四层漏斗架构。

架构说明：
1. 边缘网关层：独立进程运行，负责协议解析
2. 统一物模型：所有数据转换为标准格式
3. 消息总线：通过MQTT实现异步解耦
4. 主系统：订阅MQTT数据，进行业务处理

优势：
- 故障隔离：网关崩溃不影响主系统
- 水平扩展：可以部署多个网关实例
- 协议无关：主系统不关心底层协议
"""

import sys
import logging
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from gateway import (
    MQTTSubscriber, MQTTDataDistributor,
    DeviceTelemetry, DeviceStatus, AlarmMessage
)


def create_integrated_system():
    """
    创建集成系统示例
    
    展示如何将MQTT订阅集成到现有SCADA系统。
    """
    
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger("IntegrationExample")
    
    # 1. 创建MQTT订阅客户端
    logger.info("创建MQTT订阅客户端...")
    subscriber = MQTTSubscriber(
        broker_host="localhost",
        broker_port=1883,
        client_id="scada_main_system"
    )
    
    # 2. 创建数据分发器
    logger.info("创建数据分发器...")
    distributor = MQTTDataDistributor(subscriber)
    
    # 3. 初始化业务模块（模拟）
    # 在实际系统中，这些是真实的业务模块
    class MockOEECalculator:
        def update_from_telemetry(self, telemetry: DeviceTelemetry):
            logger.info(f"OEE收到数据: {telemetry.DeviceID}")
            # 处理OEE计算...
    
    class MockPredictiveMaintenance:
        def update_from_telemetry(self, telemetry: DeviceTelemetry):
            logger.info(f"预测性维护收到数据: {telemetry.DeviceID}")
            # 处理预测性维护...
    
    class MockAlarmManager:
        def process_alarm(self, alarm: AlarmMessage):
            logger.info(f"报警管理器收到报警: {alarm.DeviceID} - {alarm.Message}")
            # 处理报警...
    
    # 4. 注册业务模块
    logger.info("注册业务模块...")
    distributor.register_module('oee', MockOEECalculator())
    distributor.register_module('predictive', MockPredictiveMaintenance())
    distributor.register_module('alarm', MockAlarmManager())
    
    # 5. 订阅所有主题
    logger.info("订阅MQTT主题...")
    subscriber.subscribe_all()
    
    # 6. 启动订阅
    logger.info("启动MQTT订阅...")
    subscriber.start()
    
    return subscriber, distributor


def demonstrate_data_flow():
    """
    演示数据流
    
    展示数据如何从网关流向主系统。
    """
    import time
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger("DataFlowDemo")
    
    # 创建集成系统
    subscriber, distributor = create_integrated_system()
    
    logger.info("=" * 50)
    logger.info("系统已启动，等待网关数据...")
    logger.info("=" * 50)
    
    logger.info("\n数据流说明：")
    logger.info("1. 网关进程读取设备数据（Modbus/S7/OPC UA）")
    logger.info("2. 网关将数据转换为统一物模型（JSON）")
    logger.info("3. 网关发布数据到MQTT Broker")
    logger.info("4. 主系统订阅MQTT，接收标准化数据")
    logger.info("5. 数据分发到各业务模块（OEE、预测性维护等）")
    logger.info("")
    logger.info("优势：")
    logger.info("- 网关和主系统完全解耦")
    logger.info("- 网关崩溃不影响主系统")
    logger.info("- 可以水平扩展多个网关")
    logger.info("- 主系统不关心底层协议")
    
    try:
        while True:
            time.sleep(5)
            stats = subscriber.get_stats()
            logger.info(f"统计: 收到 {stats['messages_received']} 条消息")
    except KeyboardInterrupt:
        logger.info("正在停止...")
    finally:
        subscriber.stop()


def show_architecture():
    """展示架构图"""
    print("""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    工业4.0 SCADA系统 — 四层漏斗架构                          ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  ┌─────────────────────────────────────────────────────────────────────┐    ║
║  │                    第四层：时序数据库 (TSDB)                          │    ║
║  │         TDengine / InfluxDB / TimescaleDB                           │    ║
║  │    ┌─────────────────────────────────────────────────────┐          │    ║
║  │    │  历史数据存储 | 降采样聚合 | 时间窗口查询 | 数据压缩  │          │    ║
║  │    └─────────────────────────────────────────────────────┘          │    ║
║  └─────────────────────────────────────────────────────────────────────┘    ║
║                                  ↑ 订阅                                      ║
║  ┌─────────────────────────────────────────────────────────────────────┐    ║
║  │                    第三层：消息总线隔离 (Message Broker)              │    ║
║  │                    EMQX (MQTT Broker) / Kafka                       │    ║
║  │    ┌─────────────────────────────────────────────────────┐          │    ║
║  │    │  Topic: scada/devices/{device_id}/telemetry         │          │    ║
║  │    │  Topic: scada/alarms/{level}                        │          │    ║
║  │    │  Topic: scada/oee/{device_id}                       │          │    ║
║  │    └─────────────────────────────────────────────────────┘          │    ║
║  └─────────────────────────────────────────────────────────────────────┘    ║
║                                  ↑ 发布                                      ║
║  ┌─────────────────────────────────────────────────────────────────────┐    ║
║  │                    第二层：统一物模型 (Digital Twin)                  │    ║
║  │                    JSON Schema 标准化                                │    ║
║  │    ┌─────────────────────────────────────────────────────┐          │    ║
║  │    │  {                                                  │          │    ║
║  │    │    "DeviceID": "CNC_001",                           │          │    ║
║  │    │    "Timestamp": 1715129400,                         │          │    ║
║  │    │    "Metrics": {                                     │          │    ║
║  │    │      "temperature": {"value": 45.5, "unit": "°C"}, │          │    ║
║  │    │      "pressure": {"value": 0.5, "unit": "MPa"}     │          │    ║
║  │    │    }                                                │          │    ║
║  │    │  }                                                  │          │    ║
║  │    └─────────────────────────────────────────────────────┘          │    ║
║  └─────────────────────────────────────────────────────────────────────┘    ║
║                                  ↑ 转换                                      ║
║  ┌─────────────────────────────────────────────────────────────────────┐    ║
║  │                    第一层：边缘网关层 (Edge Gateway)                  │    ║
║  │                    协议转换服务                                       │    ║
║  │    ┌─────────────┬─────────────┬─────────────┬─────────────┐        │    ║
║  │    │ Modbus RTU  │  S7/MC/FINS │   OPC UA    │    MQTT     │        │    ║
║  │    │  Gateway    │   Gateway   │   Gateway   │   Gateway   │        │    ║
║  │    └─────────────┴─────────────┴─────────────┴─────────────┘        │    ║
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
    
    parser = argparse.ArgumentParser(description='网关集成示例')
    parser.add_argument('--demo', action='store_true', help='运行数据流演示')
    parser.add_argument('--arch', action='store_true', help='显示架构图')
    
    args = parser.parse_args()
    
    if args.arch:
        show_architecture()
    elif args.demo:
        demonstrate_data_flow()
    else:
        show_architecture()
        print("\n使用 --demo 参数运行数据流演示")
