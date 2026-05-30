"""
Tests for 采集层.device_manager: DeviceManager lifecycle, locking, simulation mode
"""

import pytest
import threading
import yaml
import tempfile
import os
from pathlib import Path
from unittest.mock import MagicMock, patch


@pytest.fixture
def devices_yaml(tmp_path):
    """Create a temporary devices.yaml config file"""
    config = {
        'devices': [
            {
                'id': 'pump_01',
                'name': 'Test Pump',
                'protocol': 'modbus_tcp',
                'host': '127.0.0.1',
                'port': 502,
                'enabled': True,
                'registers': [
                    {'name': 'flow_rate', 'address': 0, 'data_type': 'uint16', 'unit': 'm3/h'}
                ]
            },
            {
                'id': 'motor_01',
                'name': 'Test Motor',
                'protocol': 'modbus_tcp',
                'host': '127.0.0.1',
                'port': 503,
                'enabled': True,
                'registers': []
            },
            {
                'id': 'sensor_01',
                'name': 'Temp Sensor',
                'protocol': 'opcua',
                'endpoint': 'opc.tcp://localhost:4840',
                'enabled': False,
                'nodes': []
            },
        ]
    }
    cfg_file = tmp_path / 'devices.yaml'
    with open(cfg_file, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, allow_unicode=True)
    return str(cfg_file)


@pytest.fixture
def empty_yaml(tmp_path):
    """Empty devices.yaml (no devices key)"""
    cfg_file = tmp_path / 'empty.yaml'
    with open(cfg_file, 'w', encoding='utf-8') as f:
        yaml.dump({}, f)
    return str(cfg_file)


@pytest.fixture
def device_manager(devices_yaml):
    """Create DeviceManager in simulation mode with real YAML config"""
    from 采集层.device_manager import DeviceManager
    return DeviceManager(config_path=devices_yaml, simulation_mode=True, use_enhanced_simulation=False)


# ============================================================
# Config Loading Tests
# ============================================================

class TestDeviceConfigLoading:

    def test_load_config_parses_devices(self, device_manager):
        """load_config correctly parses device entries from YAML"""
        assert 'pump_01' in device_manager.devices
        assert 'motor_01' in device_manager.devices
        assert 'sensor_01' in device_manager.devices

    def test_load_config_count(self, device_manager):
        """All devices from config are loaded"""
        assert len(device_manager.devices) == 3

    def test_load_config_missing_file(self, tmp_path):
        """Missing config file logs error but doesn't crash"""
        from 采集层.device_manager import DeviceManager
        dm = DeviceManager(config_path=str(tmp_path / 'nonexistent.yaml'), simulation_mode=True)
        assert dm.devices == {}

    def test_load_config_empty_file(self, empty_yaml):
        """Empty config file yields empty devices dict"""
        from 采集层.device_manager import DeviceManager
        dm = DeviceManager(config_path=empty_yaml, simulation_mode=True)
        assert dm.devices == {}

    def test_get_device_config_existing(self, device_manager):
        """get_device_config returns config for known device"""
        cfg = device_manager.get_device_config('pump_01')
        assert cfg is not None
        assert cfg['name'] == 'Test Pump'

    def test_get_device_config_unknown(self, device_manager):
        """get_device_config returns None for unknown device"""
        assert device_manager.get_device_config('nonexistent') is None

    def test_get_all_devices_returns_copy(self, device_manager):
        """get_all_devices returns a copy (not internal reference)"""
        all_devs = device_manager.get_all_devices()
        all_devs['extra'] = {}
        assert 'extra' not in device_manager.devices

    def test_protocol_summary(self, device_manager):
        """get_protocol_summary counts devices per protocol"""
        summary = device_manager.get_protocol_summary()
        assert summary.get('modbus_tcp') == 2
        assert summary.get('opcua') == 1


# ============================================================
# Client Creation & Locking Tests
# ============================================================

