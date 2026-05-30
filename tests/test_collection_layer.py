"""
Tests for 采集层 modules: RecipeSimulator, DeviceManagerFactory, BaseDeviceClient, interfaces
"""
import pytest
import time
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta


# ============================================================
# RecipeSimulator Tests
# ============================================================
from 采集层.recipe_simulator import (
    RecipePhase, PhaseConfig, Recipe, RecipeSimulator
)


class TestRecipePhase:

    def test_values(self):
        assert RecipePhase.IDLE.value == 'idle'
        assert RecipePhase.HEATING.value == 'heating'
        assert RecipePhase.HOLDING.value == 'holding'
        assert RecipePhase.INJECTION.value == 'injection'
        assert RecipePhase.COOLING.value == 'cooling'
        assert RecipePhase.EJECTION.value == 'ejection'
        assert RecipePhase.COMPLETE.value == 'complete'


class TestPhaseConfig:

    def test_init(self):
        pc = PhaseConfig(name='heating', duration=120.0,
                         setpoints={'temp': 180})
        assert pc.name == 'heating'
        assert pc.duration == 120.0
        assert pc.setpoints == {'temp': 180}
        assert pc.tolerance == 0.05
        assert pc.transitions == {}


class TestRecipe:

    def test_init(self):
        phases = [PhaseConfig('p1', 10, {'x': 1})]
        r = Recipe(name='test', version='1.0', phases=phases)
        assert r.name == 'test'
        assert r.version == '1.0'
        assert len(r.phases) == 1
        assert r.parameters == {}


class TestRecipeSimulator:

    @pytest.fixture
    def recipe(self):
        return Recipe(
            name='Test Recipe', version='1.0',
            phases=[
                PhaseConfig('heating', 0.1, {'temp': 180}),
                PhaseConfig('cooling', 0.1, {'temp': 40}),
            ],
            parameters={'target': 240}
        )

    @pytest.fixture
    def sim(self, recipe):
        return RecipeSimulator(recipe)

    def test_init(self, sim, recipe):
        assert sim.recipe is recipe
        assert sim.current_phase_index == 0
        assert sim.is_running is False
        assert sim._cycle_count == 0

    def test_current_phase(self, sim):
        assert sim.current_phase is not None
        assert sim.current_phase.name == 'heating'

    def test_current_phase_out_of_range(self, sim):
        sim.current_phase_index = 99
        assert sim.current_phase is None

    def test_start(self, sim):
        sim.start()
        assert sim.is_running is True
        assert sim.current_phase_index == 0

    def test_stop(self, sim):
        sim.start()
        sim.stop()
        assert sim.is_running is False

    def test_update_not_running(self, sim):
        result = sim.update(0.1)
        assert result == {}

    def test_update_returns_setpoints(self, sim):
        sim.start()
        sim.recipe.phases[0].duration = 100  # long enough
        result = sim.update(0.1)
        assert 'temp' in result

    def test_advance_phase(self, sim):
        sim.start()
        sim.recipe.phases[0].duration = 0  # instant advance
        sim.update(0.1)
        assert sim.current_phase_index == 1

    def test_recipe_completion(self, sim):
        sim.start()
        for phase in sim.recipe.phases:
            phase.duration = 0
        sim.update(0.1)
        sim.update(0.1)
        assert sim.is_running is False
        assert sim._cycle_count == 1

    def test_completion_callback(self, recipe):
        cb = MagicMock()
        sim = RecipeSimulator(recipe)
        sim._completion_callback = cb
        sim.start()
        for phase in recipe.phases:
            phase.duration = 0
        sim.update(0.1)
        sim.update(0.1)
        cb.assert_called_once_with(1)

    def test_get_current_setpoints(self, sim):
        result = sim.get_current_setpoints()
        assert 'temp' in result

    def test_get_current_setpoints_no_phase(self, sim):
        sim.current_phase_index = 99
        result = sim.get_current_setpoints()
        assert result == {}

    def test_get_status(self, sim):
        sim.start()
        status = sim.get_status()
        assert status['recipe'] == 'Test Recipe'
        assert status['is_running'] is True
        assert status['total_phases'] == 2

    def test_interpolate_setpoints(self, sim):
        phase = sim.recipe.phases[0]
        result = sim._interpolate_setpoints(phase, 0)
        assert 'temp' in result

    def test_injection_mold_recipe_exists(self):
        r = RecipeSimulator.INJECTION_MOLD_RECIPE
        assert r.name == "标准注塑"
        assert len(r.phases) == 5

    def test_fermentation_recipe_exists(self):
        r = RecipeSimulator.FERMENTATION_RECIPE
        assert r.name == "标准发酵"
        assert len(r.phases) == 4

    def test_recipes_dict(self):
        assert 'injection_mold' in RecipeSimulator.RECIPES
        assert 'fermentation' in RecipeSimulator.RECIPES

    def test_with_behavior_simulator(self, recipe):
        mock_sim = MagicMock()
        mock_sim._current_temp = 50.0
        sim = RecipeSimulator(recipe, behavior_simulator=mock_sim)
        sim.start()
        recipe.phases[0].duration = 100
        result = sim.update(0.1)
        # Should use behavior_simulator's current value for interpolation
        assert 'temp' in result


