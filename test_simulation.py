"""
测试模拟数据初始化
"""

import sys
import logging
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def test_simulation_initialization():
    """测试模拟数据初始化"""
    try:
        # 导入模块
        from 存储层.database import Database
        from 采集层.simulated_device_manager import SimulatedDeviceManager
        from 智能层.predictive_maintenance import PredictiveMaintenance
        from 智能层.oee_calculator import OEECalculator
        from 智能层.spc_analyzer import SPCAnalyzer
        from 智能层.energy_manager import EnergyManager
        
        # 初始化组件
        logger.info("初始化测试环境...")
        database = Database('data/test_simulation.db')
        device_manager = SimulatedDeviceManager('配置/devices_simulated.yaml')
        
        predictive_maintenance = PredictiveMaintenance(database)
        oee_calculator = OEECalculator(database)
        spc_analyzer = SPCAnalyzer(database)
        energy_manager = EnergyManager(database)
        
        # 连接设备
        logger.info("连接设备...")
        connection_results = device_manager.connect_all()
        for device_id, success in connection_results.items():
            logger.info(f"  设备 {device_id}: {'成功' if success else '失败'}")
        
        # 初始化模拟数据
        logger.info("初始化模拟数据...")
        from core.simulation_initializer import initialize_simulation_data
        initialize_simulation_data(
            device_manager, oee_calculator, predictive_maintenance,
            spc_analyzer, energy_manager
        )
        
        # 验证数据
        logger.info("=" * 50)
        logger.info("验证数据...")
        logger.info("=" * 50)
        
        # 1. 验证OEE数据
        logger.info("[1/4] 验证OEE数据...")
        all_oee = oee_calculator.get_all_oee()
        if all_oee:
            for device_id, oee_data in all_oee.items():
                logger.info(f"  {device_id}: OEE={oee_data.get('oee_percent', 0):.1f}%")
        else:
            logger.warning("  OEE数据为空!")
        
        # 2. 验证能源数据
        logger.info("[2/4] 验证能源数据...")
        energy_summary = energy_manager.get_energy_summary()
        logger.info(f"  总电量: {energy_summary.get('total_energy_kwh', 0):.1f} kWh")
        logger.info(f"  总功率: {energy_manager.get_total_power():.1f} kW")
        
        # 3. 验证预测性维护数据
        logger.info("[3/4] 验证预测性维护数据...")
        health_scores = predictive_maintenance.get_health_scores()
        if health_scores:
            logger.info(f"  健康评分数量: {len(health_scores)}")
            # 显示前3个
            for i, (key, score) in enumerate(list(health_scores.items())[:3]):
                logger.info(f"    {key}: 健康评分={score.get('health_score', 0):.1f}")
        else:
            logger.warning("  健康评分数据为空!")
        
        # 4. 验证SPC数据
        logger.info("[4/4] 验证SPC数据...")
        violations = spc_analyzer.get_violations()
        logger.info(f"  SPC违规数量: {len(violations)}")
        
        logger.info("=" * 50)
        logger.info("测试完成!")
        logger.info("=" * 50)
        
        # 清理
        device_manager.disconnect_all()
        
    except Exception as e:
        logger.error(f"测试失败: {e}", exc_info=True)
        return False
    
    return True


if __name__ == '__main__':
    success = test_simulation_initialization()
    sys.exit(0 if success else 1)
