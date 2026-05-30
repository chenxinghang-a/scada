"""
Tests for 智能层.predictive_maintenance: feed_data, anomaly detection, health score, thresholds
"""

import pytest
import math
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from 智能层.predictive_maintenance import PredictiveMaintenance


@pytest.fixture
def pm():
    """Create PredictiveMaintenance with mocked database"""
    db = MagicMock()
    return PredictiveMaintenance(db)


@pytest.fixture
def pm_with_thresholds():
    """Create PredictiveMaintenance with custom thresholds"""
    db = MagicMock()
    return PredictiveMaintenance(db, config={
        'thresholds': {
            'temperature': {'upper': 100.0, 'lower': -10.0},
        }
    })


# ============================================================
# Feed Data Tests
# ============================================================

class TestFeedData:

    def test_feed_data_stores_in_window(self, pm):
        """feed_data stores data in sliding window"""
        pm.feed_data('dev1', 'temperature', 25.0)
        key = 'dev1:temperature'
        assert key in pm.data_windows
        assert len(pm.data_windows[key]) == 1
        assert pm.data_windows[key][0]['value'] == 25.0

    def test_feed_data_multiple_points(self, pm):
        """Multiple feed_data calls accumulate in window"""
        for i in range(10):
            pm.feed_data('dev1', 'temp', float(i))
        assert len(pm.data_windows['dev1:temp']) == 10

    def test_feed_data_with_timestamp(self, pm):
        """feed_data uses provided timestamp"""
        ts = datetime(2025, 1, 1, 12, 0, 0)
        pm.feed_data('dev1', 'temp', 30.0, timestamp=ts)
        assert pm.data_windows['dev1:temp'][0]['timestamp'] == ts

    def test_feed_data_uses_default_timestamp(self, pm):
        """feed_data uses current time when no timestamp given"""
        pm.feed_data('dev1', 'temp', 30.0)
        ts = pm.data_windows['dev1:temp'][0]['timestamp']
        assert isinstance(ts, datetime)

    def test_feed_data_window_maxlen(self, pm):
        """Sliding window respects maxlen"""
        for i in range(600):
            pm.feed_data('dev1', 'temp', float(i))
        assert len(pm.data_windows['dev1:temp']) == pm.window_size

    def test_feed_data_different_keys(self, pm):
        """Different device/register pairs get separate windows"""
        pm.feed_data('dev1', 'temp', 25.0)
        pm.feed_data('dev2', 'pressure', 1.0)
        assert 'dev1:temp' in pm.data_windows
        assert 'dev2:pressure' in pm.data_windows
        assert len(pm.data_windows['dev1:temp']) == 1
        assert len(pm.data_windows['dev2:pressure']) == 1


# ============================================================
# Anomaly Detection Tests
# ============================================================

class TestAnomalyDetection:

    def _fill_window(self, pm, key, values, base_time=None):
        """Helper: fill data window with values"""
        base = base_time or datetime.now()
        for i, v in enumerate(values):
            ts = base + timedelta(seconds=i)
            pm.data_windows[key].append({
                'value': v,
                'timestamp': ts,
                'ts_epoch': ts.timestamp(),
            })

    def test_no_anomalies_in_normal_data(self, pm):
        """Normal data has no anomalies"""
        values = [50.0] * 20
        self._fill_window(pm, 'd:t', values)
        window = list(pm.data_windows['d:t'])
        anomalies = pm._detect_anomalies(window)
        assert len(anomalies) == 0

    def test_detects_z_score_anomaly(self, pm):
        """Z-Score method detects outlier"""
        values = [50.0] * 20 + [500.0]  # 500 is an extreme outlier
        self._fill_window(pm, 'd:t', values)
        window = list(pm.data_windows['d:t'])
        anomalies = pm._detect_anomalies(window)
        # Should detect at least one anomaly
        assert len(anomalies) > 0

    def test_detects_iqr_anomaly(self, pm):
        """IQR method detects outlier"""
        # All same value except one far outlier
        values = [10.0] * 50 + [1000.0]
        self._fill_window(pm, 'd:t', values)
        window = list(pm.data_windows['d:t'])
        anomalies = pm._detect_anomalies(window)
        assert len(anomalies) > 0

    def test_insufficient_data_returns_empty(self, pm):
        """Less than 5 data points returns no anomalies"""
        values = [1.0, 2.0, 3.0]
        self._fill_window(pm, 'd:t', values)
        window = list(pm.data_windows['d:t'])
        anomalies = pm._detect_anomalies(window)
        assert anomalies == []

    def test_constant_data_no_anomalies(self, pm):
        """Constant data (std=0) has no anomalies"""
        values = [42.0] * 20
        self._fill_window(pm, 'd:t', values)
        window = list(pm.data_windows['d:t'])
        anomalies = pm._detect_anomalies(window)
        assert len(anomalies) == 0


# ============================================================
# Health Score Tests
# ============================================================

