"""
Tests for SimulatedDeviceManager
"""
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from pathlib import Path
import tempfile
import os
import yaml


from 采集层.simulated_device_manager import SimulatedDeviceManager


class TestSimulatedDeviceManager:

    @pytest.fixture
    def tmp_config(self, tmp_path):
        """Create a temporary device config file"""
        config = {
            'devices': [
                {
                    'id': 'pump_01',
                    'name': 'Test Pump',
                    'protocol': 'modbus_tcp',
                    'host': '127.0.0.1',
                    'port': 502,
                    'enabled': True,
                    'device_category': 'mechanical',
                    'registers': [
                        {'name': 'speed', 'address': 0, 'type': 'uint16'},
                        {'name': 'status', 'address': 100, 'type': 'bool'},
                    ]
                },
                {
                    'id': 'sensor_01',
                    'name': 'Temp Sensor',
                    'protocol': 'modbus_tcp',
                    'host': '127.0.0.1',
                    'port': 502,
                    'enabled': True,
                    'device_category': 'instrument',
                    'registers': [
                        {'name': 'temperature', 'address': 0, 'type': 'float'},
                    ]
                },
                {
                    'id': 'mqtt_01',
                    'name': 'MQTT Sensor',
                    'protocol': 'mqtt',
                    'host': 'localhost',
                    'port': 1883,
                    'enabled': False,
                    'registers': []
                },
            ]
        }
        config_file = tmp_path / 'devices.yaml'
        with open(config_file, 'w', encoding='utf-8') as f:
            yaml.dump(config, f)
        return str(config_file)

    @pytest.fixture
    def mgr(self, tmp_config):
        return SimulatedDeviceManager(config_path=tmp_config)

    def test_init(self, mgr):
        assert mgr.simulation_mode is True
        assert len(mgr.devices) == 3

    def test_init_no_config(self, tmp_path):
        mgr = SimulatedDeviceManager(config_path=str(tmp_path / 'nonexistent.yaml'))
        assert mgr.simulation_mode is True
        assert len(mgr.devices) == 0

    def test_load_config(self, mgr):
        assert 'pump_01' in mgr.devices
        assert 'sensor_01' in mgr.devices
        assert 'mqtt_01' in mgr.devices

    def test_get_client_creates_client(self, mgr):
        client = mgr.get_client('pump_01')
        assert client is not None
        assert 'pump_01' in mgr.clients

    def test_get_client_cached(self, mgr):
        client1 = mgr.get_client('pump_01')
        client2 = mgr.get_client('pump_01')
        assert client1 is client2

    def test_get_client_nonexistent(self, mgr):
        client = mgr.get_client('nonexistent')
        assert client is None

    def test_create_simulated_client_modbus(self, mgr):
        config = {'protocol': 'modbus_tcp', 'host': '127.0.0.1', 'port': 502}
        client = mgr._create_simulated_client(config)
        assert client is not None

    def test_create_simulated_client_opcua(self, mgr):
        config = {'protocol': 'opcua', 'endpoint': 'opc.tcp://localhost:4840'}
        client = mgr._create_simulated_client(config)
        assert client is not None

    def test_create_simulated_client_mqtt(self, mgr):
        config = {'protocol': 'mqtt', 'host': 'localhost', 'port': 1883}
        client = mgr._create_simulated_client(config)
        assert client is not None

    def test_create_simulated_client_rest(self, mgr):
        config = {'protocol': 'rest', 'host': 'http://localhost'}
        client = mgr._create_simulated_client(config)
        assert client is not None

    def test_create_simulated_client_mc(self, mgr):
        config = {'protocol': 'mc', 'host': '127.0.0.1'}
        client = mgr._create_simulated_client(config)
        assert client is not None

    def test_create_simulated_client_fins(self, mgr):
        config = {'protocol': 'fins', 'host': '127.0.0.1'}
        client = mgr._create_simulated_client(config)
        assert client is not None

    def test_create_simulated_client_unknown(self, mgr):
        config = {'protocol': 'unknown'}
        client = mgr._create_simulated_client(config)
        assert client is None

    def test_connect_device(self, mgr):
        result = mgr.connect_device('pump_01')
        # May return True or False depending on simulated fault injection
        assert isinstance(result, bool)

    def test_connect_device_nonexistent(self, mgr):
        result = mgr.connect_device('nonexistent')
        assert result is False

    def test_disconnect_device(self, mgr):
        mgr.connect_device('pump_01')
        mgr.disconnect_device('pump_01')
        client = mgr.clients.get('pump_01')
        assert client is not None  # client still exists
        assert client.connected is False

    def test_disconnect_device_not_connected(self, mgr):
        mgr.disconnect_device('nonexistent')  # should not raise

    def test_connect_all(self, mgr):
        results = mgr.connect_all()
        assert 'pump_01' in results
        assert results['mqtt_01'] is None  # disabled

    def test_disconnect_all(self, mgr):
        mgr.connect_all()
        mgr.disconnect_all()
        for client in mgr.clients.values():
            assert client.connected is False

    def test_get_device_status(self, mgr):
        status = mgr.get_device_status('pump_01')
        assert status['device_id'] == 'pump_01'
        assert status['name'] == 'Test Pump'
        assert status['mode'] == 'simulated'
        assert 'stopped' in status

    def test_get_device_status_nonexistent(self, mgr):
        status = mgr.get_device_status('nonexistent')
        assert 'error' in status

    def test_get_device_status_with_client(self, mgr):
        mgr.get_client('pump_01')
        status = mgr.get_device_status('pump_01')
        assert status['connected'] is False

    def test_get_all_status(self, mgr):
        statuses = mgr.get_all_status()
        assert len(statuses) == 3

    def test_get_all_devices(self, mgr):
        devices = mgr.get_all_devices()
        assert 'pump_01' in devices

    def test_stop_device_mechanical(self, mgr):
        result = mgr.stop_device('pump_01')
        assert result is True

    def test_stop_device_instrument(self, mgr):
        result = mgr.stop_device('sensor_01')
        assert result is False

    def test_stop_device_nonexistent(self, mgr):
        result = mgr.stop_device('nonexistent')
        assert result is False

    def test_start_device_mechanical(self, mgr):
        mgr.stop_device('pump_01')
        result = mgr.start_device('pump_01')
        assert result is True

    def test_start_device_instrument(self, mgr):
        result = mgr.start_device('sensor_01')
        assert result is False

    def test_adjust_device(self, mgr):
        result = mgr.adjust_device('pump_01', 'speed', 1500)
        assert result['success'] is True

    def test_adjust_device_nonexistent(self, mgr):
        result = mgr.adjust_device('nonexistent', 'x', 1)
        assert result['success'] is False

    def test_adjust_device_bad_register(self, mgr):
        result = mgr.adjust_device('pump_01', 'nonexistent_reg', 1)
        assert result['success'] is False

    def test_set_estop_override(self, mgr):
        mgr.set_estop_override(True)
        mgr.set_estop_override(False)

    def test_get_protocol_summary(self, mgr):
        summary = mgr.get_protocol_summary()
        assert summary.get('modbus_tcp', 0) == 2
        assert summary.get('mqtt', 0) == 1

    def test_switch_simulation_mode_true(self, mgr):
        result = mgr.switch_simulation_mode(True)
        assert result['success'] is True

    def test_switch_simulation_mode_false(self, mgr):
        result = mgr.switch_simulation_mode(False)
        assert result['success'] is False

    def test_add_device(self, mgr, tmp_config):
        new_device = {
            'id': 'new_device',
            'name': 'New',
            'protocol': 'modbus_tcp',
            'host': '127.0.0.1',
            'port': 502,
        }
        result = mgr.add_device(new_device)
        assert result is True
        assert 'new_device' in mgr.devices

    def test_add_device_no_id(self, mgr):
        result = mgr.add_device({'name': 'No ID'})
        assert result is False

    def test_add_device_unsupported_protocol(self, mgr):
        result = mgr.add_device({'id': 'x', 'protocol': 'unsupported'})
        assert result is False

    def test_add_device_overwrite(self, mgr, tmp_config):
        result = mgr.add_device({'id': 'pump_01', 'name': 'Updated', 'protocol': 'modbus_tcp'})
        assert result is True
        assert mgr.devices['pump_01']['name'] == 'Updated'

    def test_remove_device(self, mgr, tmp_config):
        result = mgr.remove_device('pump_01')
        assert result is True
        assert 'pump_01' not in mgr.devices

    def test_remove_device_nonexistent(self, mgr, tmp_config):
        result = mgr.remove_device('nonexistent')
        assert result is True  # pop returns None, no error

    def test_supported_protocols(self):
        assert 'modbus_tcp' in SimulatedDeviceManager.SUPPORTED_PROTOCOLS
        assert 'opcua' in SimulatedDeviceManager.SUPPORTED_PROTOCOLS
        assert 'mqtt' in SimulatedDeviceManager.SUPPORTED_PROTOCOLS
        assert 'rest' in SimulatedDeviceManager.SUPPORTED_PROTOCOLS
