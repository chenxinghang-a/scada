"""
Tests for OPC UA client - covers 采集层/opcua_client.py (17% -> high)
"""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from datetime import datetime
from 采集层.opcua_client import OPCUAClient, OPCUABridge


@pytest.fixture
def opcua_config():
    return {
        'id': 'opcua_01',
        'name': 'Siemens PLC',
        'endpoint': 'opc.tcp://192.168.1.100:4840',
        'username': 'user',
        'password': 'pass',
        'nodes': [
            {'node_id': 'ns=2;s=Temperature', 'name': 'temperature', 'unit': 'C'},
            {'node_id': 'ns=2;s=Pressure', 'name': 'pressure', 'unit': 'MPa'},
        ]
    }


@pytest.fixture
def opcua_client(opcua_config):
    with patch('采集层.opcua_client.OPCUA_AVAILABLE', True):
        return OPCUAClient(opcua_config)


class TestOPCUAClientInit:
    def test_init(self, opcua_client):
        assert opcua_client.device_id == 'opcua_01'
        assert opcua_client.device_name == 'Siemens PLC'
        assert opcua_client.endpoint == 'opc.tcp://192.168.1.100:4840'
        assert opcua_client.username == 'user'
        assert opcua_client.connected is False
        assert len(opcua_client.node_configs) == 2

    def test_init_defaults(self):
        with patch('采集层.opcua_client.OPCUA_AVAILABLE', True):
            client = OPCUAClient({})
            assert client.device_id == 'opcua_device'
            assert client.endpoint == 'opc.tcp://localhost:4840'

    def test_init_not_available(self):
        with patch('采集层.opcua_client.OPCUA_AVAILABLE', False):
            with pytest.raises(ImportError):
                OPCUAClient({})


class TestOPCUAAddCallback:
    def test_add_data_callback(self, opcua_client):
        cb = MagicMock()
        opcua_client.add_data_callback(cb)
        assert cb in opcua_client._data_callbacks


class TestOPCUAConnect:
    def test_connect_already_running(self, opcua_client):
        opcua_client._running = True
        opcua_client.connected = True

        result = opcua_client.connect()

        assert result is True

    @patch('采集层.opcua_client.threading.Thread')
    def test_connect_timeout(self, mock_thread_cls, opcua_client):
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread

        result = opcua_client.connect()

        # After timeout, returns current connected state (False)
        assert result is False

    @patch('采集层.opcua_client.threading.Thread')
    def test_connect_success_quick(self, mock_thread_cls, opcua_client):
        def set_connected(*args, **kwargs):
            opcua_client.connected = True

        mock_thread = MagicMock()
        mock_thread.start.side_effect = set_connected
        mock_thread_cls.return_value = mock_thread

        result = opcua_client.connect()

        assert result is True


class TestOPCUADisconnect:
    def test_disconnect(self, opcua_client):
        opcua_client._running = True
        opcua_client.connected = True
        opcua_client._loop = MagicMock()
        opcua_client._loop.is_running.return_value = True
        opcua_client._thread = MagicMock()

        opcua_client.disconnect()

        assert opcua_client._running is False
        assert opcua_client.connected is False

    def test_disconnect_no_loop(self, opcua_client):
        opcua_client._loop = None
        opcua_client._thread = None

        opcua_client.disconnect()
        assert opcua_client.connected is False


class TestOPCUAGetLatestData:
    def test_get_latest_data_empty(self, opcua_client):
        result = opcua_client.get_latest_data()
        assert result == {}

    def test_get_latest_data(self, opcua_client):
        opcua_client.latest_data = {'temp': {'value': 25}}
        result = opcua_client.get_latest_data()
        assert result == {'temp': {'value': 25}}


class TestOPCUADatachangeNotification:
    def test_datachange_with_matching_config(self, opcua_client):
        cb = MagicMock()
        opcua_client._data_callbacks = [cb]

        mock_node = MagicMock()
        mock_node.nodeid.to_string.return_value = 'ns=2;s=Temperature'

        opcua_client.datachange_notification(mock_node, 25.5, None)

        assert 'temperature' in opcua_client.latest_data
        assert opcua_client.latest_data['temperature']['value'] == 25.5
        assert opcua_client.stats['data_updates'] == 1
        cb.assert_called_once_with('opcua_01', 'temperature', 25.5, 'C')

    def test_datachange_with_unknown_node(self, opcua_client):
        mock_node = MagicMock()
        mock_node.nodeid.to_string.return_value = 'ns=2;s=Unknown'

        opcua_client.datachange_notification(mock_node, 10, None)

        assert 'ns=2;s=Unknown' in opcua_client.latest_data
        assert opcua_client.stats['data_updates'] == 1

    def test_datachange_callback_exception(self, opcua_client):
        cb = MagicMock(side_effect=Exception("fail"))
        opcua_client._data_callbacks = [cb]

        mock_node = MagicMock()
        mock_node.nodeid.to_string.return_value = 'ns=2;s=Temperature'

        # Should not raise
        opcua_client.datachange_notification(mock_node, 25.5, None)

    def test_datachange_processing_exception(self, opcua_client):
        mock_node = MagicMock()
        mock_node.nodeid.to_string.side_effect = Exception("nodeid error")

        opcua_client.datachange_notification(mock_node, 25.5, None)

        assert opcua_client.stats['errors'] == 1


class TestOPCUAEventNotifications:
    def test_event_notification(self, opcua_client):
        opcua_client.event_notification("test_event")

    def test_status_change_notification(self, opcua_client):
        opcua_client.status_change_notification("test_status")


class TestOPCUARunLoop:
    def test_run_loop_exception(self, opcua_client):
        with patch('采集层.opcua_client.asyncio.new_event_loop') as mock_new, \
             patch('采集层.opcua_client.asyncio.set_event_loop'):
            mock_loop = MagicMock()
            mock_new.return_value = mock_loop
            mock_loop.run_until_complete.side_effect = Exception("loop error")

            opcua_client._run_loop()

            assert opcua_client.stats['last_error'] is not None
            mock_loop.close.assert_called_once()


class TestOPCUABridge:
    def test_to_unified_format(self, opcua_client):
        opcua_client.latest_data = {
            'temperature': {'value': 25.5, 'unit': 'C', 'timestamp': '2024-01-01', 'quality': 'good'}
        }
        bridge = OPCUABridge(opcua_client)

        result = bridge.to_unified_format()

        assert 'temperature' in result
        assert result['temperature']['value'] == 25.5
        assert result['temperature']['source'] == 'opcua'
        assert result['temperature']['device_id'] == 'opcua_01'

    def test_to_unified_format_empty(self, opcua_client):
        bridge = OPCUABridge(opcua_client)
        result = bridge.to_unified_format()
        assert result == {}
