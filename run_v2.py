"""
工业数据采集与监控系统启动脚本 (v2 - 完全分离的模拟/真实模式)
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

# 确保日志目录存在
(project_root / 'logs').mkdir(parents=True, exist_ok=True)
(project_root / 'data').mkdir(parents=True, exist_ok=True)
(project_root / 'exports').mkdir(parents=True, exist_ok=True)

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
        from 报警层.alarm_manager import AlarmManager
        from 采集层.data_collector import DataCollector
        from 展示层.routes import create_app
        from factory import SystemFactory

        # 初始化数据库
        logger.info("初始化数据库...")
        database = Database(str(project_root / 'data' / 'scada.db'))

        # 判断运行模式
        if '--real' in sys.argv:
            mode = 'real'
            logger.info("运行模式: 真实设备模式")
        else:
            mode = 'simulated'
            logger.info("运行模式: 模拟模式")

        # 创建系统工厂
        factory = SystemFactory(mode=mode)

        # 使用工厂创建独立的组件
        logger.info("创建系统组件...")
        
        # 1. 创建设备管理器（完全独立）
        device_manager = factory.create_device_manager('配置/devices.yaml')
        
        # 2. 创建报警输出（完全独立）
        import yaml
        alarms_config_path = Path('配置/alarms.yaml')
        alarm_output_cfg = {'enabled': True}
        
        if alarms_config_path.exists():
            with open(alarms_config_path, 'r', encoding='utf-8') as f:
                alarms_yaml = yaml.safe_load(f) or {}
            ao_cfg = alarms_yaml.get('alarm_output', {})
            if ao_cfg:
                tower = ao_cfg.get('signal_tower', {})
                alarm_output_cfg.update({
                    'modbus': {
                        'host': tower.get('host', '192.168.1.70'),
                        'port': tower.get('port', 502),
                        'slave_id': tower.get('slave_id', 1),
                    },
                    'do_mapping': tower.get('do_mapping', {
                        'red_light': 0, 'yellow_light': 1,
                        'green_light': 2, 'buzzer': 5
                    }),
                })
        
        alarm_output = factory.create_alarm_output(alarm_output_cfg)
        
        # 3. 创建广播系统（完全独立）
        broadcast_config = {
            'enabled': True,
            'areas': ['车间A', '车间B', '仓库', '办公楼'],
            'preset_templates': {
                'alarm_critical': '注意！{area}发生严重报警：{message}，请立即处置！',
                'alarm_warning': '提醒：{area}出现告警：{message}，请关注。',
                'evacuation': '请注意，{area}发生紧急状况，请沿疏散通道撤离！',
                'all_clear': '广播通知，{area}警报解除，恢复正常。',
            }
        }
        
        # 真实模式需要MQTT配置
        if mode == 'real':
            broadcast_config.update({
                'mqtt_broker': 'localhost',
                'mqtt_port': 1883,
                'topic_prefix': 'pa/',
            })
        
        broadcast_system = factory.create_broadcast_system(broadcast_config)
        
        # 4. 创建报警管理器
        alarm_manager = AlarmManager(database, '配置/alarms.yaml',
                                     alarm_output=alarm_output,
                                     broadcast_system=broadcast_system)

        # 初始化数据采集器（注入智能层模块）
        logger.info("初始化数据采集器...")

        # 初始化工业4.0智能层
        logger.info("初始化智能层模块...")
        from 智能层.predictive_maintenance import PredictiveMaintenance
        from 智能层.oee_calculator import OEECalculator
        from 智能层.spc_analyzer import SPCAnalyzer
        from 智能层.energy_manager import EnergyManager
        from 智能层.edge_decision import EdgeDecisionEngine
        from 智能层.device_control import DeviceControlSafety

        predictive_maintenance = PredictiveMaintenance(database)
        oee_calculator = OEECalculator(database)
        spc_analyzer = SPCAnalyzer(database)
        energy_manager = EnergyManager(database)
        edge_decision = EdgeDecisionEngine(database)
        device_control = DeviceControlSafety(database, device_manager, alarm_manager)

        # 初始化TDengine适配器（可选，需要TDengine服务）
        realtime_bridge = None
        tsdb_adapter = None
        try:
            from 智能层.tsdb_adapter import TSDBAdapter, RealtimeDataBridge
            from timeseries.tdengine_client import TDengineClient

            tdengine = TDengineClient("localhost", 6041)
            if tdengine.connect():
                tdengine.init_tables()

                # 创建实时数据桥接器
                realtime_bridge = RealtimeDataBridge(tdengine)

                # 创建智能层适配器
                tsdb_adapter = TSDBAdapter(
                    tdengine,
                    oee_calculator=oee_calculator,
                    predictive_maintenance=predictive_maintenance,
                    spc_analyzer=spc_analyzer,
                    energy_manager=energy_manager
                )

                logger.info("TDengine适配器初始化成功")
            else:
                logger.warning("TDengine连接失败，跳过TDengine集成")
        except Exception as e:
            logger.warning(f"TDengine初始化失败（可选组件）: {e}")

        # 注册边缘决策的动作回调
        def edge_set_alarm(message, level='warning'):
            """边缘决策报警回调"""
            from datetime import datetime
            logger.warning(f"[边缘决策报警] [{level.upper()}] {message}")
            # 记录到数据库
            try:
                database.insert_alarm(
                    alarm_id=f'edge_{int(datetime.now().timestamp())}',
                    device_id='edge_decision',
                    register_name='auto',
                    alarm_level=level,
                    alarm_message=message,
                    threshold=0,
                    actual_value=0,
                    timestamp=datetime.now()
                )
            except Exception as e:
                logger.error(f"边缘决策报警记录失败: {e}")

        edge_decision.register_action('set_alarm', edge_set_alarm)

        data_collector = DataCollector(
            device_manager, database, alarm_manager,
            predictive_maintenance=predictive_maintenance,
            oee_calculator=oee_calculator,
            spc_analyzer=spc_analyzer,
            energy_manager=energy_manager,
            edge_decision=edge_decision,
            device_control=device_control,
            realtime_bridge=realtime_bridge,
        )

        # 创建Flask应用
        logger.info("创建Web应用...")
        app = create_app(database, device_manager, alarm_manager, data_collector,
                         predictive_maintenance=predictive_maintenance,
                         oee_calculator=oee_calculator,
                         spc_analyzer=spc_analyzer,
                         energy_manager=energy_manager,
                         edge_decision=edge_decision,
                         device_control=device_control)

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

        # 启动智能层模块
        logger.info("启动智能层模块...")
        predictive_maintenance.start()
        oee_calculator.start()
        energy_manager.start()
        edge_decision.start()

        # 启动TDengine适配器（如果可用）
        if realtime_bridge:
            realtime_bridge.start()
            logger.info("TDengine实时数据桥接器已启动")
        if tsdb_adapter:
            # 注册设备到适配器
            devices = device_manager.get_all_devices()
            for device_id, device_config in devices.items():
                registers = device_config.get('registers', [])
                if registers:
                    tsdb_adapter.register_device(device_id, registers)
            tsdb_adapter.start()
            logger.info("TDengine智能层适配器已启动")

        logger.info("智能层已启动: 预测性维护 | OEE | SPC | 能源管理 | 边缘决策")

        # 启动Web服务
        mode_str = "真实设备模式" if mode == 'real' else "模拟模式：使用仿真数据"
        logger.info("启动Web服务...")
        logger.info("=" * 50)
        logger.info("工业数据采集与监控系统已启动")
        logger.info("访问地址: http://localhost:5000")
        logger.info(f"（{mode_str}）")
        logger.info("=" * 50)

        # 注入WebSocket推送函数到报警管理器
        from 展示层.websocket import socketio, emit_alarm, emit_broadcast
        alarm_manager.set_websocket_emit(emit_alarm)

        # 注入广播WebSocket推送（广播发生时实时通知前端）
        broadcast_system.add_callback(lambda msg: emit_broadcast(msg))

        # 使用routes.py中已创建的SocketIO实例（init_socketio在create_app中已调用）
        from config import WebConfig
        host = WebConfig.HOST
        port = getattr(WebConfig, 'REAL_PORT', WebConfig.PORT) if '--real' in sys.argv else WebConfig.PORT
        if socketio is None:
            logger.warning("SocketIO未初始化，使用普通Flask服务器")
            app.run(host=host, port=port, debug=False)
        else:
            socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)

    except KeyboardInterrupt:
        logger.info("系统正在关闭...")
        if 'data_collector' in locals():
            data_collector.stop()
        if 'tsdb_adapter' in locals() and tsdb_adapter:
            tsdb_adapter.stop()
        if 'realtime_bridge' in locals() and realtime_bridge:
            realtime_bridge.stop()
        if 'alarm_output' in locals():
            alarm_output.disconnect()
        if 'broadcast_system' in locals():
            broadcast_system.disconnect()
        if 'device_manager' in locals():
            device_manager.disconnect_all()
        logger.info("系统已关闭")

    except Exception as e:
        logger.error(f"系统启动失败: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
