"""
Tests for 报警层 modules: AlarmKPI, AlarmStatistics, AlarmOutput, BroadcastSystem, Notification
"""
import pytest
import time
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta


# ============================================================
# AlarmKPI Tests
# ============================================================




# ============================================================
# AlarmStatistics Tests
# ============================================================




# ============================================================
# AlarmOutput Tests
# ============================================================
from 报警层.alarm_output import AlarmOutput, AlarmLightPattern


class TestAlarmOutput:

    @pytest.fixture
    def output(self):
        return AlarmOutput(config={'simulation': True, 'enabled': True})

    def test_init_default(self, output):
        assert output.enabled is True
        assert output.simulation is True
        assert output.current_state['green'] is True
        assert output.current_state['red'] is False

    def test_init_disabled(self):
        output = AlarmOutput(config={'enabled': False, 'simulation': True})
        assert output.enabled is False

    def test_do_mapping_defaults(self, output):
        assert output.do_red == 0
        assert output.do_yellow == 1
        assert output.do_green == 2
        assert output.do_buzzer == 3

    def test_do_mapping_custom(self):
        config = {'simulation': True, 'do_mapping': {'red_light': 10, 'yellow_light': 11,
                                                       'green_light': 12, 'buzzer': 13}}
        output = AlarmOutput(config=config)
        assert output.do_red == 10
        assert output.do_yellow == 11
        assert output.do_green == 12
        assert output.do_buzzer == 13

    def test_trigger_critical_alarm(self, output):
        result = output.trigger_alarm('critical', 'Danger!', 'd1')
        assert output.current_state['level'] == 'critical'
        assert output.current_state['red'] is True
        assert output.current_state['green'] is False
        assert output.current_state['buzzer'] is True
        assert '严重报警' in result

    def test_trigger_warning_alarm(self, output):
        result = output.trigger_alarm('warning', 'Warning!', 'd1')
        assert output.current_state['level'] == 'warning'
        assert output.current_state['yellow'] is True
        assert output.current_state['green'] is False
        assert '警告' in result

    def test_trigger_info_alarm(self, output):
        result = output.trigger_alarm('info', 'Info msg', 'd1')
        assert output.current_state['level'] == 'info'
        assert output.current_state['green'] is True
        assert output.current_state['buzzer'] is False
        assert '信息' in result

    def test_trigger_alarm_disabled(self):
        output = AlarmOutput(config={'enabled': False, 'simulation': True})
        result = output.trigger_alarm('critical', 'msg', 'd1')
        assert result is None

    def test_acknowledge(self, output):
        output.trigger_alarm('warning', 'msg', 'd1')
        result = output.acknowledge()
        assert result is True
        assert output.current_state['buzzer'] is False

    def test_reset(self, output):
        output.trigger_alarm('critical', 'msg', 'd1')
        result = output.reset()
        assert result is True
        assert output.current_state['red'] is False
        assert output.current_state['green'] is True
        assert output.current_state['level'] is None

    def test_manual_control(self, output):
        result = output.manual_control(red=True, yellow=False, green=False, buzzer=True)
        assert result['success'] is True
        assert output.current_state['red'] is True
        assert output.current_state['buzzer'] is True
        assert output.current_state['level'] == 'manual'

    def test_manual_control_partial(self, output):
        output.manual_control(red=True)
        assert output.current_state['red'] is True

    def test_activate_alarm(self, output):
        result = output.activate_alarm('critical', 'msg')
        assert result is True

    def test_get_status(self, output):
        status = output.get_status()
        assert 'enabled' in status
        assert 'simulation' in status
        assert 'state' in status
        assert 'do_mapping' in status

    def test_disconnect(self, output):
        output.trigger_alarm('critical', 'msg')
        output.disconnect()
        assert output._modbus_client is None
        assert output.current_state['green'] is True

    def test_same_level_alarm_no_repeat(self, output):
        output.trigger_alarm('critical', 'first', 'd1')
        output.trigger_alarm('critical', 'second', 'd1')
        assert output.current_state['message'] == 'second'

    def test_manual_mode_blocks_auto(self, output):
        output.manual_control(red=True)
        output.trigger_alarm('critical', 'msg')
        assert output.current_state['level'] == 'manual'

    def test_light_pattern_constants(self):
        assert AlarmLightPattern.OFF == 'off'
        assert AlarmLightPattern.STEADY == 'steady'
        assert AlarmLightPattern.SLOW_FLASH == 'slow'
        assert AlarmLightPattern.FAST_FLASH == 'fast'

    def test_buzzer_mode_config(self):
        output = AlarmOutput(config={'simulation': True, 'buzzer_mode': 'steady'})
        assert output.buzzer_mode == 'steady'


