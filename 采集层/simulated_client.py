"""
模拟客户端模块（配置驱动版）
在没有真实设备时根据设备配置自动生成仿真数据

核心设计：不硬编码任何设备ID！
- 从寄存器配置的 name/unit/data_type 自动推断模拟数据范围
- 新增设备只需改YAML，永远不需要改这个文件
"""

import math
import time
import random
import struct
import logging
import threading
from datetime import datetime
from typing import Any, Callable

from .base_client import ModbusClientInterface, PushClientInterface

logger = logging.getLogger(__name__)


# ============================================================
# 关键字→数据范围映射表（全局配置，一劳永逸）
# 每条规则: keyword → (base, amplitude, period, noise, unit_pattern)
# ============================================================

_REGISTER_RULES = [
    # --- 电气参数 ---
    {'kw': 'voltage',       'unit_kw': 'V',    'base': 220, 'amp': 15,   'period': 120, 'noise': 2.0,  'shape': 'sine'},
    {'kw': 'current',       'unit_kw': 'A',    'base': 30,  'amp': 15,   'period': 60,  'noise': 1.0,  'shape': 'sine'},
    {'kw': 'active_power',  'unit_kw': 'kW',   'base': 50,  'amp': 30,   'period': 45,  'noise': 2.0,  'shape': 'sine'},
    {'kw': 'reactive_power','unit_kw': 'kvar',  'base': 10,  'amp': 8,    'period': 60,  'noise': 1.0,  'shape': 'sine'},
    {'kw': 'apparent_power','unit_kw': 'kVA',   'base': 55,  'amp': 25,   'period': 50,  'noise': 2.0,  'shape': 'sine'},
    {'kw': 'power_factor',  'unit_kw': '',      'base': 0.92,'amp': 0.05, 'period': 90,  'noise': 0.01, 'shape': 'sine'},
    {'kw': 'frequency',     'unit_kw': 'Hz',   'base': 50,  'amp': 0.3,  'period': 30,  'noise': 0.05, 'shape': 'sine'},
    {'kw': 'energy',        'unit_kw': 'kWh',  'base': 12345, 'amp': 0,  'period': 0,   'noise': 0,    'shape': 'ramp', 'rate': 0.02},
    {'kw': 'power',         'unit_kw': 'W',    'base': 5000,'amp': 2000, 'period': 40,  'noise': 100,  'shape': 'sine'},
    {'kw': 'energy',        'unit_kw': 'MWh',  'base': 500, 'amp': 0,    'period': 0,   'noise': 0,    'shape': 'ramp', 'rate': 0.001},
    {'kw': 'thd',           'unit_kw': '%',    'base': 3.5, 'amp': 2.0,  'period': 120, 'noise': 0.1,  'shape': 'sine'},
    {'kw': 'electricity',   'unit_kw': 'kWh',  'base': 5000, 'amp': 0,   'period': 0,   'noise': 0,    'shape': 'ramp', 'rate': 0.5},
    {'kw': 'oee',           'unit_kw': '%',    'base': 85,  'amp': 5,    'period': 600, 'noise': 0.5,  'shape': 'sine'},
    {'kw': 'quality_rate',  'unit_kw': '%',    'base': 97,  'amp': 2,    'period': 600, 'noise': 0.3,  'shape': 'sine'},
    {'kw': 'defect_rate',   'unit_kw': '%',    'base': 1.5, 'amp': 1.0,  'period': 300, 'noise': 0.2,  'shape': 'sine'},
    {'kw': 'planned_quantity', 'unit_kw': '个', 'base': 1000, 'amp': 0,  'period': 0,   'noise': 0,    'shape': 'ramp', 'rate': 0.1},
    {'kw': 'actual_quantity','unit_kw': '个',   'base': 970, 'amp': 0,   'period': 0,   'noise': 0,    'shape': 'ramp', 'rate': 0.097},

    # --- 温度 ---
    {'kw': 'temperature',   'unit_kw': '°C',   'base': 50,  'amp': 20,   'period': 90,  'noise': 0.5,  'shape': 'sine'},
    {'kw': 'temperature',   'unit_kw': 'C',    'base': 50,  'amp': 20,   'period': 90,  'noise': 0.5,  'shape': 'sine'},
    {'kw': 'temp',          'unit_kw': '°C',   'base': 50,  'amp': 20,   'period': 90,  'noise': 0.5,  'shape': 'sine'},
    {'kw': 'temp',          'unit_kw': 'C',    'base': 50,  'amp': 20,   'period': 90,  'noise': 0.5,  'shape': 'sine'},

    # --- 压力 ---
    {'kw': 'pressure',      'unit_kw': 'MPa',  'base': 0.8, 'amp': 0.5,  'period': 60,  'noise': 0.01, 'shape': 'sine'},
    {'kw': 'pressure',      'unit_kw': 'kPa',  'base': 800, 'amp': 200,  'period': 60,  'noise': 5.0,  'shape': 'sine'},
    {'kw': 'pressure',      'unit_kw': 'Pa',   'base': 101325, 'amp': 5000, 'period': 120, 'noise': 100, 'shape': 'sine'},
    {'kw': 'vacuum',        'unit_kw': 'Pa',   'base': -80000, 'amp': 10000, 'period': 90, 'noise': 500, 'shape': 'sine'},
    {'kw': 'spray_pressure','unit_kw': 'MPa',  'base': 0.3, 'amp': 0.1,  'period': 60,  'noise': 0.005,'shape': 'sine'},

    # --- 流量 ---
    {'kw': 'flow',          'unit_kw': 'm³/h', 'base': 15,  'amp': 7,    'period': 45,  'noise': 0.3,  'shape': 'sine'},
    {'kw': 'flow',          'unit_kw': 'L/min','base': 50,  'amp': 20,   'period': 45,  'noise': 1.0,  'shape': 'sine'},
    {'kw': 'flow',          'unit_kw': 't/h',  'base': 5,   'amp': 2,    'period': 60,  'noise': 0.1,  'shape': 'sine'},
    {'kw': 'steam_flow',    'unit_kw': 't/h',  'base': 5,   'amp': 2,    'period': 60,  'noise': 0.1,  'shape': 'sine'},
    {'kw': 'inlet_flow',    'unit_kw': 'm³/h', 'base': 20,  'amp': 8,    'period': 45,  'noise': 0.5,  'shape': 'sine'},
    {'kw': 'outlet_flow',   'unit_kw': 'm³/h', 'base': 18,  'amp': 7,    'period': 45,  'noise': 0.4,  'shape': 'sine'},
    {'kw': 'product_flow',  'unit_kw': 'L/min','base': 30,  'amp': 10,   'period': 30,  'noise': 0.5,  'shape': 'sine'},
    {'kw': 'steam',         'unit_kw': 't',    'base': 50,  'amp': 0,    'period': 0,   'noise': 0,    'shape': 'ramp', 'rate': 0.01},

    # --- 液位 ---
    {'kw': 'level',         'unit_kw': 'm',    'base': 1.8, 'amp': 0.8,  'period': 120, 'noise': 0.02, 'shape': 'sine'},
    {'kw': 'level',         'unit_kw': 'mm',   'base': 1800,'amp': 500,  'period': 120, 'noise': 10,   'shape': 'sine'},
    {'kw': 'level',         'unit_kw': '%',    'base': 60,  'amp': 25,   'period': 120, 'noise': 1.0,  'shape': 'sine'},

    # --- 转速/速度 ---
    {'kw': 'speed',         'unit_kw': 'RPM',  'base': 1500,'amp': 500,  'period': 30,  'noise': 10,   'shape': 'sine'},
    {'kw': 'speed',         'unit_kw': 'r/min','base': 1500,'amp': 500,  'period': 30,  'noise': 10,   'shape': 'sine'},
    {'kw': 'speed',         'unit_kw': 'm/min','base': 12,  'amp': 5,    'period': 60,  'noise': 0.3,  'shape': 'sine'},
    {'kw': 'speed',         'unit_kw': 'm/s',  'base': 5,   'amp': 2,    'period': 30,  'noise': 0.1,  'shape': 'sine'},
    {'kw': 'conveyor_speed','unit_kw': 'm/min','base': 12,  'amp': 5,    'period': 60,  'noise': 0.3,  'shape': 'sine'},
    {'kw': 'spray_speed',   'unit_kw': 'm/min','base': 8,   'amp': 3,    'period': 45,  'noise': 0.2,  'shape': 'sine'},
    {'kw': 'injection_speed','unit_kw': 'mm/s','base': 80,  'amp': 30,   'period': 30,  'noise': 1.0,  'shape': 'sine'},
    {'kw': 'packaging_speed','unit_kw': '个',  'base': 120, 'amp': 20,   'period': 60,  'noise': 2,    'shape': 'sine'},
    {'kw': 'pump_speed',    'unit_kw': 'RPM',  'base': 1450,'amp': 200,  'period': 30,  'noise': 5,    'shape': 'sine'},

    # --- 电机参数 ---
    {'kw': 'motor_speed',   'unit_kw': 'RPM',  'base': 1500,'amp': 500,  'period': 30,  'noise': 10,   'shape': 'sine'},
    {'kw': 'motor_current', 'unit_kw': 'A',    'base': 20,  'amp': 10,   'period': 25,  'noise': 0.5,  'shape': 'sine'},
    {'kw': 'torque',        'unit_kw': 'N·m',  'base': 18,  'amp': 4,    'period': 30,  'noise': 0.5,  'shape': 'sine'},
    {'kw': 'torque',        'unit_kw': 'N*m',  'base': 18,  'amp': 4,    'period': 30,  'noise': 0.5,  'shape': 'sine'},
    {'kw': 'vibration',     'unit_kw': 'mm/s', 'base': 2.5, 'amp': 1.0,  'period': 20,  'noise': 0.2,  'shape': 'sine'},
    {'kw': 'vibration',     'unit_kw': 'g',    'base': 0.5, 'amp': 0.3,  'period': 15,  'noise': 0.05, 'shape': 'sine'},
    {'kw': 'clamping_force','unit_kw': 'kN',   'base': 1500,'amp': 500,  'period': 30,  'noise': 20,   'shape': 'sine'},
    {'kw': 'force',         'unit_kw': 'kN',   'base': 1500,'amp': 500,  'period': 30,  'noise': 20,   'shape': 'sine'},

    # --- 位置/距离/厚度 ---
    {'kw': 'position',      'unit_kw': 'mm',   'base': 50,  'amp': 20,   'period': 30,  'noise': 0.5,  'shape': 'sine'},
    {'kw': 'distance',      'unit_kw': 'mm',   'base': 200, 'amp': 100,  'period': 60,  'noise': 1.0,  'shape': 'sine'},
    {'kw': 'thickness',     'unit_kw': 'μm',   'base': 80,  'amp': 20,   'period': 60,  'noise': 2.0,  'shape': 'sine'},
    {'kw': 'coating_thickness', 'unit_kw': 'μm','base': 80, 'amp': 20,   'period': 60,  'noise': 2.0,  'shape': 'sine'},

    # --- 环境/气体 ---
    {'kw': 'humidity',      'unit_kw': '%RH',  'base': 62,  'amp': 10,   'period': 600, 'noise': 1.5,  'shape': 'sine'},
    {'kw': 'humidity',      'unit_kw': '%',    'base': 62,  'amp': 10,   'period': 600, 'noise': 1.5,  'shape': 'sine'},
    {'kw': 'co2',           'unit_kw': 'ppm',  'base': 520, 'amp': 150,  'period': 120, 'noise': 10,   'shape': 'sine'},
    {'kw': 'pm25',          'unit_kw': 'μg/m³','base': 35,  'amp': 15,   'period': 600, 'noise': 5,    'shape': 'sine'},
    {'kw': 'pm10',          'unit_kw': 'μg/m³','base': 55,  'amp': 25,   'period': 600, 'noise': 8,    'shape': 'sine'},
    {'kw': 'ph',            'unit_kw': '',      'base': 7.0, 'amp': 0.5,  'period': 300, 'noise': 0.05, 'shape': 'sine'},
    {'kw': 'oxygen',        'unit_kw': '%',    'base': 20.9,'amp': 0.5,  'period': 600, 'noise': 0.05, 'shape': 'sine'},
    {'kw': 'oxygen_content','unit_kw': '%',    'base': 5.5, 'amp': 2.0,  'period': 120, 'noise': 0.2,  'shape': 'sine'},
    {'kw': 'dissolved_oxygen','unit_kw': 'mg/L','base': 8.0,'amp': 2.0,  'period': 120, 'noise': 0.2,  'shape': 'sine'},
    {'kw': 'turbidity',     'unit_kw': 'NTU',  'base': 5,   'amp': 3,    'period': 180, 'noise': 0.3,  'shape': 'sine'},
    {'kw': 'conductivity',  'unit_kw': 'μS/cm','base': 500, 'amp': 100,  'period': 300, 'noise': 10,   'shape': 'sine'},

    # --- 生产计数/节拍（单调递增或缓变） ---
    {'kw': 'count',         'unit_kw': '个',   'base': 1000,'amp': 0,    'period': 0,   'noise': 0,    'shape': 'ramp', 'rate': 0.2},
    {'kw': 'count',         'unit_kw': 'pcs',  'base': 1000,'amp': 0,    'period': 0,   'noise': 0,    'shape': 'ramp', 'rate': 0.2},
    {'kw': 'cycle_time',    'unit_kw': 's',    'base': 4.5, 'amp': 1.0,  'period': 120, 'noise': 0.1,  'shape': 'sine'},
    {'kw': 'product',       'unit_kw': '个',   'base': 1000,'amp': 0,    'period': 0,   'noise': 0,    'shape': 'ramp', 'rate': 0.2},
    {'kw': 'good',          'unit_kw': '个',   'base': 970, 'amp': 0,    'period': 0,   'noise': 0,    'shape': 'ramp', 'rate': 0.19},
    {'kw': 'ng',            'unit_kw': '个',   'base': 30,  'amp': 0,    'period': 0,   'noise': 0,    'shape': 'ramp', 'rate': 0.01},
    {'kw': 'shot_count',    'unit_kw': '个',   'base': 500, 'amp': 0,    'period': 0,   'noise': 0,    'shape': 'ramp', 'rate': 0.1},
    {'kw': 'label_count',   'unit_kw': '个',   'base': 800, 'amp': 0,    'period': 0,   'noise': 0,    'shape': 'ramp', 'rate': 0.15},
    {'kw': 'palletizing_count','unit_kw': '个', 'base': 200,'amp': 0,    'period': 0,   'noise': 0,    'shape': 'ramp', 'rate': 0.05},
    {'kw': 'reject_count',  'unit_kw': '个',   'base': 5,   'amp': 0,    'period': 0,   'noise': 0,    'shape': 'ramp', 'rate': 0.002},
    {'kw': 'painted_count', 'unit_kw': '个',   'base': 300, 'amp': 0,    'period': 0,   'noise': 0,    'shape': 'ramp', 'rate': 0.08},
    {'kw': 'batch_count',   'unit_kw': '个',   'base': 15,  'amp': 0,    'period': 0,   'noise': 0,    'shape': 'ramp', 'rate': 0.001},
    {'kw': 'quantity',      'unit_kw': '个',   'base': 1000,'amp': 0,    'period': 0,   'noise': 0,    'shape': 'ramp', 'rate': 0.1},

    # --- 状态/枚举（整数） ---
    {'kw': 'status',        'unit_kw': '',      'base': 1,   'amp': 0,   'period': 0,   'noise': 0,    'shape': 'status'},
    {'kw': 'alarm',         'unit_kw': '',      'base': 0,   'amp': 0,   'period': 0,   'noise': 0,    'shape': 'constant'},
    {'kw': 'code',          'unit_kw': '',      'base': 0,   'amp': 0,   'period': 0,   'noise': 0,    'shape': 'constant'},

    # --- 特殊参数 ---
    {'kw': 'reflux_ratio',  'unit_kw': '',      'base': 3.5, 'amp': 0.5,  'period': 120, 'noise': 0.05, 'shape': 'sine'},
    {'kw': 'consumption',   'unit_kw': 'm³',    'base': 500, 'amp': 0,    'period': 0,   'noise': 0,    'shape': 'ramp', 'rate': 0.01},
    {'kw': 'consumption',   'unit_kw': 'kWh',   'base': 5000,'amp': 0,    'period': 0,   'noise': 0,    'shape': 'ramp', 'rate': 0.5},
    {'kw': 'air',           'unit_kw': 'm³',    'base': 200, 'amp': 0,    'period': 0,   'noise': 0,    'shape': 'ramp', 'rate': 0.005},
]

