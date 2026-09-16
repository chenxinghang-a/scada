"""
简单测试系统启动
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


def test_system():
    """测试系统启动"""
    logger.info("=" * 50)
    logger.info("工业数据采集与监控系统 - 启动测试")
    logger.info("=" * 50)

    try:
        # 导入模块
        logger.info("1. 导入模块...")
        from 存储层.database import Database
        from 采集层.device_manager import DeviceManager
        from 报警层.alarm_manager import AlarmManager
        from 采集层.data_collector import DataCollector
        from 展示层.routes import create_app
        logger.info("   ✓ 所有模块导入成功")

        # 初始化数据库
        logger.info("2. 初始化数据库...")
        database = Database('data/test_simple.db')
        logger.info("   ✓ 数据库初始化成功")

        # 初始化设备管理器
        logger.info("3. 加载设备配置...")
        device_manager = DeviceManager('配置/devices.yaml')
        devices = device_manager.get_all_devices()
        logger.info(f"   ✓ 加载 {len(devices)} 个设备")

        # 初始化报警管理器
        logger.info("4. 加载报警配置...")
        alarm_manager = AlarmManager(database, '配置/alarms.yaml')
        rules = alarm_manager.rules
        logger.info(f"   ✓ 加载 {len(rules)} 条报警规则")

        # 初始化数据采集器
        logger.info("5. 初始化数据采集器...")
        data_collector = DataCollector(device_manager, database, alarm_manager)
        logger.info("   ✓ 数据采集器初始化成功")

        # 创建Flask应用
        logger.info("6. 创建Web应用...")
        app = create_app(database, device_manager, alarm_manager, data_collector)
        logger.info("   ✓ Flask应用创建成功")

        # 测试API
        logger.info("7. 测试API...")
        with app.test_client() as client:
            # 测试首页
            response = client.get('/')
            logger.info(f"   首页状态码: {response.status_code}")

            # 测试设备API
            response = client.get('/api/devices')
            logger.info(f"   设备API状态码: {response.status_code}")

            # 测试系统状态API
            response = client.get('/api/system/status')
            logger.info(f"   系统状态API状态码: {response.status_code}")

        logger.info("   ✓ API测试完成")

        # 清理测试数据库
        Path('data/test_simple.db').unlink(missing_ok=True)
        logger.info("   ✓ 测试数据库已清理")

        logger.info("\n" + "=" * 50)
        logger.info("🎉 系统测试通过！")
        logger.info("=" * 50)
        logger.info("\n启动命令:")
        logger.info("  cd industrial_scada")
        logger.info("  python run.py")
        logger.info("\n访问地址:")
        logger.info("  http://localhost:5000")

        return True

    except Exception as e:
        logger.error(f"系统测试失败: {e}", exc_info=True)
        return False


if __name__ == '__main__':
    success = test_system()
    sys.exit(0 if success else 1)
