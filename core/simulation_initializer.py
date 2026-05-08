"""
模拟模式初始化器（重写版）

核心改动：
- 启动时设备数量为0，不预设任何设备
- 用户通过前端一键添加预设设备，或自定义添加
- 预设配置从 simulation_presets.yaml 加载
- 支持运行时动态添加/删除/修改设备
"""

import math
import random
import logging
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

# 预设配置文件路径
PRESETS_PATH = Path(__file__).parent.parent / '配置' / 'simulation_presets.yaml'


class SimulationInitializer:
    """
    模拟模式初始化器（重写版）

    启动时设备数量为0。
    用户通过API或前端一键添加预设设备，系统自动为新设备填充模拟数据。
    """

    def __init__(self, device_manager, oee_calculator=None, predictive_maintenance=None,
                 spc_analyzer=None, energy_manager=None):
        self.device_manager = device_manager
        self.oee_calculator = oee_calculator
        self.predictive_maintenance = predictive_maintenance
        self.spc_analyzer = spc_analyzer
        self.energy_manager = energy_manager

        # 加载预设配置
        self.presets = self._load_presets()

        # 设备模拟参数（运行时动态维护）
        self.device_sim_params = {}  # device_id -> simulation_params

        logger.info("SimulationInitializer 初始化完成（设备数量: 0）")

    # ================================================================
    # 预设管理
    # ================================================================

    def _load_presets(self) -> list[dict]:
        """从YAML加载预设配置"""
        try:
            if not PRESETS_PATH.exists():
                logger.warning(f"预设配置文件不存在: {PRESETS_PATH}")
                return []
            with open(PRESETS_PATH, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            presets = data.get('presets', [])
            logger.info(f"加载 {len(presets)} 个模拟设备预设")
            return presets
        except Exception as e:
            logger.error(f"加载预设配置失败: {e}")
            return []

    def get_presets(self) -> list[dict]:
        """获取所有可用预设（返回给前端）"""
        result = []
        for p in self.presets:
            result.append({
                'preset_id': p['id'],
                'name': p['name'],
                'category': p.get('category', 'other'),
                'description': p.get('description', ''),
                'device_id': p['device_config']['id'],
                'protocol': p['device_config'].get('protocol', 'modbus_tcp'),
            })
        return result

    def get_preset_categories(self) -> list[dict]:
        """获取预设分类"""
        try:
            if not PRESETS_PATH.exists():
                return []
            with open(PRESETS_PATH, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            return data.get('categories', [])
        except Exception:
            return []

    def get_preset_detail(self, preset_id: str) -> Optional[dict]:
        """获取单个预设详情"""
        for p in self.presets:
            if p['id'] == preset_id:
                return p
        return None

    # ================================================================
    # 设备添加（预设 + 自定义）
    # ================================================================

    def add_preset_device(self, preset_id: str, custom_device_id: str = None) -> dict:
        """
        一键添加预设设备

        Args:
            preset_id: 预设ID
            custom_device_id: 可选，自定义设备ID（覆盖预设默认ID）

        Returns:
            {'success': bool, 'message': str, 'device_id': str}
        """
        preset = self.get_preset_detail(preset_id)
        if not preset:
            return {'success': False, 'message': f'预设 {preset_id} 不存在'}

        device_config = preset['device_config'].copy()
        sim_params = preset.get('simulation_params', {})

        # 允许自定义设备ID
        if custom_device_id:
            device_config['id'] = custom_device_id

        device_id = device_config['id']

        # 检查是否已存在
        if device_id in self.device_manager.get_all_devices():
            return {'success': False, 'message': f'设备 {device_id} 已存在'}

        # 添加到设备管理器
        success = self.device_manager.add_device(device_config)
        if not success:
            return {'success': False, 'message': f'添加设备 {device_id} 失败'}

        # 保存模拟参数
        self.device_sim_params[device_id] = sim_params

        # 为新设备初始化智能层数据
        self._init_device_data(device_id, device_config, sim_params)

        logger.info(f"已添加预设设备: {device_id} (预设: {preset_id})")
        return {'success': True, 'message': f'设备 {device_id} 添加成功', 'device_id': device_id}

    def add_custom_device(self, device_config: dict, sim_params: dict = None) -> dict:
        """
        添加自定义设备

        Args:
            device_config: 设备配置字典（必须包含 id, name, protocol）
            sim_params: 可选的模拟参数

        Returns:
            {'success': bool, 'message': str, 'device_id': str}
        """
        device_id = device_config.get('id')
        if not device_id:
            return {'success': False, 'message': '设备配置缺少 id 字段'}

        if not device_config.get('name'):
            return {'success': False, 'message': '设备配置缺少 name 字段'}

        # 添加到设备管理器
        success = self.device_manager.add_device(device_config)
        if not success:
            return {'success': False, 'message': f'添加设备 {device_id} 失败'}

        # 保存模拟参数
        if sim_params:
            self.device_sim_params[device_id] = sim_params

        # 初始化智能层数据
        self._init_device_data(device_id, device_config, sim_params or {})

        logger.info(f"已添加自定义设备: {device_id}")
        return {'success': True, 'message': f'设备 {device_id} 添加成功', 'device_id': device_id}

    def remove_device(self, device_id: str) -> dict:
        """删除设备"""
        success = self.device_manager.remove_device(device_id)
        if success:
            self.device_sim_params.pop(device_id, None)
            logger.info(f"已删除设备: {device_id}")
            return {'success': True, 'message': f'设备 {device_id} 已删除'}
        return {'success': False, 'message': f'删除设备 {device_id} 失败'}

    def add_preset_batch(self, preset_ids: list[str]) -> dict:
        """批量添加预设设备"""
        results = []
        success_count = 0
        for pid in preset_ids:
            r = self.add_preset_device(pid)
            results.append(r)
            if r['success']:
                success_count += 1
        return {
            'success': success_count > 0,
            'message': f'成功添加 {success_count}/{len(preset_ids)} 个设备',
            'results': results
        }

    # ================================================================
    # 单设备数据初始化
    # ================================================================

    def _init_device_data(self, device_id: str, device_config: dict, sim_params: dict):
        """
        为单个新添加的设备初始化所有智能层数据
        """
        logger.info(f"  初始化设备 {device_id} 的智能层数据...")

        # 1. OEE
        try:
            self._init_single_oee(device_id, sim_params)
        except Exception as e:
            logger.error(f"  ✗ {device_id} OEE初始化失败: {e}")

        # 2. 能源
        try:
            self._init_single_energy(device_id, sim_params)
        except Exception as e:
            logger.error(f"  ✗ {device_id} 能源初始化失败: {e}")

        # 3. 预测性维护 + SPC
        try:
            self._init_single_predictive(device_id, device_config, sim_params)
        except Exception as e:
            logger.error(f"  ✗ {device_id} 预测性维护初始化失败: {e}")

    def _init_single_oee(self, device_id: str, sim_params: dict):
        """为单个设备初始化OEE数据"""
        if self.oee_calculator is None:
            return

        theoretical_rate = sim_params.get('theoretical_rate', 0)
        if theoretical_rate <= 0:
            logger.debug(f"  跳过 {device_id} OEE（理论产能为0）")
            return

        self.oee_calculator.start_shift(device_id, planned_hours=8.0)
        self.oee_calculator.set_theoretical_rate(device_id, theoretical_rate)
        self.oee_calculator.update_device_state(device_id, 'running')

        # 模拟2小时产量
        hours_running = 2.0
        expected_count = int(theoretical_rate * hours_running)
        actual_count = int(expected_count * random.uniform(0.95, 1.05))
        good_count = int(actual_count * random.uniform(0.97, 0.99))

        self.oee_calculator.record_production(device_id, count=actual_count, good_count=good_count)
        logger.info(f"  ✓ {device_id} OEE: 班次已启动, 理论产能={theoretical_rate}/h, 已生产={actual_count}")

    def _init_single_energy(self, device_id: str, sim_params: dict):
        """为单个设备初始化能源数据"""
        if self.energy_manager is None:
            return

        baseline = sim_params.get('energy_baseline', 0)
        if baseline <= 0:
            logger.debug(f"  跳过 {device_id} 能源（基线为0）")
            return

        self.energy_manager.set_baseline(device_id, baseline)

        current_hour = datetime.now().hour
        hour_factor = 0.5 + 0.5 * math.sin((current_hour - 6) * math.pi / 12)
        current_power = (baseline / 24) * hour_factor * random.uniform(0.8, 1.2)

        self.energy_manager.feed_power_data(device_id, current_power)
        logger.info(f"  ✓ {device_id} 能源: 基线={baseline}kWh/天, 当前功率={current_power:.1f}kW")

    def _init_single_predictive(self, device_id: str, device_config: dict, sim_params: dict):
        """为单个设备初始化预测性维护和SPC数据"""
        base_values = sim_params.get('base_values', {})
        noise_levels = sim_params.get('noise_levels', {})

        # 获取寄存器列表
        registers = device_config.get('registers', device_config.get('nodes', []))
        topics = device_config.get('topics', [])
        endpoints = device_config.get('endpoints', [])

        # 统一提取变量名
        var_names = []
        for reg in registers:
            name = reg.get('name', '')
            if name:
                var_names.append(name)
        for node in topics:
            name = node.get('name', '')
            if name:
                var_names.append(name)
        for ep in endpoints:
            name = ep.get('name', '')
            if name:
                var_names.append(name)

        if not var_names:
            logger.debug(f"  跳过 {device_id} 预测性维护（无变量）")
            return

        total_points = 0
        for var_name in var_names:
            base = base_values.get(var_name, 50.0)
            noise = noise_levels.get(var_name, 5.0)

            # 生成120个历史数据点（2小时，每分钟1个）
            for i in range(120):
                ts = datetime.now() - timedelta(minutes=120 - i)
                value = base
                value += (base * 0.1) * math.sin(i / 20)  # 主波形
                value += (base * 0.05) * math.sin(i / 60)  # 低频漂移
                value += random.gauss(0, noise)  # 噪声

                # 2%概率尖峰
                if random.random() < 0.02:
                    value += random.gauss(0, noise * 3)

                # 喂入预测性维护
                if self.predictive_maintenance is not None:
                    try:
                        self.predictive_maintenance.feed_data(device_id, var_name, value, ts)
                    except Exception:
                        pass

                # 喂入SPC
                if self.spc_analyzer is not None:
                    try:
                        self.spc_analyzer.feed_data(device_id, var_name, value)
                    except Exception:
                        pass

                total_points += 1

        # 设置SPC规格限
        if self.spc_analyzer is not None:
            self._set_spc_limits(device_id, var_names)

        logger.info(f"  ✓ {device_id} 预测性维护: {len(var_names)} 个变量, {total_points} 个数据点")

    def _set_spc_limits(self, device_id: str, var_names: list[str]):
        """根据变量名自动设置SPC规格限"""
        for var_name in var_names:
            key = f"{device_id}:{var_name}"
            name_lower = var_name.lower()

            try:
                if 'temperature' in name_lower or 'temp' in name_lower:
                    self.spc_analyzer.set_spec_limits(key, usl=100.0, lsl=0.0, target=50.0)
                elif 'pressure' in name_lower:
                    self.spc_analyzer.set_spec_limits(key, usl=1.0, lsl=0.0, target=0.5)
                elif 'voltage' in name_lower:
                    self.spc_analyzer.set_spec_limits(key, usl=250.0, lsl=200.0, target=220.0)
                elif 'current' in name_lower:
                    self.spc_analyzer.set_spec_limits(key, usl=50.0, lsl=0.0, target=25.0)
                elif 'flow' in name_lower:
                    self.spc_analyzer.set_spec_limits(key, usl=100.0, lsl=0.0, target=50.0)
                elif 'ph' in name_lower:
                    self.spc_analyzer.set_spec_limits(key, usl=9.0, lsl=5.0, target=7.0)
                elif 'vibration' in name_lower:
                    self.spc_analyzer.set_spec_limits(key, usl=10.0, lsl=0.0, target=3.0)
            except Exception:
                pass

    # ================================================================
    # 兼容旧接口
    # ================================================================

    def initialize_all(self):
        """
        兼容旧接口 — 不再预设设备，仅记录日志
        """
        device_count = len(self.device_manager.get_all_devices())
        logger.info(f"模拟模式启动完成: 当前设备数量={device_count}")
        if device_count == 0:
            logger.info("  提示: 设备数量为0，请通过前端添加预设设备或自定义设备")

    # ================================================================
    # 查询
    # ================================================================

    def get_device_sim_params(self, device_id: str) -> dict:
        """获取设备的模拟参数"""
        return self.device_sim_params.get(device_id, {})

    def get_all_sim_params(self) -> dict:
        """获取所有设备的模拟参数"""
        return self.device_sim_params.copy()

    def update_device_sim_params(self, device_id: str, params: dict) -> dict:
        """更新设备的模拟参数"""
        if device_id not in self.device_manager.get_all_devices():
            return {'success': False, 'message': f'设备 {device_id} 不存在'}
        self.device_sim_params[device_id] = params
        return {'success': True, 'message': f'设备 {device_id} 模拟参数已更新'}


# ================================================================
# 便捷函数（兼容旧调用）
# ================================================================

def initialize_simulation_data(device_manager, oee_calculator=None, predictive_maintenance=None,
                               spc_analyzer=None, energy_manager=None):
    """
    便捷函数：初始化模拟数据（兼容旧接口）

    重写后不再预设设备，仅创建初始化器实例。
    设备通过前端或API动态添加。
    """
    try:
        initializer = SimulationInitializer(
            device_manager, oee_calculator, predictive_maintenance,
            spc_analyzer, energy_manager
        )
        initializer.initialize_all()
        return initializer
    except Exception as e:
        logger.error(f"模拟数据初始化失败: {e}\n{traceback.format_exc()}")
        raise