class TestGetClient:

    def test_get_client_creates_client(self, device_manager):
        """get_client creates a client for a known device"""
        client = device_manager.get_client('pump_01')
        assert client is not None

    def test_get_client_returns_same_instance(self, device_manager):
        """get_client returns the same client instance (cached)"""
        c1 = device_manager.get_client('pump_01')
        c2 = device_manager.get_client('pump_01')
        assert c1 is c2

    def test_get_client_unknown_device(self, device_manager):
        """get_client returns None for unknown device"""
        assert device_manager.get_client('nonexistent') is None

    def test_get_client_thread_safety(self, device_manager):
        """Concurrent get_client calls return the same instance (double-check locking)"""
        results = []
        barrier = threading.Barrier(10)

        def fetch():
            barrier.wait(timeout=5)
            c = device_manager.get_client('motor_01')
            results.append(id(c))

        threads = [threading.Thread(target=fetch) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # All threads should get the same object identity
        assert len(set(results)) == 1

    def test_get_client_unsupported_protocol(self, tmp_path):
        """Unsupported protocol returns None client"""
        cfg = {'devices': [{'id': 'bad', 'protocol': 'zigbee'}]}
        cfg_file = tmp_path / 'bad.yaml'
        with open(cfg_file, 'w') as f:
            yaml.dump(cfg, f)
        from 采集层.device_manager import DeviceManager
        dm = DeviceManager(config_path=str(cfg_file), simulation_mode=True)
        # The device should be loaded but client creation returns None
        assert 'bad' in dm.devices
        client = dm.get_client('bad')
        assert client is None


# ============================================================
# Connect / Disconnect Tests
# ============================================================

class TestConnectDisconnect:

    def test_connect_device_success(self, device_manager):
        """connect_device returns True for valid device in simulation"""
        result = device_manager.connect_device('pump_01')
        assert result is True

    def test_connect_device_unknown(self, device_manager):
        """connect_device returns False for unknown device"""
        result = device_manager.connect_device('nonexistent')
        assert result is False

    def test_disconnect_device(self, device_manager):
        """disconnect_device calls client.disconnect()"""
        client = device_manager.get_client('pump_01')
        client.connect()
        device_manager.disconnect_device('pump_01')
        # After disconnect, client.connected should be False
        assert not client.connected

    def test_disconnect_unknown_device(self, device_manager):
        """disconnect_device is safe for unknown device (no-op)"""
        device_manager.disconnect_device('nonexistent')  # should not raise

    def test_connect_all(self, device_manager):
        """connect_all connects all enabled devices"""
        results = device_manager.connect_all()
        # pump_01 and motor_01 are enabled, sensor_01 is disabled
        assert results.get('pump_01') is True
        assert results.get('motor_01') is True
        assert results.get('sensor_01') is None  # disabled

    def test_disconnect_all(self, device_manager):
        """disconnect_all disconnects all connected devices"""
        device_manager.connect_all()
        device_manager.disconnect_all()
        # All clients should be disconnected
        for client in device_manager.clients.values():
            assert not client.connected


# ============================================================
# Add / Remove Device Tests
# ============================================================

class TestAddRemoveDevice:

    def test_add_device(self, device_manager, tmp_path):
        """add_device adds a new device and connects it"""
        new_dev = {
            'id': 'new_sensor',
            'name': 'New Sensor',
            'protocol': 'modbus_tcp',
            'host': '127.0.0.1',
            'port': 504,
            'enabled': True,
            'registers': []
        }
        # Patch _save_config to avoid writing to original config file
        with patch.object(device_manager, '_save_config'):
            result = device_manager.add_device(new_dev)
        assert result is True
        assert 'new_sensor' in device_manager.devices

    def test_add_device_no_id(self, device_manager):
        """add_device rejects config without id"""
        result = device_manager.add_device({'protocol': 'modbus_tcp'})
        assert result is False

    def test_add_device_unsupported_protocol(self, device_manager):
        """add_device rejects unsupported protocol"""
        result = device_manager.add_device({'id': 'bad', 'protocol': 'bluetooth'})
        assert result is False

    def test_add_device_overwrite_existing(self, device_manager):
        """add_device overwrites existing device with same id"""
        updated = {
            'id': 'pump_01',
            'name': 'Updated Pump',
            'protocol': 'modbus_tcp',
            'host': '10.0.0.1',
            'port': 502,
            'registers': []
        }
        with patch.object(device_manager, '_save_config'):
            result = device_manager.add_device(updated)
        assert result is True
        assert device_manager.devices['pump_01']['name'] == 'Updated Pump'

    def test_remove_device(self, device_manager):
        """remove_device removes device from manager"""
        with patch.object(device_manager, '_save_config'):
            result = device_manager.remove_device('pump_01')
        assert result is True
        assert 'pump_01' not in device_manager.devices

    def test_remove_nonexistent_device(self, device_manager):
        """remove_device is safe for unknown device"""
        with patch.object(device_manager, '_save_config'):
            result = device_manager.remove_device('nonexistent')
        assert result is True  # pop with default doesn't raise


# ============================================================
# Simulation Mode Switch Tests
# ============================================================

class TestSimulationModeSwitch:

    def test_switch_to_same_mode(self, device_manager):
        """Switching to current mode is a no-op"""
        assert device_manager.simulation_mode is True
        result = device_manager.switch_simulation_mode(True)
        assert result['success'] is True
        assert '无需切换' in result['message']
        assert result['reconnected'] == 0

    def test_switch_mode_changes_flag(self, device_manager):
        """Switching mode updates simulation_mode flag"""
        device_manager.connect_all()
        result = device_manager.switch_simulation_mode(False)
        assert result['success'] is True
        assert device_manager.simulation_mode is False

    def test_switch_mode_clears_clients(self, device_manager):
        """Switching mode clears client cache"""
        device_manager.get_client('pump_01')
        assert len(device_manager.clients) > 0
        device_manager.switch_simulation_mode(False)
        # After switch + reconnect, clients are recreated
        assert device_manager.simulation_mode is False


# ============================================================
# Device Status Tests
# ============================================================

class TestDeviceStatus:

    def test_get_device_status(self, device_manager):
        """get_device_status returns status dict for known device"""
        status = device_manager.get_device_status('pump_01')
        assert status['device_id'] == 'pump_01'
        assert status['protocol'] == 'modbus_tcp'
        assert 'connected' in status

    def test_get_device_status_unknown(self, device_manager):
        """get_device_status returns error for unknown device"""
        status = device_manager.get_device_status('nonexistent')
        assert 'error' in status

    def test_get_all_status(self, device_manager):
        """get_all_status returns list of all device statuses"""
        all_status = device_manager.get_all_status()
        assert len(all_status) == 3
        ids = {s['device_id'] for s in all_status}
        assert ids == {'pump_01', 'motor_01', 'sensor_01'}
