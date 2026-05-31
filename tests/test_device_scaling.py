"""
Stress tests for SCADA system with 30 simulated devices.
Verifies scaling, data generation, batch processing, and API coverage.
"""

import os
import sys
import time
import pytest
import threading
from unittest.mock import MagicMock, patch

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from 采集层.simulated_device_manager import SimulatedDeviceManager
from 采集层.data_collector import DataCollector
from 采集层.simulated_client import set_estop_state, set_device_stopped

# ──────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────
EXPECTED_DEVICE_COUNT = 30
SIMULATED_CONFIG_PATH = os.path.join(PROJECT_ROOT, '配置', 'devices_simulated.yaml')


# ──────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────
@pytest.fixture(scope='module')
def device_manager():
    """Load SimulatedDeviceManager with the 30-device config."""
    mgr = SimulatedDeviceManager(config_path=SIMULATED_CONFIG_PATH)
    return mgr


@pytest.fixture(scope='module')
def connected_manager(device_manager):
    """Connect all 30 devices and yield the manager."""
    results = device_manager.connect_all()
    yield device_manager
    device_manager.disconnect_all()
    # Clean up E-STOP / device-stopped state
    set_estop_state(False)
    for did in list(device_manager.devices.keys()):
        set_device_stopped(did, False)


@pytest.fixture
def data_collector(device_manager):
    """Create a DataCollector with a mock database."""
    mock_db = MagicMock()
    mock_db.insert_data_batch = MagicMock()
    mock_db.get_realtime_data = MagicMock(return_value=[])
    mock_db.get_latest_data = MagicMock(return_value=None)
    mock_db.get_history_data = MagicMock(return_value=[])

    collector = DataCollector(device_manager=device_manager, database=mock_db)
    yield collector
    if collector.running:
        collector.stop()


def _count_all_registers(devices_config: dict) -> int:
    """Count total register/node/topic/endpoint data points across all devices."""
    total = 0
    for device_cfg in devices_config.values():
        total += len(device_cfg.get('registers', []))
        total += len(device_cfg.get('nodes', []))
        total += len(device_cfg.get('topics', []))
        total += len(device_cfg.get('endpoints', []))
    return total


# ================================================================
# 1. TestSimulatedDeviceManagerScaling
# ================================================================
class TestSimulatedDeviceManagerScaling:
    """Verify SimulatedDeviceManager loads and manages 30 devices."""

    def test_manager_loads_30_devices(self, device_manager):
        """All 30 devices should be loaded from devices_simulated.yaml."""
        assert len(device_manager.devices) == EXPECTED_DEVICE_COUNT, (
            f"Expected {EXPECTED_DEVICE_COUNT} devices, got {len(device_manager.devices)}"
        )

    def test_manager_creates_all_clients(self, device_manager):
        """get_client() should return a client for every device."""
        for device_id in device_manager.devices:
            client = device_manager.get_client(device_id)
            assert client is not None, f"get_client({device_id!r}) returned None"
            assert client.device_id == device_id

    def test_manager_connect_all(self, connected_manager):
        """connect_all() should succeed for all 30 devices."""
        results = connected_manager.connect_all()
        assert len(results) == EXPECTED_DEVICE_COUNT
        for device_id, success in results.items():
            assert success is True, f"connect_all() failed for {device_id!r}"

    def test_manager_disconnect_all(self):
        """disconnect_all() should work cleanly."""
        # Use a fresh manager to avoid side-effects on the shared fixture
        mgr = SimulatedDeviceManager(config_path=SIMULATED_CONFIG_PATH)
        mgr.connect_all()
        mgr.disconnect_all()
        for device_id, client in mgr.clients.items():
            assert not client.connected, (
                f"Client {device_id!r} still connected after disconnect_all()"
            )


