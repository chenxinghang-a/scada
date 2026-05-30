"""
Tests for MQTT client - covers 采集层/mqtt_client.py (14% -> high)
"""
import json
import pytest
from unittest.mock import MagicMock, patch, call
from datetime import datetime
from 采集层.mqtt_client import MQTTClient, MQTTDeviceManager


@pytest.fixture
def mqtt_config():
    return {
        'id': 'mqtt_01',
        'name': 'MQTT Sensor',
        'protocol': 'mqtt',
        'host': 'localhost',
        'port': 1883,
        'username': 'user',
        'password': 'pass',
        'topics': [
            {'topic': 'factory/temp', 'qos': 1, 'name': 'temperature', 'unit': 'C'},
        ]
    }


@pytest.fixture
def mqtt_client(mqtt_config):
    with patch('采集层.mqtt_client.mqtt.Client') as mock_cls:
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance
        client = MQTTClient(mqtt_config)
        client._mqtt_client_mock = mock_instance
        yield client


class TestMQTTClientInit:
    def test_init_with_config(self, mqtt_client):
        assert mqtt_client.device_id == 'mqtt_01'
        assert mqtt_client.device_name == 'MQTT Sensor'
        assert mqtt_client.broker_host == 'localhost'
        assert mqtt_client.broker_port == 1883
        assert mqtt_client.connected is False

    def test_init_with_kwargs(self):
        with patch('采集层.mqtt_client.mqtt.Client'):
            client = MQTTClient(broker_host='10.0.0.1', broker_port=8883, client_id='test')
            assert client.broker_host == '10.0.0.1'
            assert client.broker_port == 8883

    def test_init_no_config(self):
        with patch('采集层.mqtt_client.mqtt.Client'):
            client = MQTTClient()
            assert client.broker_host == 'localhost'
            assert client.broker_port == 1883

    def test_stats_init(self, mqtt_client):
        assert mqtt_client.stats['messages_received'] == 0
        assert mqtt_client.stats['messages_parsed'] == 0
        assert mqtt_client.stats['parse_errors'] == 0


class TestMQTTCallback:
    def test_add_data_callback(self, mqtt_client):
        cb = MagicMock()
        mqtt_client.add_data_callback(cb)
        assert cb in mqtt_client._data_callbacks


class TestMQTTSubscribe:
    def test_subscribe_when_connected(self, mqtt_client):
        mqtt_client.connected = True
        mqtt_client.client.subscribe.return_value = (0, 1)

        result = mqtt_client.subscribe('test/topic', qos=1)

        assert result is True
        assert 'test/topic' in mqtt_client._subscriptions

    def test_subscribe_when_not_connected(self, mqtt_client):
        result = mqtt_client.subscribe('test/topic')

        assert result is True
        assert 'test/topic' in mqtt_client._subscriptions

    def test_subscribe_failure(self, mqtt_client):
        mqtt_client.connected = True
        mqtt_client.client.subscribe.return_value = (1, 1)  # Error code

        result = mqtt_client.subscribe('test/topic')

        assert result is False

    def test_unsubscribe(self, mqtt_client):
        mqtt_client._subscriptions['test/topic'] = 1
        mqtt_client.connected = True

        mqtt_client.unsubscribe('test/topic')

        assert 'test/topic' not in mqtt_client._subscriptions
        mqtt_client.client.unsubscribe.assert_called_once_with('test/topic')

    def test_unsubscribe_not_in_list(self, mqtt_client):
        mqtt_client.connected = True
        mqtt_client.unsubscribe('nonexistent')


class TestMQTTPublish:
    def test_publish_not_connected(self, mqtt_client):
        result = mqtt_client.publish('topic', {'value': 1})
        assert result is False

    def test_publish_success(self, mqtt_client):
        mqtt_client.connected = True
        mock_result = MagicMock()
        mock_result.rc = 0
        mqtt_client.client.publish.return_value = mock_result

        result = mqtt_client.publish('topic', {'value': 1})

        assert result is True

    def test_publish_failure(self, mqtt_client):
        mqtt_client.connected = True
        mqtt_client.client.publish.side_effect = Exception("fail")

        result = mqtt_client.publish('topic', {'value': 1})

        assert result is False


