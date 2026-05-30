"""
Tests for 智能层.oee_calculator: OEE calculation, shift tracking, grade assignment
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from 智能层.oee_calculator import OEECalculator


@pytest.fixture
def oee():
    """Create OEECalculator with mocked database"""
    return OEECalculator(MagicMock())


# ============================================================
# Shift Tracking Tests
# ============================================================

class TestShiftTracking:

    def test_start_shift_initializes_data(self, oee):
        """start_shift initializes shift data for device"""
        oee.start_shift('dev1', planned_hours=8.0)
        sd = oee.shift_data['dev1']
        assert sd['shift_start'] is not None
        assert sd['planned_production_time'] == 8 * 3600
        assert sd['actual_run_time'] == 0
        assert sd['total_count'] == 0
        assert sd['good_count'] == 0

    def test_start_shift_custom_hours(self, oee):
        """start_shift with custom planned hours"""
        oee.start_shift('dev1', planned_hours=12.0)
        assert oee.shift_data['dev1']['planned_production_time'] == 12 * 3600

    def test_start_shift_resets_previous_data(self, oee):
        """start_shift resets data from previous shift"""
        oee.start_shift('dev1')
        oee.shift_data['dev1']['total_count'] = 100
        oee.start_shift('dev1')
        assert oee.shift_data['dev1']['total_count'] == 0


# ============================================================
# Device State Tracking Tests
# ============================================================

class TestDeviceState:

    def test_update_device_state_sets_status(self, oee):
        """update_device_state stores current status"""
        oee.update_device_state('dev1', 'running')
        state = oee.get_device_state('dev1')
        assert state['status'] == 'running'

    def test_update_device_state_tracks_duration(self, oee):
        """Transitioning state accumulates run time"""
        now = datetime.now()
        oee.device_states['dev1'] = {'status': 'running', 'since': now - timedelta(seconds=60)}
        oee.shift_data['dev1'] = {
            'shift_start': now - timedelta(hours=1),
            'planned_production_time': 28800,
            'actual_run_time': 0,
            'downtime': 0,
            'total_count': 0,
            'good_count': 0,
            'ideal_cycle_time': 60.0,
        }
        oee.update_device_state('dev1', 'stopped')
        # Should have accumulated ~60 seconds of run time
        assert oee.shift_data['dev1']['actual_run_time'] >= 55

    def test_update_device_state_downtime(self, oee):
        """Stopped state accumulates downtime"""
        now = datetime.now()
        oee.device_states['dev1'] = {'status': 'stopped', 'since': now - timedelta(seconds=30)}
        oee.shift_data['dev1'] = {
            'shift_start': now - timedelta(hours=1),
            'planned_production_time': 28800,
            'actual_run_time': 0,
            'downtime': 0,
            'total_count': 0,
            'good_count': 0,
            'ideal_cycle_time': 60.0,
        }
        oee.update_device_state('dev1', 'running')
        assert oee.shift_data['dev1']['downtime'] >= 25

    def test_get_all_device_states(self, oee):
        """get_all_device_states returns all device states"""
        oee.update_device_state('dev1', 'running')
        oee.update_device_state('dev2', 'stopped')
        states = oee.get_all_device_states()
        assert 'dev1' in states
        assert 'dev2' in states


# ============================================================
# Production Recording Tests
# ============================================================

class TestProductionRecording:

    def test_record_production_absolute_count(self, oee):
        """record_production with absolute count calculates delta"""
        oee.start_shift('dev1')
        oee.record_production('dev1', count=100)
        assert oee.shift_data['dev1']['total_count'] == 100

    def test_record_production_incremental(self, oee):
        """Consecutive calls accumulate correctly"""
        oee.start_shift('dev1')
        oee.record_production('dev1', count=100)
        oee.record_production('dev1', count=200)
        assert oee.shift_data['dev1']['total_count'] == 200

    def test_record_production_counter_reset(self, oee):
        """Counter reset (negative delta) uses new value as delta"""
        oee.start_shift('dev1')
        oee.record_production('dev1', count=100)
        oee.record_production('dev1', count=10)  # counter reset
        # delta = 10 (since 10 < 100, treated as reset)
        assert oee.shift_data['dev1']['total_count'] == 110

    def test_record_production_good_count(self, oee):
        """record_production tracks good_count separately"""
        oee.start_shift('dev1')
        oee.record_production('dev1', good_count=80)
        assert oee.shift_data['dev1']['good_count'] == 80

    def test_record_production_no_args_increments(self, oee):
        """record_production with no args increments both by 1"""
        oee.start_shift('dev1')
        oee.record_production('dev1')
        assert oee.shift_data['dev1']['total_count'] == 1
        assert oee.shift_data['dev1']['good_count'] == 1


# ============================================================
# Theoretical Rate Tests
# ============================================================

class TestTheoreticalRate:

    def test_set_theoretical_rate(self, oee):
        """set_theoretical_rate sets rate and ideal cycle time"""
        oee.start_shift('dev1')
        oee.set_theoretical_rate('dev1', 60.0)
        assert oee.theoretical_rates['dev1'] == 60.0
        assert oee.shift_data['dev1']['ideal_cycle_time'] == 60.0  # 3600/60

    def test_set_theoretical_rate_calculates_cycle_time(self, oee):
        """Ideal cycle time = 3600 / rate"""
        oee.start_shift('dev1')
        oee.set_theoretical_rate('dev1', 120.0)
        assert oee.shift_data['dev1']['ideal_cycle_time'] == 30.0


# ============================================================
# OEE Calculation Tests
# ============================================================

class TestOEECalculation:

    def test_oee_perfect_score(self, oee):
        """Perfect production gives OEE near 1.0"""
        oee.start_shift('dev1', planned_hours=1)
        oee.shift_data['dev1']['actual_run_time'] = 3600
        oee.shift_data['dev1']['ideal_cycle_time'] = 60.0
        oee.shift_data['dev1']['total_count'] = 60  # 3600/60 = perfect
        oee.shift_data['dev1']['good_count'] = 60
        oee.device_states['dev1'] = {'status': 'stopped', 'since': datetime.now()}

        result = oee.calculate_oee('dev1')
        assert result is not None
        assert result['availability'] >= 0.99
        assert result['performance'] >= 0.99
        assert result['quality'] >= 0.99
        assert result['oee'] >= 0.95

    def test_oee_with_downtime(self, oee):
        """Downtime reduces availability"""
        oee.start_shift('dev1', planned_hours=1)
        oee.shift_data['dev1']['actual_run_time'] = 1800  # only 50% uptime
        oee.shift_data['dev1']['ideal_cycle_time'] = 60.0
        oee.shift_data['dev1']['total_count'] = 30
        oee.shift_data['dev1']['good_count'] = 30
        oee.device_states['dev1'] = {'status': 'stopped', 'since': datetime.now()}

        result = oee.calculate_oee('dev1')
        assert result is not None
        assert result['availability'] < 0.55

    def test_oee_with_defects(self, oee):
        """Defects reduce quality"""
        oee.start_shift('dev1', planned_hours=1)
        oee.shift_data['dev1']['actual_run_time'] = 3600
        oee.shift_data['dev1']['ideal_cycle_time'] = 60.0
        oee.shift_data['dev1']['total_count'] = 60
        oee.shift_data['dev1']['good_count'] = 50  # 10 defects
        oee.device_states['dev1'] = {'status': 'stopped', 'since': datetime.now()}

        result = oee.calculate_oee('dev1')
        assert result is not None
        assert result['quality'] < 0.85

    def test_oee_formula(self, oee):
        """OEE = A * P * Q"""
        oee.start_shift('dev1', planned_hours=1)
        oee.shift_data['dev1']['actual_run_time'] = 3000
        oee.shift_data['dev1']['ideal_cycle_time'] = 60.0
        oee.shift_data['dev1']['total_count'] = 45
        oee.shift_data['dev1']['good_count'] = 40
        oee.device_states['dev1'] = {'status': 'stopped', 'since': datetime.now()}

        result = oee.calculate_oee('dev1')
        expected_oee = result['availability'] * result['performance'] * result['quality']
        assert abs(result['oee'] - expected_oee) < 0.001

    def test_oee_no_shift_returns_none(self, oee):
        """calculate_oee returns None when no shift data"""
        assert oee.calculate_oee('unknown_device') is None

    def test_oee_zero_production_time_returns_none(self, oee):
        """Zero planned time returns None"""
        oee.start_shift('dev1', planned_hours=0)
        # Force planned to 0
        oee.shift_data['dev1']['planned_production_time'] = 0
        assert oee.calculate_oee('dev1') is None

    def test_oee_no_production_quality_is_1(self, oee):
        """Zero production yields quality = 1.0 (no defects)"""
        oee.start_shift('dev1', planned_hours=1)
        oee.shift_data['dev1']['actual_run_time'] = 3600
        oee.shift_data['dev1']['ideal_cycle_time'] = 60.0
        oee.shift_data['dev1']['total_count'] = 0
        oee.shift_data['dev1']['good_count'] = 0
        oee.device_states['dev1'] = {'status': 'stopped', 'since': datetime.now()}

        result = oee.calculate_oee('dev1')
        assert result['quality'] == 1.0


# ============================================================
# OEE Grade Tests
# ============================================================

class TestOEEGrade:

    def test_world_class_grade(self, oee):
        """OEE >= 0.85 is world-class"""
        assert oee._oee_grade(0.90) == '世界级'
        assert oee._oee_grade(0.85) == '世界级'

    def test_excellent_grade(self, oee):
        """0.75 <= OEE < 0.85 is excellent"""
        assert oee._oee_grade(0.80) == '优秀'
        assert oee._oee_grade(0.75) == '优秀'

    def test_good_grade(self, oee):
        """0.65 <= OEE < 0.75 is good"""
        assert oee._oee_grade(0.70) == '良好'
        assert oee._oee_grade(0.65) == '良好'

    def test_average_grade(self, oee):
        """0.50 <= OEE < 0.65 is average"""
        assert oee._oee_grade(0.55) == '一般'
        assert oee._oee_grade(0.50) == '一般'

    def test_needs_improvement_grade(self, oee):
        """OEE < 0.50 needs improvement"""
        assert oee._oee_grade(0.30) == '需改进'
        assert oee._oee_grade(0.0) == '需改进'

    def test_grade_in_result(self, oee):
        """calculate_oee includes grade in result"""
        oee.start_shift('dev1', planned_hours=1)
        oee.shift_data['dev1']['actual_run_time'] = 3600
        oee.shift_data['dev1']['ideal_cycle_time'] = 60.0
        oee.shift_data['dev1']['total_count'] = 60
        oee.shift_data['dev1']['good_count'] = 60
        oee.device_states['dev1'] = {'status': 'stopped', 'since': datetime.now()}
        result = oee.calculate_oee('dev1')
        assert 'grade' in result
        assert result['grade'] in ('世界级', '优秀', '良好', '一般', '需改进')


# ============================================================
# Losses Analysis Tests
# ============================================================

class TestLossesAnalysis:

    def test_losses_structure(self, oee):
        """Losses dict has expected keys"""
        oee.start_shift('dev1', planned_hours=1)
        oee.shift_data['dev1']['actual_run_time'] = 3000
        oee.shift_data['dev1']['downtime'] = 600
        oee.shift_data['dev1']['ideal_cycle_time'] = 60.0
        oee.shift_data['dev1']['total_count'] = 40
        oee.shift_data['dev1']['good_count'] = 35
        oee.device_states['dev1'] = {'status': 'stopped', 'since': datetime.now()}

        result = oee.calculate_oee('dev1')
        losses = result['losses']
        assert '故障停机损失_秒' in losses
        assert '可用率损失占比_百分比' in losses
        assert '性能损失_秒' in losses
        assert '不良品数' in losses
        assert '质量损失_百分比' in losses or '质量率损失占比_百分比' in losses


# ============================================================
# Query API Tests
# ============================================================

class TestQueryAPI:

    def test_get_all_oee(self, oee):
        """get_all_oee returns OEE for all devices with shift data"""
        oee.start_shift('dev1', planned_hours=1)
        oee.shift_data['dev1']['actual_run_time'] = 3600
        oee.shift_data['dev1']['ideal_cycle_time'] = 60.0
        oee.shift_data['dev1']['total_count'] = 60
        oee.shift_data['dev1']['good_count'] = 60
        oee.device_states['dev1'] = {'status': 'stopped', 'since': datetime.now()}

        all_oee = oee.get_all_oee()
        assert 'dev1' in all_oee

    def test_get_device_oee(self, oee):
        """get_device_oee returns OEE for specific device"""
        oee.start_shift('dev1', planned_hours=1)
        oee.shift_data['dev1']['actual_run_time'] = 3600
        oee.shift_data['dev1']['ideal_cycle_time'] = 60.0
        oee.shift_data['dev1']['total_count'] = 60
        oee.shift_data['dev1']['good_count'] = 60
        oee.device_states['dev1'] = {'status': 'stopped', 'since': datetime.now()}

        result = oee.get_device_oee('dev1')
        assert result is not None

    def test_get_oee_history(self, oee):
        """get_oee_history returns historical records"""
        oee.oee_history = [
            {'device_id': 'dev1', 'oee': 0.85},
            {'device_id': 'dev2', 'oee': 0.90},
        ]
        assert len(oee.get_oee_history()) == 2
        assert len(oee.get_oee_history('dev1')) == 1


# ============================================================
# Start/Stop Tests
# ============================================================

class TestStartStop:

    def test_start_creates_thread(self, oee):
        """start() creates calc thread"""
        oee.start()
        assert oee._running is True
        oee.stop()

    def test_double_start_no_op(self, oee):
        """Calling start() twice doesn't create duplicate threads"""
        oee.start()
        t1 = oee._thread
        oee.start()
        assert oee._thread is t1
        oee.stop()

    def test_stop_joins_thread(self, oee):
        """stop() joins the calc thread"""
        oee.start()
        oee.stop()
        assert oee._running is False
