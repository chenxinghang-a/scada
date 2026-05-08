"""
智能层 — 工业4.0核心模块
=============================
- 预测性维护 (Predictive Maintenance)
- OEE设备综合效率 (Overall Equipment Effectiveness)
- SPC统计过程控制 (Statistical Process Control)
- 能源管理 (Energy Management)
- 边缘决策引擎 (Edge Decision Engine)
- 设备控制安全 (Device Control Safety)
- TDengine适配器 (TSDB Adapter) — 连接智能层与TDengine
"""

from .predictive_maintenance import PredictiveMaintenance
from .oee_calculator import OEECalculator
from .spc_analyzer import SPCAnalyzer
from .energy_manager import EnergyManager
from .edge_decision import EdgeDecisionEngine
from .device_control import DeviceControlSafety

# TSDBAdapter依赖timeseries模块，可选导入
try:
    from .tsdb_adapter import TSDBAdapter, RealtimeDataBridge
    _has_tsdb = True
except ImportError:
    _has_tsdb = False

__all__ = [
    'PredictiveMaintenance',
    'OEECalculator',
    'SPCAnalyzer',
    'EnergyManager',
    'EdgeDecisionEngine',
    'DeviceControlSafety',
]

if _has_tsdb:
    __all__.extend(['TSDBAdapter', 'RealtimeDataBridge'])