# 备用：按unit关键字兜底
_UNIT_RULES = [
    {'unit_kw': '°C',  'base': 50,  'amp': 20,  'period': 90,  'noise': 0.5,  'shape': 'sine'},
    {'unit_kw': 'MPa', 'base': 0.8, 'amp': 0.5, 'period': 60,  'noise': 0.01, 'shape': 'sine'},
    {'unit_kw': 'V',   'base': 220, 'amp': 15,  'period': 120, 'noise': 2.0,  'shape': 'sine'},
    {'unit_kw': 'A',   'base': 30,  'amp': 15,  'period': 60,  'noise': 1.0,  'shape': 'sine'},
    {'unit_kw': 'kW',  'base': 50,  'amp': 30,  'period': 45,  'noise': 2.0,  'shape': 'sine'},
    {'unit_kw': 'Hz',  'base': 50,  'amp': 0.3, 'period': 30,  'noise': 0.05, 'shape': 'sine'},
    {'unit_kw': 'RPM', 'base': 1500,'amp': 500, 'period': 30,  'noise': 10,   'shape': 'sine'},
    {'unit_kw': '%',   'base': 60,  'amp': 25,  'period': 120, 'noise': 1.0,  'shape': 'sine'},
    {'unit_kw': 'm',   'base': 1.8, 'amp': 0.8, 'period': 120, 'noise': 0.02, 'shape': 'sine'},
    {'unit_kw': 's',   'base': 4.5, 'amp': 1.0, 'period': 120, 'noise': 0.1,  'shape': 'sine'},
    {'unit_kw': 'ppm', 'base': 500, 'amp': 150, 'period': 120, 'noise': 10,   'shape': 'sine'},
]


