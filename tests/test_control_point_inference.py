# -*- coding: utf-8 -*-
"""控制点位解析：现场设备是人自己接线、自己命名的，不能要求每台额外写 control 段。

这里的判据是**不许写死点位**：能推断就用设备自己的点位，推不出且在真实模式下
必须明确拒绝，绝不能退回 coil0（那是"所有设备起停都打到同一个线圈"）。
"""

import pytest


def _make_dc(devices, simulation=False):
    from unittest.mock import MagicMock
    from 智能层.device_control import DeviceControlSafety

    dm = MagicMock()
    dm.devices = devices
    dm.simulation_mode = simulation
    client = MagicMock()
    client.connected = True
    client.write_single_coil = MagicMock(return_value=True)
    client.write_single_register = MagicMock(return_value=True)
    dm.get_client.return_value = client
    dc = DeviceControlSafety(MagicMock(), device_manager=dm)
    return dc, client


PRESS = {
    'id': 'press_01',
    'name': '液压机',
    'protocol': 'modbus_tcp',
    'device_category': 'mechanical',
    'registers': [
        {'name': 'motor_start', 'address': 10, 'access': 'rw', 'data_type': 'bool'},
        {'name': 'motor_stop', 'address': 11, 'access': 'rw', 'data_type': 'bool'},
        {'name': 'oil_temperature', 'address': 20, 'access': 'r', 'data_type': 'float32'},
        # 可写但是工艺量 —— 绝不能被当成启停点
        {'name': 'speed_setpoint', 'address': 21, 'access': 'rw', 'data_type': 'uint16'},
        {'name': 'position_feedback', 'address': 22, 'access': 'r', 'data_type': 'int32'},
    ],
}


class TestInferFromExistingPoints:
    """不写 control 段，也能从设备自己的点位里找到启停点。"""

    def test_start_inferred_from_register_name(self):
        dc, _ = _make_dc({'press_01': PRESS})
        point = dc._resolve_control_point('press_01', 'start')
        assert point is not None, '有 motor_start 可写点位却推不出启停点'
        assert point['type'] == 'coil'
        assert point['address'] == 10
        assert point['value'] is True

    def test_stop_inferred_same_device(self):
        dc, _ = _make_dc({'press_01': PRESS})
        point = dc._resolve_control_point('press_01', 'stop')
        assert point is not None
        assert point['address'] == 11
        assert point['value'] is False

    def test_process_values_are_not_control_points(self):
        """speed_setpoint 可写且含 setpoint，但不是启停动作。"""
        dc, _ = _make_dc({'press_01': PRESS})
        assert dc._infer_control_point(PRESS, 'start')['address'] == 10
        # 若推断器把 speed_setpoint 当启停点，这里会拿到 21
        assert dc._infer_control_point(PRESS, 'start')['address'] != 21

    def test_readonly_points_ignored(self):
        """access: r 的点位不能当控制点（推不出来就是 None，而不是硬用）。"""
        dev = {
            'protocol': 'modbus_tcp',
            'device_category': 'mechanical',
            'registers': [
                {'name': 'motor_start', 'address': 10, 'access': 'r', 'data_type': 'bool'},
            ],
        }
        dc, _ = _make_dc({'x': dev})
        assert dc._infer_control_point(dev, 'start') is None

    def test_explicit_control_wins_over_inference(self):
        """显式声明优先：现场改过线，用户明确指定了就按他说的写。"""
        dev = dict(PRESS)
        dev['control'] = {'start': {'type': 'register', 'address': 99, 'value': 1}}
        dc, _ = _make_dc({'press_01': dev})
        point = dc._resolve_control_point('press_01', 'start')
        assert point['type'] == 'register'
        assert point['address'] == 99
        assert point['value'] == 1


class TestNoHardCodedFallbackInRealMode:
    """真实模式下推不出点位必须拒绝，绝不写 coil0。"""

    def test_real_mode_refuses_when_no_point(self):
        dev = {
            'protocol': 'mqtt',
            'device_category': 'mechanical',
            'topics': [{'name': 'vibration_x', 'topic': 'factory/vib/x'}],
        }
        dc, _ = _make_dc({'mqtt_dev': dev}, simulation=False)
        assert dc._resolve_control_point('mqtt_dev', 'start') is None

    def test_simulation_still_falls_back_with_warning(self, caplog):
        """模拟模式允许回退（无物理风险），但必须留可见告警。"""
        dev = {'protocol': 'modbus_tcp', 'device_category': 'mechanical', 'registers': []}
        dc, _ = _make_dc({'sim_dev': dev}, simulation=True)
        point = dc._resolve_control_point('sim_dev', 'start')
        assert point is not None
        assert point['address'] == 0  # LEGACY_FALLBACK：仅模拟模式
        assert any('回退点位' in r.message or '回退' in r.message
                   for r in caplog.records), '回退到默认点位却没有告警'
