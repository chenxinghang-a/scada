"""
Tests for 智能层.vibration_analyzer: VibrationAnalyzer, FFT, ISO zones, trend
"""

import pytest
import math
from datetime import datetime
from unittest.mock import MagicMock

from 智能层.vibration_analyzer import (
    VibrationAnalyzer, VibrationRecord, FFTResult,
    VIBRATION_ZONES, BEARING_FAULT_COEFFICIENTS,
)


@pytest.fixture
def analyzer():
    """Create VibrationAnalyzer with mocked database"""
    va = VibrationAnalyzer(database=MagicMock(), config={'sample_rate': 100})
    va.start()
    return va


# ============================================================
# VibrationRecord Tests
# ============================================================

class TestVibrationRecord:

    def test_creation(self):
        """VibrationRecord stores timestamp and value"""
        r = VibrationRecord(1000.0, 2.5, 'mm/s')
        assert r.timestamp == 1000.0
        assert r.value == 2.5
        assert r.unit == 'mm/s'

    def test_to_dict(self):
        """to_dict returns expected structure"""
        r = VibrationRecord(1000.0, 2.5)
        d = r.to_dict()
        assert d['timestamp'] == 1000.0
        assert d['value'] == 2.5
        assert d['unit'] == 'mm/s'


# ============================================================
# FFTResult Tests
# ============================================================

class TestFFTResult:

    def test_creation(self):
        """FFTResult stores frequencies and amplitudes"""
        fft = FFTResult([1.0, 2.0], [0.5, 0.3], 1.0, 0.5)
        assert fft.dominant_freq == 1.0
        assert fft.dominant_amp == 0.5

    def test_to_dict(self):
        """to_dict returns expected structure"""
        fft = FFTResult([10.0, 50.0, 200.0], [1.0, 0.5, 0.2], 10.0, 1.0)
        d = fft.to_dict()
        assert 'dominant_frequency_hz' in d
        assert 'dominant_amplitude' in d
        assert 'frequency_bands' in d

    def test_band_energies(self):
        """_get_band_energies calculates band energies"""
        fft = FFTResult(
            [10.0, 200.0, 1500.0, 8000.0],
            [1.0, 0.5, 0.3, 0.1],
            10.0, 1.0
        )
        bands = fft._get_band_energies()
        assert '0-100Hz' in bands
        assert '100-500Hz' in bands
        assert bands['0-100Hz'] > 0  # 10Hz should be in this band


# ============================================================
# ISO 10816 Zone Tests
# ============================================================

class TestVibrationZones:

    def test_zone_a_good(self):
        """Zone A is for vibration <= 0.71 mm/s"""
        assert VIBRATION_ZONES['A']['max'] == 0.71
        assert VIBRATION_ZONES['A']['color'] == 'green'

    def test_zone_b_acceptable(self):
        """Zone B is for vibration <= 1.8 mm/s"""
        assert VIBRATION_ZONES['B']['max'] == 1.8
        assert VIBRATION_ZONES['B']['color'] == 'yellow'

    def test_zone_c_alarm(self):
        """Zone C is for vibration <= 4.5 mm/s"""
        assert VIBRATION_ZONES['C']['max'] == 4.5
        assert VIBRATION_ZONES['C']['color'] == 'orange'

    def test_zone_d_danger(self):
        """Zone D is for vibration > 4.5 mm/s"""
        assert VIBRATION_ZONES['D']['max'] == float('inf')
        assert VIBRATION_ZONES['D']['color'] == 'red'


# ============================================================
# Feed Data Tests
# ============================================================

class TestFeedData:

    def test_feed_data_stores_in_buffer(self, analyzer):
        """feed_data stores vibration data in buffer"""
        analyzer.feed_data('dev1', 'vibration_x', 1.5)
        assert 'dev1' in analyzer._buffers
        assert len(analyzer._buffers['dev1']) == 1

    def test_feed_data_ignores_non_vibration(self, analyzer):
        """feed_data ignores non-vibration registers"""
        analyzer.feed_data('dev1', 'temperature', 50.0)
        assert 'dev1' not in analyzer._buffers

    def test_feed_data_when_not_running(self):
        """feed_data does nothing when not running"""
        va = VibrationAnalyzer()
        va.feed_data('dev1', 'vibration_x', 1.5)
        assert 'dev1' not in va._buffers

    def test_feed_data_with_timestamp(self, analyzer):
        """feed_data uses provided timestamp"""
        ts = datetime.now()
        analyzer.feed_data('dev1', 'vibration_x', 1.5, timestamp=ts)
        assert len(analyzer._buffers['dev1']) == 1

    def test_feed_data_multiple_values(self, analyzer):
        """Multiple feed_data calls accumulate"""
        for i in range(100):
            analyzer.feed_data('dev1', 'vibration_x', 1.0 + i * 0.01)
        assert len(analyzer._buffers['dev1']) == 100