# ============================================================
# BroadcastSystem Tests
# ============================================================
from 报警层.broadcast_system import BroadcastSystem, BroadcastMessage


class TestBroadcastMessage:

    def test_init(self):
        msg = BroadcastMessage(text='test', level='warning', area='A', source='manual')
        assert msg.text == 'test'
        assert msg.level == 'warning'
        assert msg.area == 'A'
        assert msg.source == 'manual'
        assert msg.status == 'pending'

    def test_to_dict(self):
        msg = BroadcastMessage(text='test')
        d = msg.to_dict()
        assert d['text'] == 'test'
        assert d['level'] == 'info'
        assert 'timestamp' in d
        assert d['status'] == 'pending'


class TestBroadcastSystem:

    @pytest.fixture
    def bs(self):
        return BroadcastSystem(config={'enabled': True, 'simulation': True})

    def test_init_default(self, bs):
        assert bs.enabled is True
        assert bs.simulation is True

    def test_init_disabled(self):
        bs = BroadcastSystem(config={'enabled': False})
        assert bs.enabled is False

    def test_speak_success(self, bs):
        result = bs.speak('Hello', level='info', area='all')
        assert result['success'] is True
        assert result['data']['text'] == 'Hello'

    def test_speak_disabled(self):
        bs = BroadcastSystem(config={'enabled': False})
        result = bs.speak('Hello')
        assert result['success'] is False

    def test_speak_records_history(self, bs):
        bs.speak('msg1')
        bs.speak('msg2')
        history = bs.get_history()
        assert len(history) == 2

    def test_speak_alarm(self, bs):
        bs.config['preset_templates'] = {
            'alarm_critical': '严重！{area}：{message}',
        }
        bs.preset_templates = bs.config['preset_templates']
        result = bs.speak_alarm('critical', 'Overheat', device_id='d1', area='A区')
        assert result['success'] is True

    def test_speak_alarm_default_template(self, bs):
        result = bs.speak_alarm('info', 'Notice')
        assert result['success'] is True

    def test_speak_preset_exists(self, bs):
        bs.preset_templates = {'test': 'Hello {name}'}
        result = bs.speak_preset('test', name='World')
        assert result['success'] is True

    def test_speak_preset_not_exists(self, bs):
        result = bs.speak_preset('nonexistent')
        assert result['success'] is False

    def test_speak_area(self, bs):
        result = bs.speak_area('AreaA', 'Hello AreaA')
        assert result['success'] is True

    def test_get_areas(self, bs):
        areas = bs.get_areas()
        assert 'all' in areas

    def test_get_status(self, bs):
        status = bs.get_status()
        assert status['enabled'] is True
        assert status['simulation'] is True

    def test_add_callback(self, bs):
        cb = MagicMock()
        bs.add_callback(cb)
        bs.speak('test')
        cb.assert_called_once()

    def test_callback_exception_handled(self, bs):
        cb = MagicMock(side_effect=Exception("fail"))
        bs.add_callback(cb)
        result = bs.speak('test')
        assert result['success'] is True

    def test_history_limit(self):
        bs = BroadcastSystem(config={'enabled': True, 'simulation': True})
        bs._max_history = 5
        for i in range(10):
            bs.speak(f'msg{i}')
        assert len(bs.get_history(100)) == 5

    def test_disconnect(self, bs):
        bs._mqtt_client = MagicMock()
        bs._mqtt_connected = True
        bs.disconnect()
        assert bs._mqtt_client is None
        assert bs._mqtt_connected is False