# ============================================================
# DeviceManagerFactory Tests
# ============================================================
from 采集层.device_manager_factory import DeviceManagerFactory, get_device_manager


class TestDeviceManagerFactory:

    def test_create_simulated(self):
        mgr = DeviceManagerFactory.create_simulated()
        assert mgr.simulation_mode is True

    def test_create_real(self):
        mgr = DeviceManagerFactory.create_real()
        assert mgr is not None

    @patch('采集层.device_manager_factory.Path')
    def test_create_default_simulated(self, mock_path):
        mock_path.return_value.exists.return_value = False
        mgr = DeviceManagerFactory.create(config_path='nonexistent.yaml')
        assert mgr.simulation_mode is True

    @patch('采集层.device_manager_factory.Path')
    def test_create_with_config_simulated(self, mock_path_instance):
        mock_file = MagicMock()
        mock_file.exists.return_value = True
        mock_path_instance.return_value = mock_file

        import io
        yaml_content = "system:\n  simulation_mode: true\n"
        with patch('builtins.open', return_value=io.StringIO(yaml_content)):
            with patch('采集层.device_manager_factory.yaml.safe_load',
                       return_value={'system': {'simulation_mode': True}}):
                mgr = DeviceManagerFactory.create()
                assert mgr.simulation_mode is True

    def test_get_device_manager_function(self):
        mgr = get_device_manager(config_path='nonexistent.yaml')
        assert mgr is not None


# ============================================================
# BaseDeviceClient Tests (via interfaces)
# ============================================================
from 采集层.interfaces import IDeviceManager


class TestIDeviceManagerInterface:

    def test_get_device_category_explicit(self):
        config = {'device_category': 'mechanical'}
        assert IDeviceManager.get_device_category(config) == 'mechanical'

    def test_get_device_category_explicit_instrument(self):
        config = {'device_category': 'instrument'}
        assert IDeviceManager.get_device_category(config) == 'instrument'

    def test_get_device_category_explicit_safety(self):
        config = {'device_category': 'safety'}
        assert IDeviceManager.get_device_category(config) == 'safety'

    def test_get_device_category_safety_by_name(self):
        config = {'name': 'signal_tower_01', 'registers': []}
        assert IDeviceManager.get_device_category(config) == 'safety'

    def test_get_device_category_safety_keywords(self):
        for keyword in ['信号灯塔', 'relay', '继电器', 'alarm', '报警', 'buzzer', '蜂鸣器', '警灯', '灯塔']:
            config = {'name': f'device_{keyword}', 'registers': []}
            result = IDeviceManager.get_device_category(config)
            assert result == 'safety', f"Keyword '{keyword}' should be safety, got {result}"

    def test_get_device_category_machinery_register(self):
        config = {'name': 'pump_01', 'registers': [{'name': 'motor_speed'}]}
        assert IDeviceManager.get_device_category(config) == 'mechanical'

    def test_get_device_category_default_instrument(self):
        config = {'name': 'sensor_01', 'registers': [{'name': 'temperature'}]}
        assert IDeviceManager.get_device_category(config) == 'instrument'

    def test_set_estop_override_default(self):
        """Default implementation is a no-op"""
        class ConcreteMgr(IDeviceManager):
            def load_config(self): pass
            def get_client(self, device_id): return None
            def connect_device(self, device_id): return True
            def disconnect_device(self, device_id): pass
            def connect_all(self): return {}
            def disconnect_all(self): pass
            def get_device_status(self, device_id): return {}
            def get_all_status(self): return []
            def add_device(self, config): return True
            def remove_device(self, device_id): return True
            def get_protocol_summary(self): return {}

        mgr = ConcreteMgr()
        mgr.set_estop_override(True)  # no-op

    def test_stop_device_default_returns_false(self):
        class ConcreteMgr(IDeviceManager):
            def load_config(self): pass
            def get_client(self, device_id): return None
            def connect_device(self, device_id): return True
            def disconnect_device(self, device_id): pass
            def connect_all(self): return {}
            def disconnect_all(self): pass
            def get_device_status(self, device_id): return {}
            def get_all_status(self): return []
            def add_device(self, config): return True
            def remove_device(self, device_id): return True
            def get_protocol_summary(self): return {}

        mgr = ConcreteMgr()
        assert mgr.stop_device('d1') is False
        assert mgr.start_device('d1') is False
        result = mgr.adjust_device('d1', 'reg', 10.0)
        assert result['success'] is False


