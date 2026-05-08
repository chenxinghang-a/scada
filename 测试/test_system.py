"""
系统测试脚本
测试各个模块的基本功能
"""

import sys
import logging
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_database():
    """测试数据库模块"""
    logger.info("测试数据库模块...")

    from 存储层.database import Database

    # 创建数据库实例
    db = Database('data/test.db')

    # 插入测试数据
    from datetime import datetime
    db.insert_data(
        device_id='test_device',
        register_name='temperature',
        value=25.5,
        timestamp=datetime.now(),
        unit='°C'
    )

    # 查询数据
    data = db.get_latest_data('test_device', 'temperature')
    assert data is not None, "查询数据失败"
    assert data['value'] == 25.5, "数据值不匹配"

    logger.info("数据库模块测试通过")
    return True


def test_device_manager():
    """测试设备管理器"""
    logger.info("测试设备管理器...")

    from 采集层.device_manager import DeviceManager

    # 创建设备管理器
    dm = DeviceManager('配置/devices.yaml')

    # 获取设备列表
    devices = dm.get_all_devices()
    logger.info(f"加载设备: {len(devices)} 个")

    # 获取设备状态
    for device_id in devices:
        status = dm.get_device_status(device_id)
        logger.info(f"  {device_id}: {status.get('name')}")

    logger.info("设备管理器测试通过")
    return True


def test_alarm_manager():
    """测试报警管理器"""
    logger.info("测试报警管理器...")

    from 存储层.database import Database
    from 报警层.alarm_manager import AlarmManager

    # 创建数据库和报警管理器
    db = Database('data/test.db')
    am = AlarmManager(db, '配置/alarms.yaml')

    # 获取报警规则
    rules = am.rules
    logger.info(f"加载报警规则: {len(rules)} 条")

    # 测试报警检查
    from datetime import datetime
    am.check_alarm(
        device_id='temp_sensor_01',
        register_name='temperature',
        value=55.0,  # 超过阈值50
        timestamp=datetime.now()
    )

    # 获取活动报警
    active_alarms = am.get_active_alarms()
    logger.info(f"活动报警: {len(active_alarms)} 个")

    logger.info("报警管理器测试通过")
    return True


def test_data_export():
    """测试数据导出"""
    logger.info("测试数据导出...")

    from 存储层.data_export import DataExport

    # 创建导出器
    exporter = DataExport('exports')

    # 测试数据
    test_data = [
        {'device_id': 'test', 'value': 25.5, 'timestamp': '2024-01-01 12:00:00'},
        {'device_id': 'test', 'value': 26.0, 'timestamp': '2024-01-01 12:01:00'},
    ]

    # 导出CSV
    filepath = exporter.export_csv(test_data, 'test_export.csv')
    assert filepath is not None, "CSV导出失败"
    logger.info(f"CSV导出成功: {filepath}")

    # 导出JSON
    filepath = exporter.export_json(test_data, 'test_export.json')
    assert filepath is not None, "JSON导出失败"
    logger.info(f"JSON导出成功: {filepath}")

    logger.info("数据导出测试通过")
    return True


def main():
    """运行所有测试"""
    logger.info("=" * 50)
    logger.info("开始系统测试")
    logger.info("=" * 50)

    tests = [
        test_database,
        test_device_manager,
        test_alarm_manager,
        test_data_export,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            if test():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            logger.error(f"测试失败: {e}")
            failed += 1

    logger.info("=" * 50)
    logger.info(f"测试完成: 通过 {passed}, 失败 {failed}")
    logger.info("=" * 50)

    return failed == 0


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