# ============================================================
# Notification Tests
# ============================================================
from 报警层.notification import Notification


class TestNotification:

    def test_init_default(self):
        n = Notification()
        assert n.email_enabled is False
        assert n.sms_enabled is False

    def test_init_with_config(self):
        config = {
            'email': {'enabled': True, 'smtp_server': 'smtp.test.com'},
            'sms': {'enabled': True}
        }
        n = Notification(config)
        assert n.email_enabled is True
        assert n.sms_enabled is True

    def test_send_email_disabled(self):
        n = Notification()
        result = n.send_email('subject', 'message')
        assert result is False

    def test_send_email_incomplete_config(self):
        config = {'email': {'enabled': True, 'smtp_server': None}}
        n = Notification(config)
        result = n.send_email('subject', 'message', ['test@test.com'])
        assert result is False

    @patch('报警层.notification.smtplib.SMTP')
    def test_send_email_success(self, mock_smtp):
        config = {
            'email': {
                'enabled': True,
                'smtp_server': 'smtp.test.com',
                'smtp_port': 587,
                'username': 'user@test.com',
                'password': 'pass',
            }
        }
        n = Notification(config)
        result = n.send_email('Subject', '<p>Body</p>', ['dest@test.com'])
        assert result is True

    @patch('报警层.notification.smtplib.SMTP')
    def test_send_email_failure(self, mock_smtp):
        mock_smtp.side_effect = Exception("Connection failed")
        config = {
            'email': {
                'enabled': True,
                'smtp_server': 'smtp.test.com',
                'username': 'user@test.com',
                'password': 'pass',
            }
        }
        n = Notification(config)
        result = n.send_email('Subject', 'Body', ['dest@test.com'])
        assert result is False

    def test_send_email_no_recipients(self):
        config = {
            'email': {
                'enabled': True,
                'smtp_server': 'smtp.test.com',
                'username': 'user@test.com',
                'password': 'pass',
                'recipients': [],
            }
        }
        n = Notification(config)
        result = n.send_email('Subject', 'Body')
        assert result is False

    def test_send_alarm_notification(self):
        config = {
            'email': {
                'enabled': True,
                'smtp_server': 'smtp.test.com',
                'username': 'user@test.com',
                'password': 'pass',
                'recipients': ['dest@test.com'],
            }
        }
        n = Notification(config)
        alarm_data = {
            'level': 'critical',
            'device_id': 'd1',
            'register_name': 'temp',
            'value': 95,
            'threshold': 80,
            'timestamp': datetime.now().isoformat(),
            'message': 'High temp',
        }
        with patch.object(n, 'send_email', return_value=True) as mock_send:
            result = n.send_alarm_notification(alarm_data)
            assert result is True
            mock_send.assert_called_once()

    def test_send_sms_disabled(self):
        n = Notification()
        result = n.send_sms(['13800138000'], 'test')
        assert result is False

    def test_send_sms_enabled(self):
        config = {'sms': {'enabled': True}}
        n = Notification(config)
        result = n.send_sms(['13800138000'], 'test')
        assert result is True

    def test_test_email(self):
        config = {
            'email': {
                'enabled': True,
                'smtp_server': 'smtp.test.com',
                'username': 'user@test.com',
                'password': 'pass',
                'recipients': ['dest@test.com'],
            }
        }
        n = Notification(config)
        with patch.object(n, 'send_email', return_value=True) as mock_send:
            result = n.test_email()
            assert result is True