# ============================================================
# AlarmRules additional tests (for alarm_rules.py coverage)
# ============================================================
from 报警层.alarm_rules import AlarmRule, AlarmRules


class TestAlarmRuleEdgeCases:

    def test_check_all_conditions(self):
        conditions = {
            'greater_than': (90, 80, True),
            'less_than': (70, 80, True),
            'equal_to': (80, 80, True),
            'not_equal_to': (80, 80, False),
            'greater_equal': (80, 80, True),
            'less_equal': (80, 80, True),
        }
        for condition, (value, threshold, expected) in conditions.items():
            rule = AlarmRule('r1', 'test', 'd1', 'v', condition, threshold)
            assert rule.check(value) == expected, f"Condition {condition} failed"

    def test_from_dict_defaults(self):
        data = {
            'id': 'r1', 'name': 'X', 'device_id': 'd1',
            'register_name': 'v', 'condition': 'greater_than',
            'threshold': 80.0
        }
        rule = AlarmRule.from_dict(data)
        assert rule.level == 'warning'
        assert rule.enabled is True
        assert rule.delay == 0
        assert rule.description == ''

    def test_rules_multiple_device_check(self):
        rules = AlarmRules()
        rules.add_rule(AlarmRule('r1', 'A', 'd1', 'temp', 'greater_than', 80.0))
        rules.add_rule(AlarmRule('r2', 'B', 'd1', 'temp', 'greater_than', 90.0))
        rules.add_rule(AlarmRule('r3', 'C', 'd2', 'temp', 'greater_than', 70.0))
        triggered = rules.check_value('d1', 'temp', 95.0)
        assert len(triggered) == 2

    def test_rules_check_disabled_rule(self):
        rules = AlarmRules()
        rules.add_rule(AlarmRule('r1', 'A', 'd1', 'temp', 'greater_than', 80.0, enabled=False))
        triggered = rules.check_value('d1', 'temp', 90.0)
        assert len(triggered) == 0


# ============================================================
# Notification additional tests
# ============================================================
from 报警层.notification import Notification


class TestNotificationEdgeCases:

    def test_init_empty_config(self):
        n = Notification(None)
        assert n.config == {}
        assert n.email_enabled is False

    def test_send_email_no_smtp_server(self):
        config = {
            'email': {
                'enabled': True,
                'username': 'user@test.com',
                'password': 'pass',
            }
        }
        n = Notification(config)
        result = n.send_email('subject', 'body', ['dest@test.com'])
        assert result is False

    def test_send_alarm_notification_default_fields(self):
        n = Notification()
        with patch.object(n, 'send_email', return_value=False) as mock_send:
            result = n.send_alarm_notification({})
            assert result is False
            # Verify subject contains 'WARNING' (default level)
            call_args = mock_send.call_args
            assert 'WARNING' in call_args[0][0] or 'WARNING' in str(call_args)


# ============================================================
# BroadcastSystem additional tests
# ============================================================
from 报警层.broadcast_system import BroadcastSystem


class TestBroadcastSystemEdgeCases:

    def test_speak_custom_area(self):
        bs = BroadcastSystem(config={'enabled': True, 'simulation': True,
                                      'areas': ['A区', 'B区']})
        result = bs.speak_area('A区', '测试')
        assert result['success'] is True

    def test_history_ordering(self):
        bs = BroadcastSystem(config={'enabled': True, 'simulation': True})
        bs.speak('first')
        bs.speak('second')
        bs.speak('third')
        history = bs.get_history(limit=2)
        assert len(history) == 2
        # Should be reversed (newest first)
        assert history[0]['text'] == 'third'

    def test_status_with_history(self):
        bs = BroadcastSystem(config={'enabled': True, 'simulation': True})
        bs.speak('msg1')
        status = bs.get_status()
        assert status['history_count'] == 1

    def test_speak_with_template_params(self):
        bs = BroadcastSystem(config={
            'enabled': True, 'simulation': True,
            'preset_templates': {'evac': '请{area}撤离'}
        })
        result = bs.speak_preset('evac', area='车间A')
        assert result['success'] is True