def _find_rule(name: str, unit: str, data_type: str = 'uint16') -> dict[str, Any]:
    """
    从映射表查找匹配规则，先匹配name关键字，再匹配unit。
    如果都没匹配到，根据data_type和unit智能推断一个合理的规则。
    
    Args:
        name: 寄存器名称
        unit: 单位
        data_type: 数据类型 (uint16/int16/float32/int32/uint32)
    """
    name_lower = name.lower() if name else ''
    unit_lower = unit.lower() if unit else ''

    # 按name关键字匹配（精确unit匹配优先）
    for rule in _REGISTER_RULES:
        kw = rule['kw']
        unit_kw = rule.get('unit_kw', '')
        if kw in name_lower:
            if unit_kw and unit_kw.lower() in unit_lower:
                return rule
            elif not unit_kw:
                return rule

    # 按name关键字匹配（忽略unit）
    for rule in _REGISTER_RULES:
        if rule['kw'] in name_lower:
            return rule

    # 按unit兜底
    for rule in _UNIT_RULES:
        if rule['unit_kw'].lower() in unit_lower:
            return rule

    # ===== 智能推断：根据data_type和unit生成合理规则 =====
    # 这样新增设备时，即使寄存器名不在映射表中，也能生成合理的模拟数据
    return _infer_rule_from_type(name, unit, data_type)


