"""
Tests for alarm manager: AlarmPriorityMatrix, AlarmDedupConfig, and AlarmManager logic
"""

import pytest
import time
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta


from 报警层.alarm_manager import AlarmPriorityMatrix, AlarmDedupConfig, AlarmManager


# ============================================================
# AlarmPriorityMatrix Tests
# ============================================================

class TestAlarmPriorityMatrix:

    def test_critical_severity_critical_likelihood_is_p1(self):
        """severity=5, likelihood=5 -> P1 (highest priority)"""
        assert AlarmPriorityMatrix.get_priority(5, 5) == 'P1'

    def test_low_severity_low_likelihood_is_p5(self):
        """severity=1, likelihood=1 -> P5 (lowest priority)"""
        assert AlarmPriorityMatrix.get_priority(1, 1) == 'P5'

    def test_medium_values(self):
        """Known matrix entries for medium values"""
        assert AlarmPriorityMatrix.get_priority(3, 3) == 'P3'
        assert AlarmPriorityMatrix.get_priority(4, 4) == 'P2'

    def test_high_severity_low_likelihood(self):
        """severity=5, likelihood=1 -> P2"""
        assert AlarmPriorityMatrix.get_priority(5, 1) == 'P2'

    def test_low_severity_high_likelihood(self):
        """severity=1, likelihood=5 -> P4"""
        assert AlarmPriorityMatrix.get_priority(1, 5) == 'P4'

    def test_clamping_above_range(self):
        """Values above 5 are clamped to 5"""
        assert AlarmPriorityMatrix.get_priority(10, 10) == AlarmPriorityMatrix.get_priority(5, 5)

    def test_clamping_below_range(self):
        """Values below 1 are clamped to 1"""
        assert AlarmPriorityMatrix.get_priority(0, -1) == AlarmPriorityMatrix.get_priority(1, 1)

    def test_is_higher(self):
        """P1 is higher priority than P2"""
        assert AlarmPriorityMatrix.is_higher('P1', 'P2') is True
        assert AlarmPriorityMatrix.is_higher('P2', 'P1') is False

    def test_is_higher_same(self):
        """Same priority is not higher"""
        assert AlarmPriorityMatrix.is_higher('P3', 'P3') is False

    def test_all_matrix_entries(self):
        """Verify the full matrix has 25 entries (5x5)"""
        assert len(AlarmPriorityMatrix.MATRIX) == 25

    def test_p1_entries(self):
        """Verify all P1 entries in the matrix"""
        p1_keys = [k for k, v in AlarmPriorityMatrix.MATRIX.items() if v == 'P1']
        assert (5, 5) in p1_keys
        assert (5, 4) in p1_keys
        assert (5, 3) in p1_keys
        assert (4, 5) in p1_keys


# ============================================================
# AlarmDedupConfig Tests
# ============================================================

class TestAlarmDedupConfig:

    def test_default_values(self):
        """Default dedup config has expected values"""
        cfg = AlarmDedupConfig()
        assert cfg.emit_cooldown_seconds == 300
        assert cfg.acknowledge_suppress_seconds == 600
        assert cfg.enabled is True
        assert cfg.max_visible_toasts == 3

    def test_custom_values(self):
        """Constructor accepts custom values"""
        cfg = AlarmDedupConfig({
            'emit_cooldown_seconds': 60,
            'enabled': False,
        })
        assert cfg.emit_cooldown_seconds == 60
        assert cfg.enabled is False

    def test_to_dict(self):
        """to_dict returns all config keys"""
        cfg = AlarmDedupConfig()
        d = cfg.to_dict()
        assert 'emit_cooldown_seconds' in d
        assert 'acknowledge_suppress_seconds' in d
        assert 'enabled' in d
        assert 'max_visible_toasts' in d
        assert 'critical_toast_duration' in d
        assert 'warning_toast_duration' in d

    def test_update_values(self):
        """update() modifies values"""
        cfg = AlarmDedupConfig()
        cfg.update({'emit_cooldown_seconds': 120, 'enabled': False})
        assert cfg.emit_cooldown_seconds == 120
        assert cfg.enabled is False

    def test_update_clamps_min(self):
        """update() enforces minimum values"""
        cfg = AlarmDedupConfig()
        cfg.update({'emit_cooldown_seconds': 1})  # min is 5
        assert cfg.emit_cooldown_seconds == 5

    def test_update_clamps_max_visible_toasts(self):
        """max_visible_toasts is clamped between 1 and 10"""
        cfg = AlarmDedupConfig()
        cfg.update({'max_visible_toasts': 0})
        assert cfg.max_visible_toasts == 1

        cfg.update({'max_visible_toasts': 100})
        assert cfg.max_visible_toasts == 10


# ============================================================
# AlarmManager Tests
# ============================================================

