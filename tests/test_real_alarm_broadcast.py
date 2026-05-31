"""
Tests for RealAlarmOutput and RealBroadcastSystem - covers 报警层/real_alarm_output.py (0%) and real_broadcast.py (0%)
"""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime
from 报警层.real_alarm_output import RealAlarmOutput, AlarmLightPattern
from 报警层.real_broadcast import RealBroadcastSystem


# ── RealAlarmOutput Tests ──

@pytest.fixture
def alarm_config():
    return {
        'enabled': True,
        'modbus': {'host': '192.168.1.100', 'port': 502, 'slave_id': 1},
        'do_mapping': {
            'red_light': 0, 'yellow_light': 1, 'green_light': 2, 'buzzer': 3
        },
        'buzzer_mode': 'pulse',
    }


@pytest.fixture
def alarm_output(alarm_config):
    return RealAlarmOutput(alarm_config)


class TestAlarmLightPattern:
    def test_patterns(self):
        assert AlarmLightPattern.OFF == 'off'
        assert AlarmLightPattern.STEADY == 'steady'
        assert AlarmLightPattern.SLOW_FLASH == 'slow'
        assert AlarmLightPattern.FAST_FLASH == 'fast'


class TestRealAlarmOutputInit:
    def test_init_with_config(self, alarm_output):
        assert alarm_output._enabled is True
        assert alarm_output.modbus_host == '192.168.1.100'
        assert alarm_output.modbus_port == 502
        assert alarm_output.modbus_slave_id == 1
        assert alarm_output.do_red == 0
        assert alarm_output.do_yellow == 1
        assert alarm_output.do_green == 2
        assert alarm_output.do_buzzer == 3
        assert alarm_output.current_state['green'] is True
        assert alarm_output.current_state['red'] is False

    def test_init_defaults(self):
        output = RealAlarmOutput()
        assert output._enabled is True
        assert output.modbus_host == '192.168.1.100'

    def test_init_disabled(self):
        output = RealAlarmOutput({'enabled': False})
        assert output._enabled is False

    def test_enabled_property(self, alarm_output):
        assert alarm_output.enabled is True


class TestRealAlarmOutputModbus:
    @patch('pymodbus.client.ModbusTcpClient')
    def test_get_modbus_client_creates(self, mock_cls, alarm_output):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        result = alarm_output._get_modbus_client()

        assert result is mock_client
        assert alarm_output._modbus_client is mock_client

    @patch('pymodbus.client.ModbusTcpClient')
    def test_get_modbus_client_failure(self, mock_cls, alarm_output):
        mock_cls.side_effect = Exception("connection failed")

        result = alarm_output._get_modbus_client()

        assert result is None

    def test_get_modbus_client_reuses(self, alarm_output):
        mock_client = MagicMock()
        alarm_output._modbus_client = mock_client

        result = alarm_output._get_modbus_client()

        assert result is mock_client

    def test_write_do_success(self, alarm_output):
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.isError.return_value = False
        mock_client.write_coil.return_value = mock_result
        alarm_output._modbus_client = mock_client

        result = alarm_output._write_do(0, True)

        assert result is True

    def test_write_do_failure(self, alarm_output):
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.isError.return_value = True
        mock_client.write_coil.return_value = mock_result
        alarm_output._modbus_client = mock_client

        result = alarm_output._write_do(0, True)

        assert result is False

    def test_write_do_no_client(self, alarm_output):
        with patch.object(alarm_output, '_get_modbus_client', return_value=None):
            result = alarm_output._write_do(0, True)
            assert result is False

    def test_write_do_exception(self, alarm_output):
        mock_client = MagicMock()
        mock_client.write_coil.side_effect = OSError("write error")
        alarm_output._modbus_client = mock_client

        result = alarm_output._write_do(0, True)

        assert result is False


class TestRealAlarmOutputLights:
    def test_set_lights(self, alarm_output):
        with patch.object(alarm_output, '_write_do') as mock_write:
            mock_write.return_value = True
            alarm_output._set_lights(True, False, True)

            assert mock_write.call_count == 3


class TestRealAlarmOutputBuzzer:
    def test_set_buzzer_on(self, alarm_output):
        with patch.object(alarm_output, '_write_do') as mock_write:
            mock_write.return_value = True
            alarm_output._set_buzzer(True)

            mock_write.assert_called_once_with(3, True)


class TestRealAlarmOutputFlash:
    @patch('报警层.real_alarm_output.time.sleep')
    def test_start_stop_flash(self, mock_sleep, alarm_output):
        with patch.object(alarm_output, '_set_lights'):
            alarm_output._start_flash(AlarmLightPattern.FAST_FLASH)
            assert alarm_output._flash_running is True

            alarm_output._stop_flash()
            assert alarm_output._flash_running is False

    def test_stop_flash_no_thread(self, alarm_output):
        alarm_output._flash_thread = None
        alarm_output._stop_flash()