def _infer_rule_from_type(name: str, unit: str, data_type: str) -> dict[str, Any]:
    """
    根据data_type和unit智能推断模拟规则。
    新增设备时的最后兜底，确保不会返回None。
    """
    name_lower = name.lower() if name else ''
    unit_lower = unit.lower() if unit else ''
    
    # 状态/枚举类型（通常是整数，值域小）
    status_keywords = ['status', 'state', 'mode', 'alarm', 'code', 'flag', 'error']
    for kw in status_keywords:
        if kw in name_lower:
            return {'kw': kw, 'base': 1, 'amp': 0, 'period': 0, 'noise': 0, 'shape': 'status'}
    
    # 计数类型（单调递增）
    count_keywords = ['count', 'total', 'sum', 'quantity', 'number', 'num']
    for kw in count_keywords:
        if kw in name_lower:
            return {'kw': kw, 'unit_kw': unit, 'base': 500, 'amp': 0, 'period': 0, 'noise': 0, 'shape': 'ramp', 'rate': 0.1}
    
    # 根据单位推断
    unit_infer_rules = {
        '°c': {'base': 50, 'amp': 20, 'period': 90, 'noise': 0.5},
        'c': {'base': 50, 'amp': 20, 'period': 90, 'noise': 0.5},
        'k': {'base': 300, 'amp': 20, 'period': 90, 'noise': 0.5},
        'mpa': {'base': 0.8, 'amp': 0.5, 'period': 60, 'noise': 0.01},
        'kpa': {'base': 800, 'amp': 200, 'period': 60, 'noise': 5.0},
        'pa': {'base': 101325, 'amp': 5000, 'period': 120, 'noise': 100},
        'v': {'base': 220, 'amp': 15, 'period': 120, 'noise': 2.0},
        'a': {'base': 30, 'amp': 15, 'period': 60, 'noise': 1.0},
        'kw': {'base': 50, 'amp': 30, 'period': 45, 'noise': 2.0},
        'w': {'base': 5000, 'amp': 2000, 'period': 40, 'noise': 100},
        'hz': {'base': 50, 'amp': 0.3, 'period': 30, 'noise': 0.05},
        'rpm': {'base': 1500, 'amp': 500, 'period': 30, 'noise': 10},
        'r/min': {'base': 1500, 'amp': 500, 'period': 30, 'noise': 10},
        'm/min': {'base': 12, 'amp': 5, 'period': 60, 'noise': 0.3},
        'mm/s': {'base': 80, 'amp': 30, 'period': 30, 'noise': 1.0},
        'm/s': {'base': 5, 'amp': 2, 'period': 30, 'noise': 0.1},
        'm³/h': {'base': 15, 'amp': 7, 'period': 45, 'noise': 0.3},
        'l/min': {'base': 50, 'amp': 20, 'period': 45, 'noise': 1.0},
        't/h': {'base': 5, 'amp': 2, 'period': 60, 'noise': 0.1},
        'm': {'base': 1.8, 'amp': 0.8, 'period': 120, 'noise': 0.02},
        'mm': {'base': 200, 'amp': 100, 'period': 60, 'noise': 1.0},
        'μm': {'base': 80, 'amp': 20, 'period': 60, 'noise': 2.0},
        'um': {'base': 80, 'amp': 20, 'period': 60, 'noise': 2.0},
        'kn': {'base': 1500, 'amp': 500, 'period': 30, 'noise': 20},
        'n·m': {'base': 18, 'amp': 4, 'period': 30, 'noise': 0.5},
        'n*m': {'base': 18, 'amp': 4, 'period': 30, 'noise': 0.5},
        '%': {'base': 60, 'amp': 25, 'period': 120, 'noise': 1.0},
        '%rh': {'base': 62, 'amp': 10, 'period': 600, 'noise': 1.5},
        'ppm': {'base': 500, 'amp': 150, 'period': 120, 'noise': 10},
        'μg/m³': {'base': 35, 'amp': 15, 'period': 600, 'noise': 5},
        'ntu': {'base': 5, 'amp': 3, 'period': 180, 'noise': 0.3},
        'mg/l': {'base': 8.0, 'amp': 2.0, 'period': 120, 'noise': 0.2},
        'μs/cm': {'base': 500, 'amp': 100, 'period': 300, 'noise': 10},
        'kwh': {'base': 5000, 'amp': 0, 'period': 0, 'noise': 0, 'shape': 'ramp', 'rate': 0.5},
        'mwh': {'base': 500, 'amp': 0, 'period': 0, 'noise': 0, 'shape': 'ramp', 'rate': 0.001},
        'kvar': {'base': 10, 'amp': 8, 'period': 60, 'noise': 1.0},
        'kva': {'base': 55, 'amp': 25, 'period': 50, 'noise': 2.0},
        's': {'base': 4.5, 'amp': 1.0, 'period': 120, 'noise': 0.1},
        '个': {'base': 500, 'amp': 0, 'period': 0, 'noise': 0, 'shape': 'ramp', 'rate': 0.1},
        'pcs': {'base': 500, 'amp': 0, 'period': 0, 'noise': 0, 'shape': 'ramp', 'rate': 0.1},
        't': {'base': 50, 'amp': 0, 'period': 0, 'noise': 0, 'shape': 'ramp', 'rate': 0.01},
        'm³': {'base': 500, 'amp': 0, 'period': 0, 'noise': 0, 'shape': 'ramp', 'rate': 0.01},
    }
    
    if unit_lower in unit_infer_rules:
        params = unit_infer_rules[unit_lower]
        return {'kw': name_lower, 'unit_kw': unit, 'shape': 'sine', **params}
    
    # 最终兜底：根据data_type生成
    if data_type == 'float32':
        return {'kw': name_lower, 'base': 50.0, 'amp': 20.0, 'period': 60, 'noise': 1.0, 'shape': 'sine'}
    elif data_type in ('int32', 'uint32'):
        return {'kw': name_lower, 'base': 1000, 'amp': 500, 'period': 60, 'noise': 10, 'shape': 'sine'}
    else:  # uint16, int16
        return {'kw': name_lower, 'base': 100, 'amp': 50, 'period': 60, 'noise': 5, 'shape': 'sine'}


