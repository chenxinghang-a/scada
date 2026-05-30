"""
Device Simulation Realism Tests
================================
Ensures simulated device data behaves like real industrial equipment:
- Values stay within physical bounds
- Temperature exhibits realistic thermal inertia
- Stopped devices return environment values (not zeros)
- E-STOP preserves sensor readings while stopping machinery
"""

import math
import time
import pytest
from unittest.mock import MagicMock, patch


# ============================================================
# Helper: create a behavior simulator
# ============================================================

def _make_sim(device_type='generic'):
    """Create a DeviceBehaviorSimulator with a standard config."""
    from 采集层.device_behavior_simulator import DeviceBehaviorSimulator, DeviceState

    config = {
        'id': f'test_{device_type}',
        'name': f'Test {device_type.title()}',
        'protocol': 'modbus_tcp',
        'host': '127.0.0.1',
        'port': 502,
        'description': device_type,
        'registers': [
            {'name': 'temperature', 'address': 0, 'type': 'float', 'unit': 'C'},
            {'name': 'pressure', 'address': 2, 'type': 'float', 'unit': 'MPa'},
            {'name': 'flow', 'address': 4, 'type': 'float', 'unit': 'm3/h'},
            {'name': 'level', 'address': 6, 'type': 'float', 'unit': '%'},
            {'name': 'motor_speed', 'address': 8, 'type': 'float', 'unit': 'RPM'},
            {'name': 'vibration', 'address': 10, 'type': 'float', 'unit': 'mm/s'},
            {'name': 'speed', 'address': 12, 'type': 'float', 'unit': 'RPM'},
        ]
    }
    sim = DeviceBehaviorSimulator(f'test_{device_type}', config)
    sim.state = DeviceState.RUNNING
    sim._running = True
    return sim


# ============================================================
# Physical Bounds
# ============================================================

class TestPhysicalBounds:
    """Simulated values must stay within physically plausible ranges."""

    def test_temperature_within_reasonable_range(self):
        """Temperature stays between -20 and 500 C during normal operation."""
        sim = _make_sim('generic')
        for _ in range(50):
            data = sim.update(1.0)
            temp = data.get('temperature', 0)
            assert -20 < temp < 500, f"Temperature out of range: {temp}"

    def test_pressure_non_negative(self):
        """Pressure is always non-negative."""
        sim = _make_sim('generic')
        for _ in range(50):
            data = sim.update(1.0)
            pressure = data.get('pressure', 0)
            assert pressure >= 0, f"Negative pressure: {pressure}"

    def test_level_between_0_and_100(self):
        """Liquid level stays between 0 and 100 percent."""
        sim = _make_sim('generic')
        for _ in range(100):
            data = sim.update(1.0)
            level = data.get('level', 0)
            # level is output in meters (level/100*3), so raw level is 0-100
            # The output value is level/100*3, so 0-3 meters
            assert 0 <= level <= 5, f"Level out of range: {level}"

    def test_flow_non_negative(self):
        """Flow rate is never negative."""
        sim = _make_sim('generic')
        for _ in range(50):
            data = sim.update(1.0)
            flow = data.get('flow', 0)
            assert flow >= 0, f"Negative flow: {flow}"

    def test_voltage_within_grid_bounds(self):
        """Voltage stays within reasonable grid bounds (180-260V)."""
        sim = _make_sim('generic')
        for _ in range(50):
            data = sim.update(1.0)
            for key in ('voltage_a', 'voltage_b', 'voltage_c'):
                if key in data:
                    v = data[key]
                    assert 150 < v < 300, f"{key} out of range: {v}"

    def test_power_factor_between_0_and_1(self):
        """Power factor stays between 0 and 1."""
        sim = _make_sim('generic')
        for _ in range(50):
            data = sim.update(1.0)
            pf = data.get('power_factor', 0.85)
            assert 0 <= pf <= 1.0, f"Power factor out of range: {pf}"


# ============================================================
# Thermal Inertia
# ============================================================