class TestMQTTConnect:
    def test_connect_success(self, mqtt_client):
        mqtt_client.client.connect.return_value = 0

        result = mqtt_client.connect()

        assert result is True
        mqtt_client.client.loop_start.assert_called_once()

    def test_connect_failure(self, mqtt_client):
        mqtt_client.client.connect.side_effect = ConnectionError("fail")

        result = mqtt_client.connect()

        assert result is False

    @patch('采集层.mqtt_client.ssl.create_default_context')
    def test_connect_with_tls(self, mock_ssl_ctx):
        config = {
            'host': 'broker.example.com',
            'tls_enabled': True,
            'ca_cert': '/path/to/ca.pem',
        }
        with patch('采集层.mqtt_client.mqtt.Client') as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance
            client = MQTTClient(config)
            client.client.connect.return_value = 0

            result = client.connect()
            assert result is True

    @patch('采集层.mqtt_client.ssl.create_default_context')
    def test_connect_tls_no_ca(self, mock_ssl_ctx):
        config = {
            'host': 'broker.example.com',
            'tls_enabled': True,
            'tls_insecure': True,
        }
        with patch('采集层.mqtt_client.mqtt.Client') as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance
            client = MQTTClient(config)
            client.client.connect.return_value = 0

            result = client.connect()
            assert result is True

    @patch('采集层.mqtt_client.ssl.create_default_context')
    def test_connect_tls_with_cert(self, mock_ssl_ctx):
        config = {
            'host': 'broker.example.com',
            'tls_enabled': True,
            'client_cert': '/path/to/cert.pem',
            'client_key': '/path/to/key.pem',
        }
        with patch('采集层.mqtt_client.mqtt.Client') as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value = mock_instance
            client = MQTTClient(config)
            client.client.connect.return_value = 0

            result = client.connect()
            assert result is True


class TestMQTTDisconnect:
    def test_disconnect(self, mqtt_client):
        mqtt_client.connected = True
        mqtt_client.disconnect()
        assert mqtt_client.connected is False
        mqtt_client.client.loop_stop.assert_called_once()
        mqtt_client.client.disconnect.assert_called_once()

    def test_disconnect_failure(self, mqtt_client):
        mqtt_client.client.loop_stop.side_effect = Exception("fail")
        mqtt_client.disconnect()


class TestMQTTOnConnect:
    def test_on_connect_success(self, mqtt_client):
        mqtt_client._subscriptions = {'test/topic': 1}

        mqtt_client._on_connect(mqtt_client.client, None, None, 0)

        assert mqtt_client.connected is True
        mqtt_client.client.subscribe.assert_called_with('test/topic', 1)

    def test_on_connect_failure(self, mqtt_client):
        mqtt_client._on_connect(mqtt_client.client, None, None, 1)
        assert mqtt_client.connected is False


class TestMQTTOnDisconnect:
    def test_on_disconnect_normal(self, mqtt_client):
        mqtt_client.connected = True
        mqtt_client._on_disconnect(mqtt_client.client, None, 0)
        assert mqtt_client.connected is False

    def test_on_disconnect_unexpected(self, mqtt_client):
        mqtt_client.connected = True
        mqtt_client._on_disconnect(mqtt_client.client, None, 1)
        assert mqtt_client.connected is False


class TestMQTTOnMessage:
    def test_on_message_json_format1(self, mqtt_client):
        cb = MagicMock()
        mqtt_client._data_callbacks = [cb]
        msg = MagicMock()
        msg.topic = 'factory/temp'
        msg.payload = json.dumps({
            'device_id': 'dev1',
            'register': 'temperature',
            'value': 25.5,
            'unit': 'C'
        }).encode()

        mqtt_client._on_message(mqtt_client.client, None, msg)

        assert mqtt_client.stats['messages_received'] == 1
        assert mqtt_client.stats['messages_parsed'] == 1
        cb.assert_called_once_with('dev1', 'temperature', 25.5, 'C')

    def test_on_message_json_format2_simple(self, mqtt_client):
        cb = MagicMock()
        mqtt_client._data_callbacks = [cb]
        msg = MagicMock()
        msg.topic = 'factory/sensor1'
        msg.payload = json.dumps({'temperature': 25.5}).encode()

        mqtt_client._on_message(mqtt_client.client, None, msg)

        assert mqtt_client.stats['messages_parsed'] == 1
        cb.assert_called_once()

    def test_on_message_json_format3_nested(self, mqtt_client):
        cb = MagicMock()
        mqtt_client._data_callbacks = [cb]
        msg = MagicMock()
        msg.topic = 'factory/data'
        msg.payload = json.dumps({
            'device_id': 'dev1',
            'data': {'temp': 25.5, 'pressure': 101.3}
        }).encode()

        mqtt_client._on_message(mqtt_client.client, None, msg)

        assert cb.call_count == 2

    def test_on_message_raw_key_value(self, mqtt_client):
        cb = MagicMock()
        mqtt_client._data_callbacks = [cb]
        msg = MagicMock()
        msg.topic = 'factory/sensor'
        msg.payload = b'temperature=25.5'

        mqtt_client._on_message(mqtt_client.client, None, msg)

        cb.assert_called_once()

    def test_on_message_raw_number(self, mqtt_client):
        cb = MagicMock()
        mqtt_client._data_callbacks = [cb]
        msg = MagicMock()
        msg.topic = 'factory/sensor'
        msg.payload = b'abc25.5'  # Not valid JSON, triggers raw parsing

        mqtt_client._on_message(mqtt_client.client, None, msg)

        # "abc25.5" doesn't contain '=' so goes to float(payload.strip()) which fails
        # so no callback but also no crash

    def test_on_message_raw_unparseable(self, mqtt_client):
        msg = MagicMock()
        msg.topic = 'factory/sensor'
        msg.payload = b'not a number'

        mqtt_client._on_message(mqtt_client.client, None, msg)

        assert mqtt_client.stats['parse_errors'] == 0  # _process_raw_message doesn't set parse_errors

    def test_on_message_exception(self, mqtt_client):
        msg = MagicMock()
        msg.payload = None  # Will cause decode error
        msg.topic = 'test'

        mqtt_client._on_message(mqtt_client.client, None, msg)

        assert mqtt_client.stats['parse_errors'] == 1