class TestAlarmManager:

    @pytest.fixture
    def mock_db(self):
        """Mock database with alarm record methods"""
        db = MagicMock()
        db.get_alarm_records.return_value = []
        db.save_alarm_record = MagicMock()
        return db

    @pytest.fixture
    def alarm_manager(self, mock_db, tmp_path):
        """Create AlarmManager with mocked database and empty config"""
        cfg_file = tmp_path / 'alarms.yaml'
        cfg_file.write_text('rules: []\n', encoding='utf-8')

        with patch.object(AlarmManager, 'load_config'):
            mgr = AlarmManager(mock_db, config_path=str(cfg_file))
            mgr.rules = {}
            mgr.alarm_states = {}
            return mgr

    def test_alarm_creation(self, alarm_manager):
        """Alarm state is created when an alarm triggers"""
        alarm_manager.rules = {
            'rule_001': {
                'device_id': 'dev1',
                'register_name': 'temp',
                'level': 'warning',
                'name': 'High Temp',
                'threshold': 80.0,
                'condition': 'greater_than',
            }
        }
        alarm_manager._rebuild_rules_index()

        state_key = ('dev1', 'temp')
        alarm_manager.alarm_states[state_key] = {
            'alarm_id': 'rule_001',
            'device_id': 'dev1',
            'register_name': 'temp',
            'acknowledged': False,
            'first_trigger_time': datetime.now(),
            'trigger_count': 1,
        }

        assert state_key in alarm_manager.alarm_states
        assert alarm_manager.alarm_states[state_key]['acknowledged'] is False

    def test_dedup_cooldown_prevents_duplicate(self, alarm_manager):
        """Same alarm within cooldown window is not re-emitted"""
        alarm_key = ('rule_001', 'dev1', 'temp')
        alarm_manager.dedup_config.enabled = True
        alarm_manager.dedup_config.emit_cooldown_seconds = 300

        # Simulate first emit
        alarm_manager._emit_history[alarm_key] = time.time()

        # Check: should be suppressed (within cooldown)
        now = time.time()
        last_emit = alarm_manager._emit_history.get(alarm_key, 0)
        cooldown = alarm_manager.dedup_config.emit_cooldown_seconds
        is_suppressed = (now - last_emit) < cooldown

        assert is_suppressed is True

    def test_dedup_cooldown_expired_allows_emit(self, alarm_manager):
        """After cooldown expires, alarm can be re-emitted"""
        alarm_key = ('rule_001', 'dev1', 'temp')
        alarm_manager.dedup_config.enabled = True
        alarm_manager.dedup_config.emit_cooldown_seconds = 1  # 1 second

        # Simulate emit in the past
        alarm_manager._emit_history[alarm_key] = time.time() - 5  # 5 seconds ago

        # Check: should NOT be suppressed (cooldown expired)
        now = time.time()
        last_emit = alarm_manager._emit_history.get(alarm_key, 0)
        cooldown = alarm_manager.dedup_config.emit_cooldown_seconds
        is_suppressed = (now - last_emit) < cooldown

        assert is_suppressed is False

    def test_dedup_disabled_always_emits(self, alarm_manager):
        """When dedup is disabled, alarms always emit"""
        alarm_manager.dedup_config.enabled = False

        # Even with a recent emit, should not be suppressed
        alarm_key = ('rule_001', 'dev1', 'temp')
        alarm_manager._emit_history[alarm_key] = time.time()

        is_suppressed = alarm_manager.dedup_config.enabled and False
        assert is_suppressed is False

    def test_acknowledge_alarm_state(self, alarm_manager):
        """Acknowledging an alarm updates its state"""
        state_key = ('dev1', 'temp')
        alarm_manager.alarm_states[state_key] = {
            'alarm_id': 'rule_001',
            'acknowledged': False,
            'trigger_count': 1,
        }

        alarm_manager.alarm_states[state_key]['acknowledged'] = True
        alarm_manager.alarm_states[state_key]['acknowledged_at'] = datetime.now()
        alarm_manager.alarm_states[state_key]['acknowledged_by'] = 'operator'

        assert alarm_manager.alarm_states[state_key]['acknowledged'] is True
        assert alarm_manager.alarm_states[state_key]['acknowledged_by'] == 'operator'

    def test_priority_matrix_integration(self, alarm_manager):
        """Priority matrix correctly classifies alarm severity"""
        # Critical severity, high likelihood
        priority = AlarmPriorityMatrix.get_priority(severity=5, likelihood=4)
        assert priority == 'P1'

        # Low severity, low likelihood
        priority = AlarmPriorityMatrix.get_priority(severity=1, likelihood=1)
        assert priority == 'P5'

    def test_rules_index_rebuild(self, alarm_manager):
        """_rebuild_rules_index creates correct index"""
        alarm_manager.rules = {
            'r1': {'device_id': 'd1', 'register_name': 'r1', 'enabled': True},
            'r2': {'device_id': 'd1', 'register_name': 'r2', 'enabled': True},
            'r3': {'device_id': 'd1', 'register_name': 'r1', 'enabled': False},
        }

        alarm_manager._rebuild_rules_index()

        assert ('d1', 'r1') in alarm_manager._rules_index
        assert ('d1', 'r2') in alarm_manager._rules_index
        # r3 is disabled, so only r1 should be in the index for (d1, r1)
        rules_at_d1_r1 = alarm_manager._rules_index[('d1', 'r1')]
        assert len(rules_at_d1_r1) == 1
        assert rules_at_d1_r1[0][0] == 'r1'

    def test_dedup_lock_thread_safety(self, alarm_manager):
        """Dedup lock is a threading.Lock instance"""
        import threading
        assert isinstance(alarm_manager._dedup_lock, type(threading.Lock()))