def _generate_value(rule: dict[str, Any], t: float, phase_offset: float = 0.0) -> float:
    """根据规则和时间生成模拟值"""
    shape = rule.get('shape', 'sine')
    base = rule['base']
    amp = rule.get('amp', 0)
    period = rule.get('period', 60)
    noise = rule.get('noise', 0)

    if shape == 'sine' and period > 0:
        value = base + amp * math.sin(t / period + phase_offset)
        if noise > 0:
            value += random.gauss(0, noise)
    elif shape == 'ramp':
        rate = rule.get('rate', 0.01)
        value = base + t * rate
    elif shape == 'status':
        # 交替状态: 大部分时间运行(1), 偶尔故障(2)或停机(0)
        cycle = int(t) % 60
        if cycle < 55:
            value = 1
        elif cycle < 58:
            value = 2
        else:
            value = 0
    elif shape == 'constant':
        value = base
    else:
        value = base
        if noise > 0:
            value += random.gauss(0, noise)

    return value


# ==================== 模拟Modbus客户端 ====================

class SimulatedModbusClient(ModbusClientInterface):
    """
    模拟Modbus客户端（配置驱动版）
    从寄存器配置的 name/unit/data_type 自动推断模拟数据范围
    新增设备只需改YAML，永远不需要改代码
    """

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.connected = False
        self.start_time = time.time()

        # 预缓存每个寄存器的模拟规则（只算一次）
        self._rules_cache = {}
        self._register_types = {}  # address -> data_type, scale
        for reg in config.get('registers', []):
            data_type = reg.get('data_type', 'uint16')
            self._rules_cache[reg['address']] = _find_rule(
                reg.get('name', ''), reg.get('unit', ''), data_type
            )
            self._register_types[reg['address']] = {
                'data_type': data_type,
                'scale': reg.get('scale', 1.0)
            }

        self.stats: dict[str, Any] = {
            'total_reads': 0, 'successful_reads': 0, 'failed_reads': 0,
            'last_read_time': None, 'last_error': None
        }

    def connect(self) -> bool:
        self.connected = True
        logger.info(f"[模拟] 设备 {self.device_name} 连接成功")
        return True

    def disconnect(self):
        self.connected = False
        logger.info(f"[模拟] 设备 {self.device_name} 已断开")

    def read_holding_registers(self, address: int, count: int,
                               slave_id: int | None = None) -> list[int] | None:
        if not self.connected:
            return None

        self.stats['total_reads'] += 1
        self.stats['successful_reads'] += 1
        self.stats['last_read_time'] = time.time()

        t = time.time() - self.start_time

        # 从缓存查找规则
        rule = self._rules_cache.get(address)
        if rule is None:
            # 没有匹配规则，按寄存器读取数量猜测类型
            if count >= 4:
                # float64: 返回4个寄存器
                raw = struct.pack('>d', float(random.uniform(0, 100)))
                return [struct.unpack('>H', raw[i*2:(i+1)*2])[0] for i in range(4)]
            elif count >= 2:
                # float32/int32/uint32: 返回2个寄存器
                raw = struct.pack('>f', float(random.uniform(0, 100)))
                return [struct.unpack('>H', raw[0:2])[0], struct.unpack('>H', raw[2:4])[0]]
            return [random.randint(0, 1000)]

        # 用地址做相位偏移，避免同类型寄存器曲线完全一致
        phase = address * 0.3
        value = _generate_value(rule, t, phase)

        # 根据data_type决定编码方式
        reg_type = self._register_types.get(address, {})
        data_type = reg_type.get('data_type', 'uint16')
        scale = reg_type.get('scale', 1.0)

        # 模拟值是实际值，需要反算为原始寄存器值再编码
        raw_value = value / scale if scale != 0 else value

        if data_type == 'float32':
            raw = struct.pack('>f', float(value))
            return [struct.unpack('>H', raw[0:2])[0], struct.unpack('>H', raw[2:4])[0]]
        elif data_type in ('int32', 'uint32'):
            int_val = int(round(raw_value))
            raw = struct.pack('>i' if data_type == 'int32' else '>I', int_val)
            return [struct.unpack('>H', raw[0:2])[0], struct.unpack('>H', raw[2:4])[0]]
        elif count >= 2:
            # 兜底：未知类型但count>=2，按float32处理
            raw = struct.pack('>f', float(value))
            return [struct.unpack('>H', raw[0:2])[0], struct.unpack('>H', raw[2:4])[0]]
        else:
            # uint16 / int16
            return [int(round(value)) & 0xFFFF]

    def decode_float32(self, registers: list[int]) -> float:
        raw = (registers[0] << 16) | registers[1]
        return struct.unpack('>f', struct.pack('>I', raw))[0]

    def decode_float64(self, registers: list[int]) -> float:
        raw = struct.pack('>HHHH', registers[0], registers[1], registers[2], registers[3])
        return struct.unpack('>d', raw)[0]

    def decode_uint16(self, register: int) -> int:
        return register & 0xFFFF

    def decode_int16(self, register: int) -> int:
        if register & 0x8000:
            return register - 0x10000
        return register

    def decode_int32(self, registers: list[int]) -> int:
        raw = (registers[0] << 16) | registers[1]
        if raw & 0x80000000:
            raw -= 0x100000000
        return raw

    def decode_uint32(self, registers: list[int]) -> int:
        return (registers[0] << 16) | registers[1]

    def write_single_register(self, address: int, value: int, slave_id: int | None = None) -> bool:
        if not self.connected:
            return False
        logger.info(f"[模拟] 设备 {self.device_name} 写入寄存器: address={address}, value={value}")
        return True

    def write_single_coil(self, address: int, value: bool, slave_id: int | None = None) -> bool:
        if not self.connected:
            return False
        logger.info(f"[模拟] 设备 {self.device_name} 写入线圈: address={address}, value={value}")
        return True

    def read_coils(self, address: int, count: int, slave_id: int | None = None) -> list[bool] | None:
        if not self.connected:
            return None
        self.stats['total_reads'] += 1
        self.stats['successful_reads'] += 1
        return [random.choice([True, False]) for _ in range(count)]

    def read_discrete_inputs(self, address: int, count: int, slave_id: int | None = None) -> list[bool] | None:
        if not self.connected:
            return None
        self.stats['total_reads'] += 1
        self.stats['successful_reads'] += 1
        return [random.choice([True, False]) for _ in range(count)]

    def read_input_registers(self, address: int, count: int, slave_id: int | None = None) -> list[int] | None:
        """读取输入寄存器（与保持寄存器逻辑相同）"""
        return self.read_holding_registers(address, count, slave_id)

    def get_stats(self) -> dict[str, Any]:
        return {'device_id': self.device_id, 'device_name': self.device_name,
                'connected': self.connected, **self.stats}