# ============================================================
# Score Calculation Tests
# ============================================================

class TestScoreCalculation:

    def _feed_vibration_data(self, analyzer, device_id, count, base_value=1.0):
        """Helper: feed vibration data"""
        for i in range(count):
            analyzer.feed_data(device_id, 'vibration_x', base_value + 0.1 * math.sin(i))

    def test_score_not_updated_with_few_points(self, analyzer):
        """Score not updated with fewer than 10 points"""
        self._feed_vibration_data(analyzer, 'dev1', 5)
        assert analyzer.get_device_vibration('dev1') is None

    def test_score_updated_with_enough_points(self, analyzer):
        """Score updated with >= 10 points"""
        self._feed_vibration_data(analyzer, 'dev1', 20, base_value=0.5)
        score = analyzer.get_device_vibration('dev1')
        assert score is not None
        assert 'rms' in score
        assert 'peak' in score
        assert 'health_score' in score
        assert 'zone' in score

    def test_score_zone_a_for_low_vibration(self, analyzer):
        """Low vibration gets Zone A"""
        self._feed_vibration_data(analyzer, 'dev1', 20, base_value=0.3)
        score = analyzer.get_device_vibration('dev1')
        assert score['zone'] == 'A'

    def test_score_health_100_for_low_vibration(self, analyzer):
        """Very low vibration gets health score 100"""
        self._feed_vibration_data(analyzer, 'dev1', 20, base_value=0.1)
        score = analyzer.get_device_vibration('dev1')
        assert score['health_score'] == 100

    def test_get_vibration_scores_empty(self, analyzer):
        """get_vibration_scores returns empty initially"""
        assert analyzer.get_vibration_scores() == {}

    def test_get_vibration_scores_after_data(self, analyzer):
        """get_vibration_scores returns scores after data"""
        self._feed_vibration_data(analyzer, 'dev1', 20, base_value=0.5)
        scores = analyzer.get_vibration_scores()
        assert 'dev1' in scores


# ============================================================
# Zone Evaluation Tests
# ============================================================

class TestZoneEvaluation:

    def test_evaluate_zone_a(self, analyzer):
        """RMS <= 0.71 returns zone A"""
        zone = analyzer._evaluate_zone(0.5)
        assert zone['name'] == 'A'

    def test_evaluate_zone_b(self, analyzer):
        """0.71 < RMS <= 1.8 returns zone B"""
        zone = analyzer._evaluate_zone(1.0)
        assert zone['name'] == 'B'

    def test_evaluate_zone_c(self, analyzer):
        """1.8 < RMS <= 4.5 returns zone C"""
        zone = analyzer._evaluate_zone(3.0)
        assert zone['name'] == 'C'

    def test_evaluate_zone_d(self, analyzer):
        """RMS > 4.5 returns zone D"""
        zone = analyzer._evaluate_zone(10.0)
        assert zone['name'] == 'D'


# ============================================================
# Trend Detection Tests
# ============================================================

class TestTrendDetection:

    def test_stable_trend(self, analyzer):
        """Constant values show stable trend"""
        values = [1.0] * 30
        trend = analyzer._detect_trend(values)
        assert trend == 'stable'

    def test_rising_trend(self, analyzer):
        """Increasing values show rising trend"""
        values = [0.1 * i for i in range(50)]
        trend = analyzer._detect_trend(values)
        assert trend == 'rising'

    def test_falling_trend(self, analyzer):
        """Decreasing values show falling trend"""
        values = [50 - 0.1 * i for i in range(50)]
        trend = analyzer._detect_trend(values)
        assert trend == 'falling'

    def test_short_data_stable(self, analyzer):
        """Less than 20 points returns stable"""
        values = [1.0, 2.0, 3.0]
        trend = analyzer._detect_trend(values)
        assert trend == 'stable'


# ============================================================
# FFT / Spectrum Tests
# ============================================================

class TestSpectrum:

    def test_get_spectrum_returns_none_insufficient_data(self, analyzer):
        """get_spectrum returns None with insufficient data"""
        analyzer.feed_data('dev1', 'vibration_x', 1.0)
        assert analyzer.get_spectrum('dev1') is None

    def test_get_spectrum_with_data(self, analyzer):
        """get_spectrum returns spectrum with enough data"""
        for i in range(100):
            analyzer.feed_data('dev1', 'vibration_x', math.sin(2 * math.pi * 10 * i / 100))
        result = analyzer.get_spectrum('dev1')
        assert result is not None
        assert 'spectrum' in result
        assert 'sample_count' in result

    def test_get_spectrum_nonexistent_device(self, analyzer):
        """get_spectrum returns None for unknown device"""
        assert analyzer.get_spectrum('unknown') is None


