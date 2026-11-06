# -*- coding: utf-8 -*-
"""控制自主等级：HITL / HOTL / HOOTL（round 378）。

* HITL（manual）  —— 自动动作变成**待审批提案**，人必须批准/改值/驳回；
* HOTL（supervised）—— 自动动作可执行，但必须先由人武装（可随时解除）；
* HOOTL（automatic）—— 全自动，无人值守。
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from 智能层.device_control import DeviceControlSafety


def _dc(devices):
    dm = MagicMock()
    dm.devices = devices
    dm.simulation_mode = True          # 跳过“零联锁 fail-closed”，专测自主等级
    client = MagicMock()
    client.connected = True
    client.write_single_register = MagicMock(return_value=True)
    client.write_single_coil = MagicMock(return_value=True)
    client.read_holding_registers = MagicMock(return_value=[7])
    dm.get_client.return_value = client
    return DeviceControlSafety(MagicMock(), device_manager=dm), client


class TestHITL:
    def test_auto_write_becomes_pending(self):
        dc, client = _dc({'d1': {'autonomy': 'manual'}})

        result = dc.write_with_verification('d1', 10, 7, 'edge_decision',
                                            origin='auto')

        assert result['success'] is False
        assert result['pending_approval'] is True
        assert result['action_id']
        client.write_single_register.assert_not_called()

        actions = dc.get_pending_actions()
        assert len(actions) == 1
        assert actions[0]['device_id'] == 'd1'
        assert actions[0]['value'] == 7
        assert actions[0]['source'] == 'edge_decision'

    def test_human_write_is_not_blocked(self):
        """HITL 只约束自动来源；人直接下发就是“人的决策”。"""
        dc, client = _dc({'d1': {'autonomy': 'manual'}})

        result = dc.write_with_verification('d1', 10, 7, 'operator1')

        assert result['success'] is True
        client.write_single_register.assert_called_once_with(10, 7)

    def test_approve_executes(self):
        dc, client = _dc({'d1': {'autonomy': 'manual'}})
        pending = dc.write_with_verification('d1', 10, 7, 'edge_decision',
                                             origin='auto')

        handled, result = dc.approve_pending(pending['action_id'], 'operator1')

        assert handled is True
        assert result['success'] is True
        client.write_single_register.assert_called_once_with(10, 7)
        assert dc.get_pending_actions() == []

    def test_approve_with_modified_value(self):
        """人可以在批准时改值 —— “修改”是 HITL 的组成部分。"""
        dc, client = _dc({'d1': {'autonomy': 'manual'}})
        pending = dc.write_with_verification('d1', 10, 7, 'edge_decision',
                                             origin='auto')
        client.read_holding_registers.return_value = [9]

        handled, result = dc.approve_pending(pending['action_id'], 'operator1',
                                             value=9)

        assert handled is True
        assert result['success'] is True
        client.write_single_register.assert_called_once_with(10, 9)

    def test_reject_does_not_write(self):
        dc, client = _dc({'d1': {'autonomy': 'manual'}})
        pending = dc.write_with_verification('d1', 10, 7, 'edge_decision',
                                             origin='auto')

        assert dc.reject_pending(pending['action_id'], 'operator1', '不需要') is True

        client.write_single_register.assert_not_called()
        assert dc.get_pending_actions() == []

    def test_duplicate_proposals_are_merged(self):
        """PID 每周期都提案：同目标必须合并成一条，值取最新。"""
        dc, _ = _dc({'d1': {'autonomy': 'manual'}})

        first = dc.write_with_verification('d1', 10, 7, 'edge_decision',
                                           origin='auto')
        second = dc.write_with_verification('d1', 10, 8, 'edge_decision',
                                            origin='auto')

        assert first['action_id'] == second['action_id']
        actions = dc.get_pending_actions()
        assert len(actions) == 1
        assert actions[0]['value'] == 8
        assert actions[0]['updates'] == 2

    def test_expired_proposal_is_dropped(self):
        dc, _ = _dc({'d1': {'autonomy': 'manual'}})
        pending = dc.write_with_verification('d1', 10, 7, 'edge_decision',
                                             origin='auto')
        dc._pending_actions[pending['action_id']].expires_mono = time.monotonic() - 1

        assert dc.get_pending_actions() == []


class TestHOTL:
    def test_unarmed_supervised_becomes_pending(self):
        dc, client = _dc({'d1': {'autonomy': 'supervised'}})

        result = dc.write_with_verification('d1', 10, 7, 'edge_decision',
                                            origin='auto')

        assert result['pending_approval'] is True
        client.write_single_register.assert_not_called()

    def test_armed_supervised_executes(self):
        dc, client = _dc({'d1': {'autonomy': 'supervised'}})
        ok, _ = dc.arm_autonomy('d1', 'supervisor', 10)
        assert ok is True

        result = dc.write_with_verification('d1', 10, 7, 'edge_decision',
                                            origin='auto')

        assert result['success'] is True
        client.write_single_register.assert_called_once_with(10, 7)
        status = dc.get_autonomy('d1')
        assert status['armed'] is True
        assert status['armed_by'] == 'supervisor'

    def test_disarm_takes_control_back(self):
        dc, client = _dc({'d1': {'autonomy': 'supervised'}})
        dc.arm_autonomy('d1', 'supervisor', 10)
        dc.disarm_autonomy('d1', 'supervisor')

        result = dc.write_with_verification('d1', 10, 7, 'edge_decision',
                                            origin='auto')

        assert result['pending_approval'] is True
        client.write_single_register.assert_not_called()

    def test_arm_rejects_invalid_minutes(self):
        dc, _ = _dc({'d1': {'autonomy': 'supervised'}})
        assert dc.arm_autonomy('d1', 'op', 0)[0] is False
        assert dc.arm_autonomy('d1', 'op', True)[0] is False
        assert dc.arm_autonomy('d1', 'op', 999)[0] is False

    def test_arm_only_for_supervised(self):
        dc, _ = _dc({'d1': {'autonomy': 'automatic'}})
        assert dc.arm_autonomy('d1', 'op', 10)[0] is False


class TestHOOTL:
    def test_automatic_executes_without_arming(self):
        dc, client = _dc({'d1': {'autonomy': 'automatic'}})

        result = dc.write_with_verification('d1', 10, 7, 'edge_decision',
                                            origin='auto')

        assert result['success'] is True
        client.write_single_register.assert_called_once_with(10, 7)

    def test_default_is_automatic_for_backward_compat(self):
        dc, _ = _dc({'d1': {}})
        assert dc.get_autonomy('d1')['mode'] == 'automatic'


class TestModeSwitch:
    def test_set_mode_and_override(self):
        dc, _ = _dc({'d1': {'autonomy': 'manual'}})
        ok, mode = dc.set_autonomy_mode('d1', 'automatic', 'engineer', '夜间无人')
        assert ok is True and mode == 'automatic'
        assert dc.get_autonomy('d1')['mode'] == 'automatic'

    def test_invalid_mode_rejected(self):
        dc, _ = _dc({'d1': {}})
        assert dc.set_autonomy_mode('d1', 'robot', 'engineer')[0] is False

    def test_switching_away_from_supervised_revokes_arming(self):
        dc, _ = _dc({'d1': {'autonomy': 'supervised'}})
        dc.arm_autonomy('d1', 'supervisor', 10)
        dc.set_autonomy_mode('d1', 'manual', 'engineer')
        assert dc.get_autonomy('d1')['armed'] is False
