"""
Tests for 智能层.spc_analyzer: SPC control charts, capability analysis, violations
"""

import pytest
import math
from unittest.mock import MagicMock

from 智能层.spc_analyzer import SPCAnalyzer


@pytest.fixture
def spc():
    """Create SPCAnalyzer with mocked database"""
    return SPCAnalyzer(MagicMock())


# ============================================================
# Feed Data Tests
# ============================================================

class TestFeedData:

    def test_feed_data_stores_in_buffer(self, spc):
        """feed_data stores value in data buffer"""
        spc.feed_data('dev1', 'temperature', 50.0)
        assert len(spc.data_buffers['dev1:temperature']) == 1

    def test_feed_data_auto_sets_spec_limits(self, spc):
        """feed_data auto-sets spec limits for known parameters"""
        spc.feed_data('dev1', 'temperature', 50.0)
        assert 'dev1:temperature' in spc.spec_limits
        assert spc.spec_limits['dev1:temperature']['usl'] == 100.0

    def test_feed_data_no_auto_spec_for_unknown(self, spc):
        """feed_data doesn't set spec limits for unknown register names"""
        spc.feed_data('dev1', 'custom_metric', 50.0)
        assert 'dev1:custom_metric' not in spc.spec_limits

    def test_feed_data_multiple_values(self, spc):
        """Multiple feed_data calls accumulate in buffer"""
        for i in range(100):
            spc.feed_data('dev1', 'temperature', 50.0 + i * 0.1)
        assert len(spc.data_buffers['dev1:temperature']) == 100

    def test_set_spec_limits(self, spc):
        """set_spec_limits stores limits correctly"""
        spc.set_spec_limits('dev1:temp', usl=100.0, lsl=0.0, target=50.0)
        assert spc.spec_limits['dev1:temp']['usl'] == 100.0
        assert spc.spec_limits['dev1:temp']['lsl'] == 0.0
        assert spc.spec_limits['dev1:temp']['target'] == 50.0

    def test_set_spec_limits_auto_target(self, spc):
        """set_spec_limits auto-calculates target if not provided and both usl/lsl are truthy"""
        spc.set_spec_limits('dev1:temp', usl=100.0, lsl=10.0)
        assert spc.spec_limits['dev1:temp']['target'] == 55.0


# ============================================================
# Control Chart Tests
# ============================================================

class TestControlCharts:

    def _feed_values(self, spc, key, count=30, base=50.0, variation=1.0):
        """Helper: feed stable values"""
        import random
        random.seed(42)
        for _ in range(count):
            spc.data_buffers[key].append(base + random.uniform(-variation, variation))

    def test_xbar_r_chart_returns_none_insufficient_data(self, spc):
        """xbar-r chart returns None with insufficient data"""
        spc.feed_data('dev1', 'temp', 50.0)
        result = spc.calculate_xbar_r_chart('dev1', 'temp')
        assert result is None

    def test_xbar_r_chart_with_data(self, spc):
        """xbar-r chart returns valid result with enough data"""
        self._feed_values(spc, 'dev1:temp', count=30)
        result = spc.calculate_xbar_r_chart('dev1', 'temp')
        assert result is not None
        assert result['chart_type'] == 'X̄-R'
        assert 'xbar_chart' in result
        assert 'r_chart' in result
        assert 'violations' in result

    def test_xbar_r_chart_control_limits(self, spc):
        """xbar-r chart has UCL > CL > LCL"""
        self._feed_values(spc, 'dev1:temp', count=30)
        result = spc.calculate_xbar_r_chart('dev1', 'temp')
        xbar = result['xbar_chart']
        assert xbar['ucl'] > xbar['cl']
        assert xbar['cl'] > xbar['lcl']

    def test_xbar_s_chart_returns_none_insufficient_data(self, spc):
        """xbar-s chart returns None with insufficient data"""
        result = spc.calculate_xbar_s_chart('dev1', 'temp')
        assert result is None

    def test_xbar_s_chart_with_data(self, spc):
        """xbar-s chart returns valid result"""
        self._feed_values(spc, 'dev1:temp', count=30)
        result = spc.calculate_xbar_s_chart('dev1', 'temp')
        assert result is not None
        assert result['chart_type'] == 'X̄-S'
        assert 's_chart' in result

    def test_get_control_chart(self, spc):
        """get_control_chart delegates to calculate_xbar_r_chart"""
        self._feed_values(spc, 'dev1:temp', count=30)
        result = spc.get_control_chart('dev1', 'temp')
        assert result is not None


