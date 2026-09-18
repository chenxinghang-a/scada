# -*- coding: utf-8 -*-
"""时间戳解析失败不能伪造（round 168g）。

背景
----
`智能层/tsdb_adapter.py::_feed_to_modules()` 把 TDengine 查出的历史数据喂给
SPC 控制图和预测性维护。原实现里时间戳解析失败会**静默用 `datetime.now()` 顶替**：

    try:
        timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
    except Exception:
        timestamp = datetime.now()        # ← 历史数据被改写成「现在」

后果（且不会报任何错）：
- 几天前的采样被当成刚发生 → SPC 控制图的时间轴错乱、基线漂移
- 预测性维护的时序错位 → 趋势判断失真，可能误报或漏报设备故障

修复：解析失败 → 记 warning + **跳过该条**。宁可丢一条，也不污染模型时间轴。
"""

import logging
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def adapter_and_pm():
    from 智能层.tsdb_adapter import TSDBAdapter

    tdengine = MagicMock()
    pm = MagicMock()
    adapter = TSDBAdapter(tdengine, predictive_maintenance=pm)
    return adapter, pm


def test_bad_timestamp_record_is_skipped(adapter_and_pm, caplog):
    """**核心回归**：坏时间戳的记录必须被跳过，不能喂给下游。"""
    adapter, pm = adapter_and_pm

    with caplog.at_level(logging.WARNING):
        adapter._feed_to_modules('dev1', 'temperature', [
            {'value': 1.0, 'timestamp': '2026-09-18T10:00:00'},   # 正常
            {'value': 2.0, 'timestamp': '完全不是时间戳'},           # 坏
        ])

    calls = pm.feed_data.call_args_list
    assert len(calls) == 1, \
        f'坏时间戳的记录必须被跳过，实际喂了 {len(calls)} 条 —— 说明它被伪造时间后放行了'
    assert calls[0][0][2] == 1.0, f'喂进去的应该是正常那条，实际 {calls[0][0]}'
    assert any('时间戳无法解析' in rec.message for rec in caplog.records), \
        '跳过时没有落日志'


def test_good_timestamp_keeps_real_time(adapter_and_pm):
    """正常路径不受影响：时间戳必须原样传下去，不能被改成 now()。"""
    from datetime import datetime

    adapter, pm = adapter_and_pm
    adapter._feed_to_modules('dev1', 'temperature', [
        {'value': 42.5, 'timestamp': '2026-01-02T03:04:05'},
    ])

    calls = pm.feed_data.call_args_list
    assert len(calls) == 1
    device_id, register, value, ts = calls[0][0]
    assert (device_id, register, value) == ('dev1', 'temperature', 42.5)
    assert ts == datetime(2026, 1, 2, 3, 4, 5), f'时间戳被改写了: {ts}'


def test_z_suffix_timestamp_parsed(adapter_and_pm):
    """带 Z 后缀的 UTC 时间戳要能解析（不能因为解析失败被误跳）。"""
    adapter, pm = adapter_and_pm
    adapter._feed_to_modules('dev1', 'temperature', [
        {'value': 7.0, 'timestamp': '2026-01-02T03:04:05Z'},
    ])
    assert len(pm.feed_data.call_args_list) == 1


def test_none_value_still_skipped(adapter_and_pm):
    """value 为 None 的记录照旧跳过（原有行为，别被这次改动破坏）。"""
    adapter, pm = adapter_and_pm
    adapter._feed_to_modules('dev1', 'temperature', [
        {'value': None, 'timestamp': '2026-01-02T03:04:05'},
    ])
    assert pm.feed_data.call_count == 0
