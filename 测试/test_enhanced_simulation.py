"""
增强版模拟测试脚本
==================
测试设备行为模拟器和增强版模拟客户端
"""

import sys
import time
import logging
from pathlib import Path

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from 采集层.device_behavior_simulator import (
    DeviceBehaviorSimulator, 
    DeviceState, 
    FaultType,
    MultiDeviceSimulator
)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def test_single_device():
    """测试单个设备模拟器"""
    logger.info("=== 测试单个设备模拟器 ===")
    
    # 创建设备配置
    device_config = {
        'id': 'test_boiler_01',
        'name': '测试锅炉',
        'description': '锅炉出口温度控制',
        'protocol': 'modbus_tcp',
        'registers': [
            {'name': 'temperature', 'address': 0, 'data_type': 'float32', 'unit': '°C'},
            {'name': 'pressure', 'address': 2, 'data_type': 'float32', 'unit': 'MPa'},
            {'name': 'flow', 'address': 4, 'data_type': 'float32', 'unit': 't/h'},
            {'name': 'level', 'address': 6, 'data_type': 'float32', 'unit': 'mm'}
        ]
    }
    
    # 创建模拟器
    simulator = DeviceBehaviorSimulator('test_boiler_01', device_config)
    
    # 启动模拟器
    simulator.start()
    
    # 运行10秒，每秒更新一次
    logger.info("运行模拟器10秒...")
    for i in range(10):
        data = simulator.update(1.0)
        logger.info(f"时间 {i+1}s: 温度={data.get('temperature', 0):.2f}°C, "
                    f"压力={data.get('pressure', 0):.4f}MPa, "
                    f"状态={simulator.state.name}")
        time.sleep(1)
    
    # 注入故障
    logger.info("\n注入过热故障...")
    simulator.inject_fault(FaultType.OVERHEATING, severity=0.7)
    
    # 继续运行5秒
    for i in range(5):
        data = simulator.update(1.0)
        logger.info(f"故障后 {i+1}s: 温度={data.get('temperature', 0):.2f}°C, "
                    f"故障={simulator.active_fault.value}, "
                    f"严重度={simulator.fault_severity:.2f}")
        time.sleep(1)
    
    # 停止模拟器
    simulator.stop()
    logger.info("单设备测试完成\n")


def test_multi_device():
    """测试多设备模拟器"""
    logger.info("=== 测试多设备模拟器 ===")
    
    # 创建多设备模拟器
    multi_sim = MultiDeviceSimulator()
    
    # 添加多个设备
    devices = [
        {
            'id': 'boiler_01',
            'name': '锅炉1号',
            'description': '锅炉出口温度控制',
            'protocol': 'modbus_tcp'
        },
        {
            'id': 'pump_01',
            'name': '水泵1号',
            'description': '水处理车间控制',
            'protocol': 'modbus_tcp'
        },
        {
            'id': 'motor_01',
            'name': '电机1号',
            'description': '电机驱动控制',
            'protocol': 'modbus_tcp'
        }
    ]
    
    for device_config in devices:
        multi_sim.add_device(device_config['id'], device_config)
    
    # 启动模拟器
    multi_sim.start()
    
    # 运行5秒
    logger.info("运行多设备模拟器5秒...")
    for i in range(5):
        status_list = multi_sim.get_all_status()
        for status in status_list:
            logger.info(f"设备 {status['device_name']}: "
                        f"状态={status['state']}, "
                        f"健康={status['health_score']:.1f}")
        time.sleep(1)
    
    # 注入故障
    logger.info("\n对锅炉注入故障...")
    multi_sim.inject_fault('boiler_01', FaultType.OVERHEATING, severity=0.8)
    
    # 继续运行3秒
    for i in range(3):
        status_list = multi_sim.get_all_status()
        for status in status_list:
            logger.info(f"设备 {status['device_name']}: "
                        f"状态={status['state']}, "
                        f"故障={status['active_fault']}")
        time.sleep(1)
    
    # 停止模拟器
    multi_sim.stop()
    logger.info("多设备测试完成\n")


def test_enhanced_client():
    """测试增强版模拟客户端"""
    logger.info("=== 测试增强版模拟客户端 ===")
    
    from 采集层.enhanced_simulated_client import EnhancedSimulatedModbusClient
    
    # 创建设备配置
    device_config = {
        'id': 'test_plc_01',
        'name': '测试PLC',
        'description': '西门子S7-1500 PLC',
        'protocol': 'modbus_tcp',
        'host': '192.168.1.53',
        'port': 502,
        'slave_id': 1,
        'registers': [
            {'name': 'boiler_temperature', 'address': 0, 'data_type': 'float32', 'unit': '°C'},
            {'name': 'boiler_pressure', 'address': 2, 'data_type': 'float32', 'unit': 'MPa'},
            {'name': 'steam_flow', 'address': 4, 'data_type': 'float32', 'unit': 't/h'},
            {'name': 'feed_water_level', 'address': 6, 'data_type': 'float32', 'unit': 'mm'},
            {'name': 'boiler_status', 'address': 14, 'data_type': 'uint16', 'unit': ''}
        ]
    }
    
    # 创建客户端
    client = EnhancedSimulatedModbusClient(device_config)
    
    # 连接
    if client.connect():
        logger.info("客户端连接成功")
        
        # 运行5秒
        for i in range(5):
            # 读取寄存器
            result = client.read_holding_registers(0, 2)
            if result:
                value = client.decode_float32(result)
                logger.info(f"锅炉温度: {value:.2f}°C")
            
            # 获取最新数据
            latest = client.get_latest_data()
            logger.info(f"最新数据: 温度={latest.get('boiler_temperature', 0):.2f}, "
                        f"压力={latest.get('boiler_pressure', 0):.4f}")
            
            time.sleep(1)
        
        # 获取统计信息
        stats = client.get_stats()
        logger.info(f"统计信息: {stats}")
        
        # 断开连接
        client.disconnect()
        logger.info("客户端已断开")
    else:
        logger.error("客户端连接失败")
    
    logger.info("增强版客户端测试完成\n")


def main():
    """主测试函数"""
    logger.info("开始增强版模拟测试...")
    
    try:
        # 测试单设备
        test_single_device()
        
        # 测试多设备
        test_multi_device()
        
        # 测试增强版客户端
        test_enhanced_client()
        
        logger.info("所有测试完成！")
        
    except Exception as e:
        logger.error(f"测试异常: {e}", exc_info=True)
        return 1
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