# ==================== 模拟OPC UA客户端 ====================

class SimulatedOPCUAClient(PushClientInterface):
    """
    模拟OPC UA客户端（配置驱动版）
    从节点配置的 name/unit 自动推断模拟数据
    """

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.connected = False
        self.start_time = time.time()

        self.node_configs = config.get('nodes', [])
        self.latest_data: dict[str, dict[str, Any]] = {}
        self._data_callbacks: list[Callable[..., Any]] = []
        self._push_thread: threading.Thread | None = None
        self._running = False

        # 预缓存规则
        self._rules_cache = {}
        for i, node in enumerate(self.node_configs):
            name = node.get('name', node.get('node_id', ''))
            self._rules_cache[name] = _find_rule(name, node.get('unit', ''), 'float32')

        self.stats: dict[str, Any] = {
            'connected_since': None, 'nodes_subscribed': 0,
            'data_updates': 0, 'errors': 0, 'last_error': None
        }

    def add_data_callback(self, callback: Callable[..., Any]):
        self._data_callbacks.append(callback)

    def connect(self) -> bool:
        self.connected = True
        self._running = True
        self.stats['connected_since'] = datetime.now().isoformat()
        self.stats['nodes_subscribed'] = len(self.node_configs)
        logger.info(f"[模拟] OPC UA设备 {self.device_name} 连接成功")

        self._generate_data()
        self._push_thread = threading.Thread(target=self._push_loop, daemon=True)
        self._push_thread.start()
        return True

    def disconnect(self):
        self._running = False
        self.connected = False
        logger.info(f"[模拟] OPC UA设备 {self.device_name} 已断开")

    def _push_loop(self):
        while self._running and self.connected:
            time.sleep(2)
            if self._running and self.connected:
                self._generate_data()

    def get_latest_data(self) -> dict[str, dict[str, Any]]:
        if self.connected:
            self._generate_data()
        return dict(self.latest_data)

    def get_stats(self) -> dict[str, Any]:
        return {'device_id': self.device_id, 'device_name': self.device_name,
                'connected': self.connected, **self.stats}

    def _generate_data(self):
        t = time.time() - self.start_time

        for i, node_cfg in enumerate(self.node_configs):
            name = node_cfg.get('name', node_cfg.get('node_id', 'unknown'))
            unit = node_cfg.get('unit', '')

            rule = self._rules_cache.get(name)
            if rule:
                value = _generate_value(rule, t, i * 0.5)
            else:
                value = 50 + 30 * math.sin(t / 20) + random.gauss(0, 2)

            value = round(value, 2)
            self.latest_data[name] = {
                'value': value, 'unit': unit,
                'timestamp': datetime.now().isoformat(),
                'quality': 'good', 'node_id': node_cfg.get('node_id', '')
            }

            self.stats['data_updates'] += 1

            for callback in self._data_callbacks:
                try:
                    callback(self.device_id, name, value, unit)
                except Exception as e:
                    logger.error(f"[模拟] OPC UA回调异常: {e}")

    def datachange_notification(self, node, val, data):
        pass

    def event_notification(self, event):
        pass

    def status_change_notification(self, status):
        pass


