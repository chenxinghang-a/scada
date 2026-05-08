"""
测试系统启动
"""
import sys
import logging
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


def test_imports():
    """测试所有模块导入"""
    logger.info("测试模块导入...")

    try:
        from 存储层.database import Database
        logger.info("✓ Database 导入成功")
    except Exception as e:
        logger.error(f"✗ Database 导入失败: {e}")
        return False

    try:
        from 采集层.device_manager import DeviceManager
        logger.info("✓ DeviceManager 导入成功")
    except Exception as e:
        logger.error(f"✗ DeviceManager 导入失败: {e}")
        return False

    try:
        from 报警层.alarm_manager import AlarmManager
        logger.info("✓ AlarmManager 导入成功")
    except Exception as e:
        logger.error(f"✗ AlarmManager 导入失败: {e}")
        return False

    try:
        from 采集层.data_collector import DataCollector
        logger.info("✓ DataCollector 导入成功")
    except Exception as e:
        logger.error(f"✗ DataCollector 导入失败: {e}")
        return False

    try:
        from 展示层.routes import create_app
        logger.info("✓ Routes 导入成功")
    except Exception as e:
        logger.error(f"✗ Routes 导入失败: {e}")
        return False

    return True


def test_database():
    """测试数据库初始化"""
    logger.info("测试数据库初始化...")

    try:
        from 存储层.database import Database
        db = Database('data/test_scada.db')
        logger.info("✓ 数据库初始化成功")

        # 测试插入数据
        from datetime import datetime
        db.insert_data('test_device', 'temperature', 25.5, datetime.now(), '°C')
        logger.info("✓ 数据插入成功")

        # 测试查询数据
        data = db.get_realtime_data('test_device')
        logger.info(f"✓ 数据查询成功，获取 {len(data)} 条记录")

        # 清理测试数据库
        Path('data/test_scada.db').unlink(missing_ok=True)
        logger.info("✓ 测试数据库已清理")

        return True
    except Exception as e:
        logger.error(f"✗ 数据库测试失败: {e}")
        return False


def test_device_manager():
    """测试设备管理器"""
    logger.info("测试设备管理器...")

    try:
        from 采集层.device_manager import DeviceManager
        dm = DeviceManager('配置/devices.yaml')
        logger.info("✓ 设备管理器初始化成功")

        # 获取设备列表
        devices = dm.get_all_devices()
        logger.info(f"✓ 获取设备列表成功，共 {len(devices)} 个设备")

        for device in devices:
            logger.info(f"  - {device['id']}: {device['name']}")

        return True
    except Exception as e:
        logger.error(f"✗ 设备管理器测试失败: {e}")
        return False


def test_alarm_manager():
    """测试报警管理器"""
    logger.info("测试报警管理器...")

    try:
        from 存储层.database import Database
        from 报警层.alarm_manager import AlarmManager

        db = Database('data/test_alarm.db')
        am = AlarmManager(db, '配置/alarms.yaml')
        logger.info("✓ 报警管理器初始化成功")

        # 获取报警规则
        rules = am.get_alarm_rules()
        logger.info(f"✓ 获取报警规则成功，共 {len(rules)} 条规则")

        # 清理测试数据库
        Path('data/test_alarm.db').unlink(missing_ok=True)
        logger.info("✓ 测试数据库已清理")

        return True
    except Exception as e:
        logger.error(f"✗ 报警管理器测试失败: {e}")
        return False


def test_flask_app():
    """测试Flask应用创建"""
    logger.info("测试Flask应用创建...")

    try:
        from 存储层.database import Database
        from 采集层.device_manager import DeviceManager
        from 报警层.alarm_manager import AlarmManager
        from 采集层.data_collector import DataCollector
        from 展示层.routes import create_app

        # 初始化组件
        db = Database('data/test_flask.db')
        dm = DeviceManager('配置/devices.yaml')
        am = AlarmManager(db, '配置/alarms.yaml')
        dc = DataCollector(dm, db, am)

        # 创建Flask应用
        app = create_app(db, dm, am, dc)
        logger.info("✓ Flask应用创建成功")

        # 清理测试数据库
        Path('data/test_flask.db').unlink(missing_ok=True)
        logger.info("✓ 测试数据库已清理")

        return True
    except Exception as e:
        logger.error(f"✗ Flask应用测试失败: {e}")
        return False


def main():
    """主测试函数"""
    logger.info("=" * 50)
    logger.info("工业数据采集与监控系统 - 启动测试")
    logger.info("=" * 50)

    tests = [
        ("模块导入", test_imports),
        ("数据库初始化", test_database),
        ("设备管理器", test_device_manager),
        ("报警管理器", test_alarm_manager),
        ("Flask应用", test_flask_app),
    ]

    results = []

    for test_name, test_func in tests:
        logger.info(f"\n--- {test_name} ---")
        try:
            result = test_func()
            results.append((test_name, result))
        except Exception as e:
            logger.error(f"测试 {test_name} 发生异常: {e}")
            results.append((test_name, False))

    # 输出测试结果
    logger.info("\n" + "=" * 50)
    logger.info("测试结果汇总:")
    logger.info("=" * 50)

    passed = 0
    failed = 0

    for test_name, result in results:
        status = "✓ 通过" if result else "✗ 失败"
        logger.info(f"{test_name}: {status}")
        if result:
            passed += 1
        else:
            failed += 1

    logger.info(f"\n总计: {passed + failed} 项测试")
    logger.info(f"通过: {passed} 项")
    logger.info(f"失败: {failed} 项")

    if failed == 0:
        logger.info("\n🎉 所有测试通过！系统可以正常启动。")
        logger.info("\n启动命令: python run.py")
        logger.info("访问地址: http://localhost:5000")
    else:
        logger.error(f"\n❌ 有 {failed} 项测试失败，请检查错误信息。")

    return failed == 0


if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