# ============================================================
# Capability Analysis Tests
# ============================================================

class TestCapabilityAnalysis:

    def _feed_values(self, spc, key, count=50, base=50.0, variation=2.0):
        import random
        random.seed(42)
        for _ in range(count):
            spc.data_buffers[key].append(base + random.uniform(-variation, variation))

    def test_capability_returns_none_without_spec(self, spc):
        """calculate_capability returns None without spec limits"""
        self._feed_values(spc, 'dev1:temp', count=50)
        result = spc.calculate_capability('dev1', 'temp')
        assert result is None

    def test_capability_returns_none_insufficient_data(self, spc):
        """calculate_capability returns None with insufficient data (< 20 points)"""
        spc.set_spec_limits('dev1:temp', usl=100.0, lsl=10.0)
        for i in range(5):
            spc.data_buffers['dev1:temp'].append(50.0 + i)
        result = spc.calculate_capability('dev1', 'temp')
        assert result is None

    def test_capability_with_data(self, spc):
        """calculate_capability returns valid result"""
        spc.set_spec_limits('dev1:temp', usl=100.0, lsl=0.0)
        self._feed_values(spc, 'dev1:temp', count=50)
        result = spc.calculate_capability('dev1', 'temp')
        assert result is not None
        assert 'cp' in result
        assert 'cpk' in result
        assert 'pp' in result
        assert 'ppk' in result
        assert 'capability_grade' in result

    def test_capability_grade_excellent(self, spc):
        """Cpk >= 1.67 is excellent grade"""
        assert spc._capability_grade(2.0) == '特级(优秀)'
        assert spc._capability_grade(1.67) == '特级(优秀)'

    def test_capability_grade_good(self, spc):
        """1.33 <= Cpk < 1.67 is good grade"""
        assert spc._capability_grade(1.5) == '一级(良好)'
        assert spc._capability_grade(1.33) == '一级(良好)'

    def test_capability_grade_pass(self, spc):
        """1.00 <= Cpk < 1.33 is pass grade"""
        assert spc._capability_grade(1.1) == '二级(合格)'
        assert spc._capability_grade(1.00) == '二级(合格)'

    def test_capability_grade_marginal(self, spc):
        """0.67 <= Cpk < 1.00 is marginal"""
        assert spc._capability_grade(0.8) == '三级(不足)'
        assert spc._capability_grade(0.67) == '三级(不足)'

    def test_capability_grade_poor(self, spc):
        """Cpk < 0.67 is poor"""
        assert spc._capability_grade(0.3) == '四级(严重不足)'

    def test_get_capability(self, spc):
        """get_capability delegates to calculate_capability"""
        spc.set_spec_limits('dev1:temp', usl=100.0, lsl=0.0)
        self._feed_values(spc, 'dev1:temp', count=50)
        result = spc.get_capability('dev1', 'temp')
        assert result is not None

    def test_normal_ppm_high_z(self, spc):
        """High z-score gives very low PPM"""
        ppm = spc._normal_ppm(6.0)
        assert ppm < 1

    def test_normal_ppm_low_z(self, spc):
        """Low z-score gives high PPM"""
        ppm = spc._normal_ppm(-6.0)
        assert ppm > 999999

    def test_normal_ppm_zero(self, spc):
        """z=0 gives ~500000 PPM"""
        ppm = spc._normal_ppm(0)
        assert 400000 < ppm < 600000


# ============================================================
# Violation Detection Tests
# ============================================================