# ==================== 模拟MQTT客户端 ====================

class SimulatedMQTTClient(PushClientInterface):
    """
    模拟MQTT客户端（配置驱动版）
    从topics配置的 name/unit 自动推断模拟数据
    """

    def __init__(self, config: dict[str, Any] | None = None, **kwargs):
        config = config or kwargs
        super().__init__(config)
        self.broker_host = config.get('host', 'localhost')
        self.broker_port = config.get('port', 1883)
        self.connected = False
        self.start_time = time.time()

        self.topics_config = self.config.get('topics', [])
        self.latest_data: dict[str, dict[str, Any]] = {}
        self._data_callbacks: list[Callable[..., Any]] = []
        self._subscriptions: dict[str, int] = {}
        self._push_thread: threading.Thread | None = None
        self._running = False

        # 预缓存规则
        self._rules_cache = {}
        for topic_cfg in self.topics_config:
            name = topic_cfg.get('name', '')
            self._rules_cache[name] = _find_rule(name, topic_cfg.get('unit', ''), 'float32')

        self.stats: dict[str, Any] = {
            'messages_received': 0, 'messages_parsed': 0,
            'parse_errors': 0, 'connected_since': None,
            'last_message_time': None
        }

    def add_data_callback(self, callback: Callable[..., Any]):
        self._data_callbacks.append(callback)

    def connect(self) -> bool:
        self.connected = True
        self._running = True
        self.stats['connected_since'] = datetime.now().isoformat()
        logger.info(f"[模拟] MQTT设备 {self.device_name} 连接成功")

        for topic_cfg in self.topics_config:
            topic = topic_cfg.get('topic', '')
            if topic:
                self._subscriptions[topic] = topic_cfg.get('qos', 1)

        self._generate_data()
        self._push_thread = threading.Thread(target=self._push_loop, daemon=True)
        self._push_thread.start()
        return True

    def disconnect(self):
        self._running = False
        self.connected = False
        logger.info(f"[模拟] MQTT设备 {self.device_name} 已断开")

    def subscribe(self, topic: str, qos: int = 1) -> bool:
        self._subscriptions[topic] = qos
        return True

    def unsubscribe(self, topic: str):
        self._subscriptions.pop(topic, None)

    def get_latest_data(self) -> dict[str, dict[str, Any]]:
        return dict(self.latest_data)

    def get_stats(self) -> dict[str, Any]:
        return {'device_id': self.device_id, 'device_name': self.device_name,
                'connected': self.connected, **self.stats}

    def get_status(self) -> dict[str, Any]:
        return {
            'connected': self.connected,
            'broker': f'{self.broker_host}:{self.broker_port}',
            'subscriptions': list(self._subscriptions.keys()),
            'stats': self.stats.copy()
        }

    def _push_loop(self):
        while self._running and self.connected:
            time.sleep(3)
            if self._running and self.connected:
                self._generate_data()

    def _generate_data(self):
        t = time.time() - self.start_time

        for i, topic_cfg in enumerate(self.topics_config):
            name = topic_cfg.get('name', 'unknown')
            unit = topic_cfg.get('unit', '')

            rule = self._rules_cache.get(name)
            if rule:
                value = _generate_value(rule, t, i * 0.7)
            else:
                value = 50 + 30 * math.sin(t / 20) + random.gauss(0, 2)

            value = round(value, 2)
            self.stats['messages_received'] += 1
            self.stats['messages_parsed'] += 1
            self.stats['last_message_time'] = datetime.now().isoformat()

            self.latest_data[name] = {
                'value': value, 'unit': unit,
                'timestamp': datetime.now().isoformat(),
                'quality': 'good', 'device_id': self.device_id
            }

            for callback in self._data_callbacks:
                try:
                    callback(self.device_id, name, value, unit)
                except Exception as e:
                    logger.error(f"[模拟] MQTT回调异常: {e}")


