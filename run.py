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
        
        # 初始化报警管理器（含声光报警器+广播系统）
        logger.info("加载报警配置...")
        from 报警层.alarm_output import AlarmOutput
        from 报警层.broadcast_system import BroadcastSystem

        # 从alarms.yaml读取灯控设备配置
        import yaml
        alarms_config_path = Path('配置/alarms.yaml')
        alarm_output_cfg = {'enabled': True, 'simulation': simulation_mode}
        if alarms_config_path.exists():
            with open(alarms_config_path, 'r', encoding='utf-8') as f:
                alarms_yaml = yaml.safe_load(f) or {}
            ao_cfg = alarms_yaml.get('alarm_output', {})
            if ao_cfg:
                # 使用Patlite信号灯塔作为主输出
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
                logger.info(f"灯控设备: {tower.get('device_id')} @ {tower.get('host')}")
        alarm_output = AlarmOutput(alarm_output_cfg)
        broadcast_system = BroadcastSystem({
            'enabled': True,
            'simulation': simulation_mode,
            'areas': ['车间A', '车间B', '仓库', '办公楼'],
            'default_area': 'all',
            'preset_templates': {
                'alarm_critical': '注意！{area}发生严重报警：{message}，请立即处置！',
                'alarm_warning': '提醒：{area}出现告警：{message}，请关注。',
                'evacuation': '请注意，{area}发生紧急状况，请沿疏散通道撤离！',
                'all_clear': '广播通知，{area}警报解除，恢复正常。',
            }
        })
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
        
        data_collector = DataCollector(
            device_manager, database, alarm_manager,
            predictive_maintenance=predictive_maintenance,
            oee_calculator=oee_calculator,
            spc_analyzer=spc_analyzer,
            energy_manager=energy_manager,
            edge_decision=edge_decision,
            device_control=device_control,
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
        logger.info("智能层已启动: 预测性维护 | OEE | SPC | 能源管理 | 边缘决策")
        
        # 启动Web服务
        mode_str = "真实设备模式" if not simulation_mode else "模拟模式：使用仿真数据"
        logger.info("启动Web服务...")
        logger.info("=" * 50)
        logger.info("工业数据采集与监控系统已启动")
        logger.info("访问地址: http://localhost:5000")
        logger.info(f"（{mode_str}）")
        logger.info("=" * 50)
        
        # 注入WebSocket推送函数到报警管理器
        from 展示层.websocket import socketio as ws_socketio, emit_alarm, emit_broadcast
        alarm_manager.set_websocket_emit(emit_alarm)
        
        # 注入广播WebSocket推送（广播发生时实时通知前端）
        broadcast_system.add_callback(lambda msg: emit_broadcast(msg))
        
        # 使用routes.py中已创建的SocketIO实例（init_socketio在create_app中已调用）
        from 展示层.websocket import socketio
        if socketio is None:
            logger.warning("SocketIO未初始化，使用普通Flask服务器")
            app.run(host='0.0.0.0', port=5000, debug=False)
        else:
            socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)
        
    except KeyboardInterrupt:
        logger.info("系统正在关闭...")
        if 'data_collector' in locals():
            data_collector.stop()
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
