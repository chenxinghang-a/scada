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