# ============================================================
# Bearing Fault Tests
# ============================================================

class TestBearingFault:

    def test_check_bearing_fault_no_spectrum(self, analyzer):
        """check_bearing_fault returns None without spectrum"""
        assert analyzer.check_bearing_fault('dev1', 1500) is None

    def test_check_bearing_fault_with_data(self, analyzer):
        """check_bearing_fault returns result with spectrum data"""
        for i in range(100):
            analyzer.feed_data('dev1', 'vibration_x', math.sin(2 * math.pi * 25 * i / 100))
        result = analyzer.check_bearing_fault('dev1', 1500)
        assert result is not None
        assert 'bearing_faults' in result
        assert 'shaft_frequency_hz' in result
        assert result['rpm'] == 1500


# ============================================================
# Trend Data Tests
# ============================================================

class TestTrendData:

    def test_get_trend_data_empty(self, analyzer):
        """get_trend_data returns empty for unknown device"""
        assert analyzer.get_trend_data('unknown') == []

    def test_get_trend_data_with_data(self, analyzer):
        """get_trend_data returns data points"""
        analyzer.feed_data('dev1', 'vibration_x', 1.5)
        analyzer.feed_data('dev1', 'vibration_x', 2.0)
        trend = analyzer.get_trend_data('dev1')
        assert len(trend) == 2


# ============================================================
# Start/Stop Tests
# ============================================================

class TestStartStop:

    def test_start(self):
        """start() sets running flag"""
        va = VibrationAnalyzer()
        va.start()
        assert va._running is True

    def test_stop(self):
        """stop() clears running flag"""
        va = VibrationAnalyzer()
        va.start()
        va.stop()
        assert va._running is False


# ============================================================
# Bearing Fault Coefficients Tests
# ============================================================

class TestBearingCoefficients:

    def test_all_fault_types_exist(self):
        """All expected fault types are defined"""
        for ft in ('BPFO', 'BPFI', 'BSF', 'FTF'):
            assert ft in BEARING_FAULT_COEFFICIENTS
            assert 'name' in BEARING_FAULT_COEFFICIENTS[ft]
            assert 'typical' in BEARING_FAULT_COEFFICIENTS[ft]


# ============================================================
# 采样率真实性（2026-09 修复：不再假定 100Hz）
# ============================================================