# ==================== 模拟REST客户端 ====================

class SimulatedRESTClient(PushClientInterface):
    """
    模拟REST HTTP客户端（配置驱动版）
    从endpoints配置的 name/unit 自动推断模拟数据
    """

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.base_url = config.get('base_url', 'http://localhost')
        self.connected = False
        self.start_time = time.time()

        self.endpoints = config.get('endpoints', [])
        self.latest_data: dict[str, dict[str, Any]] = {}
        self._data_callbacks: list[Callable[..., Any]] = []

        # 预缓存规则
        self._rules_cache = {}
        for ep in self.endpoints:
            name = ep.get('name', '')
            self._rules_cache[name] = _find_rule(name, ep.get('unit', ''), 'float32')

        self.stats: dict[str, Any] = {
            'total_requests': 0, 'successful_requests': 0,
            'failed_requests': 0, 'connected_since': None,
            'last_request_time': None, 'last_error': None
        }

    def add_data_callback(self, callback: Callable[..., Any]):
        self._data_callbacks.append(callback)

    def connect(self) -> bool:
        self.connected = True
        self.stats['connected_since'] = datetime.now().isoformat()
        logger.info(f"[模拟] REST设备 {self.device_name} 连接成功")
        self._generate_data()
        return True

    def disconnect(self):
        self.connected = False
        logger.info(f"[模拟] REST设备 {self.device_name} 已断开")

    def get_latest_data(self) -> dict[str, dict[str, Any]]:
        if self.connected:
            self._generate_data()
        return dict(self.latest_data)

    def read_endpoint(self, endpoint_config: dict[str, Any]) -> Any:
        self.stats['total_requests'] += 1
        self.stats['successful_requests'] += 1
        self.stats['last_request_time'] = datetime.now().isoformat()

        name = endpoint_config.get('name', 'unknown')
        t = time.time() - self.start_time

        rule = self._rules_cache.get(name)
        if rule:
            return round(_generate_value(rule, t, hash(name) % 10 * 0.3), 2)
        else:
            return round(50 + 30 * math.sin(t / 20) + random.gauss(0, 2), 2)

    def write_endpoint(self, endpoint_config: dict[str, Any], value: Any) -> bool:
        logger.info(f"[模拟] REST写入: {endpoint_config.get('name')} = {value}")
        return True

    def get_stats(self) -> dict[str, Any]:
        return {'device_id': self.device_id, 'device_name': self.device_name,
                'connected': self.connected, **self.stats}

    def _generate_data(self):
        t = time.time() - self.start_time

        for i, ep in enumerate(self.endpoints):
            name = ep.get('name', 'unknown')
            unit = ep.get('unit', '')

            rule = self._rules_cache.get(name)
            if rule:
                value = _generate_value(rule, t, i * 0.4)
            else:
                value = 50 + 30 * math.sin(t / 20) + random.gauss(0, 2)

            value = round(value, 2)
            self.latest_data[name] = {
                'value': value, 'unit': unit,
                'timestamp': datetime.now().isoformat(),
                'quality': 'good', 'endpoint': ep.get('path', ''),
                'device_id': self.device_id
            }

            for callback in self._data_callbacks:
                try:
                    callback(self.device_id, name, value, unit)
                except Exception as e:
                    logger.error(f"[模拟] REST回调异常: {e}")