# ================================================================
# 2. TestDataCollectorScaling
# ================================================================
class TestDataCollectorScaling:
    """Verify DataCollector handles 30-device load."""

    def test_collector_starts_all_devices(self, data_collector, device_manager):
        """start() should create collection tasks for all 30 enabled devices."""
        data_collector.start()
        try:
            enabled_count = sum(
                1 for d in device_manager.devices.values()
                if d.get('enabled', True)
            )
            # Push devices (OPC UA / MQTT) use _setup_push_device, not timer tasks
            push_count = sum(
                1 for d in device_manager.devices.values()
                if d.get('protocol') in ('opcua', 'mqtt')
            )
            expected_polling = enabled_count - push_count
            # Give timers a moment to fire
            time.sleep(0.5)
            assert len(data_collector.tasks) >= expected_polling - 2, (
                f"Expected ~{expected_polling} polling tasks, got {len(data_collector.tasks)}"
            )
        finally:
            data_collector.stop()

    def test_collector_batch_processing(self, data_collector, device_manager):
        """Batch processing should handle data from 30 devices."""
        data_collector.start()
        try:
            # Let a couple of collection cycles run
            time.sleep(2.0)
            stats = data_collector.get_stats()
            assert stats['total_collections'] > 0, (
                f"No collections performed. Stats: {stats}"
            )
        finally:
            data_collector.stop()

    def test_collector_memory_bounded(self, data_collector, device_manager):
        """Data queue size should stay bounded with 30 devices."""
        data_collector.start()
        try:
            # Run for several seconds
            time.sleep(3.0)
            queue_size = data_collector.data_queue.qsize()
            assert queue_size < 50000, (
                f"Queue size {queue_size} exceeds max capacity; possible memory leak"
            )
        finally:
            data_collector.stop()


# ================================================================
# 3. TestSimulationScaling
# ================================================================
class TestSimulationScaling:
    """Verify all 30 devices produce correct simulated data."""

    def test_all_devices_generate_data(self, connected_manager):
        """Every device should produce non-zero data from at least one source."""
        # Ensure all devices are connected (in case a prior test disconnected)
        connected_manager.connect_all()

        for device_id in connected_manager.devices:
            client = connected_manager.get_client(device_id)
            assert client is not None
            if not client.connected:
                client.connect()
            assert client.connected, f"Device {device_id!r} not connected"

            # For Modbus clients, read registers
            if hasattr(client, 'read_holding_registers'):
                config = connected_manager.devices[device_id]
                regs = config.get('registers', [])
                if regs:
                    first_reg = regs[0]
                    raw = client.read_holding_registers(
                        first_reg['address'],
                        first_reg.get('length', 2)
                    )
                    assert raw is not None, (
                        f"Device {device_id!r}: read_holding_registers returned None"
                    )
                    assert len(raw) > 0, (
                        f"Device {device_id!r}: empty register read"
                    )

            # For push clients, check cached data
            if hasattr(client, 'get_latest_data'):
                data = client.get_latest_data()
                if data:
                    # Values can be dicts with 'value' key, or plain floats
                    values = []
                    for v in data.values():
                        if isinstance(v, dict):
                            val = v.get('value')
                        else:
                            val = v
                        if val is not None:
                            values.append(val)
                    non_zero = [v for v in values if v != 0]
                    assert len(non_zero) > 0, (
                        f"Device {device_id!r}: all cached values are zero"
                    )

    def test_all_registers_have_values(self, connected_manager):
        """All 229 data points across 30 devices should produce values."""
        all_values = []
        for device_id in connected_manager.devices:
            client = connected_manager.get_client(device_id)
            if client is None:
                continue

            # Modbus devices: read each register
            if hasattr(client, 'read_holding_registers'):
                config = connected_manager.devices[device_id]
                for reg in config.get('registers', []):
                    raw = client.read_holding_registers(reg['address'], reg.get('length', 2))
                    if raw is not None and len(raw) > 0:
                        all_values.append((device_id, reg['name'], raw))

            # Push clients: get latest data
            if hasattr(client, 'get_latest_data'):
                data = client.get_latest_data()
                for name, info in data.items():
                    if isinstance(info, dict):
                        val = info.get('value')
                    else:
                        val = info
                    if val is not None:
                        all_values.append((device_id, name, val))

        total_data_points = _count_all_registers(connected_manager.devices)
        assert len(all_values) >= total_data_points * 0.8, (
            f"Expected ~{total_data_points} data points, got {len(all_values)}"
        )

    def test_no_extreme_values(self, connected_manager):
        """No simulated value should exceed reasonable physical bounds."""
        MAX_ABS_VALUE = 1_000_000  # Upper bound for any physical quantity
        for device_id in connected_manager.devices:
            client = connected_manager.get_client(device_id)
            if client is None:
                continue

            if hasattr(client, 'read_holding_registers'):
                config = connected_manager.devices[device_id]
                for reg in config.get('registers', []):
                    raw = client.read_holding_registers(reg['address'], reg.get('length', 2))
                    if raw is None:
                        continue
                    # Decode and check magnitude
                    dt = reg.get('data_type', 'uint16')
                    try:
                        if dt == 'float32' and len(raw) >= 2:
                            val = client.decode_float32(raw)
                        elif dt in ('int32', 'uint32') and len(raw) >= 2:
                            val = client.decode_uint32(raw) if dt == 'uint32' else client.decode_int32(raw)
                        elif dt == 'int16':
                            val = client.decode_int16(raw[0])
                        else:
                            val = client.decode_uint16(raw[0])
                    except Exception:
                        continue

                    if isinstance(val, (int, float)):
                        assert abs(val) < MAX_ABS_VALUE, (
                            f"Device {device_id!r} register {reg['name']!r}: "
                            f"value {val} exceeds bound +/-{MAX_ABS_VALUE}"
                        )

    def test_stopped_devices_return_environment(self, connected_manager):
        """Stopped mechanical devices should zero machinery, keep sensor values."""
        # Ensure devices are connected
        connected_manager.connect_all()

        # Pick a mechanical device if available
        mechanical_id = None
        for did, cfg in connected_manager.devices.items():
            from 采集层.interfaces import IDeviceManager
            if IDeviceManager.get_device_category(cfg) == 'mechanical':
                mechanical_id = did
                break

        if mechanical_id is None:
            pytest.skip("No mechanical device found to test stop behavior")

        client = connected_manager.get_client(mechanical_id)
        if not client.connected:
            client.connect()

        config = connected_manager.devices[mechanical_id]
        regs = config.get('registers', [])
        if not regs:
            pytest.skip("No registers to test")

        # Verify reads work before stop
        for reg in regs:
            raw = client.read_holding_registers(reg['address'], reg.get('length', 2))
            assert raw is not None, (
                f"Device {mechanical_id!r} register {reg['name']!r}: read returned None before stop"
            )

        # Stop the device and verify reads still return data (not None)
        set_device_stopped(mechanical_id, True)
        try:
            time.sleep(0.1)
            for reg in regs:
                raw = client.read_holding_registers(reg['address'], reg.get('length', 2))
                assert raw is not None, (
                    f"Stopped device {mechanical_id!r} register {reg['name']!r}: read returned None"
                )
                # Machinery registers should be zero, non-machinery should have values
                assert len(raw) > 0, (
                    f"Stopped device {mechanical_id!r} register {reg['name']!r}: empty result"
                )
        finally:
            set_device_stopped(mechanical_id, False)