class TestHealthScore:

    def _fill_window(self, pm, key, values, base_time=None):
        base = base_time or datetime.now()
        for i, v in enumerate(values):
            ts = base + timedelta(seconds=i)
            pm.data_windows[key].append({
                'value': v,
                'timestamp': ts,
                'ts_epoch': ts.timestamp(),
            })

    def test_perfect_health_for_stable_data(self, pm):
        """Stable data yields high health score"""
        values = [100.0] * 50
        self._fill_window(pm, 'd:t', values)
        window = list(pm.data_windows['d:t'])
        trend = pm._analyze_trend(window)
        anomalies = pm._detect_anomalies(window)
        health = pm._calculate_health_score('d', 't', window, trend, anomalies)
        assert health >= 90

    def test_low_health_for_noisy_data(self, pm):
        """Highly variable data yields lower health score than stable data"""
        import random
        random.seed(42)
        values = [random.uniform(0, 1000) for _ in range(100)]
        self._fill_window(pm, 'd:t', values)
        window = list(pm.data_windows['d:t'])
        trend = pm._analyze_trend(window)
        anomalies = pm._detect_anomalies(window)
        health = pm._calculate_health_score('d', 't', window, trend, anomalies)
        # Noisy data should have lower health than stable data
        assert health < 100

    def test_health_score_range(self, pm):
        """Health score is always between 0 and 100"""
        values = list(range(100))
        self._fill_window(pm, 'd:t', values)
        window = list(pm.data_windows['d:t'])
        trend = pm._analyze_trend(window)
        anomalies = pm._detect_anomalies(window)
        health = pm._calculate_health_score('d', 't', window, trend, anomalies)
        assert 0 <= health <= 100

    def test_insufficient_data_returns_100(self, pm):
        """Less than 5 data points returns 100 (default healthy)"""
        values = [1.0, 2.0, 3.0]
        self._fill_window(pm, 'd:t', values)
        window = list(pm.data_windows['d:t'])
        trend = pm._analyze_trend(window)
        anomalies = pm._detect_anomalies(window)
        health = pm._calculate_health_score('d', 't', window, trend, anomalies)
        assert health == 100.0


# ============================================================
# Trend Analysis Tests
# ============================================================

class TestTrendAnalysis:

    def _fill_window(self, pm, key, values, base_time=None):
        base = base_time or datetime.now()
        for i, v in enumerate(values):
            ts = base + timedelta(seconds=i)
            pm.data_windows[key].append({
                'value': v,
                'timestamp': ts,
                'ts_epoch': ts.timestamp(),
            })

    def test_stable_trend(self, pm):
        """Constant data shows stable trend"""
        values = [50.0] * 20
        self._fill_window(pm, 'd:t', values)
        window = list(pm.data_windows['d:t'])
        trend = pm._analyze_trend(window)
        assert trend['direction'] == 'stable'
        assert abs(trend['slope']) < 1e-6

    def test_rising_trend(self, pm):
        """Increasing data shows rising trend"""
        values = list(range(20))
        self._fill_window(pm, 'd:t', values)
        window = list(pm.data_windows['d:t'])
        trend = pm._analyze_trend(window)
        assert trend['direction'] == 'rising'
        assert trend['slope'] > 0

    def test_falling_trend(self, pm):
        """Decreasing data shows falling trend"""
        values = list(range(20, 0, -1))
        self._fill_window(pm, 'd:t', values)
        window = list(pm.data_windows['d:t'])
        trend = pm._analyze_trend(window)
        assert trend['direction'] == 'falling'
        assert trend['slope'] < 0

    def test_single_point_returns_stable(self, pm):
        """Single data point returns stable trend"""
        values = [50.0]
        self._fill_window(pm, 'd:t', values)
        window = list(pm.data_windows['d:t'])
        trend = pm._analyze_trend(window)
        assert trend['direction'] == 'stable'

    def test_r_squared_near_1_for_linear(self, pm):
        """Perfect linear data has R-squared near 1"""
        values = [float(i) for i in range(50)]
        self._fill_window(pm, 'd:t', values)
        window = list(pm.data_windows['d:t'])
        trend = pm._analyze_trend(window)
        assert trend['r_squared'] > 0.99


# ============================================================
# Threshold & Prediction Tests
# ============================================================