class TestViolationDetection:

    def test_no_critical_violations_for_stable_data(self, spc):
        """Stable data has no critical violations (rule 1)"""
        points = [50.0] * 20
        violations = spc._check_violations(points, 50.0, 55.0, 45.0)
        # Rule 7 may trigger (points too close to center), but no critical violations
        critical = [v for v in violations if v['severity'] == 'critical']
        assert len(critical) == 0

    def test_rule1_violation_out_of_control(self, spc):
        """Rule 1: point outside 3-sigma limits"""
        points = [50.0] * 10 + [100.0]  # 100 is way outside
        violations = spc._check_violations(points, 50.0, 55.0, 45.0)
        rule1 = [v for v in violations if v['rule'] == 1]
        assert len(rule1) > 0

    def test_rule2_violation_same_side(self, spc):
        """Rule 2: 9 consecutive points on same side of center"""
        points = [51.0] * 9  # all above center (50)
        violations = spc._check_violations(points, 50.0, 55.0, 45.0)
        rule2 = [v for v in violations if v['rule'] == 2]
        assert len(rule2) > 0

    def test_rule3_violation_monotonic(self, spc):
        """Rule 3: 6 consecutive increasing points"""
        points = [46.0, 47.0, 48.0, 49.0, 50.0, 51.0]
        violations = spc._check_violations(points, 50.0, 55.0, 45.0)
        rule3 = [v for v in violations if v['rule'] == 3]
        assert len(rule3) > 0

    def test_empty_points_no_violations(self, spc):
        """Empty points list returns no violations"""
        violations = spc._check_violations([], 50.0, 55.0, 45.0)
        assert violations == []

    def test_single_point_no_violations(self, spc):
        """Single point returns no violations"""
        violations = spc._check_violations([50.0], 50.0, 55.0, 45.0)
        assert violations == []


# ============================================================
# Helper Method Tests
# ============================================================

class TestHelperMethods:

    def test_form_subgroups(self, spc):
        """_form_subgroups creates correct subgroups"""
        data = list(range(15))
        subgroups = spc._form_subgroups(data)
        assert len(subgroups) == 3  # 15 / 5 = 3
        assert len(subgroups[0]) == 5

    def test_form_subgroups_partial(self, spc):
        """_form_subgroups discards incomplete last group"""
        data = list(range(12))
        subgroups = spc._form_subgroups(data)
        assert len(subgroups) == 2  # 12 / 5 = 2 remainder 2 (discarded)

    def test_std_dev(self, spc):
        """_std_dev calculates sample standard deviation"""
        data = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        std = spc._std_dev(data)
        # Sample std dev (n-1) for this dataset is ~2.138
        assert abs(std - 2.138) < 0.05

    def test_std_dev_single_value(self, spc):
        """_std_dev returns 0 for single value"""
        assert spc._std_dev([5.0]) == 0

    def test_auto_set_spec_limits_known_params(self, spc):
        """_auto_set_spec_limits sets limits for known parameter names"""
        for param in ('temperature', 'pressure', 'ph', 'voltage', 'current', 'speed', 'flow', 'humidity'):
            spc._auto_set_spec_limits(f'dev:{param}', param)
            assert f'dev:{param}' in spc.spec_limits


# ============================================================
# Query API Tests
# ============================================================

class TestQueryAPI:

    def test_get_violations_empty(self, spc):
        """get_violations returns empty list initially"""
        assert spc.get_violations() == []

    def test_get_violations_with_device_filter(self, spc):
        """get_violations filters by device_id"""
        spc.violations['dev1:temp'] = [{'rule': 1, 'index': 0}]
        spc.violations['dev2:temp'] = [{'rule': 2, 'index': 0}]
        result = spc.get_violations(device_id='dev1')
        assert len(result) == 1

    def test_get_violations_with_limit(self, spc):
        """get_violations respects limit"""
        for i in range(100):
            spc.violations['dev1:temp'].append({'rule': 1, 'index': i})
        result = spc.get_violations(limit=10)
        assert len(result) <= 10


# ============================================================
# 判异结果去重（2026-09 修复：每轮重扫窗口 + 锁外 extend → 重复累积）
# ============================================================

