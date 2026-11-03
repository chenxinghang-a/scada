# -*- coding: utf-8 -*-
"""round 352：四路并行审计后的第一批（返回契约 + 安全路径）。

共同判据：**「部分成功」在安全路径上等于失败，不能回 success=True。**
"""

from __future__ import annotations

import threading
from datetime import datetime
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# 1. insert_data_batch 必须报告"实际写入了几条"
# ---------------------------------------------------------------------------

def _db(tmp_path):
    from 存储层.database import Database
    return Database(str(tmp_path / 'scada.db'))


def test_insert_batch_returns_written_count(tmp_path):
    db = _db(tmp_path)
    rows = [{'device_id': 'd1', 'register_name': 't', 'value': 1.0,
             'timestamp': datetime.now()} for _ in range(3)]

    assert db.insert_data_batch(rows) == 3


def test_insert_batch_returns_zero_when_all_invalid(tmp_path):
    """整批缺字段时必须返回 0 —— 调用方据此保留磁盘副本。

    原来返回 None，而 `data_collector` 只靠 try/except 判成败：
    None 不抛异常 → 认定成功 → `clear_persistence()` 把磁盘上唯一一份
    崩溃保护删掉。内存已被消费线程取走、磁盘副本又被删 = 这批数据彻底消失。
    """
    db = _db(tmp_path)
    rows = [{'device_id': 'd1'}, {'device_id': 'd2'}]      # 缺 value/timestamp

    assert db.insert_data_batch(rows) == 0


def test_insert_batch_returns_zero_for_empty(tmp_path):
    db = _db(tmp_path)
    assert db.insert_data_batch([]) == 0


# ---------------------------------------------------------------------------
# 2. 急停：有设备没停下来就不能回 success
# ---------------------------------------------------------------------------

def _dc():
    from 智能层.device_control import DeviceControlSafety
    dc = DeviceControlSafety(MagicMock(), MagicMock(), MagicMock())
    return dc


def _mechanical(n=3):
    # 字段名是 `device_category`（见 interfaces.py:206），不是 `category`
    return {f'm{i}': {'id': f'm{i}', 'device_category': 'mechanical'} for i in range(n)}


def test_estop_partial_failure_is_not_success():
    dc = _dc()
    dc.device_manager.get_all_devices.return_value = _mechanical(3)
    dc.device_manager.stop_device.side_effect = [True, False, True]
    dc.alarm_manager = None

    result = dc.trigger_emergency_stop('test')

    assert result['success'] is False, '3 台只停了 2 台，接口不能回成功'
    assert result['failed_devices'] == ['m1']
    assert len(result['stopped_devices']) == 2


def test_estop_all_stopped_is_success():
    """反向对照：全部停下时必须是 success。"""
    dc = _dc()
    dc.device_manager.get_all_devices.return_value = _mechanical(2)
    dc.device_manager.stop_device.return_value = True
    dc.alarm_manager = None

    result = dc.trigger_emergency_stop('test')

    assert result['success'] is True
    assert result['failed_devices'] == []


# ---------------------------------------------------------------------------
# 3. 故障降级：handler 失败不许标记「已降级」
# ---------------------------------------------------------------------------

def test_degradation_not_marked_when_handler_fails():
    dc = _dc()
    dc._comm_threshold = 2
    dc.register_safe_state_handler('d1', MagicMock(side_effect=RuntimeError('valve jammed')))

    for _ in range(3):
        dc.update_device_health('d1', False)

    assert 'd1' not in dc._degraded_devices, (
        'handler 失败却标记了「已降级」—— 整个掉线期间不会再重试安全动作')


def test_degradation_marked_when_handler_ok():
    dc = _dc()
    dc._comm_threshold = 2
    handler = MagicMock()
    dc.register_safe_state_handler('d1', handler)

    dc.update_device_health('d1', False)
    dc.update_device_health('d1', False)

    assert 'd1' in dc._degraded_devices
    handler.assert_called_once()


# ---------------------------------------------------------------------------
# 4. 手动模式下报警未驱动声光 → 必须返回 False
# ---------------------------------------------------------------------------

def test_trigger_alarm_returns_false_in_manual_mode():
    from 报警层.alarm_output import AlarmOutput

    ao = AlarmOutput.__new__(AlarmOutput)
    ao._lock = threading.RLock()
    ao._io_lock = threading.RLock()
    ao._fail_streak = 0
    ao._circuit_open_until = None
    ao.simulation = True
    ao.enabled = True
    ao.do_red, ao.do_yellow, ao.do_green, ao.do_buzzer = 0, 1, 2, 3
    ao.current_state = {'level': 'manual', 'pattern': None, 'acknowledged': False,
                        'buzzer': False, 'red': False, 'yellow': False,
                        'green': True, 'since': None, 'message': ''}
    ao._buzzer_running = False
    ao._buzzer_thread = None
    ao._flash_running = False
    ao._flash_thread = None
    ao.config = {'modbus': {}}

    assert ao.trigger_alarm('critical', '超温') is False, (
        '手动模式下报警没驱动声光，却回了"成功"—— 上游不会记失败、'
        '通道故障报警永不触发')


# ---------------------------------------------------------------------------
# 5. 配置解析失败不许降级成空配置
# ---------------------------------------------------------------------------

def test_config_parse_error_raises(tmp_path, monkeypatch):
    from core import config_manager as cm

    bad = tmp_path / 'system.yaml'
    bad.write_text('a: [unclosed\n', encoding='utf-8')   # 语法错

    with pytest.raises(ValueError):
        cm.ConfigManager.load_yaml(str(bad), reload=True)


def test_config_missing_file_returns_empty(tmp_path):
    """反向对照：文件不存在是正常情况，仍返回空配置。"""
    from core import config_manager as cm

    assert cm.ConfigManager.load_yaml(str(tmp_path / 'nope.yaml'), reload=True) == {}