class TestMQTTOnSubscribe:
    def test_on_subscribe(self, mqtt_client):
        mqtt_client._on_subscribe(mqtt_client.client, None, 1, [1])


class TestMQTTNotifyCallbacks:
    def test_notify_callbacks(self, mqtt_client):
        cb = MagicMock()
        mqtt_client._data_callbacks = [cb]

        mqtt_client._notify_callbacks('dev1', 'temp', 25.5, 'C')

        cb.assert_called_once_with('dev1', 'temp', 25.5, 'C')
        assert 'temp' in mqtt_client.latest_data

    def test_notify_callbacks_exception(self, mqtt_client):
        cb = MagicMock(side_effect=Exception("fail"))
        mqtt_client._data_callbacks = [cb]

        # Should not raise
        mqtt_client._notify_callbacks('dev1', 'temp', 25.5, 'C')


class TestMQTTGetDataAndStats:
    def test_get_latest_data(self, mqtt_client):
        mqtt_client.latest_data = {'temp': {'value': 25}}
        result = mqtt_client.get_latest_data()
        assert result == {'temp': {'value': 25}}

    def test_get_stats(self, mqtt_client):
        stats = mqtt_client.get_stats()
        assert 'device_id' in stats
        assert 'connected' in stats
        assert 'broker' in stats

    def test_get_status(self, mqtt_client):
        status = mqtt_client.get_status()
        assert 'connected' in status
        assert 'broker' in status


class TestMQTTDeviceManager:
    def test_init(self):
        config = {'host': 'localhost', 'topics': []}
        mgr = MQTTDeviceManager(config)
        assert mgr.client is None
        assert mgr._running is False

    def test_get_status_no_client(self):
        config = {'host': 'localhost'}
        mgr = MQTTDeviceManager(config)
        status = mgr.get_status()
        assert status['connected'] is False

    @patch('采集层.mqtt_client.MQTTClient')
    def test_start_success(self, mock_cls):
        config = {
            'host': 'localhost',
            'topics': [{'topic': 'test', 'qos': 1}]
        }
        mock_client = MagicMock()
        mock_client.connect.return_value = True
        mock_cls.return_value = mock_client

        mgr = MQTTDeviceManager(config)
        result = mgr.start(MagicMock())

        assert result is True
        assert mgr._running is True

    @patch('采集层.mqtt_client.MQTTClient')
    def test_start_failure(self, mock_cls):
        config = {'host': 'localhost', 'topics': []}
        mock_client = MagicMock()
        mock_client.connect.return_value = False
        mock_cls.return_value = mock_client

        mgr = MQTTDeviceManager(config)
        result = mgr.start(MagicMock())

        assert result is False

    @patch('采集层.mqtt_client.MQTTClient')
    def test_start_exception(self, mock_cls):
        mock_cls.side_effect = Exception("fail")
        mgr = MQTTDeviceManager({'host': 'localhost'})
        result = mgr.start(MagicMock())
        assert result is False

    def test_stop(self):
        config = {'host': 'localhost'}
        mgr = MQTTDeviceManager(config)
        mgr.client = MagicMock()
        mgr._running = True
        mgr.stop()
        assert mgr._running is False

    def test_get_status_with_client(self):
        config = {'host': 'localhost'}
        mgr = MQTTDeviceManager(config)
        mgr.client = MagicMock()
        mgr.client.get_status.return_value = {'connected': True}
        status = mgr.get_status()
        assert status['connected'] is True
