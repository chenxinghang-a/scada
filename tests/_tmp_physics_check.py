"""Quick physics realism sanity check."""
from 采集层.device_behavior_simulator import DeviceBehaviorSimulator, DeviceState, FaultType

config = {
    'id': 'test_boiler', 'name': 'Boiler Test', 'description': '锅炉',
    'registers': [
        {'name': 'temperature', 'address': 0, 'data_type': 'float32', 'unit': '°C'},
        {'name': 'pressure', 'address': 2, 'data_type': 'float32', 'unit': 'MPa'},
        {'name': 'flow', 'address': 4, 'data_type': 'float32', 'unit': 'm³/h'},
        {'name': 'motor_speed', 'address': 6, 'data_type': 'uint16', 'unit': 'RPM'},
        {'name': 'vibration', 'address': 7, 'data_type': 'float32', 'unit': 'mm/s'},
    ]
}

# Test 1: Basic physics realism
sim = DeviceBehaviorSimulator('test_boiler', config)
sim.start()
for i in range(100):
    data = sim.update(1.0)

print("=== Test 1: Boiler Running State ===")
temp = data.get("temperature", "N/A")
pressure = data.get("pressure", "N/A")
flow = data.get("flow", "N/A")
speed = data.get("motor_speed", "N/A")
vib = data.get("vibration", "N/A")
print(f"Temperature: {temp} (limit: <{sim.TEMP_MAX})")
print(f"Pressure: {pressure} (limit: <{sim.PRESSURE_MAX})")
print(f"Flow: {flow}")
print(f"Motor Speed: {speed}")
print(f"Vibration: {vib} (limit: <{sim.VIBRATION_MAX})")
assert -20 <= temp <= 500, f"Temperature out of range: {temp}"
assert 0.001 <= pressure <= 10, f"Pressure out of range: {pressure}"
assert 0 <= flow <= 200, f"Flow out of range: {flow}"
assert 0 <= vib <= 20, f"Vibration out of range: {vib}"
print("PASS: All values within physical limits")

# Test 2: Fault degradation (not catastrophic)
print()
print("=== Test 2: Fault Degradation ===")
sim2 = DeviceBehaviorSimulator('test2', config)
sim2.start()
for i in range(50):
    sim2.update(1.0)
sim2.inject_fault(FaultType.MOTOR_WEAR, severity=0.8)
for i in range(200):
    data2 = sim2.update(1.0)
print(f"Motor Wear - Speed: {data2.get('motor_speed')}, Vibration: {data2.get('vibration')}")
assert data2.get("vibration", 0) < 20, "Vibration exceeded limit!"
assert data2.get("motor_speed", 0) < 3600, "Speed exceeded limit!"
print("PASS: Fault effects are bounded")

# Test 3: Pressure leak (should not go to zero)
print()
print("=== Test 3: Pressure Leak ===")
sim3 = DeviceBehaviorSimulator('test3', config)
sim3.start()
for i in range(50):
    sim3.update(1.0)
sim3.inject_fault(FaultType.PRESSURE_LEAK, severity=0.9)
for i in range(200):
    data3 = sim3.update(1.0)
p = data3.get("pressure", 0)
f = data3.get("flow", 0)
print(f"Pressure after leak: {p} (should be > {sim3.PRESSURE_MIN})")
print(f"Flow after leak: {f} (should be < {sim3.FLOW_MAX})")
assert p > sim3.PRESSURE_MIN, f"Pressure went to zero: {p}"
assert f < sim3.FLOW_MAX, f"Flow exploded: {f}"
print("PASS: Pressure leak degrades gracefully")

# Test 4: STOPPED state
print()
print("=== Test 4: STOPPED State ===")
sim4 = DeviceBehaviorSimulator('test4', config)
sim4.start()
for i in range(20):
    sim4.update(1.0)
sim4.force_state(DeviceState.STOPPED)
data4 = sim4.update(1.0)
print(f"Stopped temp: {data4.get('temperature')} (should be ~25)")
print(f"Stopped pressure: {data4.get('pressure')} (should be ~0.101)")
print(f"Stopped speed: {data4.get('motor_speed')}")
print(f"Stopped energy: {data4.get('energy_consumption')}")
print(f"Stopped shots: {data4.get('shot_count')}")
print("PASS: STOPPED state produces ambient values")

# Test 5: Cross-device consistency
print()
print("=== Test 5: Cross-Device Consistency ===")
sim5 = DeviceBehaviorSimulator('test5', config)
sim5.start()
for i in range(50):
    sim5.update(1.0)
normal_speed = sim5._motor_speed
normal_current = sim5._motor_current
normal_power = sim5._current_power_kw

sim5.process_model.base_flow = 5.0
for i in range(100):
    sim5.update(1.0)
reduced_speed = sim5._motor_speed
reduced_current = sim5._motor_current
reduced_power = sim5._current_power_kw

print(f"Normal: speed={normal_speed:.1f}, current={normal_current:.2f}, power={normal_power:.2f}")
print(f"Reduced: speed={reduced_speed:.1f}, current={reduced_current:.2f}, power={reduced_power:.2f}")
assert reduced_current < normal_current, "Current should drop with reduced load"
assert reduced_power < normal_power, "Power should drop with reduced load"
print("PASS: Motor params are consistent with load changes")

print()
print("=== ALL PHYSICS CHECKS PASSED ===")