class TestViolationDedup:
    """同一违规只能计入一次：控制图重复计算不得让判异列表越长越多。"""

    KEY = 'dev1:temp'

    @staticmethod
    def _feed_monotonic(spc, count, start=50.0, step=0.5):
        """喂入单调递增数据 → 必然触发规则3（连续6点递增）等判异"""
        for i in range(count):
            spc.feed_data('dev1', 'temp', start + i * step)

    def test_repeated_calculation_does_not_accumulate(self, spc):
        """同一窗口重复计算 5 次，判异条数必须保持不变"""
        self._feed_monotonic(spc, 40)

        spc.calculate_xbar_r_chart('dev1', 'temp')
        after_first = len(spc.violations[self.KEY])
        assert after_first > 0, '构造的数据应当触发判异，否则本测试无意义'

        for _ in range(4):
            spc.calculate_xbar_r_chart('dev1', 'temp')

        assert len(spc.violations[self.KEY]) == after_first, \
            f'重复计算导致判异重复计入: {after_first} -> {len(spc.violations[self.KEY])}'

    def test_repeated_calculation_of_s_chart_does_not_accumulate(self, spc):
        """X̄-S 图同样不得重复累积"""
        self._feed_monotonic(spc, 40)

        spc.calculate_xbar_s_chart('dev1', 'temp')
        after_first = len(spc.violations[self.KEY])
        assert after_first > 0

        for _ in range(4):
            spc.calculate_xbar_s_chart('dev1', 'temp')

        assert len(spc.violations[self.KEY]) == after_first

    def test_r_and_s_chart_share_dedup_state(self, spc):
        """R 图与 S 图对同一窗口给出相同判异 → 合并后仍不重复"""
        self._feed_monotonic(spc, 40)
        spc.calculate_xbar_r_chart('dev1', 'temp')
        first = len(spc.violations[self.KEY])
        spc.calculate_xbar_s_chart('dev1', 'temp')
        spc.calculate_xbar_r_chart('dev1', 'temp')
        assert len(spc.violations[self.KEY]) == first

    def test_new_data_still_recorded_once(self, spc):
        """去重不能过度抑制：真正的新违规必须被记录，且只记录一次"""
        self._feed_monotonic(spc, 40)
        spc.calculate_xbar_r_chart('dev1', 'temp')
        baseline = len(spc.violations[self.KEY])

        self._feed_monotonic(spc, 30, start=100.0, step=0.5)
        spc.calculate_xbar_r_chart('dev1', 'temp')
        after_new = len(spc.violations[self.KEY])
        assert after_new > baseline, '新数据产生的新违规必须被记录'

        spc.calculate_xbar_r_chart('dev1', 'temp')
        assert len(spc.violations[self.KEY]) == after_new

    def test_violations_are_unique_by_rule_and_position(self, spc):
        """判异列表内不得存在 (rule, 绝对位置区间) 重复项"""
        self._feed_monotonic(spc, 60)
        for _ in range(3):
            spc.calculate_xbar_r_chart('dev1', 'temp')

        fingerprints = [(v['rule'], v['abs_index'], v['span'])
                        for v in spc.violations[self.KEY]]
        assert len(fingerprints) == len(set(fingerprints))

    def test_violations_carry_span(self, spc):
        """每条判异都必须带 span（去重换算绝对区间所需）"""
        violations = spc._check_violations([46.0, 47.0, 48.0, 49.0, 50.0, 51.0],
                                           50.0, 55.0, 45.0)
        assert violations
        for v in violations:
            assert isinstance(v['span'], int)
            assert v['span'] >= 1

    def test_get_violations_reflects_dedup(self, spc):
        """查询接口返回的也是去重后的结果"""
        self._feed_monotonic(spc, 40)
        for _ in range(3):
            spc.calculate_xbar_r_chart('dev1', 'temp')
        listed = spc.get_violations(device_id='dev1', limit=1000)
        stored = spc.violations[self.KEY]
        assert len(listed) == len(stored)

    def test_concurrent_calculation_is_consistent(self, spc):
        """并发重算（锁竞争）下不得破坏去重状态、不得抛异常"""
        import threading

        self._feed_monotonic(spc, 60)
        errors = []

        def worker():
            try:
                for _ in range(5):
                    spc.calculate_xbar_r_chart('dev1', 'temp')
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        fingerprints = [(v['rule'], v['abs_index'], v['span'])
                        for v in spc.violations[self.KEY]]
        assert len(fingerprints) == len(set(fingerprints))


# ============================================================
# BUG-046：NaN/bool 入口、0.0 规格限、X̄-S 缓存与选图
# ============================================================

