"""
降级策略管理器 - 服务降级配置
工业SCADA场景: 当系统部分组件故障时，通过降级策略维持核心功能运行。

降级等级:
  NORMAL    → 全功能运行
  LIGHT     → 关闭非核心功能（报表生成、趋势对比等）
  MODERATE  → 关闭智能分析，保留数据采集和报警
  SEVERE    → 仅保留核心报警和设备控制
  EMERGENCY → 最小化运行，仅报警通知
"""

import threading
import time
import logging
from enum import IntEnum
from typing import Dict, Any, Callable, List, Optional, Set
from datetime import datetime

logger = logging.getLogger(__name__)


class DegradationLevel(IntEnum):
    """降级等级（数值越大降级越深）"""
    NORMAL = 0      # 全功能
    LIGHT = 1       # 轻度降级
    MODERATE = 2    # 中度降级
    SEVERE = 3      # 重度降级
    EMERGENCY = 4   # 紧急降级


class FeatureDisabledError(RuntimeError):
    """功能因系统降级被禁用。

    继承 RuntimeError 而不是自定义基类，是为了让现有那些
    `except Exception` / `except RuntimeError` 的兜底能接住它；
    但 API 层应当**先**捕获它并转成 503，而不是落到通用 500 分支。
    """

    def __init__(self, feature_name: str, level_name: str):
        self.feature_name = feature_name
        self.level_name = level_name
        super().__init__(
            f"功能 {feature_name} 因系统降级（当前 {level_name}）已禁用"
        )


# 各降级等级下被禁用的功能模块
DISABLED_FEATURES = {
    DegradationLevel.NORMAL: set(),
    DegradationLevel.LIGHT: {
        'report_generator',     # 报表生成
        'trend_comparison',     # 趋势对比
        'device_topology',      # 设备拓扑
        'energy_optimizer',     # 能耗优化
    },
    DegradationLevel.MODERATE: {
        'report_generator',
        'trend_comparison',
        'device_topology',
        'energy_optimizer',
        'spc_analyzer',         # SPC分析
        'oee_calculator',       # OEE计算
        'vibration_analyzer',   # 振动分析
        'predictive_maintenance',  # 预测性维护
    },
    DegradationLevel.SEVERE: {
        'report_generator',
        'trend_comparison',
        'device_topology',
        'energy_optimizer',
        'spc_analyzer',
        'oee_calculator',
        'vibration_analyzer',
        'predictive_maintenance',
        'data_export',          # 数据导出
        'config_hot_reload',    # 配置热重载
        'edge_decision',        # 边缘决策
    },
    DegradationLevel.EMERGENCY: {
        'report_generator',
        'trend_comparison',
        'device_topology',
        'energy_optimizer',
        'spc_analyzer',
        'oee_calculator',
        'vibration_analyzer',
        'predictive_maintenance',
        'data_export',
        'config_hot_reload',
        'edge_decision',
        'data_archive',         # 数据归档
        'data_lifecycle',       # 数据生命周期
        'auto_ops',             # 自动运维
        'performance_monitor',  # 性能监控
    },
}


# 功能名 → 真实消费方（文件:函数）。
#
# 为什么需要这张表：`DISABLED_FEATURES` 只是**声明**「降级时该停什么」，
# 它自己不会让任何东西停下来。一个功能名如果没有任何代码去查
# `is_feature_enabled(这个名字)`，那它在界面上显示「已禁用」就是**假的** ——
# 运维在事故中手动降级后以为资源被释放了，实际一点没省。
#
# 本表登记「确实接了闸门」的功能，`ENFORCED_FEATURES` 由它派生；
# 未登记的名字一律进 `get_status()['advisory_disabled_features']`，明确告诉
# 调用方「这只是声明，当前没有代码消费」。tests/test_degradation_enforcement.py
# 会逐条核对这里的路径与函数名真实存在，防止表本身腐化。
FEATURE_CONSUMERS = {
    'predictive_maintenance': '采集层/data_collector.py:_dispatch_intelligence',
    'spc_analyzer':           '采集层/data_collector.py:_dispatch_intelligence',
    'oee_calculator':         '采集层/data_collector.py:_dispatch_intelligence',
    'energy_optimizer':       '采集层/data_collector.py:_dispatch_intelligence',
    'edge_decision':          '采集层/data_collector.py:_dispatch_intelligence',
    'vibration_analyzer':     '采集层/data_collector.py:_dispatch_intelligence',
    'data_export':            '展示层/api/api_data.py:_require_feature_export',
    'data_archive':           'core/maintenance.py:_data_archive',
}

