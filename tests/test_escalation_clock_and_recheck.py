# -*- coding: utf-8 -*-
"""报警升级：超时用墙钟 → 校时后永不升级；执行前不复核 → 给已恢复的报警打电话（round 280）。

两个缺陷
--------
**1) 升级超时用墙钟**

    now = time.time()
    elapsed = now - state.first_trigger_time
    if elapsed >= rule.timeout_seconds: ...

升级超时表达的是**时长**。墙钟被回拨一次（NTP 校时 / 现场机开机后校时 /
手工改时间）→ `elapsed` 变负 → **永不升级**：报警响了半小时，值班手机一声没响。
向前跳则一次补跑所有级别，半夜给三级主管连环打电话。

**2) 检查到执行之间不复核**

`check_escalations()` 先在锁内拍快照，再在锁外逐条 `_do_escalation`。
`_do_escalation` 会调回调（发短信/打电话，可能耗时数秒）—— 这段时间里
报警可能已经恢复（`remove_alarm`）或被确认。不复核就会对一条**已经不存在的
报警**执行升级：半夜把值班电话打起来，而现场早就正常了。

判据：**"该升级"要在执行的最后一刻仍然成立**。
"""

from __future__ import annotations

import time

import pytest


def _make(monkeypatch=None, rules=None):
    from 报警层.alarm_escalation import AlarmEscalationManager

    # ⚠️ `AlarmEscalationManager._load_config` 吃的是 **escalation 段本身**
    #    （`config.get('rules')`），不是外层包一层的 `{'escalation': ...}`。
    #    包错层会静默保留 DEFAULT_RULES（300/900/1800s），测试就永远等不到升级。
    cfg = {'rules': rules or [
        {'level': 1, 'timeout_seconds': 30, 'notify_roles': ['operator']},
        {'level': 2, 'timeout_seconds': 60, 'notify_roles': ['engineer']},
    ]}
    mgr = AlarmEscalationManager(cfg)
    fired = []
    mgr.add_callback(lambda *a, **kw: fired.append((a, kw)))
    return mgr, fired


# ---------------------------------------------------------------------------
# 1. 升级超时用单调时钟
# ---------------------------------------------------------------------------

def test_escalates_after_timeout():
    mgr, fired = _make()
    mgr.record_alarm('r1', 'd1', 'temp')

    # 把首次触发时刻推到阈值之外（单调时钟）
    state = mgr._states['r1:d1:temp']
    state.first_trigger_mono = time.monotonic() - 31

    mgr.check_escalations()
    assert fired, '超过阈值却没有升级'


def test_wall_clock_rollback_does_not_block_escalation(monkeypatch):
    """墙钟回拨 8 小时后，升级仍按真实经过的时间触发。"""
    mgr, fired = _make()
    mgr.record_alarm('r1', 'd1', 'temp')

    state = mgr._states['r1:d1:temp']
    state.first_trigger_mono = time.monotonic() - 31

    real_time = time.time
    monkeypatch.setattr(time, 'time', lambda: real_time() - 8 * 3600)

    mgr.check_escalations()
    assert fired, (
        '墙钟回拨后不升级了 —— 升级超时用了墙钟，校时后值班手机永远不会响')


def test_no_escalation_before_timeout():
    mgr, fired = _make()
    mgr.record_alarm('r1', 'd1', 'temp')
    mgr.check_escalations()
    assert not fired


def test_acknowledged_alarm_does_not_escalate():
    mgr, fired = _make()
    mgr.record_alarm('r1', 'd1', 'temp')
    mgr.acknowledge_alarm('r1', 'd1', 'temp')
    mgr._states['r1:d1:temp'].first_trigger_mono = time.monotonic() - 999

    mgr.check_escalations()
    assert not fired, '已确认的报警不该再升级'


# ---------------------------------------------------------------------------
# 2. 执行前复核（不许给已恢复的报警打电话）
# ---------------------------------------------------------------------------

def test_recovered_before_check_is_not_escalated():
    """检查开始前报警就已恢复 → 连快照都进不去，一条升级都不许发。

    round 301：这条原来叫 `test_recovered_alarm_is_not_escalated`，注释里自己写着
    「这里删掉的是自己，无法体现。见下一条用例」，**全程没有一个断言** ——
    名字说测了，其实什么都没测，是一张占位符。
    「快照后 → 执行前」的两种复核已由下面两条用例覆盖（remove / acknowledge），
    所以这里补最基础的一层：恢复发生在检查之前。
    """
    mgr, fired = _make()
    mgr.record_alarm('r1', 'd1', 'temp')
    mgr._states['r1:d1:temp'].first_trigger_mono = time.monotonic() - 31
    mgr.remove_alarm('r1', 'd1', 'temp')

    mgr.check_escalations()

    assert fired == [], f'已恢复的报警仍触发了升级：{fired}'


def test_remove_between_snapshot_and_execution_skips(monkeypatch):
    """核心：快照里还有、执行时已没了 → 必须跳过。"""
    mgr, fired = _make()

    # 手工构造"快照里两条，其中一条在执行前被移除"的场景
    mgr.record_alarm('r1', 'd1', 'temp')
    mgr.record_alarm('r2', 'd1', 'temp')
    for key in ('r1:d1:temp', 'r2:d1:temp'):
        mgr._states[key].first_trigger_mono = time.monotonic() - 31

    original = mgr._do_escalation
    calls = []

    def _spy(key, state, target_level):
        calls.append(key)
        # 第一条执行时，模拟第二条的报警已经恢复
        if len(calls) == 1:
            mgr.remove_alarm('r2', 'd1', 'temp')
        return original(key, state, target_level)

    mgr._do_escalation = _spy
    mgr.check_escalations()

    assert 'r2:d1:temp' not in calls, (
        '报警已恢复却仍执行了升级 —— 半夜会把值班电话打起来')


def test_ack_between_snapshot_and_execution_skips():
    mgr, fired = _make()
    mgr.record_alarm('r1', 'd1', 'temp')
    mgr.record_alarm('r2', 'd1', 'temp')
    for key in ('r1:d1:temp', 'r2:d1:temp'):
        mgr._states[key].first_trigger_mono = time.monotonic() - 31

    original = mgr._do_escalation
    calls = []

    def _spy(key, state, target_level):
        calls.append(key)
        if len(calls) == 1:
            mgr.acknowledge_alarm('r2', 'd1', 'temp')
        return original(key, state, target_level)

    mgr._do_escalation = _spy
    mgr.check_escalations()

    assert 'r2:d1:temp' not in calls, '报警已确认却仍执行了升级'


# ---------------------------------------------------------------------------
# 3. 状态接口的 elapsed 也不许是负数
# ---------------------------------------------------------------------------

def test_status_elapsed_is_not_negative_after_rollback(monkeypatch):
    mgr, _ = _make()
    mgr.record_alarm('r1', 'd1', 'temp')

    real_time = time.time
    monkeypatch.setattr(time, 'time', lambda: real_time() - 8 * 3600)

    elapsed = mgr.get_status()['states']['r1:d1:temp']['elapsed_seconds']
    assert elapsed >= 0, f'elapsed_seconds={elapsed} 是负数 —— 用了墙钟'