class TestBug046NumericGuardAndChartSelection:
    """BUG-046 (P1)：四个子问题各自的回归用例。"""

    @pytest.mark.parametrize('bad', [
        float('nan'), float('inf'), float('-inf'), True, False, None, 'x',
    ])
    def test_feed_data_rejects_non_finite_and_bool(self, spc, bad):
        """② bool/NaN/Inf 不得进入缓冲区。

        NaN 与任何阈值的比较都是 False → 判异规则会静默显示"受控"；
        bool 经 float() 变成 1.0，会把温度序列的均值整个拽偏。
        """
        assert spc.feed_data('dev1', 'temperature', bad) is False
        assert len(spc.data_buffers['dev1:temperature']) == 0
        assert spc._total_fed['dev1:temperature'] == 0

    def test_spec_limits_with_zero_lsl_keeps_target(self, spc):
        """① `lsl == 0.0` 是 falsy，但规格下限为 0 是最常见的配置。"""
        spc.set_spec_limits('dev1:temp', usl=100.0, lsl=0.0)
        spec = spc.spec_limits['dev1:temp']
        assert spec['lsl'] == 0.0
        assert spec['target'] == 50.0

    def test_spec_limits_explicit_zero_target(self, spc):
        """target=0.0 不能被 `target or ...` 吞掉。"""
        spc.set_spec_limits('dev1:temp', usl=10.0, lsl=-10.0, target=0.0)
        assert spc.spec_limits['dev1:temp']['target'] == 0.0

    @pytest.mark.parametrize('bad', [float('nan'), float('inf'), 'x'])
    def test_spec_limits_reject_non_finite(self, spc, bad):
        """NaN/Inf 规格限会让 Cpk/PPM 全部变 NaN，必须在入口拒绝。"""
        with pytest.raises(ValueError):
            spc.set_spec_limits('dev1:temp', usl=bad, lsl=0.0)

    def test_spec_limits_reject_inverted_bounds(self, spc):
        with pytest.raises(ValueError):
            spc.set_spec_limits('dev1:temp', usl=10.0, lsl=10.0)

    def test_xbar_s_chart_caches_control_limits(self, spc):
        """③ X̄-S 计算结果必须写入 control_limits。

        否则 `calculate_capability` 取不到 's_chart'，组内 σ 只能退化成
        总体 σ → n>10（只能走 S 图）时 Cp/Cpk 与 Pp/Ppk 同值，能力被高估。
        """
        for i in range(60):
            spc.feed_data('dev1', 'temp', 50.0 + (i % 7) * 0.3)
        result = spc.calculate_xbar_s_chart('dev1', 'temp')
        assert result is not None
        cached = spc.control_limits['dev1:temp']
        assert cached['s_chart']['cl'] == result['s_chart']['cl']

    def test_capability_uses_s_chart_sigma(self, spc):
        """③ 联动：能力分析用 s̄/c4 作为组内 σ，与总体 σ 区分开。"""
        for i in range(160):
            spc.feed_data('dev1', 'temp', 50.0 + i * 0.05 + (i % 7) * 0.3)
        spc.calculate_xbar_s_chart('dev1', 'temp')
        spc.set_spec_limits('dev1:temp', usl=80.0, lsl=20.0)
        result = spc.calculate_capability('dev1', 'temp')
        assert result is not None
        s_bar = spc.control_limits['dev1:temp']['s_chart']['cl']
        # 结果对 4 位小数取整，比较时留相应余量
        assert math.isclose(result['sigma_within'], s_bar / SPCAnalyzer._c4(5),
                            abs_tol=1e-3)
        assert result['sigma_within'] != result['sigma_total']

    def test_get_control_chart_switches_to_s_chart_above_10(self):
        """④ 子组 >10 时 R 图系数（A2/D3/D4）无定义，必须改用 X̄-S 图。

        原实现无条件调 `calculate_xbar_r_chart`，而 `a2_table.get(n, 0.577)`
        会静默套用 n=5 的系数 —— 控制限偏窄，正常波动被成批判成异常。
        """
        spc = SPCAnalyzer(MagicMock(), {'subgroup_size': 15})
        for i in range(160):
            spc.feed_data('dev1', 'temp', 50.0 + (i % 7) * 0.3)

        chart = spc.get_control_chart('dev1', 'temp')
        assert chart['chart_type'] == 'X̄-S'
        assert chart['subgroup_size'] == 15
        # 直接调 R 图入口也不能静默给出错误系数
        assert spc.calculate_xbar_r_chart('dev1', 'temp')['chart_type'] == 'X̄-S'

    def test_get_control_chart_keeps_r_chart_at_10_or_below(self, spc):
        """④ 反向约束：n<=10 仍走 X̄-R，不能一刀切成 S 图。"""
        for i in range(60):
            spc.feed_data('dev1', 'temp', 50.0 + (i % 5) * 0.2)
        assert spc.get_control_chart('dev1', 'temp')['chart_type'] == 'X̄-R'

    def test_c4_fallback_matches_gamma_definition(self):
        """c4 超出查表范围时用 Γ 函数，结果必须连续可算。"""
        assert math.isclose(SPCAnalyzer._c4(10), 0.9727, rel_tol=1e-4)
        assert math.isclose(SPCAnalyzer._c4(26), 0.99, rel_tol=5e-3)
