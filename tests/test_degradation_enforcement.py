"""
降级管理器「接线」守卫（round 238）

背景：`DISABLED_FEATURES` 只是一张**声明**表。在 round 238 之前，
`is_feature_enabled()` 全仓库只有一个调用点，就是只读展示 API
（`展示层/api/api_resilience.py`），`get_frequency_multiplier()` 更是
**零消费方**。也就是说：

    运维在事故中把系统降级到 EMERGENCY，界面显示「仅保留报警通知」，
    但采集、智能分析、导出、归档全都照常跑 —— 降级没省下任何资源。

本文件守住三件事：
1. `FEATURE_CONSUMERS` 登记的消费方真实存在（防止表本身腐化）；
2. 智能层分发真的按降级等级跳过模块，且**安全联锁永不被跳过**；
3. 采集频率倍数真的作用在采集间隔上。
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.degradation_manager import (  # noqa: E402
    DISABLED_FEATURES,
    ENFORCED_FEATURES,
    FEATURE_CONSUMERS,
    DegradationLevel,
    degradation_manager,
)
from 采集层.data_collector import DataCollector  # noqa: E402


@pytest.fixture
def level_guard():
    """保存 / 还原全局单例的降级状态（测试之间不串味）"""
    saved = (degradation_manager._level, degradation_manager._manual_override,
             degradation_manager._manual_reason)
    yield degradation_manager
    with degradation_manager._lock:
        degradation_manager._level = saved[0]
        degradation_manager._manual_override = saved[1]
        degradation_manager._manual_reason = saved[2]


def _set_level(mgr, level):
    with mgr._lock:
        mgr._manual_override = True
        mgr._set_level(level, reason="test")


class _Recorder:
    """记录被调用的智能模块（含调用次数）"""

    def __init__(self):
        self.calls = []
        self.kwargs = []

    def feed_data(self, *a, **kw):
        self.calls.append(('feed_data', a))
        self.kwargs.append(kw)

    def update_data(self, *a, **kw):
        self.calls.append(('update_data', a))
        self.kwargs.append(kw)

    def feed_power_data(self, *a, **kw):
        self.calls.append(('feed_power_data', a))
        self.kwargs.append(kw)

    def check_interlocks(self, *a, **kw):
        self.calls.append(('check_interlocks', a))
        self.kwargs.append(kw)

    def update_device_state(self, *a, **kw):
        self.calls.append(('update_device_state', a))
        self.kwargs.append(kw)

    def record_production(self, *a, **kw):
        self.calls.append(('record_production', a))
        self.kwargs.append(kw)

    def get_health_scores(self):
        """采集器算动态间隔时会调它；返回空 = 没有健康数据，走状态分支。"""
        return {}


def _make_collector():
    """构造一个只带智能层桩的采集器（不启动线程、不连库）"""
    from unittest.mock import MagicMock

    dm = MagicMock()
    dm.get_device_status.return_value = {'stopped': False, 'error': False, 'stats': {'state': 'idle'}}
    collector = DataCollector(dm, MagicMock(), MagicMock())
    collector.predictive_maintenance = _Recorder()
    collector.edge_decision = _Recorder()
    collector.energy_manager = _Recorder()
    collector.spc_analyzer = _Recorder()
    collector.vibration_analyzer = _Recorder()
    collector.oee_calculator = _Recorder()
    collector.device_control = _Recorder()
    return collector


def _data(register_name='spindle_temperature', value=12.5):
    from datetime import datetime
    return {
        'device_id': 'DEV1',
        'register_name': register_name,
        'value': value,
        'timestamp': datetime.now(),
    }


# ============================================================
# 1. 静态守卫：消费方表不能腐化
# ============================================================

class TestConsumerRegistry:
    def test_registry_not_empty(self):
        assert FEATURE_CONSUMERS, "FEATURE_CONSUMERS 为空 = 降级仍然是纯装饰品"

    def test_enforced_derived_from_registry(self):
        assert ENFORCED_FEATURES == frozenset(FEATURE_CONSUMERS)

    @pytest.mark.parametrize('feature,target', sorted(FEATURE_CONSUMERS.items()))
    def test_consumer_file_and_symbol_exist(self, feature, target):
        """登记了消费方就必须真的有这个文件 + 这个函数 + 这个功能名。

        这条是防「表和代码各说各话」的核心闸门：谁把接线删了但没改表，
        或者改了功能名没改表，这里立刻红。
        """
        rel_path, func_name = target.split(':')
        full = os.path.join(PROJECT_ROOT, rel_path.replace('/', os.sep))
        assert os.path.isfile(full), f"消费方文件不存在: {rel_path}"

        with open(full, encoding='utf-8') as fh:
            source = fh.read()

        assert f'def {func_name}' in source, \
            f"{rel_path} 里找不到函数 {func_name}（表已过期）"
        assert feature in source, \
            f"{rel_path} 里找不到功能名 {feature!r}（表已过期）"

    @pytest.mark.parametrize('feature', sorted(ENFORCED_FEATURES))
    def test_enforced_feature_is_actually_disabled_somewhere(self, feature):
        """接线的功能必须真的出现在某个降级等级的禁用清单里。

        否则「接线」是空的：代码查了闸门，但任何等级都不会关它。
        """
        levels_disabling = [lvl.name for lvl, names in DISABLED_FEATURES.items() if feature in names]
        assert levels_disabling, f"{feature} 已接线，但没有任何降级等级会禁用它"


# ============================================================
# 2. 状态接口：声明禁用 vs 真禁用 必须分开报
# ============================================================

class TestStatusHonesty:
    def test_severe_splits_enforced_and_advisory(self, level_guard):
        _set_level(level_guard, DegradationLevel.SEVERE)
        status = level_guard.get_status()

        enforced = set(status['enforced_disabled_features'])
        advisory = set(status['advisory_disabled_features'])

        # 接线的功能必须落在 enforced 里
        assert 'spc_analyzer' in enforced
        assert 'data_export' in enforced
        # 没有模块的功能必须落在 advisory 里，而不是混进 enforced 骗人
        assert 'report_generator' in advisory
        assert 'report_generator' not in enforced
        # 两栏不重叠，合起来等于声明的全集
        assert not (enforced & advisory)
        assert enforced | advisory == set(status['disabled_features'])

    def test_normal_has_nothing_disabled(self, level_guard):
        _set_level(level_guard, DegradationLevel.NORMAL)
        status = level_guard.get_status()
        assert status['disabled_features'] == []
        assert status['enforced_disabled_features'] == []
        assert status['advisory_disabled_features'] == []
        assert status['frequency_multiplier'] == 1.0

    def test_require_feature_raises_when_disabled(self, level_guard):
        from core.degradation_manager import FeatureDisabledError

        _set_level(level_guard, DegradationLevel.MODERATE)
        with pytest.raises(FeatureDisabledError) as exc:
            level_guard.require_feature('oee_calculator')
        assert exc.value.feature_name == 'oee_calculator'
        assert exc.value.level_name == 'MODERATE'

    def test_require_feature_passes_when_enabled(self, level_guard):
        _set_level(level_guard, DegradationLevel.NORMAL)
        level_guard.require_feature('oee_calculator')  # 不抛即通过


# ============================================================
# 3. 智能层分发真的按降级等级跳过模块
# ============================================================

class TestDispatchGating:
    def test_normal_feeds_every_module(self, level_guard):
        _set_level(level_guard, DegradationLevel.NORMAL)
        c = _make_collector()
        c._dispatch_intelligence(_data('spindle_temperature'))

        for name in ('predictive_maintenance', 'edge_decision', 'spc_analyzer',
                     'vibration_analyzer', 'device_control'):
            assert getattr(c, name).calls, f"NORMAL 等级下 {name} 没收到数据"

        # 能耗模块走「功率 / 电量」关键字分支，OEE 走「状态 / 产量」分支，
        # 各自单独喂一条（同一条数据不会同时命中所有模块）。
        c._dispatch_intelligence(_data('main_power_kw'))
        assert c.energy_manager.calls, 'NORMAL 等级下 energy_manager 没收到数据'

        c._dispatch_intelligence(_data('line_status', 2))
        assert c.oee_calculator.calls, 'NORMAL 等级下 oee_calculator 没收到数据'

    def test_moderate_stops_all_analytics(self, level_guard):
        """MODERATE 关掉 SPC / OEE / 振动 / 预测性维护 / 能耗优化。"""
        _set_level(level_guard, DegradationLevel.MODERATE)
        c = _make_collector()
        c._dispatch_intelligence(_data('spindle_temperature'))
        c._dispatch_intelligence(_data('main_power_kw'))

        for name in ('predictive_maintenance', 'energy_manager',
                     'spc_analyzer', 'vibration_analyzer', 'oee_calculator'):
            assert not getattr(c, name).calls, f"MODERATE 等级下 {name} 仍在收数据"

        # edge_decision 在 MODERATE 仍然启用（SEVERE 才关）
        assert c.edge_decision.calls, 'MODERATE 不该关闭边缘决策'

    def test_severe_stops_edge_decision(self, level_guard):
        _set_level(level_guard, DegradationLevel.SEVERE)
        c = _make_collector()
        c._dispatch_intelligence(_data('spindle_temperature'))
        assert not c.edge_decision.calls, 'SEVERE 应关闭边缘决策'

    def test_light_keeps_analytics_but_stops_energy_optimizer(self, level_guard):
        """LIGHT 只关报表/趋势/拓扑/能耗优化，SPC 与 OEE 必须继续跑。"""
        _set_level(level_guard, DegradationLevel.LIGHT)
        c = _make_collector()
        c._dispatch_intelligence(_data('spindle_temperature'))
        c._dispatch_intelligence(_data('main_power_kw'))
        c._dispatch_intelligence(_data('line_status', 2))

        assert not c.energy_manager.calls, 'LIGHT 应关闭能耗优化'
        assert c.spc_analyzer.calls, 'LIGHT 不应关闭 SPC'
        assert c.oee_calculator.calls, 'LIGHT 不应关闭 OEE'
        assert c.predictive_maintenance.calls, 'LIGHT 不应关闭预测性维护'

    def test_emergency_still_runs_interlock(self, level_guard):
        """EMERGENCY = 仅保留报警与联锁。联锁绝不能跟着分析一起被关掉。"""
        _set_level(level_guard, DegradationLevel.EMERGENCY)
        c = _make_collector()
        c._dispatch_intelligence(_data('spindle_temperature'))

        assert c.device_control.calls, 'EMERGENCY 下安全联锁被跳过了（安全功能丢失）'
        assert not c.spc_analyzer.calls
        assert not c.oee_calculator.calls


# ============================================================
# 4. 安全联锁不能排在会抛异常的模块后面
# ============================================================

class TestInterlockIsolation:
    def test_interlock_runs_when_analytics_raises(self, level_guard):
        """智能模块抛异常时，联锁必须照样执行。

        变异验证靶点：把 `check_interlocks` 从函数开头挪回结尾，本用例变红。
        """
        _set_level(level_guard, DegradationLevel.NORMAL)
        c = _make_collector()

        def boom(*a, **kw):
            raise OverflowError('模拟 int(inf)')

        c.oee_calculator.record_production = boom
        c.predictive_maintenance.feed_data = boom
        c._dispatch_intelligence(_data(register_name='total_count', value=float('inf')))

        assert c.device_control.calls, \
            '联锁被前面抛异常的智能模块带走了（安全功能静默丢失）'

    def test_one_module_failure_does_not_starve_others(self, level_guard):
        """一个模块抛异常，其它模块必须继续收到数据。"""
        _set_level(level_guard, DegradationLevel.NORMAL)
        c = _make_collector()

        def boom(*a, **kw):
            raise RuntimeError('模拟模块内部故障')

        c.predictive_maintenance.feed_data = boom
        c._dispatch_intelligence(_data('spindle_temperature'))

        assert c.edge_decision.calls, '预测性维护抛异常后，边缘决策没收到数据'
        assert c.spc_analyzer.calls, '预测性维护抛异常后，SPC 没收到数据'
        assert c.vibration_analyzer.calls, '预测性维护抛异常后，振动分析没收到数据'

        c._dispatch_intelligence(_data('total_count', 50))
        assert c.oee_calculator.calls, '预测性维护抛异常后，OEE 没收到数据'

    def test_non_finite_production_count_is_not_recorded(self, level_guard):
        """inf / nan 产量不得被记进 OEE，且不得靠抛异常来「跳过」。

        直接调 `_dispatch_oee`（绕过 `_safe_intel` 的兜底）：
        修复前 `int(float('inf'))` 会抛 OverflowError —— 靠异常来跳过，
        代价是整条分发的后续模块一起被带走；这里要求它**安静地**不记录。
        """
        _set_level(level_guard, DegradationLevel.NORMAL)

        for bad in (float('inf'), float('-inf'), float('nan')):
            c = _make_collector()
            c._dispatch_oee('DEV1', 'total_count', bad)   # 不应抛异常
            assert not c.oee_calculator.calls, f"{bad!r} 被当成产量记进了 OEE"

    def test_non_finite_status_is_not_recorded_as_stopped(self, level_guard):
        """状态寄存器读到 NaN 时，不能兜底成 0（0 = 'stopped'）压低可用率。"""
        _set_level(level_guard, DegradationLevel.NORMAL)

        for bad in (float('nan'), float('inf'), 'garbage'):
            c = _make_collector()
            c._dispatch_oee('DEV1', 'line_status', bad)
            assert not c.oee_calculator.calls, \
                f"{bad!r} 被兜底成 'stopped' 记进了 OEE（编造停机）"

    def test_unknown_status_code_is_not_recorded(self, level_guard):
        _set_level(level_guard, DegradationLevel.NORMAL)
        c = _make_collector()
        c._dispatch_oee('DEV1', 'line_status', 99)
        assert not c.oee_calculator.calls, '未定义状态码 99 被兜底成 stopped'

    def test_valid_status_code_is_recorded(self, level_guard):
        _set_level(level_guard, DegradationLevel.NORMAL)
        c = _make_collector()
        c._dispatch_oee('DEV1', 'line_status', 2)
        assert c.oee_calculator.calls
        assert c.oee_calculator.calls[0][1][1] == 'running'

    def test_normal_production_count_is_recorded(self, level_guard):
        _set_level(level_guard, DegradationLevel.NORMAL)
        c = _make_collector()
        c._dispatch_intelligence(_data(register_name='total_count', value=120))
        assert c.oee_calculator.calls
        assert c.oee_calculator.kwargs[0]['count'] == 120


# ============================================================
# 5. 采集频率倍数真的作用在采集间隔上
# ============================================================

class TestFrequencyMultiplier:
    def test_multiplier_noop_at_normal(self, level_guard):
        _set_level(level_guard, DegradationLevel.NORMAL)
        assert DataCollector._apply_degradation_multiplier(5.0) == 5.0

    def test_multiplier_scales_interval(self, level_guard):
        _set_level(level_guard, DegradationLevel.MODERATE)
        assert DataCollector._apply_degradation_multiplier(5.0) == 10.0

        _set_level(level_guard, DegradationLevel.SEVERE)
        assert DataCollector._apply_degradation_multiplier(5.0) == 25.0

        _set_level(level_guard, DegradationLevel.EMERGENCY)
        assert DataCollector._apply_degradation_multiplier(5.0) == 50.0

    def test_dynamic_interval_uses_multiplier(self, level_guard):
        """端到端：`_get_dynamic_interval` 返回的间隔必须随降级等级拉长。"""
        _set_level(level_guard, DegradationLevel.NORMAL)
        c = _make_collector()
        base = c._get_dynamic_interval('DEV1', 5.0)

        _set_level(level_guard, DegradationLevel.MODERATE)
        scaled = c._get_dynamic_interval('DEV1', 5.0)

        assert scaled > base, f"降级后采集间隔没变长（{base} → {scaled}），频率倍数没接线"
        assert scaled == pytest.approx(base * 2.0)

    def test_fault_interval_also_scaled(self, level_guard):
        """故障设备的 1 秒快采同样要按降级拉长 —— 否则降级期间反而最耗资源。"""
        c = _make_collector()
        c.device_manager.get_device_status.return_value = {
            'stopped': True, 'error': True, 'stats': {},
        }

        _set_level(level_guard, DegradationLevel.NORMAL)
        normal = c._get_dynamic_interval('DEV1', 5.0)

        _set_level(level_guard, DegradationLevel.EMERGENCY)
        degraded = c._get_dynamic_interval('DEV1', 5.0)

        assert normal == 1
        assert degraded == 10


# ============================================================
# 6. 导出接口在降级后返回 503
# ============================================================

class TestExportGate:
    def _call(self, level):
        from flask import Flask, jsonify
        from 展示层.api.api_data import _require_feature_export

        app = Flask(__name__)
        with app.app_context():
            @_require_feature_export
            def _inner():
                return jsonify({'success': True, 'reached': True}), 200

            _set_level(degradation_manager, level)
            return _inner()

    def test_export_allowed_at_normal(self, level_guard):
        body, status = self._call(DegradationLevel.NORMAL)
        assert status == 200
        assert body.get_json()['reached'] is True

    def test_export_blocked_at_severe(self, level_guard):
        body, status = self._call(DegradationLevel.SEVERE)
        assert status == 503
        payload = body.get_json()
        assert payload['success'] is False
        assert payload['code'] == 'FEATURE_DISABLED'
        assert payload['feature'] == 'data_export'
        assert payload['level'] == 'SEVERE'
        # 关键：必须是 503 而不是「200 + 空文件」，否则调用方以为拿到了数据
        assert 'reached' not in payload

    def test_export_blocked_at_emergency(self, level_guard):
        _, status = self._call(DegradationLevel.EMERGENCY)
        assert status == 503

    def test_export_allowed_at_light_and_moderate(self, level_guard):
        for level in (DegradationLevel.LIGHT, DegradationLevel.MODERATE):
            _, status = self._call(level)
            assert status == 200, f"{level.name} 不该禁用数据导出"


# ============================================================
# 7. 维护任务里的数据归档按降级等级跳过
# ============================================================

class TestArchiveGate:
    def _archive_task(self):
        from core.maintenance import build_maintenance_tasks

        class _DB:
            def __init__(self):
                self.archived = 0
                self.checkpoints = 0

            def archive_old_data(self, **kw):
                self.archived += 1
                return {'archived': 0}

            def wal_checkpoint(self):
                self.checkpoints += 1

        db = _DB()
        tasks = build_maintenance_tasks(database=db)
        match = [t for t in tasks if t[0] == 'data_archive']
        assert match, '维护任务里没有 data_archive'
        return match[0][3], db

    def test_archive_runs_at_normal(self, level_guard):
        _set_level(level_guard, DegradationLevel.NORMAL)
        task, db = self._archive_task()
        task()
        assert db.archived == 1
        assert db.checkpoints == 1

    def test_archive_skipped_at_emergency(self, level_guard):
        """EMERGENCY 下归档（跨天删除 + WAL checkpoint）必须跳过。"""
        _set_level(level_guard, DegradationLevel.EMERGENCY)
        task, db = self._archive_task()
        result = task()

        assert db.archived == 0, 'EMERGENCY 下仍在跑数据归档（最不该在数据库吃紧时跑的重操作）'
        assert db.checkpoints == 0
        assert result.get('skipped') == 'degradation'

    def test_archive_runs_at_severe(self, level_guard):
        """SEVERE 不关归档 —— 只在 EMERGENCY 关，别扩大化。"""
        _set_level(level_guard, DegradationLevel.SEVERE)
        task, db = self._archive_task()
        task()
        assert db.archived == 1