class TestRealAlarmOutputBuzzerPulse:
    @patch('报警层.real_alarm_output.time.sleep')
    def test_start_stop_buzzer_pulse(self, mock_sleep, alarm_output):
        with patch.object(alarm_output, '_set_buzzer'):
            alarm_output._start_buzzer_pulse()
            assert alarm_output._buzzer_running is True

            alarm_output._stop_buzzer_pulse()
            assert alarm_output._buzzer_running is False

    def test_stop_buzzer_no_thread(self, alarm_output):
        alarm_output._buzzer_thread = None
        alarm_output._stop_buzzer_pulse()


class TestRealAlarmOutputActivate:
    def test_activate_critical(self, alarm_output):
        with patch.object(alarm_output, '_set_lights'), \
             patch.object(alarm_output, '_set_buzzer'), \
             patch.object(alarm_output, '_start_flash'), \
             patch.object(alarm_output, '_start_buzzer_pulse'), \
             patch.object(alarm_output, '_stop_flash'), \
             patch.object(alarm_output, '_stop_buzzer_pulse'):

            result = alarm_output.activate_alarm('critical', 'Danger!')

            assert result is True
            assert alarm_output.current_state['red'] is True
            assert alarm_output.current_state['green'] is False
            assert alarm_output.current_state['level'] == 'critical'
            assert len(alarm_output.history) == 1

    def test_activate_warning(self, alarm_output):
        with patch.object(alarm_output, '_set_lights'), \
             patch.object(alarm_output, '_set_buzzer'), \
             patch.object(alarm_output, '_start_flash'), \
             patch.object(alarm_output, '_start_buzzer_pulse'), \
             patch.object(alarm_output, '_stop_flash'), \
             patch.object(alarm_output, '_stop_buzzer_pulse'):

            result = alarm_output.activate_alarm('warning', 'Watch out!')

            assert result is True
            assert alarm_output.current_state['yellow'] is True
            assert alarm_output.current_state['level'] == 'warning'

    def test_activate_info(self, alarm_output):
        with patch.object(alarm_output, '_set_lights'), \
             patch.object(alarm_output, '_stop_flash'), \
             patch.object(alarm_output, '_stop_buzzer_pulse'):

            result = alarm_output.activate_alarm('info', 'FYI')

            assert result is True
            assert alarm_output.current_state['green'] is True
            assert alarm_output.current_state['buzzer'] is False

    def test_activate_disabled(self):
        output = RealAlarmOutput({'enabled': False})
        result = output.activate_alarm('critical', 'test')
        assert result is False


class TestRealAlarmOutputAcknowledge:
    def test_acknowledge(self, alarm_output):
        with patch.object(alarm_output, '_set_buzzer'), \
             patch.object(alarm_output, '_stop_buzzer_pulse'):

            result = alarm_output.acknowledge()

            assert result is True
            assert alarm_output.current_state['buzzer'] is False
            assert len(alarm_output.history) == 1

    def test_acknowledge_disabled(self):
        output = RealAlarmOutput({'enabled': False})
        result = output.acknowledge()
        assert result is False


class TestRealAlarmOutputReset:
    def test_reset(self, alarm_output):
        with patch.object(alarm_output, '_set_lights'), \
             patch.object(alarm_output, '_set_buzzer'), \
             patch.object(alarm_output, '_stop_flash'), \
             patch.object(alarm_output, '_stop_buzzer_pulse'):

            result = alarm_output.reset()

            assert result is True
            assert alarm_output.current_state['green'] is True
            assert alarm_output.current_state['red'] is False
            assert alarm_output.current_state['level'] is None
            assert len(alarm_output.history) == 1

    def test_reset_disabled(self):
        output = RealAlarmOutput({'enabled': False})
        result = output.reset()
        assert result is False


class TestRealAlarmOutputManualControl:
    def test_manual_control(self, alarm_output):
        with patch.object(alarm_output, '_set_lights'), \
             patch.object(alarm_output, '_set_buzzer'), \
             patch.object(alarm_output, '_stop_flash'), \
             patch.object(alarm_output, '_stop_buzzer_pulse'):

            result = alarm_output.manual_control(red=True, yellow=False, green=True, buzzer=True)

            assert result['success'] is True
            assert len(alarm_output.history) == 1

    def test_manual_control_disabled(self):
        output = RealAlarmOutput({'enabled': False})
        result = output.manual_control(red=True)
        assert result['success'] is False


class TestRealAlarmOutputGetStatus:
    def test_get_status(self, alarm_output):
        status = alarm_output.get_status()

        assert status['enabled'] is True
        assert status['mode'] == 'real'
        assert status['modbus_host'] == '192.168.1.100'
        assert 'state' in status
        assert 'history_count' in status


# ── RealBroadcastSystem Tests ──

@pytest.fixture
def broadcast_config():
    return {
        'enabled': True,
        'mqtt_broker': 'localhost',
        'mqtt_port': 1883,
        'topic_prefix': 'pa/',
        'areas': ['workshop_A', 'warehouse'],
    }