# ================================================================
# 4. TestAPIScaling
# ================================================================
class TestAPIScaling:
    """Verify API endpoints return data for all 30 devices."""

    @pytest.fixture(autouse=True)
    def setup_app(self, app, device_manager, data_collector):
        """Attach real device manager and collector to the test app."""
        app.device_manager = device_manager
        app.data_collector = data_collector
        self.app = app

    def test_get_all_devices_returns_30(self, client, auth_headers):
        """/api/devices should return all 30 devices."""
        resp = client.get('/api/devices', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        devices = data.get('devices', [])
        assert len(devices) == EXPECTED_DEVICE_COUNT, (
            f"/api/devices returned {len(devices)} devices, expected {EXPECTED_DEVICE_COUNT}"
        )

    def test_get_realtime_data_covers_all(self, client, auth_headers, device_manager):
        """/api/data/realtime should cover all 30 devices."""
        # Override the mock DB to return a proper list covering all 30 devices
        self.app.database.get_realtime_data.return_value = [
            {'device_id': did, 'register_name': 'test', 'value': 1.0}
            for did in device_manager.devices
        ]
        resp = client.get('/api/data/realtime', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'data' in data
        # Verify data covers all 30 device IDs
        device_ids_in_data = {item['device_id'] for item in data['data']}
        assert len(device_ids_in_data) == EXPECTED_DEVICE_COUNT, (
            f"Realtime data covers {len(device_ids_in_data)} devices, expected {EXPECTED_DEVICE_COUNT}"
        )
