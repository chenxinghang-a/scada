"""
Tests for 智能层.edge_decision: rule evaluation, interlock checking, PID controller
"""

import pytest
import time
from unittest.mock import MagicMock

from 智能层.edge_decision import EdgeDecisionEngine


@pytest.fixture
def engine():
    """Create EdgeDecisionEngine with mocked database"""
    return EdgeDecisionEngine(MagicMock())


@pytest.fixture
def engine_with_rules(engine):
    """Engine with pre-configured rules and interlocks"""
    engine.add_rule('temp_rule', condition={
        'type': 'threshold', 'key': 'dev1:temp', 'operator': 'gt', 'value': 80.0
    }, action={'type': 'set_alarm', 'message': 'High temp', 'level': 'warning'}, name='Temp Rule')

    engine.add_interlock('temp_interlock', condition={
        'type': 'threshold', 'key': 'dev1:temp', 'operator': 'gt', 'value': 150.0
    }, action={'type': 'set_alarm', 'message': 'Critical temp!', 'level': 'critical'}, name='Temp Interlock')

    engine.add_pid_controller('temp_pid', input_key='dev1:temp', output_key='dev1:fan_speed',
                              setpoint=75.0, kp=2.0, ki=0.5, kd=0.1,
                              output_min=0, output_max=1500)
    return engine


# ============================================================
# Rule Evaluation Tests
# ============================================================

class TestRuleEvaluation:

    def test_gt_condition_true(self, engine):
        """gt condition returns True when value > threshold"""
        result = engine._evaluate_condition(
            {'type': 'threshold', 'key': 'dev1:temp', 'operator': 'gt', 'value': 80.0},
            {'dev1:temp': 100.0}
        )
        assert result is True

    def test_gt_condition_false(self, engine):
        """gt condition returns False when value <= threshold"""
        result = engine._evaluate_condition(
            {'type': 'threshold', 'key': 'dev1:temp', 'operator': 'gt', 'value': 80.0},
            {'dev1:temp': 50.0}
        )
        assert result is False

    def test_lt_condition(self, engine):
        """lt condition works correctly"""
        assert engine._evaluate_condition(
            {'type': 'threshold', 'key': 'dev1:temp', 'operator': 'lt', 'value': 80.0},
            {'dev1:temp': 50.0}
        ) is True
        assert engine._evaluate_condition(
            {'type': 'threshold', 'key': 'dev1:temp', 'operator': 'lt', 'value': 80.0},
            {'dev1:temp': 100.0}
        ) is False

    def test_eq_condition(self, engine):
        """eq condition uses approximate comparison"""
        assert engine._evaluate_condition(
            {'type': 'threshold', 'key': 'dev1:temp', 'operator': 'eq', 'value': 80.0},
            {'dev1:temp': 80.0005}
        ) is True
        assert engine._evaluate_condition(
            {'type': 'threshold', 'key': 'dev1:temp', 'operator': 'eq', 'value': 80.0},
            {'dev1:temp': 85.0}
        ) is False

    def test_gte_condition(self, engine):
        """gte condition works correctly"""
        assert engine._evaluate_condition(
            {'type': 'threshold', 'key': 'dev1:temp', 'operator': 'gte', 'value': 80.0},
            {'dev1:temp': 80.0}
        ) is True
        assert engine._evaluate_condition(
            {'type': 'threshold', 'key': 'dev1:temp', 'operator': 'gte', 'value': 80.0},
            {'dev1:temp': 79.0}
        ) is False

    def test_lte_condition(self, engine):
        """lte condition works correctly"""
        assert engine._evaluate_condition(
            {'type': 'threshold', 'key': 'dev1:temp', 'operator': 'lte', 'value': 80.0},
            {'dev1:temp': 80.0}
        ) is True
        assert engine._evaluate_condition(
            {'type': 'threshold', 'key': 'dev1:temp', 'operator': 'lte', 'value': 80.0},
            {'dev1:temp': 81.0}
        ) is False

    def test_between_condition(self, engine):
        """between condition works correctly"""
        assert engine._evaluate_condition(
            {'type': 'threshold', 'key': 'dev1:temp', 'operator': 'between', 'value': 50.0, 'value2': 100.0},
            {'dev1:temp': 75.0}
        ) is True
        assert engine._evaluate_condition(
            {'type': 'threshold', 'key': 'dev1:temp', 'operator': 'between', 'value': 50.0, 'value2': 100.0},
            {'dev1:temp': 200.0}
        ) is False

    def test_missing_key_returns_false(self, engine):
        """Missing data key returns False"""
        result = engine._evaluate_condition(
            {'type': 'threshold', 'key': 'dev1:missing', 'operator': 'gt', 'value': 0},
            {'dev1:temp': 100.0}
        )
        assert result is False

    def test_and_condition(self, engine):
        """AND condition requires all sub-conditions to be true"""
        result = engine._evaluate_condition(
            {'type': 'and', 'conditions': [
                {'type': 'threshold', 'key': 'a', 'operator': 'gt', 'value': 0},
                {'type': 'threshold', 'key': 'b', 'operator': 'gt', 'value': 0},
            ]},
            {'a': 5, 'b': 10}
        )
        assert result is True

        result = engine._evaluate_condition(
            {'type': 'and', 'conditions': [
                {'type': 'threshold', 'key': 'a', 'operator': 'gt', 'value': 0},
                {'type': 'threshold', 'key': 'b', 'operator': 'gt', 'value': 0},
            ]},
            {'a': 5, 'b': -1}
        )
        assert result is False

    def test_or_condition(self, engine):
        """OR condition requires at least one sub-condition to be true"""
        result = engine._evaluate_condition(
            {'type': 'or', 'conditions': [
                {'type': 'threshold', 'key': 'a', 'operator': 'gt', 'value': 100},
                {'type': 'threshold', 'key': 'b', 'operator': 'gt', 'value': 0},
            ]},
            {'a': 5, 'b': 10}
        )
        assert result is True

    def test_unknown_condition_type(self, engine):
        """Unknown condition type returns False"""
        result = engine._evaluate_condition({'type': 'unknown'}, {})
        assert result is False


