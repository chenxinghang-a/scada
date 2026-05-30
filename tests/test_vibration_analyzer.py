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
