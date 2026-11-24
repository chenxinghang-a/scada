# -*- coding: utf-8 -*-
"""round 395（Q7）：仿真设备被 STOP / ESTOP 后的停机锁存。

## 背景

状态机里 IDLE 超过 5 秒自动回切 RUNNING（模拟"自动调度"）。演示时操作员
刚点停止，5 秒后设备又自己转起来 —— 观感是"停止没生效"，值班员分不清
"指令没生效"和"仿真自动复位"。

## 方案 A（架构评审已拍板）

* 每台设备一个 ``external_stopped`` 锁存位；
* 收到 STOP / ESTOP：置位并切入 IDLE；
* 步进函数里只有 ``not external_stopped`` 才执行 5 秒自动回切；
* 只有显式 START 才复位锁存；
* 只对 mechanical 生效，instrument/safety 的启停语义不变。

## 验收判据（本文件的断言）

对运行中的 mechanical 设备下发 STOP 或 ESTOP 后，连续观测 30 秒以上
状态**始终** IDLE 且不自启；随后下发 START 能恢复 RUNNING。
用假时钟推进，不真的 sleep 30 秒。
"""

from __future__ import annotations

import pytest


class FakeClock:
    """可推进的假时钟，注入到 ``sim._clock``。"""

    def __init__(self, start: float = 1_000_000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


MECHANICAL_CONFIG = {
    'id': 'm_press_01',
    'name': '液压冲压机',
    'device_category': 'mechanical',
    'description': '机械类生产装备',
    'registers': [
        {'name': 'motor_speed', 'address': 0, 'type': 'float', 'unit': 'RPM'},
        {'name': 'spindle_position', 'address': 2, 'type': 'float', 'unit': 'mm'},
        {'name': 'temperature', 'address': 4, 'type': 'float', 'unit': 'C'},
    ],
}

SENSOR_CONFIG = {
    'id': 'i_vib_01',
    'name': '振动监测传感器',
    'device_category': 'instrument',
    'description': '振动监测传感器',
    'registers': [
        {'name': 'vibration', 'address': 0, 'type': 'float', 'unit': 'mm/s'},
    ],
}

SAFETY_CONFIG = {
    'id': 's_tower_01',
    'name': 'signal_tower 信号灯塔',
    'device_category': 'safety',
    'description': '声光报警',
    'registers': [
        {'name': 'light_state', 'address': 0, 'type': 'bool', 'unit': ''},
    ],
}


@pytest.fixture(autouse=True)
def _clean_global_stop_flags():
    """``_ESTOP_ACTIVE`` / ``_DEVICE_STOPPED`` 都是模块级全局，前后都复位。"""
    from 采集层.simulated_client import set_device_stopped, set_estop_state

    set_estop_state(False)
    for cfg in (MECHANICAL_CONFIG, SENSOR_CONFIG, SAFETY_CONFIG):
        set_device_stopped(cfg['id'], False)
    yield
    set_estop_state(False)
    for cfg in (MECHANICAL_CONFIG, SENSOR_CONFIG, SAFETY_CONFIG):
        set_device_stopped(cfg['id'], False)


def _make_sim(config: dict, clock: FakeClock):
    """构造一台处于 RUNNING 的模拟设备，并注入假时钟。"""
    from 采集层.device_behavior_simulator import DeviceBehaviorSimulator, DeviceState

    sim = DeviceBehaviorSimulator(config['id'], dict(config))
    sim._clock = clock
    sim._running = True
    sim.state = DeviceState.RUNNING
    return sim


def _tick(sim, clock: FakeClock, seconds: float, step: float = 1.0) -> None:
    """按 ``step`` 秒步进，累计推进 ``seconds`` 秒。"""
    from 采集层.device_behavior_simulator import DeviceState

    elapsed = 0.0
    while elapsed < seconds:
        clock.advance(step)
        sim.update(step)
        elapsed += step
        assert sim.state == DeviceState.IDLE, (
            f'停机锁存失效：第 {elapsed:.0f} 秒状态变成 {sim.state.name}')


class TestMechanicalStaysStopped:
    """核心验收：STOP / ESTOP 后 30 秒以上始终 IDLE，START 才恢复。"""

    def test_stop_register_stays_idle_for_35s(self):
        from 采集层.device_behavior_simulator import DeviceState

        clock = FakeClock()
        sim = _make_sim(MECHANICAL_CONFIG, clock)
        assert sim.is_mechanical_device is True

        sim.handle_write_register(100, 0)
        assert sim.state == DeviceState.IDLE
        assert sim.external_stopped is True, 'STOP 未置停机锁存位'

        _tick(sim, clock, 35.0)   # 判据要求 ≥30 秒

    def test_stop_coil_stays_idle_for_35s(self):
        from 采集层.device_behavior_simulator import DeviceState

        clock = FakeClock()
        sim = _make_sim(MECHANICAL_CONFIG, clock)

        sim.handle_write_coil(0, False)
        assert sim.state == DeviceState.IDLE
        assert sim.external_stopped is True, '线圈 STOP 未置停机锁存位'

        _tick(sim, clock, 35.0)

    def test_start_register_recovers_running_after_long_stop(self):
        from 采集层.device_behavior_simulator import DeviceState

        clock = FakeClock()
        sim = _make_sim(MECHANICAL_CONFIG, clock)

        sim.handle_write_register(100, 0)
        _tick(sim, clock, 35.0)

        sim.handle_write_register(100, 1)
        assert sim.state == DeviceState.RUNNING, 'START 未恢复运行'
        assert sim.external_stopped is False, 'START 未复位停机锁存位'

        # 恢复后不能被误判成"还在锁存"
        clock.advance(10.0)
        sim.update(1.0)
        assert sim.state == DeviceState.RUNNING

    def test_start_coil_recovers_running_after_long_stop(self):
        from 采集层.device_behavior_simulator import DeviceState

        clock = FakeClock()
        sim = _make_sim(MECHANICAL_CONFIG, clock)

        sim.handle_write_coil(0, False)
        _tick(sim, clock, 35.0)

        sim.handle_write_coil(0, True)
        assert sim.state == DeviceState.RUNNING
        assert sim.external_stopped is False


class TestEstopLatch:
    """急停分支也必须置位：否则"急停后 5 秒复活"。"""

    def test_estop_keeps_idle_for_35s(self):
        from 采集层.device_behavior_simulator import DeviceState
        from 采集层.simulated_client import set_estop_state

        clock = FakeClock()
        sim = _make_sim(MECHANICAL_CONFIG, clock)

        set_estop_state(True)
        clock.advance(1.0)
        sim.update(1.0)
        assert sim.state == DeviceState.IDLE
        assert sim.external_stopped is True, '急停未置停机锁存位'

        _tick(sim, clock, 35.0)

    def test_estop_release_without_start_does_not_auto_resume(self):
        """解除 E-STOP 本身不是 START —— 设备应保持在 IDLE 等人工启动。"""
        from 采集层.device_behavior_simulator import DeviceState
        from 采集层.simulated_client import set_estop_state

        clock = FakeClock()
        sim = _make_sim(MECHANICAL_CONFIG, clock)

        set_estop_state(True)
        clock.advance(1.0)
        sim.update(1.0)

        set_estop_state(False)
        _tick(sim, clock, 30.0)
        assert sim.external_stopped is True, '仅解除急停就清掉了锁存位'

        # 显式 START 才能恢复
        sim.handle_write_register(100, 1)
        assert sim.state == DeviceState.RUNNING
        assert sim.external_stopped is False

    def test_estop_stops_machinery_only_idle_state(self):
        """急停只作用于 mechanical，instrument/safety 不参与。"""
        from 采集层.device_behavior_simulator import DeviceState
        from 采集层.simulated_client import set_estop_state

        clock = FakeClock()
        set_estop_state(True)
        try:
            for cfg in (SENSOR_CONFIG, SAFETY_CONFIG):
                sim = _make_sim(cfg, clock)
                assert sim.is_mechanical_device is False, cfg['name']
                clock.advance(1.0)
                sim.update(1.0)
                assert sim.state == DeviceState.RUNNING, (
                    f'{cfg["name"]} 不应被急停改状态')
                assert sim.external_stopped is False, (
                    f'{cfg["name"]} 不应被置停机锁存位')
                for _ in range(10):
                    clock.advance(1.0)
                    sim.update(1.0)
                    assert sim.state == DeviceState.RUNNING
        finally:
            set_estop_state(False)


class TestGlobalStopFlag:
    """模拟层整机停机标志（device_manager.stop_device 的入口）也要收敛。"""

    def test_device_stopped_flag_latches(self):
        from 采集层.device_behavior_simulator import DeviceState
        from 采集层.simulated_client import set_device_stopped

        clock = FakeClock()
        sim = _make_sim(MECHANICAL_CONFIG, clock)

        set_device_stopped(MECHANICAL_CONFIG['id'], True)
        clock.advance(1.0)
        sim.update(1.0)
        assert sim.state == DeviceState.IDLE
        assert sim.external_stopped is True

        _tick(sim, clock, 35.0)

    def test_explicit_manager_start_clears_latch(self):
        """`set_device_stopped(False)` 只来自显式 start_device，必须能再启。"""
        from 采集层.device_behavior_simulator import DeviceState
        from 采集层.simulated_client import set_device_stopped

        clock = FakeClock()
        sim = _make_sim(MECHANICAL_CONFIG, clock)
        device_id = MECHANICAL_CONFIG['id']

        set_device_stopped(device_id, True)
        clock.advance(1.0)
        sim.update(1.0)
        assert sim.external_stopped is True

        set_device_stopped(device_id, False)
        clock.advance(1.0)
        sim.update(1.0)
        assert sim.external_stopped is False, '显式启动后锁存位未复位'
        assert sim.state == DeviceState.RUNNING, '人工停止后彻底无法再启'


class TestAutoSchedulingPreserved:
    """锁存只"挡住"自动调度，不能把原有自动调度整体删掉。"""

    def test_idle_without_latch_still_auto_starts_after_5s(self):
        from 采集层.device_behavior_simulator import DeviceState

        clock = FakeClock()
        sim = _make_sim(MECHANICAL_CONFIG, clock)
        sim.state = DeviceState.IDLE
        sim._idle_since = None

        clock.advance(1.0)
        sim.update(1.0)
        assert sim.state == DeviceState.IDLE, '第一步只应记录 idle 起点'

        clock.advance(6.0)
        sim.update(1.0)
        assert sim.state == DeviceState.RUNNING, '无锁存时 5 秒自动回切被误删'