class TestRealSampleRate:
    """频谱必须建立在**真实**采样率上；取不到采样率时降级为不可用。"""

    @staticmethod
    def _feed_sine(analyzer, device_id, freq_hz, fs_hz, n=200):
        for i in range(n):
            analyzer.feed_data(device_id, 'vibration_x',
                               math.sin(2 * math.pi * freq_hz * i / fs_hz),
                               sample_rate=fs_hz)

    def test_no_sample_rate_degrades_instead_of_faking(self):
        """未提供采样率时：available=False、spectrum=None，绝不产出假频谱"""
        va = VibrationAnalyzer()
        va.start()
        try:
            for i in range(128):
                va.feed_data('dev1', 'vibration_x', math.sin(2 * math.pi * 10 * i / 100))
            result = va.get_spectrum('dev1')
        finally:
            va.stop()

        assert result is not None
        assert result['available'] is False
        assert result['spectrum'] is None
        assert result['sample_rate'] is None
        assert '采样率' in result['reason']
        # 关键：不得出现任何"看起来对"的频谱数据
        assert 'frequencies' not in result

    def test_invalid_sample_rate_treated_as_unknown(self):
        """非法采样率(0/负数/非数值)按"未知"处理，不得被当作有效值使用"""
        for bad in (0, -100, 'abc', None):
            va = VibrationAnalyzer(config={'sample_rate': bad})
            assert va._sample_rate is None
            assert va.resolve_sample_rate('dev1') is None

    def test_spectrum_becomes_available_with_real_sample_rate(self):
        """提供真实采样率后频谱可用，且返回该采样率"""
        va = VibrationAnalyzer(config={'sample_rate': 200})
        va.start()
        try:
            self._feed_sine(va, 'dev1', 10.0, 200.0)
            result = va.get_spectrum('dev1')
        finally:
            va.stop()

        assert result['available'] is True
        assert result['sample_rate'] == 200
        assert result['spectrum'] is not None

    def test_dominant_frequency_matches_physical_frequency_at_200hz(self):
        """物理 10Hz 信号以 200Hz 采样：主频必须是 10Hz（若用假定的 100Hz 会算成 5Hz）"""
        va = VibrationAnalyzer(config={'device_sample_rates': {'dev1': 200}})
        va.start()
        try:
            self._feed_sine(va, 'dev1', 10.0, 200.0)
            result = va.get_spectrum('dev1')
        finally:
            va.stop()

        dominant = result['spectrum']['dominant_frequency_hz']
        assert abs(dominant - 10.0) < 0.6, f'主频应为10Hz，实际 {dominant}Hz（疑似使用了假采样率）'

    def test_dominant_frequency_matches_physical_frequency_at_500hz(self):
        """物理 30Hz 信号以 500Hz 采样：主频必须是 30Hz"""
        va = VibrationAnalyzer(config={'device_sample_rates': {'dev1': 500}})
        va.start()
        try:
            self._feed_sine(va, 'dev1', 30.0, 500.0)
            result = va.get_spectrum('dev1')
        finally:
            va.stop()

        dominant = result['spectrum']['dominant_frequency_hz']
        assert abs(dominant - 30.0) < 1.5, f'主频应为30Hz，实际 {dominant}Hz'

    def test_feed_sample_rate_overrides_config(self):
        """feed_data 随数据点上报的采样率优先级最高"""
        va = VibrationAnalyzer(config={'sample_rate': 100})
        va.start()
        try:
            self._feed_sine(va, 'dev1', 10.0, 200.0)  # 显式 200Hz
            result = va.get_spectrum('dev1')
        finally:
            va.stop()

        assert result['sample_rate'] == 200

    def test_do_fft_requires_explicit_sample_rate(self):
        """_do_fft 的采样率是必填参数：缺参/传 None 一律拒绝，防止复用假定频率"""
        va = VibrationAnalyzer()
        with pytest.raises(TypeError):
            va._do_fft([1.0] * 64)          # 缺参
        with pytest.raises(ValueError):
            va._do_fft([1.0] * 64, None)     # 显式 None
        with pytest.raises(ValueError):
            va._do_fft([1.0] * 64, 0)        # 非法 0

    def test_bearing_fault_skipped_without_sample_rate(self):
        """无采样率时不做轴承故障判定（不得给出"正常/故障"的假结论）"""
        va = VibrationAnalyzer()
        va.start()
        try:
            for i in range(128):
                va.feed_data('dev1', 'vibration_x', math.sin(2 * math.pi * 25 * i / 100))
            result = va.check_bearing_fault('dev1', 1500)
        finally:
            va.stop()

        assert result is not None
        assert result['available'] is False
        assert result['bearing_faults'] == {}
        assert result['fault_count'] == 0
        assert '跳过' in result['diagnosis']


# ============================================================
# 后台分析线程（2026-09 修复：start/stop 原为空实现）
# ============================================================

class TestAnalysisThread:

    def test_start_launches_thread(self):
        """start() 必须真正拉起后台分析线程"""
        va = VibrationAnalyzer()
        va.start()
        try:
            assert va._thread is not None
            assert va._thread.is_alive() is True
        finally:
            va.stop()

    def test_stop_joins_thread(self):
        """stop() 必须回收线程，而不是只翻一个标志位"""
        va = VibrationAnalyzer()
        va.start()
        thread = va._thread
        va.stop()
        assert thread is not None
        assert thread.is_alive() is False
        assert va._running is False

    def test_stop_without_start_is_safe(self):
        """未启动就 stop 不得抛异常"""
        VibrationAnalyzer().stop()

    def test_double_start_no_duplicate_thread(self):
        """重复 start 不得产生第二个线程"""
        va = VibrationAnalyzer()
        va.start()
        try:
            t1 = va._thread
            va.start()
            assert va._thread is t1
        finally:
            va.stop()

    def test_refresh_scores_recomputes_buffered_devices(self):
        """后台刷新会对已缓存设备重算评分（线程做的是实际工作，不是空转）"""
        va = VibrationAnalyzer()
        va.start()
        try:
            for i in range(30):
                va.feed_data('dev1', 'vibration_x', 0.5)
            va._scores.clear()  # 模拟评分丢失
            devices = va.refresh_scores()
            assert 'dev1' in devices
            assert va.get_device_vibration('dev1') is not None
        finally:
            va.stop()

    def test_refresh_scores_does_not_raise_on_empty(self):
        """无数据时刷新是安全的"""
        va = VibrationAnalyzer()
        assert va.refresh_scores() == []
