"""
工业数据采集与监控系统 (SCADA) 启动脚本
重构版本 - 符合SCADA标准的模块化架构
"""

import sys
import logging
from pathlib import Path
from datetime import datetime

# 添加项目根目录到Python路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# 确保必要目录存在
for dir_name in ['logs', 'data', 'exports']:
    (project_root / dir_name).mkdir(parents=True, exist_ok=True)

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


class SCADASystem:
    """SCADA系统主类 - 管理系统生命周期"""
    
    def __init__(self, simulation_mode=True):
        self.simulation_mode = simulation_mode
        self.start_time = datetime.now()
        self.components = {}
        self._initialized = False
        
    def initialize(self):
        """初始化所有系统组件"""
        try:
            logger.info("=" * 60)
            logger.info("工业数据采集与监控系统 (SCADA) 启动中...")
            logger.info(f"运行模式: {'模拟模式' if self.simulation_mode else '真实设备模式'}")
            logger.info("=" * 60)
            
            # 第一步：初始化核心基础设施
            self._init_core()
            
            # 第二步：初始化数据存储层
            self._init_storage()
            
            # 第三步：初始化数据采集层
            self._init_collection()
            
            # 第四步：初始化报警管理层
            self._init_alarm()
            
            # 第五步：初始化智能分析层
            self._init_intelligence()
            
            # 第六步：初始化Web展示层
            self._init_presentation()
            
            # 第七步：连接设备并启动采集
            self._start_collection()
            
            # 第八步：为模拟模式预填充数据
            if self.simulation_mode:
                self._prefill_simulation_data()
            
            self._initialized = True
            logger.info("系统初始化完成")
            return True
            
        except Exception as e:
            logger.error(f"系统初始化失败: {e}", exc_info=True)
            return False
    
    def _init_core(self):
        """初始化核心基础设施层"""
        logger.info("[1/8] 初始化核心基础设施...")
        
        from core.event_bus import EventBus
        from core.health_checker import HealthChecker
        
        self.components['event_bus'] = EventBus()
        self.components['health_checker'] = HealthChecker()
        
        logger.info("  ✓ 事件总线")
        logger.info("  ✓ 健康检查器")
    
    def _init_storage(self):
        """初始化数据存储层"""
        logger.info("[2/8] 初始化数据存储层...")
        
        from 存储层.database import Database
        
        db_path = 'data/scada_simulated.db' if self.simulation_mode else 'data/scada_real.db'
        self.components['database'] = Database(str(project_root / db_path))
        
        logger.info(f"  ✓ 数据库: {db_path}")
    
    def _init_collection(self):
        """初始化数据采集层"""
        logger.info("[3/8] 初始化数据采集层...")
        
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        from 采集层.real_device_manager import RealDeviceManager
        from 采集层.data_collector import DataCollector
        
        # 根据模式选择设备管理器
        config_path = '配置/devices_simulated.yaml' if self.simulation_mode else '配置/devices_real.yaml'
        
        if self.simulation_mode:
            self.components['device_manager'] = SimulatedDeviceManager(config_path)
        else:
            self.components['device_manager'] = RealDeviceManager(config_path)
        
        logger.info(f"  ✓ 设备管理器: {config_path}")
    
    def _init_alarm(self):
        """初始化报警管理层"""
        logger.info("[4/8] 初始化报警管理层...")
        
        from 报警层.alarm_manager import AlarmManager
        from 报警层.alarm_output import AlarmOutput
        from 报警层.broadcast_system import BroadcastSystem
        import yaml
        
        # 加载报警配置
        alarms_config_path = Path('配置/alarms.yaml')
        alarm_output_cfg = {'enabled': True, 'simulation': self.simulation_mode}
        
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
        
        self.components['alarm_output'] = AlarmOutput(alarm_output_cfg)
        self.components['broadcast_system'] = BroadcastSystem({
            'enabled': True,
            'simulation': self.simulation_mode,
            'areas': ['车间A', '车间B', '仓库', '办公楼'],
            'default_area': 'all',
            'preset_templates': {
                'alarm_critical': '注意！{area}发生严重报警：{message}，请立即处置！',
                'alarm_warning': '提醒：{area}出现告警：{message}，请关注。',
                'evacuation': '请注意，{area}发生紧急状况，请沿疏散通道撤离！',
                'all_clear': '广播通知，{area}警报解除，恢复正常。',
            }
        })
        
        self.components['alarm_manager'] = AlarmManager(
            self.components['database'],
            '配置/alarms.yaml',
            alarm_output=self.components['alarm_output'],
            broadcast_system=self.components['broadcast_system']
        )
        
        logger.info("  ✓ 报警管理器")
        logger.info("  ✓ 报警输出")
        logger.info("  ✓ 广播系统")
    
    def _init_intelligence(self):
        """初始化智能分析层"""
        logger.info("[5/8] 初始化智能分析层...")
        
        from 智能层.predictive_maintenance import PredictiveMaintenance
        from 智能层.oee_calculator import OEECalculator
        from 智能层.spc_analyzer import SPCAnalyzer
        from 智能层.energy_manager import EnergyManager
        from 智能层.edge_decision import EdgeDecisionEngine
        from 智能层.device_control import DeviceControlSafety
        
        database = self.components['database']
        device_manager = self.components['device_manager']
        alarm_manager = self.components['alarm_manager']
        
        self.components['predictive_maintenance'] = PredictiveMaintenance(database)
        self.components['oee_calculator'] = OEECalculator(database)
        self.components['spc_analyzer'] = SPCAnalyzer(database)
        self.components['energy_manager'] = EnergyManager(database)
        self.components['edge_decision'] = EdgeDecisionEngine(database)
        self.components['device_control'] = DeviceControlSafety(database, device_manager, alarm_manager)
        
        # 注册边缘决策回调
        def edge_set_alarm(message, level='warning'):
            from datetime import datetime
            logger.warning(f"[边缘决策报警] [{level.upper()}] {message}")
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
        
        self.components['edge_decision'].register_action('set_alarm', edge_set_alarm)
        
        logger.info("  ✓ 预测性维护")
        logger.info("  ✓ OEE计算器")
        logger.info("  ✓ SPC分析器")
        logger.info("  ✓ 能源管理")
        logger.info("  ✓ 边缘决策引擎")
        logger.info("  ✓ 设备控制安全")
    
    def _init_presentation(self):
        """初始化Web展示层"""
        logger.info("[6/8] 初始化Web展示层...")
        
        from 展示层.routes import create_app
        from 展示层.websocket import socketio, emit_alarm, emit_broadcast
        
        # 创建Flask应用
        self.components['app'] = create_app(
            self.components['database'],
            self.components['device_manager'],
            self.components['alarm_manager'],
            None,  # data_collector稍后设置
            predictive_maintenance=self.components['predictive_maintenance'],
            oee_calculator=self.components['oee_calculator'],
            spc_analyzer=self.components['spc_analyzer'],
            energy_manager=self.components['energy_manager'],
            edge_decision=self.components['edge_decision'],
            device_control=self.components['device_control']
        )
        
        # 注入WebSocket推送
        self.components['alarm_manager'].set_websocket_emit(emit_alarm)
        self.components['broadcast_system'].add_callback(lambda msg: emit_broadcast(msg))
        
        logger.info("  ✓ Flask应用")
        logger.info("  ✓ WebSocket")
    
    def _start_collection(self):
        """启动数据采集"""
        logger.info("[7/8] 启动数据采集...")
        
        from 采集层.data_collector import DataCollector
        
        # 创建数据采集器
        self.components['data_collector'] = DataCollector(
            self.components['device_manager'],
            self.components['database'],
            self.components['alarm_manager'],
            predictive_maintenance=self.components['predictive_maintenance'],
            oee_calculator=self.components['oee_calculator'],
            spc_analyzer=self.components['spc_analyzer'],
            energy_manager=self.components['energy_manager'],
            edge_decision=self.components['edge_decision'],
            device_control=self.components['device_control']
        )
        
        # 连接设备
        logger.info("  连接设备...")
        connection_results = self.components['device_manager'].connect_all()
        for device_id, success in connection_results.items():
            status = "✓" if success else "✗"
            logger.info(f"    {status} {device_id}")
        
        # 启动采集
        self.components['data_collector'].start()
        
        # 启动智能层模块
        self.components['predictive_maintenance'].start()
        self.components['oee_calculator'].start()
        self.components['energy_manager'].start()
        self.components['edge_decision'].start()
        
        logger.info("  ✓ 数据采集器已启动")
    
    def _prefill_simulation_data(self):
        """为模拟模式预填充初始数据"""
        logger.info("[8/8] 预填充模拟数据...")
        
        import random
        import math
        from datetime import datetime, timedelta
        
        device_manager = self.components['device_manager']
        oee_calculator = self.components['oee_calculator']
        energy_manager = self.components['energy_manager']
        predictive_maintenance = self.components['predictive_maintenance']
        spc_analyzer = self.components['spc_analyzer']
        
        # 1. OEE数据预填充
        logger.info("  预填充OEE数据...")
        theoretical_rates = {
            'siemens_1500_01': 120,
            'hollysys_lk_01': 80,
            'mitsubishi_fx5u_01': 200,
            'delta_dvp_01': 150,
            'inovance_h5u_01': 100,
        }
        
        for device_id in device_manager.get_all_devices():
            oee_calculator.start_shift(device_id, planned_hours=8.0)
            if device_id in theoretical_rates:
                oee_calculator.set_theoretical_rate(device_id, theoretical_rates[device_id])
            oee_calculator.update_device_state(device_id, 'running')
            oee_calculator.record_production(device_id, count=150, good_count=145)
        
        # 2. 能源数据预填充
        logger.info("  预填充能源数据...")
        energy_baselines = {
            'abb_m4m_01': 500,
            'siemens_1500_01': 200,
            'hollysys_lk_01': 150,
            'mitsubishi_fx5u_01': 100,
            'delta_dvp_01': 80,
            'inovance_h5u_01': 120,
        }
        
        for device_id, baseline in energy_baselines.items():
            energy_manager.set_baseline(device_id, baseline)
            current_power = (baseline / 24) * random.uniform(0.8, 1.2)
            energy_manager.feed_power_data(device_id, current_power)
            energy_manager.energy_accumulated[device_id]['energy_kwh'] = baseline * 0.5
            energy_manager.energy_accumulated[device_id]['peak_kwh'] = baseline * 0.2
            energy_manager.energy_accumulated[device_id]['flat_kwh'] = baseline * 0.2
            energy_manager.energy_accumulated[device_id]['valley_kwh'] = baseline * 0.1
        
        # 3. 预测性维护和SPC数据预填充
        logger.info("  预填充预测性维护和SPC数据...")
        for device_id, device_config in device_manager.get_all_devices().items():
            registers = device_config.get('registers', device_config.get('nodes', []))
            for reg in registers:
                reg_name = reg.get('name', '')
                if not reg_name:
                    continue
                
                base_value = random.uniform(20, 80)
                for i in range(60):
                    ts = datetime.now() - timedelta(minutes=60-i)
                    value = base_value + random.gauss(0, 5) + 10 * math.sin(i / 10)
                    predictive_maintenance.feed_data(device_id, reg_name, value, ts)
                    spc_analyzer.feed_data(device_id, reg_name, value)
        
        # 触发分析
        predictive_maintenance._run_analysis()
        
        logger.info("  ✓ 模拟数据预填充完成")
    
    def run(self):
        """运行系统"""
        if not self._initialized:
            logger.error("系统未初始化，请先调用 initialize()")
            return
        
        from config import WebConfig
        from 展示层.websocket import socketio
        
        app = self.components['app']
        host = WebConfig.HOST
        port = WebConfig.PORT
        
        logger.info("=" * 60)
        logger.info("工业数据采集与监控系统已启动")
        logger.info(f"访问地址: http://{host}:{port}")
        logger.info(f"运行模式: {'模拟模式' if self.simulation_mode else '真实设备模式'}")
        logger.info("=" * 60)
        
        if socketio is None:
            logger.warning("SocketIO未初始化，使用普通Flask服务器")
            app.run(host=host, port=port, debug=False)
        else:
            socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)
    
    def shutdown(self):
        """关闭系统"""
        logger.info("系统正在关闭...")
        
        # 停止数据采集
        if 'data_collector' in self.components:
            self.components['data_collector'].stop()
        
        # 断开报警输出
        if 'alarm_output' in self.components:
            self.components['alarm_output'].disconnect()
        
        # 断开广播系统
        if 'broadcast_system' in self.components:
            self.components['broadcast_system'].disconnect()
        
        # 断开设备连接
        if 'device_manager' in self.components:
            self.components['device_manager'].disconnect_all()
        
        logger.info("系统已关闭")


def main():
    """主函数"""
    # 判断运行模式
    simulation_mode = '--real' not in sys.argv
    
    # 创建系统实例
    system = SCADASystem(simulation_mode=simulation_mode)
    
    try:
        # 初始化系统
        if not system.initialize():
            logger.error("系统初始化失败")
            sys.exit(1)
        
        # 运行系统
        system.run()
        
    except KeyboardInterrupt:
        logger.info("收到中断信号")
    except Exception as e:
        logger.error(f"系统运行异常: {e}", exc_info=True)
    finally:
        system.shutdown()


if __name__ == '__main__':
    main()
