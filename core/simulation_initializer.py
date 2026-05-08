"""
模拟模式初始化器
为工业4.0模块预填充初始数据，让页面立即显示有效数据

重写说明：
- 只使用模块的公共API，不访问私有属性
- 添加完整的错误处理和日志记录
- 每个模块初始化独立，一个失败不影响其他模块
- 添加数据验证和回退机制
"""

import math
import random
import logging
import traceback
from datetime import datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)


class SimulationInitializer:
    """
    模拟模式初始化器
    
    为所有工业4.0模块预填充合理的模拟数据，
    使得页面在启动后立即显示有效数据，而不是空白或"--"。
    """
    
    def __init__(self, device_manager, oee_calculator, predictive_maintenance, 
                 spc_analyzer, energy_manager):
        """
        初始化模拟数据生成器
        
        Args:
            device_manager: 设备管理器实例
            oee_calculator: OEE计算器实例
            predictive_maintenance: 预测性维护实例
            spc_analyzer: SPC分析器实例
            energy_manager: 能源管理器实例
        """
        self.device_manager = device_manager
        self.oee_calculator = oee_calculator
        self.predictive_maintenance = predictive_maintenance
        self.spc_analyzer = spc_analyzer
        self.energy_manager = energy_manager
        
        # 设备理论产能配置（件/小时）
        self.theoretical_rates = {
            'siemens_1500_01': 120,    # 锅炉产线
            'hollysys_lk_01': 80,      # 化工车间
            'mitsubishi_fx5u_01': 200, # 注塑车间
            'delta_dvp_01': 150,       # 包装线
            'inovance_h5u_01': 100,    # 涂装车间
        }
        
        # 设备能源基线配置（kWh/天）
        self.energy_baselines = {
            'abb_m4m_01': 500,           # 电力分析仪
            'siemens_1500_01': 200,       # 锅炉产线
            'hollysys_lk_01': 150,        # 化工车间
            'mitsubishi_fx5u_01': 100,    # 注塑车间
            'delta_dvp_01': 80,           # 包装线
            'inovance_h5u_01': 120,       # 涂装车间
        }
        
        # 设备运行状态配置
        self.device_states = {
            'siemens_1500_01': 'running',
            'hollysys_lk_01': 'running',
            'mitsubishi_fx5u_01': 'running',
            'delta_dvp_01': 'running',
            'inovance_h5u_01': 'running',
        }
    
    def initialize_all(self):
        """
        初始化所有模块数据
        
        每个模块独立初始化，一个模块失败不影响其他模块。
        """
        logger.info("=" * 60)
        logger.info("开始初始化模拟数据...")
        logger.info("=" * 60)
        
        success_count = 0
        total_count = 4
        
        # 1. 初始化OEE数据
        try:
            self._init_oee_data()
            success_count += 1
        except Exception as e:
            logger.error(f"[1/4] OEE数据初始化失败: {e}\n{traceback.format_exc()}")
        
        # 2. 初始化能源数据
        try:
            self._init_energy_data()
            success_count += 1
        except Exception as e:
            logger.error(f"[2/4] 能源数据初始化失败: {e}\n{traceback.format_exc()}")
        
        # 3. 初始化预测性维护数据
        try:
            self._init_predictive_maintenance_data()
            success_count += 1
        except Exception as e:
            logger.error(f"[3/4] 预测性维护数据初始化失败: {e}\n{traceback.format_exc()}")
        
        # 4. 初始化SPC数据
        try:
            self._init_spc_data()
            success_count += 1
        except Exception as e:
            logger.error(f"[4/4] SPC数据初始化失败: {e}\n{traceback.format_exc()}")
        
        logger.info("=" * 60)
        logger.info(f"模拟数据初始化完成: {success_count}/{total_count} 个模块成功")
        logger.info("=" * 60)
    
    def _init_oee_data(self):
        """
        初始化OEE数据
        
        为每个设备启动班次、设置理论产能、记录模拟产量。
        """
        logger.info("[1/4] 初始化OEE数据...")
        
        if self.oee_calculator is None:
            logger.warning("OEE计算器未初始化，跳过")
            return
        
        devices = self._get_devices()
        if not devices:
            logger.warning("未找到设备，跳过OEE初始化")
            return
        
        initialized_count = 0
        for device_id in devices:
            try:
                # 启动班次（8小时班次）
                self.oee_calculator.start_shift(device_id, planned_hours=8.0)
                
                # 设置理论产能
                theoretical_rate = self.theoretical_rates.get(device_id, 100)
                self.oee_calculator.set_theoretical_rate(device_id, theoretical_rate)
                
                # 设置设备状态为运行
                self.oee_calculator.update_device_state(device_id, 'running')
                
                # 记录产量数据（模拟运行2小时）
                hours_running = 2.0
                expected_count = int(theoretical_rate * hours_running)
                
                # 添加随机波动（±5%）
                actual_count = int(expected_count * random.uniform(0.95, 1.05))
                # 97-99% 良品率
                good_count = int(actual_count * random.uniform(0.97, 0.99))
                
                self.oee_calculator.record_production(
                    device_id, count=actual_count, good_count=good_count
                )
                
                logger.info(
                    f"  ✓ {device_id}: 班次已启动, "
                    f"理论产能={theoretical_rate}/h, "
                    f"已生产={actual_count}, 良品={good_count}"
                )
                initialized_count += 1
            except Exception as e:
                logger.error(f"  ✗ {device_id} OEE初始化失败: {e}")
        
        logger.info(f"  OEE初始化完成: {initialized_count}/{len(devices)} 个设备")
    
    def _init_energy_data(self):
        """
        初始化能源数据
        
        为每个设备设置能源基线、喂入模拟功率数据。
        使用公共API，不直接访问内部属性。
        """
        logger.info("[2/4] 初始化能源数据...")
        
        if self.energy_manager is None:
            logger.warning("能源管理器未初始化，跳过")
            return
        
        initialized_count = 0
        for device_id, baseline in self.energy_baselines.items():
            try:
                # 设置能源基线
                self.energy_manager.set_baseline(device_id, baseline)
                
                # 计算当前功率（基于时间因子和随机波动）
                current_hour = datetime.now().hour
                # 白天功率高，夜间功率低（正弦模拟）
                hour_factor = 0.5 + 0.5 * math.sin((current_hour - 6) * math.pi / 12)
                current_power = (baseline / 24) * hour_factor * random.uniform(0.8, 1.2)
                
                # 喂入功率数据（使用公共API）
                self.energy_manager.feed_power_data(device_id, current_power)
                
                logger.info(
                    f"  ✓ {device_id}: 基线={baseline}kWh/天, "
                    f"当前功率={current_power:.1f}kW"
                )
                initialized_count += 1
            except Exception as e:
                logger.error(f"  ✗ {device_id} 能源数据初始化失败: {e}")
        
        logger.info(f"  能源初始化完成: {initialized_count}/{len(self.energy_baselines)} 个设备")
    
    def _init_predictive_maintenance_data(self):
        """
        初始化预测性维护数据
        
        为每个设备的每个寄存器生成历史数据，喂入预测性维护模块。
        """
        logger.info("[3/4] 初始化预测性维护数据...")
        
        if self.predictive_maintenance is None:
            logger.warning("预测性维护模块未初始化，跳过")
            return
        
        devices = self._get_devices()
        if not devices:
            logger.warning("未找到设备，跳过预测性维护初始化")
            return
        
        total_points = 0
        device_count = 0
        
        for device_id, device_config in devices.items():
            try:
                registers = device_config.get('registers', device_config.get('nodes', []))
                if not registers:
                    logger.debug(f"  跳过 {device_id}: 无寄存器配置")
                    continue
                
                device_points = 0
                for reg in registers:
                    reg_name = reg.get('name', '')
                    if not reg_name:
                        continue
                    
                    # 生成过去2小时的历史数据（每分钟一个点）
                    base_value = random.uniform(20, 80)
                    noise_level = random.uniform(2, 8)
                    
                    for i in range(120):  # 120分钟 = 2小时
                        ts = datetime.now() - timedelta(minutes=120 - i)
                        
                        # 生成更真实的工业数据
                        # 基础值 + 正弦波 + 低频漂移 + 高斯噪声
                        value = base_value
                        value += 10 * math.sin(i / 20)  # 主波形
                        value += 5 * math.sin(i / 60)   # 低频漂移
                        value += random.gauss(0, noise_level)  # 噪声
                        
                        # 偶尔添加尖峰（模拟异常，2%概率）
                        if random.random() < 0.02:
                            value += random.gauss(0, noise_level * 3)
                        
                        # 喂入预测性维护模块（使用公共API）
                        self.predictive_maintenance.feed_data(
                            device_id, reg_name, value, ts
                        )
                        
                        # 同时喂入SPC分析器（使用公共API）
                        if self.spc_analyzer is not None:
                            self.spc_analyzer.feed_data(device_id, reg_name, value)
                        
                        device_points += 1
                        total_points += 1
                
                logger.info(
                    f"  ✓ {device_id}: {len(registers)} 个寄存器, "
                    f"{device_points} 个数据点"
                )
                device_count += 1
            except Exception as e:
                logger.error(f"  ✗ {device_id} 预测性维护数据初始化失败: {e}")
        
        # 尝试触发分析（使用公共API，如果存在）
        try:
            if hasattr(self.predictive_maintenance, 'run_analysis'):
                self.predictive_maintenance.run_analysis()
                logger.info("  已触发预测性维护分析")
            elif hasattr(self.predictive_maintenance, '_run_analysis'):
                # 兼容旧版本私有方法
                self.predictive_maintenance._run_analysis()
                logger.info("  已触发预测性维护分析（私有方法）")
        except Exception as e:
            logger.warning(f"  触发分析失败（非致命）: {e}")
        
        logger.info(
            f"  预测性维护初始化完成: {device_count} 个设备, "
            f"{total_points} 个数据点"
        )
    
    def _init_spc_data(self):
        """
        初始化SPC数据
        
        为常见参数设置规格限（USL/LSL/目标值）。
        SPC数据已在预测性维护初始化时一起喂入。
        """
        logger.info("[4/4] 初始化SPC数据...")
        
        if self.spc_analyzer is None:
            logger.warning("SPC分析器未初始化，跳过")
            return
        
        devices = self._get_devices()
        if not devices:
            logger.warning("未找到设备，跳过SPC初始化")
            return
        
        configured_count = 0
        for device_id, device_config in devices.items():
            try:
                registers = device_config.get('registers', device_config.get('nodes', []))
                for reg in registers:
                    reg_name = reg.get('name', '')
                    if not reg_name:
                        continue
                    
                    key = f"{device_id}:{reg_name}"
                    
                    # 根据寄存器名称设置合理的规格限
                    spec_set = False
                    if 'temperature' in reg_name.lower():
                        self.spc_analyzer.set_spec_limits(
                            key, usl=100.0, lsl=0.0, target=50.0
                        )
                        spec_set = True
                    elif 'pressure' in reg_name.lower():
                        self.spc_analyzer.set_spec_limits(
                            key, usl=1.0, lsl=0.0, target=0.5
                        )
                        spec_set = True
                    elif 'voltage' in reg_name.lower():
                        self.spc_analyzer.set_spec_limits(
                            key, usl=250.0, lsl=200.0, target=220.0
                        )
                        spec_set = True
                    elif 'current' in reg_name.lower():
                        self.spc_analyzer.set_spec_limits(
                            key, usl=50.0, lsl=0.0, target=25.0
                        )
                        spec_set = True
                    
                    if spec_set:
                        configured_count += 1
            except Exception as e:
                logger.error(f"  ✗ {device_id} SPC规格限配置失败: {e}")
        
        logger.info(f"  SPC规格限配置完成: {configured_count} 个参数")
    
    def _get_devices(self) -> dict:
        """
        安全获取设备列表
        
        Returns:
            设备字典，失败返回空字典
        """
        try:
            devices = self.device_manager.get_all_devices()
            return devices if devices else {}
        except Exception as e:
            logger.error(f"获取设备列表失败: {e}")
            return {}


def initialize_simulation_data(device_manager, oee_calculator, predictive_maintenance,
                               spc_analyzer, energy_manager):
    """
    便捷函数：初始化所有模拟数据
    
    Args:
        device_manager: 设备管理器
        oee_calculator: OEE计算器
        predictive_maintenance: 预测性维护
        spc_analyzer: SPC分析器
        energy_manager: 能源管理器
    """
    try:
        initializer = SimulationInitializer(
            device_manager, oee_calculator, predictive_maintenance,
            spc_analyzer, energy_manager
        )
        initializer.initialize_all()
    except Exception as e:
        logger.error(f"模拟数据初始化失败: {e}\n{traceback.format_exc()}")
        raise
