"""
模拟模式初始化器
为工业4.0模块预填充初始数据，让页面立即显示有效数据
"""

import math
import random
import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


class SimulationInitializer:
    """模拟模式初始化器"""
    
    def __init__(self, device_manager, oee_calculator, predictive_maintenance, 
                 spc_analyzer, energy_manager):
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
        """初始化所有模块数据"""
        logger.info("=" * 50)
        logger.info("开始初始化模拟数据...")
        logger.info("=" * 50)
        
        # 1. 初始化OEE数据
        self._init_oee_data()
        
        # 2. 初始化能源数据
        self._init_energy_data()
        
        # 3. 初始化预测性维护数据
        self._init_predictive_maintenance_data()
        
        # 4. 初始化SPC数据
        self._init_spc_data()
        
        logger.info("=" * 50)
        logger.info("模拟数据初始化完成")
        logger.info("=" * 50)
    
    def _init_oee_data(self):
        """初始化OEE数据"""
        logger.info("[1/4] 初始化OEE数据...")
        
        for device_id in self.device_manager.get_all_devices():
            # 启动班次
            self.oee_calculator.start_shift(device_id, planned_hours=8.0)
            
            # 设置理论产能
            if device_id in self.theoretical_rates:
                self.oee_calculator.set_theoretical_rate(device_id, self.theoretical_rates[device_id])
            
            # 设置设备状态为运行
            self.oee_calculator.update_device_state(device_id, 'running')
            
            # 记录产量数据（模拟运行2小时）
            # 基于理论产能计算合理产量
            theoretical_rate = self.theoretical_rates.get(device_id, 100)
            hours_running = 2.0
            expected_count = int(theoretical_rate * hours_running)
            
            # 添加一些随机波动（±5%）
            actual_count = int(expected_count * random.uniform(0.95, 1.05))
            good_count = int(actual_count * random.uniform(0.97, 0.99))  # 97-99% 良品率
            
            self.oee_calculator.record_production(device_id, count=actual_count, good_count=good_count)
            
            logger.info(f"  ✓ {device_id}: 班次已启动，理论产能={theoretical_rate}/h，已生产={actual_count}")
    
    def _init_energy_data(self):
        """初始化能源数据"""
        logger.info("[2/4] 初始化能源数据...")
        
        for device_id, baseline in self.energy_baselines.items():
            # 设置能源基线
            self.energy_manager.set_baseline(device_id, baseline)
            
            # 模拟当前功率（基线的1/24 * 随机波动，模拟一天中的当前时刻）
            current_hour = datetime.now().hour
            # 白天功率高，夜间功率低
            hour_factor = 0.5 + 0.5 * math.sin((current_hour - 6) * math.pi / 12)
            current_power = (baseline / 24) * hour_factor * random.uniform(0.8, 1.2)
            
            # 喂入功率数据
            self.energy_manager.feed_power_data(device_id, current_power)
            
            # 设置累积电量（基于当前时间计算）
            hours_elapsed = current_hour + datetime.now().minute / 60.0
            accumulated_energy = baseline * (hours_elapsed / 24.0)
            
            # 分时电量分配
            peak_hours = min(hours_elapsed, 3)  # 峰时最多3小时
            valley_hours = max(0, min(hours_elapsed - 17, 7))  # 谷时最多7小时
            flat_hours = hours_elapsed - peak_hours - valley_hours
            
            self.energy_manager.energy_accumulated[device_id]['energy_kwh'] = accumulated_energy
            self.energy_manager.energy_accumulated[device_id]['peak_kwh'] = accumulated_energy * (peak_hours / hours_elapsed) if hours_elapsed > 0 else 0
            self.energy_manager.energy_accumulated[device_id]['flat_kwh'] = accumulated_energy * (flat_hours / hours_elapsed) if hours_elapsed > 0 else 0
            self.energy_manager.energy_accumulated[device_id]['valley_kwh'] = accumulated_energy * (valley_hours / hours_elapsed) if hours_elapsed > 0 else 0
            
            logger.info(f"  ✓ {device_id}: 基线={baseline}kWh/天，当前功率={current_power:.1f}kW，累积电量={accumulated_energy:.1f}kWh")
    
    def _init_predictive_maintenance_data(self):
        """初始化预测性维护数据"""
        logger.info("[3/4] 初始化预测性维护数据...")
        
        # 为每个设备的每个寄存器预填充历史数据
        devices = self.device_manager.get_all_devices()
        total_points = 0
        
        for device_id, device_config in devices.items():
            registers = device_config.get('registers', device_config.get('nodes', []))
            
            for reg in registers:
                reg_name = reg.get('name', '')
                if not reg_name:
                    continue
                
                # 生成过去2小时的历史数据（每分钟一个点）
                base_value = random.uniform(20, 80)
                noise_level = random.uniform(2, 8)
                
                for i in range(120):  # 120分钟 = 2小时
                    ts = datetime.now() - timedelta(minutes=120-i)
                    
                    # 生成更真实的工业数据
                    # 基础值 + 正弦波 + 低频漂移 + 高斯噪声
                    value = base_value
                    value += 10 * math.sin(i / 20)  # 主波形
                    value += 5 * math.sin(i / 60)   # 低频漂移
                    value += random.gauss(0, noise_level)  # 噪声
                    
                    # 偶尔添加尖峰（模拟异常）
                    if random.random() < 0.02:
                        value += random.gauss(0, noise_level * 3)
                    
                    # 喂入预测性维护模块
                    self.predictive_maintenance.feed_data(device_id, reg_name, value, ts)
                    
                    # 同时喂入SPC分析器
                    self.spc_analyzer.feed_data(device_id, reg_name, value)
                    
                    total_points += 1
        
        # 手动触发一次分析
        self.predictive_maintenance._run_analysis()
        
        logger.info(f"  ✓ 已预填充 {total_points} 个数据点")
    
    def _init_spc_data(self):
        """初始化SPC数据"""
        logger.info("[4/4] 初始化SPC数据...")
        
        # SPC数据已经在预测性维护初始化时一起喂入了
        # 这里可以添加一些额外的SPC配置
        
        # 为常见参数设置规格限
        devices = self.device_manager.get_all_devices()
        for device_id, device_config in devices.items():
            registers = device_config.get('registers', device_config.get('nodes', []))
            for reg in registers:
                reg_name = reg.get('name', '')
                if not reg_name:
                    continue
                
                key = f"{device_id}:{reg_name}"
                
                # 根据寄存器名称设置合理的规格限
                if 'temperature' in reg_name.lower():
                    self.spc_analyzer.set_spec_limits(key, usl=100.0, lsl=0.0, target=50.0)
                elif 'pressure' in reg_name.lower():
                    self.spc_analyzer.set_spec_limits(key, usl=1.0, lsl=0.0, target=0.5)
                elif 'voltage' in reg_name.lower():
                    self.spc_analyzer.set_spec_limits(key, usl=250.0, lsl=200.0, target=220.0)
                elif 'current' in reg_name.lower():
                    self.spc_analyzer.set_spec_limits(key, usl=50.0, lsl=0.0, target=25.0)
        
        logger.info("  ✓ SPC规格限配置完成")


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
    initializer = SimulationInitializer(
        device_manager, oee_calculator, predictive_maintenance,
        spc_analyzer, energy_manager
    )
    initializer.initialize_all()