@pytest.fixture
def broadcast(broadcast_config):
    return RealBroadcastSystem(broadcast_config)


class TestRealBroadcastInit:
    def test_init_with_config(self, broadcast):
        assert broadcast._enabled is True
        assert broadcast.mqtt_broker == 'localhost'
        assert broadcast.mqtt_port == 1883
        assert broadcast.topic_prefix == 'pa/'
        assert 'workshop_A' in broadcast.areas
        assert broadcast.preset_templates is not None

    def test_init_defaults(self):
        sys = RealBroadcastSystem()
        assert sys._enabled is True
        assert sys.mqtt_broker == 'localhost'

    def test_enabled_property(self, broadcast):
        assert broadcast.enabled is True


class TestRealBroadcastMqtt:
    @patch('paho.mqtt.client.Client')
    def test_get_mqtt_client_creates(self, mock_cls, broadcast):
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        result = broadcast._get_mqtt_client()

        assert result is mock_client
        mock_client.connect.assert_called_once()

    @patch('paho.mqtt.client.Client')
    def test_get_mqtt_client_failure(self, mock_cls, broadcast):
        mock_cls.side_effect = Exception("no mqtt")

        result = broadcast._get_mqtt_client()

        assert result is None

    def test_get_mqtt_client_reuses(self, broadcast):
        mock_client = MagicMock()
        broadcast._mqtt_client = mock_client

        result = broadcast._get_mqtt_client()

        assert result is mock_client

    def test_publish_success(self, broadcast):
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.rc = 0
        mock_client.publish.return_value = mock_result
        broadcast._mqtt_client = mock_client

        result = broadcast._publish('pa/test', {'msg': 'hello'})

        assert result is True

    def test_publish_failure(self, broadcast):
        mock_client = MagicMock()
        mock_result = MagicMock()
        mock_result.rc = 1
        mock_client.publish.return_value = mock_result
        broadcast._mqtt_client = mock_client

        result = broadcast._publish('pa/test', {'msg': 'hello'})

        assert result is False

    def test_publish_no_client(self, broadcast):
        with patch.object(broadcast, '_get_mqtt_client', return_value=None):
            result = broadcast._publish('pa/test', {'msg': 'hello'})
            assert result is False

    def test_publish_exception(self, broadcast):
        mock_client = MagicMock()
        mock_client.publish.side_effect = OSError("fail")
        broadcast._mqtt_client = mock_client

        result = broadcast._publish('pa/test', {'msg': 'hello'})

        assert result is False


class TestRealBroadcastSpeak:
    def test_speak_success(self, broadcast):
        with patch.object(broadcast, '_publish', return_value=True):
            result = broadcast.speak('Emergency!', level='critical', area='workshop_A')

            assert result['success'] is True
            assert result['area'] == 'workshop_A'
            assert len(broadcast.history) == 1

    def test_speak_all_area(self, broadcast):
        with patch.object(broadcast, '_publish', return_value=True):
            result = broadcast.speak('Attention!', level='info')

            assert result['success'] is True
            assert result['area'] == 'all'

    def test_speak_unknown_area(self, broadcast):
        with patch.object(broadcast, '_publish', return_value=True):
            result = broadcast.speak('Test', area='unknown_area')

            assert result['area'] == 'all'

    def test_speak_failure(self, broadcast):
        with patch.object(broadcast, '_publish', return_value=False):
            result = broadcast.speak('Fail!')

            assert result['success'] is False

    def test_speak_disabled(self):
        sys = RealBroadcastSystem({'enabled': False})
        result = sys.speak('Test')
        assert result['success'] is False

    def test_speak_history_limit(self, broadcast):
        with patch.object(broadcast, '_publish', return_value=True):
            broadcast.history = [{}] * 1005
            broadcast.speak('Test')
            assert len(broadcast.history) <= 501


class TestRealBroadcastGetAreas:
    def test_get_areas(self, broadcast):
        areas = broadcast.get_areas()
        assert 'workshop_A' in areas
        assert 'warehouse' in areas


class TestRealBroadcastGetHistory:
    def test_get_history_empty(self, broadcast):
        assert broadcast.get_history() == []

    def test_get_history_with_data(self, broadcast):
        broadcast.history = [{'text': 'a'}, {'text': 'b'}, {'text': 'c'}]
        result = broadcast.get_history(limit=2)
        assert len(result) == 2


class TestRealBroadcastGetStatus:
    def test_get_status(self, broadcast):
        status = broadcast.get_status()

        assert status['enabled'] is True
        assert status['mode'] == 'real'
        assert status['mqtt_broker'] == 'localhost'
        assert 'areas' in status

    def test_get_status_with_history(self, broadcast):
        broadcast.history = [{'text': 'last'}]
        status = broadcast.get_status()
        assert status['last_broadcast'] is not None
