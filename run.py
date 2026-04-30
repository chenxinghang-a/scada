"""
工业数据采集与监控系统启动脚本
"""

import sys
import logging
from pathlib import Path
from datetime import datetime

# 添加项目根目录到Python路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# 记录系统启动时间
SYSTEM_START_TIME = datetime.now()

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('logs/scada.log', encoding='utf-8')
    ]
)

logger = logging.getLogger(__name__)


def main():
    """主函数"""
    try:
        logger.info("正在启动工业数据采集与监控系统...")
        
        # 导入模块
        from 存储层.database import Database
        from 采集层.device_manager import DeviceManager
        from 报警层.alarm_manager import AlarmManager
        from 采集层.data_collector import DataCollector
        from 展示层.routes import create_app
        
        # 初始化数据库
        logger.info("初始化数据库...")
        database = Database('data/scada.db')
        
        # 判断是否使用模拟模式
        simulation_mode = '--real' not in sys.argv
        
        # 初始化设备管理器
        logger.info("加载设备配置...")
        device_manager = DeviceManager('配置/devices.yaml', simulation_mode=simulation_mode)
        
        # 初始化报警管理器
        logger.info("加载报警配置...")
        alarm_manager = AlarmManager(database, '配置/alarms.yaml')
        
        # 初始化数据采集器
        logger.info("初始化数据采集器...")
        data_collector = DataCollector(device_manager, database, alarm_manager)
        
        # 创建Flask应用
        logger.info("创建Web应用...")
        app = create_app(database, device_manager, alarm_manager, data_collector)
        
        # 将启动时间传递给app
        app.system_start_time = SYSTEM_START_TIME
        
        # 连接所有设备
        logger.info("连接设备...")
        connection_results = device_manager.connect_all()
        for device_id, success in connection_results.items():
            status = "成功" if success else "失败"
            logger.info(f"  设备 {device_id}: {status}")
        
        # 启动数据采集
        logger.info("启动数据采集...")
        data_collector.start()
        
        # 启动Web服务
        mode_str = "真实设备模式" if not simulation_mode else "模拟模式：使用仿真数据"
        logger.info("启动Web服务...")
        logger.info("=" * 50)
        logger.info("工业数据采集与监控系统已启动")
        logger.info("访问地址: http://localhost:5000")
        logger.info(f"（{mode_str}）")
        logger.info("=" * 50)
        
        from flask_socketio import SocketIO
        socketio = SocketIO(app, cors_allowed_origins="*")
        socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
        
    except KeyboardInterrupt:
        logger.info("系统正在关闭...")
        if 'data_collector' in locals():
            data_collector.stop()
        if 'device_manager' in locals():
            device_manager.disconnect_all()
        logger.info("系统已关闭")
        
    except Exception as e:
        logger.error(f"系统启动失败: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
