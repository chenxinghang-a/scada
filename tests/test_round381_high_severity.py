# -*- coding: utf-8 -*-
"""round 381：全量分片扫描确认的高严重度缺陷回归。

只覆盖**已核验为真**且能稳定单测的条目；误报（OPC UA 自死锁、BADC 64 位、
WS rooms 结构）不在本文件里重复。
"""
from __future__ import annotations

import struct
import time

import pytest


class TestDataCollectorSpans:
    @pytest.mark.parametrize('data_type, span', [
        ('float64', 4), ('float32', 2), ('int32', 2), ('uint32', 2),
        ('uint16', 1), ('int16', 1), ('bool', 1),
    ])
    def test_register_span(self, data_type, span):
        from 采集层.data_collector import _register_span
        assert _register_span(data_type) == span


class TestMcProtocol:
    def _client(self):
        from 采集层.mc_client import MCClient
        return MCClient({'id': 'mc1', 'host': '127.0.0.1', 'port': 5000})

    def test_request_length_includes_timer(self):
        """请求数据长 = 监视定时器(2)+命令(2)+子命令(2)+数据(N)。"""
        client = self._client()
        frame = client._build_3e_frame(0x0401, 0x0000, b'1234')
        # 9 字节头 + 2 字节监视定时器 + 2 命令 + 2 子命令 + 4 数据
        assert len(frame) == 19
        assert struct.unpack('<H', frame[7:9])[0] == 10

    def test_device_spec_address_before_code(self):
        """3E 数据区顺序：地址 3 字节（低位在前）→ 设备代码 1 字节。"""
        client = self._client()
        assert client._pack_device_spec(0xA8, 0x123456) == b'\x56\x34\x12\xa8'


class TestRecipeTimebase:
    def test_update_does_not_skip_phase(self):
        """phase_start_time 是 monotonic，elapsed 也必须用 monotonic。"""
        from 采集层.recipe_simulator import PhaseConfig, Recipe, RecipeSimulator

        recipe = Recipe(name='r', version='1', phases=[
            PhaseConfig(name='p1', duration=60, setpoints={'temperature': 50}),
            PhaseConfig(name='p2', duration=60, setpoints={'temperature': 60}),
        ])
        sim = RecipeSimulator(recipe)
        sim.start()

        sim.update(0.1)

        assert sim.current_phase_index == 0, '配方被瞬间推进到下一阶段'
        assert sim.is_running is True


class TestBehaviorIdle:
    def test_idle_after_none_does_not_crash(self):
        """非 IDLE 会把 _idle_since 置 None，再回 IDLE 不能 None 相减。"""
        from 采集层.device_behavior_simulator import (
            DeviceBehaviorSimulator, DeviceState,
        )

        sim = DeviceBehaviorSimulator('d1', {'name': 'd1', 'registers': []})
        sim.state = DeviceState.IDLE
        sim._idle_since = None

        sim.update(0.1)      # 修复前这里 TypeError

        assert sim._idle_since is not None


class TestAlarmEscalationInterval:
    @pytest.mark.parametrize('value', [0, -1, 'bad'])
    def test_invalid_check_interval_rejected(self, value):
        from 报警层.alarm_escalation import AlarmEscalationManager

        with pytest.raises(ValueError):
            AlarmEscalationManager({'check_interval': value})


class TestArchiveDays:
    @pytest.mark.parametrize('archive, delete', [
        (0, 30), (-1, 30), (7, 0), (7, -5),
    ])
    def test_non_positive_days_rejected(self, archive, delete):
        from 存储层.database import Database

        # 校验发生在任何 self 属性访问之前，用 __new__ 即可
        db = Database.__new__(Database)
        with pytest.raises(ValueError, match='天'):
            db.archive_old_data(archive_days=archive, delete_days=delete)