# 降级后会**真的**停止的功能（由 FEATURE_CONSUMERS 派生，不要手工维护两份）
ENFORCED_FEATURES = frozenset(FEATURE_CONSUMERS)


# 各降级等级下降低采样/推送频率的倍数
FREQUENCY_MULTIPLIERS = {
    DegradationLevel.NORMAL: 1.0,
    DegradationLevel.LIGHT: 1.0,
    DegradationLevel.MODERATE: 2.0,    # 采集频率降为1/2
    DegradationLevel.SEVERE: 5.0,      # 采集频率降为1/5
    DegradationLevel.EMERGENCY: 10.0,  # 采集频率降为1/10
}


class DegradationManager:
    """
    降级策略管理器

    管理系统降级等级、自动升降级决策、功能可用性查询。

    使用示例:
        manager = DegradationManager()
        manager.start()

        # 查询某功能是否可用
        if manager.is_feature_enabled('spc_analyzer'):
            spc_results = spc_analyzer.analyze(data)

        # 手动降级
        manager.manual_degrade(DegradationLevel.MODERATE, reason="数据库连接池耗尽")

        # 恢复
        manager.manual_restore()
    """

    def __init__(self):
        self._level = DegradationLevel.NORMAL
        self._lock = threading.RLock()

        # 手动降级标志（手动降级时不自动升降级）
        self._manual_override = False
        self._manual_reason = ""

        # 自动降级触发器
        self._triggers: List[Callable[[], Optional[int]]] = []

        # 变更历史
        self._history: List[Dict[str, Any]] = []
        self._max_history = 100

        # 监控
        self._stop_event = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._check_interval = 15.0

    @property
    def level(self) -> int:
        """当前降级等级"""
        with self._lock:
            return self._level

    @property
    def level_name(self) -> str:
        """当前降级等级名称"""
        with self._lock:
            return DegradationLevel(self._level).name

    def start(self):
        """启动自动降级监控"""
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._stop_event.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True,
            name="degradation-manager",
        )
        self._monitor_thread.start()
        logger.info("降级策略管理器已启动")

    def stop(self):
        """停止监控"""
        self._stop_event.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=self._check_interval + 5)
        self._monitor_thread = None

    def register_trigger(self, trigger: Callable[[], Optional[int]]):
        """
        注册自动降级触发器

        触发器返回:
          - DegradationLevel 值: 要求降到该等级
          - None: 不要求变更
        """
        with self._lock:
            self._triggers.append(trigger)
        logger.info("注册降级触发器，当前共 %d 个", len(self._triggers))

    def _monitor_loop(self):
        """自动降级监控循环"""
        while not self._stop_event.wait(self._check_interval):
            try:
                self._auto_evaluate()
            except Exception as e:
                logger.error("降级评估异常: %s", e)

    def _auto_evaluate(self):
        """自动评估是否需要升降级"""
        if self._manual_override:
            return

        # 收集所有触发器的建议
        requested_level = DegradationLevel.NORMAL
        with self._lock:
            triggers = list(self._triggers)

        for trigger in triggers:
            try:
                suggestion = trigger()
                if suggestion is not None and suggestion > requested_level:
                    requested_level = suggestion
            except Exception as e:
                logger.debug("触发器执行异常: %s", e)

        with self._lock:
            if requested_level != self._level:
                old = self._level
                self._set_level(requested_level, reason="自动触发")
                logger.warning(
                    "自动降级变更: %s → %s",
                    DegradationLevel(old).name,
                    DegradationLevel(requested_level).name,
                )

    def _set_level(self, new_level: int, reason: str = ""):
        """设置降级等级（必须在锁内调用）"""
        old = self._level
        self._level = new_level
        entry = {
            'time': datetime.now().isoformat(),
            'from': DegradationLevel(old).name,
            'to': DegradationLevel(new_level).name,
            'reason': reason,
            'manual': self._manual_override,
        }
        self._history.append(entry)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

    def manual_degrade(self, level: int, reason: str = ""):
        """
        手动降级

        手动降级后自动升降级暂停，直到调用 manual_restore()。

        Args:
            level: 降级等级（DegradationLevel值）
            reason: 降级原因
        """
        with self._lock:
            self._manual_override = True
            self._manual_reason = reason
            old = self._level
            self._set_level(level, reason=f"手动: {reason}")
        logger.warning(
            "手动降级: %s → %s (原因: %s)",
            DegradationLevel(old).name,
            DegradationLevel(level).name,
            reason,
        )

    def manual_restore(self):
        """恢复自动管理"""
        with self._lock:
            self._manual_override = False
            self._manual_reason = ""
        logger.info("恢复自动降级管理")

    def is_feature_enabled(self, feature_name: str) -> bool:
        """检查某功能在当前降级等级下是否可用"""
        with self._lock:
            disabled = DISABLED_FEATURES.get(self._level, set())
            return feature_name not in disabled

    def get_frequency_multiplier(self) -> float:
        """获取当前采集频率倍数（>1 表示降低频率）"""
        with self._lock:
            return FREQUENCY_MULTIPLIERS.get(self._level, 1.0)

    def get_disabled_features(self) -> Set[str]:
        """获取当前被禁用的功能列表"""
        with self._lock:
            return DISABLED_FEATURES.get(self._level, set()).copy()

    def get_enforced_disabled_features(self) -> Set[str]:
        """当前被禁用、且**真的有消费方**的功能（降级后确实停止）"""
        return self.get_disabled_features() & ENFORCED_FEATURES

    def get_advisory_disabled_features(self) -> Set[str]:
        """当前被声明禁用、但没有任何消费方的功能（只是名义上禁用）"""
        return self.get_disabled_features() - ENFORCED_FEATURES

    def require_feature(self, feature_name: str):
        """
        功能被降级禁用时抛 `FeatureDisabledError`。

        API 层用：把「降级了」如实回给调用方，而不是继续算出一份
        结果让人误以为功能正常。
        """
        if not self.is_feature_enabled(feature_name):
            raise FeatureDisabledError(feature_name, self.level_name)

    def get_status(self) -> Dict[str, Any]:
        """获取降级管理器状态"""
        with self._lock:
            disabled = DISABLED_FEATURES.get(self._level, set())
            return {
                'level': self._level,
                'level_name': DegradationLevel(self._level).name,
                'manual_override': self._manual_override,
                'manual_reason': self._manual_reason,
                'disabled_features': sorted(disabled),
                # 「声明禁用」与「真的禁用」必须分开报 —— 只报一个 disabled_features
                # 会让运维以为 data_export / report_generator 之类降级后真的停了。
                'enforced_disabled_features': sorted(disabled & ENFORCED_FEATURES),
                'advisory_disabled_features': sorted(disabled - ENFORCED_FEATURES),
                'enforced_features': sorted(ENFORCED_FEATURES),
                'frequency_multiplier': FREQUENCY_MULTIPLIERS.get(self._level, 1.0),
                'trigger_count': len(self._triggers),
                'recent_changes': self._history[-10:],
                'running': self._monitor_thread is not None and self._monitor_thread.is_alive(),
            }

    def get_all_levels_info(self) -> Dict[str, Any]:
        """获取所有降级等级信息（API展示用）"""
        result = {}
        for level in DegradationLevel:
            result[level.name] = {
                'value': level.value,
                'disabled_features': sorted(DISABLED_FEATURES.get(level, set())),
                'frequency_multiplier': FREQUENCY_MULTIPLIERS.get(level, 1.0),
            }
        return result


# 全局降级管理器
degradation_manager = DegradationManager()