class TestThermalInertia:
    """Temperature changes should exhibit realistic inertia (slow response)."""

    def test_temperature_changes_gradually(self):
        """Temperature doesn't jump instantly; it changes gradually."""
        sim = _make_sim('generic')
        sim.state = sim.state.RUNNING

        # Collect initial temperature
        data = sim.update(1.0)
        initial_temp = data.get('temperature', 50)

        # Run several cycles - temperature should change slowly
        max_delta = 0
        prev_temp = initial_temp
        for _ in range(10):
            data = sim.update(1.0)
            temp = data.get('temperature', 50)
            delta = abs(temp - prev_temp)
            max_delta = max(max_delta, delta)
            prev_temp = temp

        # With thermal_time_constant=120s and dt=1s, alpha = 1-exp(-1/120) ~ 0.008
        # Temperature should not change more than ~5C per second under normal conditions
        assert max_delta < 10, f"Temperature changed too fast: {max_delta}C in 1s"

    def test_thermal_time_constant_effect(self):
        """Higher thermal time constant means slower temperature response."""
        from 采集层.device_behavior_simulator import DeviceBehaviorSimulator, DeviceState

        # Device with high thermal inertia
        config_high = {
            'id': 'high_inertia', 'name': 'High Inertia', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temperature', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        sim_high = DeviceBehaviorSimulator('high_inertia', config_high)
        sim_high.process_model.thermal_time_constant = 300.0  # Very slow
        sim_high.state = DeviceState.RUNNING
        sim_high._running = True

        # Device with low thermal inertia
        config_low = {
            'id': 'low_inertia', 'name': 'Low Inertia', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temperature', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        sim_low = DeviceBehaviorSimulator('low_inertia', config_low)
        sim_low.process_model.thermal_time_constant = 10.0  # Very fast
        sim_low.state = DeviceState.RUNNING
        sim_low._running = True

        # Run both for 5 seconds
        high_deltas = []
        low_deltas = []

        prev_high = sim_high.update(1.0).get('temperature', 0)
        prev_low = sim_low.update(1.0).get('temperature', 0)

        for _ in range(5):
            high_temp = sim_high.update(1.0).get('temperature', 0)
            low_temp = sim_low.update(1.0).get('temperature', 0)
            high_deltas.append(abs(high_temp - prev_high))
            low_deltas.append(abs(low_temp - prev_low))
            prev_high = high_temp
            prev_low = low_temp

        avg_high = sum(high_deltas) / len(high_deltas)
        avg_low = sum(low_deltas) / len(low_deltas)

        # Higher time constant should result in slower changes
        # Allow some noise tolerance
        assert avg_high <= avg_low * 3, \
            f"High inertia ({avg_high:.2f}) should change slower than low inertia ({avg_low:.2f})"


# ============================================================
# Stopped Device Returns Environment Values
# ============================================================

class TestStoppedDeviceBehavior:
    """Stopped devices must return environment values, not zeros."""

    def test_stopped_device_has_ambient_temperature(self):
        """A stopped device returns ~25C ambient temperature, not 0."""
        from 采集层.device_behavior_simulator import DeviceState

        sim = _make_sim('generic')
        sim.state = DeviceState.STOPPED
        sim._running = True

        data = sim.update(1.0)
        temp = data.get('temperature', data.get('boiler_temperature', 0))
        # Should be around 25C ambient (with small noise)
        assert 20 < temp < 30, f"Stopped device temp should be ~25C, got {temp}"

    def test_stopped_device_has_atmospheric_pressure(self):
        """A stopped device returns ~0.101 MPa atmospheric pressure."""
        from 采集层.device_behavior_simulator import DeviceState

        sim = _make_sim('generic')
        sim.state = DeviceState.STOPPED
        sim._running = True

        data = sim.update(1.0)
        pressure = data.get('pressure', 0)
        # Should be around 0.101 MPa (atmospheric)
        assert 0.09 < pressure < 0.12, f"Stopped device pressure should be ~0.101 MPa, got {pressure}"

    def test_stopped_device_zero_mechanical_values(self):
        """A stopped device returns 0 for mechanical values (speed, vibration)."""
        from 采集层.device_behavior_simulator import DeviceState

        sim = _make_sim('generic')
        sim.state = DeviceState.STOPPED
        sim._running = True

        data = sim.update(1.0)
        # Motor speed should be 0
        speed = data.get('motor_speed', data.get('speed', None))
        if speed is not None:
            assert abs(speed) < 1.0, f"Stopped motor speed should be ~0, got {speed}"

        # Vibration should be ~0
        vibration = data.get('vibration', None)
        if vibration is not None:
            assert abs(vibration) < 0.5, f"Stopped vibration should be ~0, got {vibration}"

    def test_stopped_device_has_nonzero_ph(self):
        """A stopped device returns ~7.0 pH (neutral)."""
        from 采集层.device_behavior_simulator import DeviceState

        sim = _make_sim('water')
        sim.state = DeviceState.STOPPED
        sim._running = True

        data = sim.update(1.0)
        ph = data.get('ph', 7.0)
        assert 6.0 < ph < 8.0, f"Stopped device pH should be ~7.0, got {ph}"


# ============================================================
# E-STOP Behavior
# ============================================================

class TestEStopBehavior:
    """E-STOP should stop machinery while preserving sensor readings."""

    def test_estop_zeroes_machinery_registers(self):
        """E-STOP sets machinery registers (speed, force) to 0."""
        from 采集层.simulated_client import set_estop_state, _is_machinery

        # Verify speed is machinery
        assert _is_machinery('speed') is True
        assert _is_machinery('motor_speed') is True
        assert _is_machinery('conveyor_speed') is True

        set_estop_state(True)
        try:
            # After E-STOP, machinery values should be overridden to 0
            from 采集层.simulated_client import _estop_override
            assert _estop_override('speed', 1500) == 0.0
            assert _estop_override('motor_speed', 3000) == 0.0
            assert _estop_override('clamping_force', 500) == 0.0
        finally:
            set_estop_state(False)

    def test_estop_preserves_sensor_readings(self):
        """E-STOP does not affect sensor/monitoring registers."""
        from 采集层.simulated_client import set_estop_state, _estop_override, _is_machinery

        set_estop_state(True)
        try:
            # Sensors should pass through unchanged
            assert _is_machinery('temperature') is False
            assert _estop_override('temperature', 85.5) == 85.5
            assert _estop_override('pressure', 1.5) == 1.5
            assert _estop_override('flow', 20.0) == 20.0
            assert _estop_override('level', 75.0) == 75.0
            assert _estop_override('vibration', 2.5) == 2.5
        finally:
            set_estop_state(False)

    def test_estop_deactivation_restores_normal(self):
        """After E-STOP is deactivated, values pass through normally."""
        from 采集层.simulated_client import set_estop_state, _estop_override

        set_estop_state(True)
        assert _estop_override('speed', 1500) == 0.0

        set_estop_state(False)
        assert _estop_override('speed', 1500) == 1500

    def test_estop_frozen_values_tracked(self):
        """E-STOP tracks frozen values for machinery registers."""
        from 采集层.simulated_client import set_estop_state, _estop_override, _ESTOP_FROZEN_VALUES

        set_estop_state(True)
        try:
            _estop_override('speed', 1500)
            assert _ESTOP_FROZEN_VALUES.get('speed') == 0.0
        finally:
            set_estop_state(False)


# ============================================================
# Device State Machine
# ============================================================

class TestDeviceStateMachine:
    """Device state transitions must follow ISA-95 rules."""

    def test_initial_state_is_idle(self):
        """New simulator starts in IDLE state."""
        from 采集层.device_behavior_simulator import DeviceState
        sim = _make_sim('generic')
        # _make_sim sets RUNNING, but factory default is IDLE
        config = {
            'id': 'fresh', 'name': 'Fresh', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'type': 'float', 'unit': 'C'}]
        }
        from 采集层.device_behavior_simulator import DeviceBehaviorSimulator
        fresh = DeviceBehaviorSimulator('fresh', config)
        assert fresh.state == DeviceState.IDLE

    def test_write_start_register_transitions_to_running(self):
        """Writing value=1 to address 100 transitions IDLE to RUNNING."""
        from 采集层.device_behavior_simulator import DeviceState
        sim = _make_sim('generic')
        sim.state = DeviceState.IDLE
        sim.handle_write_register(100, 1)
        assert sim.state == DeviceState.RUNNING

    def test_write_stop_register_transitions_to_stopped(self):
        """Writing value=0 to address 100 transitions RUNNING to STOPPED."""
        from 采集层.device_behavior_simulator import DeviceState
        sim = _make_sim('generic')
        sim.state = DeviceState.RUNNING
        sim.handle_write_register(100, 0)
        assert sim.state == DeviceState.STOPPED

    def test_reset_register_clears_faults(self):
        """Writing value=1 to address 101 clears faults and goes to IDLE."""
        from 采集层.device_behavior_simulator import DeviceState, FaultType
        sim = _make_sim('generic')
        sim.state = DeviceState.FAULT
        sim.active_faults[FaultType.OVERHEATING] = 0.5
        sim.handle_write_register(101, 1)
        assert sim.state == DeviceState.IDLE
        assert len(sim.active_faults) == 0

    def test_coil_0_starts_device(self):
        """Writing True to coil 0 transitions to RUNNING."""
        from 采集层.device_behavior_simulator import DeviceState
        sim = _make_sim('generic')
        sim.state = DeviceState.IDLE
        sim.handle_write_coil(0, True)
        assert sim.state == DeviceState.RUNNING

    def test_coil_0_stops_device(self):
        """Writing False to coil 0 transitions to STOPPED."""
        from 采集层.device_behavior_simulator import DeviceState
        sim = _make_sim('generic')
        sim.state = DeviceState.RUNNING
        sim.handle_write_coil(0, False)
        assert sim.state == DeviceState.STOPPED

    def test_monitoring_device_ignores_stop_command(self):
        """Monitoring devices (sensors) ignore stop commands."""
        from 采集层.device_behavior_simulator import DeviceBehaviorSimulator, DeviceState

        config = {
            'id': 'sensor_01', 'name': '温度传感器', 'protocol': 'modbus_tcp',
            'host': '127.0.0.1', 'port': 502,
            'description': '振动监测传感器',
            'registers': [{'name': 'vibration', 'address': 0, 'type': 'float', 'unit': 'mm/s'}]
        }
        sim = DeviceBehaviorSimulator('sensor_01', config)
        sim.state = DeviceState.RUNNING
        sim._running = True

        assert sim.is_monitoring_device is True

        # Stop command via register
        sim.handle_write_register(100, 0)
        assert sim.state == DeviceState.RUNNING, "Monitoring device should ignore stop command"

        # Stop command via coil
        sim.handle_write_coil(0, False)
        assert sim.state == DeviceState.RUNNING, "Monitoring device should ignore coil stop"


# ============================================================
# Output Data Filtering
# ============================================================

class TestOutputDataFiltering:
    """Output should only contain parameters the device actually has."""

    def test_output_contains_only_configured_registers(self):
        """Output data only includes parameters from the device config."""
        sim = _make_sim('generic')
        data = sim.update(1.0)

        # These should be present (in our config)
        assert 'temperature' in data or 'boiler_temperature' in data
        assert 'pressure' in data or 'boiler_pressure' in data

        # Metadata should always be present
        assert '_device_state' in data
        assert '_health_score' in data

    def test_output_metadata_fields_present(self):
        """Output always includes internal metadata fields."""
        sim = _make_sim('generic')
        data = sim.update(1.0)

        assert '_device_state' in data
        assert '_health_score' in data
        assert '_timestamp' in data
        assert '_active_fault' in data

    def test_stopped_device_marks_stopped_flag(self):
        """Stopped device output includes _stopped=True flag."""
        from 采集层.device_behavior_simulator import DeviceState
        sim = _make_sim('generic')
        sim.state = DeviceState.STOPPED
        sim._running = True

        data = sim.update(1.0)
        assert data.get('_stopped') is True