# ============================================================
# Interlock Tests
# ============================================================

class TestInterlockChecking:

    def test_add_interlock(self, engine):
        """add_interlock stores interlock rule"""
        engine.add_interlock('il1', condition={
            'type': 'threshold', 'key': 'd:t', 'operator': 'gt', 'value': 100
        }, action={'type': 'set_alarm', 'message': 'test'}, name='Test IL')
        assert 'il1' in engine.interlocks
        assert engine.interlocks['il1']['name'] == 'Test IL'

    def test_remove_interlock(self, engine):
        """remove_interlock removes interlock rule"""
        engine.add_interlock('il1', condition={
            'type': 'threshold', 'key': 'd:t', 'operator': 'gt', 'value': 100
        }, action={'type': 'set_alarm', 'message': 'test'})
        engine.remove_interlock('il1')
        assert 'il1' not in engine.interlocks

    def test_interlock_triggers_on_threshold(self, engine_with_rules):
        """Interlock triggers when threshold is exceeded"""
        alarm_callback = MagicMock()
        engine_with_rules.register_action('set_alarm', alarm_callback)
        engine_with_rules.update_data('dev1:temp', 160.0)
        engine_with_rules._execute_cycle()
        alarm_callback.assert_called()

    def test_interlock_does_not_trigger_below_threshold(self, engine_with_rules):
        """Interlock does not trigger when below threshold"""
        alarm_callback = MagicMock()
        engine_with_rules.register_action('set_alarm', alarm_callback)
        engine_with_rules.update_data('dev1:temp', 50.0)
        engine_with_rules._execute_cycle()
        # Only temp_rule should NOT trigger (80 > 50), interlock should NOT trigger
        assert not alarm_callback.called


