# -*- coding: utf-8 -*-
"""采集退避 / 熔断 / 卡死检测的时钟与下限（round 264）。

两个独立缺陷
------------
**1) "退避"第 1 次反而更激进**

    原实现：`interval = min(2 ** failures, BACKOFF_CEILING_S)`

第 1 次失败 → 2 秒，而正常采集间隔是 5 秒 —— 一台已经出问题的设备被
**更频繁**地打（每秒多打 1.5 次），既加重设备/网关负担，也让日志与指标更吵。
"退避"必须满足：**不小于基础间隔**，再指数增长。

**2) 熔断冷却窗口用墙钟**

    elapsed = time.time() - cb['opened_at']

冷却 30 秒表达的是**时长**。墙钟被回拨一次（NTP 校时 / 现场机开机后校时 /
手工改时间），`elapsed` 变负 → 永远 `< COOLDOWN` → **该设备再也不被探测**，
采集永久停摆，只能重启进程。传感器卡死检测（300s 窗口）、健康分缓存（5s）
同理。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest


# ---------------------------------------------------------------------------
# 1. 退避间隔：下限与指数增长
# ---------------------------------------------------------------------------

def test_backoff_never_below_base_interval():
    """核心判据：退避不得比正常采集更激进。"""
    from 采集层.data_collector import compute_backoff_interval

    for failures in range(1, 6):
        interval = compute_backoff_interval(failures, base_interval=5)
        assert interval >= 5, (
            f'连续失败 {failures} 次后间隔只有 {interval}s，比基础间隔 5s 还短 —— '
            f'「退避」把一台已经出问题的设备打得更频繁')


def test_backoff_grows_exponentially():
    from 采集层.data_collector import compute_backoff_interval

    seq = [compute_backoff_interval(n, 5) for n in range(1, 6)]
    assert seq == [5, 5, 8, 16, 32], f'退避序列不符合预期: {seq}'
    # 单调不减
    assert all(b >= a for a, b in zip(seq, seq[1:]))


def test_backoff_is_capped():
    from 采集层.data_collector import compute_backoff_interval, BACKOFF_CEILING_S

    assert compute_backoff_interval(20, 5) == BACKOFF_CEILING_S
    assert compute_backoff_interval(100, 5) == BACKOFF_CEILING_S


def test_backoff_survives_bad_input():
    from 采集层.data_collector import compute_backoff_interval

    assert compute_backoff_interval(0, 5) == 5
    assert compute_backoff_interval(None, 5) == 5
    assert compute_backoff_interval(3, None) == 8
    assert compute_backoff_interval(3, 0) == 8
    assert compute_backoff_interval(3, float('inf')) == 8


def test_backoff_respects_larger_base_interval():
    """基础间隔本身很大时（如 30s 的停机态），退避也不该突然变快。"""
    from 采集层.data_collector import compute_backoff_interval

    assert compute_backoff_interval(1, 30) == 30
    assert compute_backoff_interval(3, 30) == 30
    assert compute_backoff_interval(5, 30) == 32


# ---------------------------------------------------------------------------
# 2. 熔断冷却：墙钟回拨不影响
# ---------------------------------------------------------------------------

class _FakeCollector:
    """只带 `get_circuit_breaker_states` 需要的最小状态。"""

    def __init__(self):
        self._circuit_state = {}
        self._failure_counts = {}


def _states(collector):
    from 采集层.data_collector import DataCollector
    return DataCollector.get_circuit_breaker_states(collector)


def test_breaker_cooldown_uses_monotonic(monkeypatch):
    """墙钟回拨 8 小时后，冷却剩余时间仍然按真实经过的时间算。"""
    c = _FakeCollector()
    c._circuit_state['dev1'] = {'opened_at': time.monotonic(),
                                'opened_at_wall': time.time()}
    c._failure_counts['dev1'] = 5

    monkeypatch.setattr(time, 'time', lambda: time.time() - 8 * 3600)

    entry = _states(c)['devices']['dev1']
    assert entry['phase'] == 'cooldown'
    assert entry['cooldown_remaining_sec'] > 25, (
        f'冷却剩余 {entry["cooldown_remaining_sec"]}s 不合理 —— 说明用了墙钟')


def test_breaker_cooldown_expires_in_real_time():
    """真实等过冷却期后必须进入半开试探（反向对照）。"""
    c = _FakeCollector()
    c._circuit_state['dev2'] = {'opened_at': time.monotonic() - 31,
                                'opened_at_wall': time.time() - 31}
    c._failure_counts['dev2'] = 5
    entry = _states(c)['devices']['dev2']
    assert entry['phase'] == 'half_open_probe'
    assert entry['cooldown_remaining_sec'] == 0


def test_breaker_status_exposes_wall_clock_time():
    """对外仍要给"什么时候断的"这个墙钟时刻（前端要显示日期）。"""
    c = _FakeCollector()
    wall = time.time()
    c._circuit_state['dev3'] = {'opened_at': time.monotonic(), 'opened_at_wall': wall}
    c._failure_counts['dev3'] = 5

    entry = _states(c)['devices']['dev3']
    assert abs(entry['opened_at'] - wall) < 5, (
        f'opened_at={entry["opened_at"]!r} 不是墙钟时刻 —— 前端会显示 1970 年')


def test_breaker_state_without_wall_field_still_works():
    """兼容老状态（只有 opened_at 一个字段）时不许崩。"""
    c = _FakeCollector()
    c._circuit_state['dev4'] = {'opened_at': time.monotonic()}
    c._failure_counts['dev4'] = 5
    entry = _states(c)['devices']['dev4']
    assert entry['state'] == 'open'
    assert entry['cooldown_remaining_sec'] >= 0


# ---------------------------------------------------------------------------
# 3. 传感器卡死检测：墙钟回拨不影响
# ---------------------------------------------------------------------------

def test_sensor_stuck_detected_after_clock_rollback(monkeypatch):
    """卡死窗口（300s）是时长：墙钟回拨后仍要能判出"值长时间不变"。"""
    from 采集层.data_collector import DataQualityAssessor

    last_time = time.monotonic() - 400        # 真实已过 400s
    monkeypatch.setattr(time, 'time', lambda: time.time() - 8 * 3600)

    quality = DataQualityAssessor.assess(
        10.0, 'temp', 'running', last_value=10.0, last_time=last_time)
    assert quality == DataQualityAssessor.UNCERTAIN_LAST_USABLE, (
        '墙钟回拨后卡死检测失效 —— 卡住的传感器会一直被当成 GOOD')


def test_sensor_not_stuck_within_window():
    from 采集层.data_collector import DataQualityAssessor

    quality = DataQualityAssessor.assess(
        10.0, 'temp', 'running', last_value=10.0, last_time=time.monotonic() - 5)
    assert quality == DataQualityAssessor.GOOD


# ---------------------------------------------------------------------------
# 4. 静态守卫
# ---------------------------------------------------------------------------

def test_no_wall_clock_in_duration_math():
    """采集链路里"算时长"的地方不许用 `time.time()`。"""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / '采集层' / 'data_collector.py'
    code = re.sub(r'#.*$', '', src.read_text(encoding='utf-8'), flags=re.M)

    forbidden = [
        r"elapsed\s*=\s*time\.time\(\)",
        r"deadline\s*=\s*time\.time\(\)",
        r"time\.time\(\)\s*-\s*cb\[",
        r"time\.time\(\)\s*-\s*last_time",
    ]
    hits = [p for p in forbidden if re.search(p, code)]
    assert not hits, (
        '采集链路里又出现墙钟算时长：' + '、'.join(hits) +
        '\n  时长（冷却/退避/卡死/缓存）一律 time.monotonic()。')