class TestThresholdAndPrediction:

    def _fill_window(self, pm, key, values, base_time=None):
        base = base_time or datetime.now()
        for i, v in enumerate(values):
            ts = base + timedelta(seconds=i)
            pm.data_windows[key].append({
                'value': v,
                'timestamp': ts,
                'ts_epoch': ts.timestamp(),
            })

    def test_set_threshold(self, pm):
        """set_threshold stores threshold for key"""
        pm.set_threshold('dev1:temp', upper=100.0, lower=0.0)
        assert pm.thresholds['dev1:temp']['upper'] == 100.0
        assert pm.thresholds['dev1:temp']['lower'] == 0.0

    def test_set_threshold_partial(self, pm):
        """set_threshold with only upper sets only upper"""
        pm.set_threshold('dev1:temp', upper=80.0)
        assert pm.thresholds['dev1:temp']['upper'] == 80.0
        assert 'lower' not in pm.thresholds['dev1:temp']

    def test_predict_failure_with_rising_trend(self, pm_with_thresholds):
        """Rising trend predicts when upper limit is hit"""
        pm = pm_with_thresholds
        # Feed rising temperature data
        values = [80.0 + i * 0.5 for i in range(30)]
        base = datetime.now()
        for i, v in enumerate(values):
            ts = base + timedelta(seconds=i * 60)
            pm.feed_data('dev1', 'temperature', v, timestamp=ts)
        window = list(pm.data_windows['dev1:temperature'])
        trend = pm._analyze_trend(window)
        failure = pm._predict_failure('dev1', 'temperature', window, trend)
        assert failure is not None
        assert failure['limit_type'] == 'upper'
        assert failure['days_to_limit'] >= 0

    def test_predict_failure_no_threshold(self, pm):
        """No threshold configured returns None prediction"""
        values = [50.0 + i for i in range(20)]
        base = datetime.now()
        for i, v in enumerate(values):
            ts = base + timedelta(seconds=i)
            pm.feed_data('dev1', 'temp', v, timestamp=ts)
        window = list(pm.data_windows['dev1:temp'])
        trend = pm._analyze_trend(window)
        failure = pm._predict_failure('dev1', 'temp', window, trend)
        assert failure is None

    def test_predict_failure_flat_trend(self, pm):
        """Flat trend returns None prediction"""
        pm.set_threshold('dev1:temp', upper=100.0)
        values = [50.0] * 20
        base = datetime.now()
        for i, v in enumerate(values):
            ts = base + timedelta(seconds=i)
            pm.feed_data('dev1', 'temp', v, timestamp=ts)
        window = list(pm.data_windows['dev1:temp'])
        trend = pm._analyze_trend(window)
        failure = pm._predict_failure('dev1', 'temp', window, trend)
        assert failure is None


# ============================================================
# Public Query API Tests
# ============================================================

class TestPublicAPI:

    def test_get_health_scores_empty(self, pm):
        """get_health_scores returns empty initially"""
        assert pm.get_health_scores() == {}

    def test_get_health_scores_after_analysis(self, pm):
        """get_health_scores returns cached scores after analysis"""
        # Manually set a score
        pm.health_scores['d1:t'] = {
            'device_id': 'd1', 'register_name': 't',
            'health_score': 85.0, 'trend': {},
            'anomaly_count': 0, 'failure_prediction': None,
            'updated_at': datetime.now().isoformat(),
        }
        scores = pm.get_health_scores()
        assert 'd1:t' in scores
        assert scores['d1:t']['health_score'] == 85.0

    def test_get_device_health(self, pm):
        """get_device_health filters by device_id"""
        pm.health_scores['d1:t'] = {'device_id': 'd1', 'register_name': 't', 'health_score': 80}
        pm.health_scores['d2:p'] = {'device_id': 'd2', 'register_name': 'p', 'health_score': 90}
        result = pm.get_device_health('d1')
        assert len(result) == 1
        assert 'd1:t' in result

    def test_get_maintenance_alerts(self, pm):
        """get_maintenance_alerts returns alerts list"""
        pm.maintenance_alerts = [
            {'device_id': 'd1', 'severity': 'warning', 'message': 'test'}
        ]
        alerts = pm.get_maintenance_alerts()
        assert len(alerts) == 1

    def test_get_trend_data_empty(self, pm):
        """get_trend_data returns empty for unknown register"""
        result = pm.get_trend_data('dev1', 'unknown')
        assert result == {}

    def test_get_trend_data_with_data(self, pm):
        """get_trend_data returns summary for known register"""
        pm.feed_data('dev1', 'temp', 25.0)
        pm.feed_data('dev1', 'temp', 30.0)
        result = pm.get_trend_data('dev1', 'temp')
        assert result['data_points'] == 2
        assert result['current_value'] == 30.0
        assert result['min_value'] == 25.0
        assert result['max_value'] == 30.0


# ============================================================
# Start/Stop Tests
# ============================================================

class TestStartStop:

    def test_start_creates_thread(self, pm):
        """start() creates and starts analysis thread"""
        pm.start()
        assert pm._running is True
        assert pm._thread is not None
        pm.stop()

    def test_double_start_no_op(self, pm):
        """Calling start() twice doesn't create duplicate threads"""
        pm.start()
        thread1 = pm._thread
        pm.start()
        assert pm._thread is thread1
        pm.stop()

    def test_stop_joins_thread(self, pm):
        """stop() stops the analysis thread"""
        pm.start()
        pm.stop()
        assert pm._running is False