# ============================================================
# Rule Management Tests
# ============================================================

class TestRuleManagement:

    def test_add_rule(self, engine):
        """add_rule stores rule with metadata"""
        engine.add_rule('r1', condition={
            'type': 'threshold', 'key': 'd:t', 'operator': 'gt', 'value': 50
        }, action={'type': 'set_alarm', 'message': 'test'}, name='Test', priority=5)
        assert 'r1' in engine.rules
        assert engine.rules['r1']['priority'] == 5
        assert engine.rules['r1']['enabled'] is True

    def test_remove_rule(self, engine):
        """remove_rule removes rule"""
        engine.add_rule('r1', condition={
            'type': 'threshold', 'key': 'd:t', 'operator': 'gt', 'value': 50
        }, action={'type': 'set_alarm', 'message': 'test'})
        engine.remove_rule('r1')
        assert 'r1' not in engine.rules

    def test_disabled_rule_not_evaluated(self, engine):
        """Disabled rules are skipped during execution"""
        alarm_cb = MagicMock()
        engine.register_action('set_alarm', alarm_cb)
        engine.add_rule('r1', condition={
            'type': 'threshold', 'key': 'd:t', 'operator': 'gt', 'value': 50
        }, action={'type': 'set_alarm', 'message': 'test'}, enabled=False)
        engine.update_data('d:t', 100.0)
        engine._execute_cycle()
        assert not alarm_cb.called

    def test_rule_trigger_count_increments(self, engine):
        """Rule trigger_count increments on each trigger"""
        engine.register_action('set_alarm', MagicMock())
        engine.add_rule('r1', condition={
            'type': 'threshold', 'key': 'd:t', 'operator': 'gt', 'value': 50
        }, action={'type': 'set_alarm', 'message': 'test'})
        engine.update_data('d:t', 100.0)
        engine._execute_cycle()
        engine._execute_cycle()
        assert engine.rules['r1']['trigger_count'] == 2


# ============================================================
# PID Controller Tests
# ============================================================

class TestPIDController:

    def test_add_pid_controller(self, engine):
        """add_pid_controller stores PID config"""
        engine.add_pid_controller('pid1', input_key='d:temp', output_key='d:fan',
                                  setpoint=75.0, kp=2.0, ki=0.5, kd=0.1)
        assert 'pid1' in engine.pid_controllers
        ctrl = engine.pid_controllers['pid1']
        assert ctrl['setpoint'] == 75.0
        assert ctrl['kp'] == 2.0

    def test_pid_output_when_no_data(self, engine_with_rules):
        """PID controller does nothing when input data is missing"""
        write_cb = MagicMock()
        engine_with_rules.register_action('write_register', write_cb)
        engine_with_rules._execute_cycle()
        # PID should not fire because no data for dev1:temp
        write_cb.assert_not_called()

    def test_pid_output_with_data(self, engine_with_rules):
        """PID controller produces output when data is available"""
        write_cb = MagicMock()
        engine_with_rules.register_action('write_register', write_cb)
        engine_with_rules.update_data('dev1:temp', 60.0)  # below setpoint 75
        # Need to set prev_time to allow dt > 0
        engine_with_rules.pid_controllers['temp_pid']['prev_time'] = time.time() - 1.0
        engine_with_rules._execute_cycle()
        write_cb.assert_called()

    def test_pid_output_clamped(self, engine_with_rules):
        """PID output is clamped to [output_min, output_max]"""
        write_cb = MagicMock()
        engine_with_rules.register_action('write_register', write_cb)
        # Very large error to force clamping
        engine_with_rules.update_data('dev1:temp', -1000.0)
        engine_with_rules.pid_controllers['temp_pid']['prev_time'] = time.time() - 1.0
        engine_with_rules._execute_cycle()
        if write_cb.called:
            args = write_cb.call_args[0]
            output = args[2]  # third arg is value
            assert output >= 0
            assert output <= 1500

    def test_disabled_pid_not_evaluated(self, engine):
        """Disabled PID controller is skipped"""
        engine.add_pid_controller('pid1', input_key='d:t', output_key='d:o',
                                  setpoint=75.0, enabled=False)
        write_cb = MagicMock()
        engine.register_action('write_register', write_cb)
        engine.update_data('d:t', 60.0)
        engine._execute_cycle()
        write_cb.assert_not_called()


# ============================================================
# Data Snapshot Tests
# ============================================================

class TestDataSnapshot:

    def test_update_data(self, engine):
        """update_data stores value in snapshot"""
        engine.update_data('dev1:temp', 25.0)
        assert engine._data_snapshot['dev1:temp'] == 25.0

    def test_get_data_snapshot(self, engine):
        """get_data_snapshot returns copy of snapshot"""
        engine.update_data('a', 1.0)
        engine.update_data('b', 2.0)
        snap = engine.get_data_snapshot()
        assert snap == {'a': 1.0, 'b': 2.0}
        # Modifying copy doesn't affect original
        snap['c'] = 3.0
        assert 'c' not in engine._data_snapshot


# ============================================================
# Action Callback Tests
# ============================================================

class TestActionCallbacks:

    def test_register_action(self, engine):
        """register_action stores callback"""
        cb = MagicMock()
        engine.register_action('test_action', cb)
        assert 'test_action' in engine._action_callbacks

    def test_write_register_action(self, engine):
        """write_register action calls callback with correct args"""
        cb = MagicMock()
        engine.register_action('write_register', cb)
        engine.add_rule('r1', condition={
            'type': 'threshold', 'key': 'd:t', 'operator': 'gt', 'value': 50
        }, action={'type': 'write_register', 'target': 'd:fan_speed', 'value': 500})
        engine.update_data('d:t', 100.0)
        engine._execute_cycle()
        cb.assert_called_once_with('d', 'fan_speed', 500)

    def test_callback_action(self, engine):
        """callback action calls named callback"""
        cb = MagicMock()
        engine.register_action('my_callback', cb)
        engine.add_rule('r1', condition={
            'type': 'threshold', 'key': 'd:t', 'operator': 'gt', 'value': 50
        }, action={'type': 'callback', 'callback_name': 'my_callback'})
        engine.update_data('d:t', 100.0)
        engine._execute_cycle()
        cb.assert_called_once()


# ============================================================
# Query API Tests
# ============================================================

class TestQueryAPI:

    def test_get_rules(self, engine_with_rules):
        """get_rules returns rules, interlocks, pid_controllers"""
        result = engine_with_rules.get_rules()
        assert 'rules' in result
        assert 'interlocks' in result
        assert 'pid_controllers' in result

    def test_get_decision_log(self, engine):
        """get_decision_log returns list"""
        log = engine.get_decision_log()
        assert isinstance(log, list)

    def test_get_status(self, engine_with_rules):
        """get_status returns engine status dict"""
        status = engine_with_rules.get_status()
        assert status['rules_count'] == 1
        assert status['interlocks_count'] == 1
        assert status['pid_controllers_count'] == 1


# ============================================================
# Start/Stop Tests
# ============================================================

class TestStartStop:

    def test_start_loads_defaults_when_empty(self, engine):
        """start() loads default rules when rule DB is empty"""
        engine.start()
        assert len(engine.rules) > 0 or len(engine.interlocks) > 0
        engine.stop()

    def test_start_creates_thread(self, engine):
        """start() creates decision loop thread"""
        engine.start()
        assert engine._running is True
        assert engine._thread is not None
        engine.stop()

    def test_double_start_no_op(self, engine):
        """Calling start() twice doesn't create duplicate threads"""
        engine.start()
        t1 = engine._thread
        engine.start()
        assert engine._thread is t1
        engine.stop()

    def test_stop_joins_thread(self, engine):
        """stop() joins the decision thread"""
        engine.start()
        engine.stop()
        assert engine._running is False
