# -*- coding: utf-8 -*-
"""报警判定引擎（纯函数）的单元测试 + 与真实数据的回放校验（round 262）。

为什么单独测这一层
------------------
`decide()` 是"一条采样值该不该报警"的唯一判定入口，运行时
（`AlarmManager._process_alarm_state`）、离线回放（`tools/alarm_replay.py`）、
以及未来的任何消费者都用它。它一旦有偏差，**所有下游结论一起偏**，
而且不会报错 —— 只会"报警多了/少了"。

所以这里覆盖三类：
1. 判定语义本身（死区/延迟/边界/非有限值）
2. 与 manager 的一致性（同一序列，两者判定必须一致 —— 防"平行实现"）
3. 真实数据的回放能跑通且方向正确（加抑制 → 报警数下降）
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from 报警层.alarm_engine import (
    CLEAR, FIRE, NONE, PENDING, UPDATE,
    decide, evaluate_condition, new_state, normalize_deadband, normalize_delay,
)

T0 = datetime(2026, 10, 25, 9, 0, 0)


def _rule(**kw):
    rule = {'id': 'r1', 'device_id': 'd1', 'register_name': 'temp',
            'condition': 'greater_than', 'threshold': 100.0, 'level': 'critical'}
    rule.update(kw)
    return rule


# ---------------------------------------------------------------------------
# 1. 归一化
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('raw,expected', [
    (None, 0.0), (0, 0.0), (30, 30.0), ('30', 30.0), (-5, 0.0),
    (float('inf'), 0.0), (float('nan'), 0.0), ('abc', 0.0),
])
def test_normalize_delay(raw, expected):
    assert normalize_delay(raw) == expected


@pytest.mark.parametrize('raw,expected', [
    (None, 0.0), (2, 2.0), ('2', 2.0), (-1, 0.0), (float('inf'), 0.0),
])
def test_normalize_deadband(raw, expected):
    assert normalize_deadband(raw) == expected


# ---------------------------------------------------------------------------
# 2. 条件判定与死区
# ---------------------------------------------------------------------------

def test_trigger_uses_exact_threshold():
    rule = _rule()
    assert evaluate_condition(rule, 100.0, active=False) is False
    assert evaluate_condition(rule, 100.1, active=False) is True


def test_hold_uses_deadband():
    """已在报警时，要跌破 threshold - deadband 才清除（迟滞）。"""
    rule = _rule()
    assert evaluate_condition(rule, 99.0, active=True, deadband=2.0) is True
    assert evaluate_condition(rule, 98.5, active=True, deadband=2.0) is True
    assert evaluate_condition(rule, 97.9, active=True, deadband=2.0) is False


def test_deadband_does_not_affect_first_trigger():
    """死区只影响"保持"，不影响"首次触发" —— 否则会漏报。"""
    rule = _rule()
    assert evaluate_condition(rule, 99.0, active=False, deadband=5.0) is False


# ---------------------------------------------------------------------------
# 3. 状态机：触发 / 延迟 / 清除
# ---------------------------------------------------------------------------

def test_immediate_fire_without_delay():
    d = decide(_rule(), None, 120.0, T0)
    assert d.action == FIRE
    assert d.updates['trigger_count'] == 1


def test_delay_holds_as_pending():
    d = decide(_rule(delay=10), None, 120.0, T0)
    assert d.action == PENDING
    assert d.updates['pending'] is True


def test_delay_fires_after_window():
    rule = _rule(delay=10)
    d1 = decide(rule, None, 120.0, T0)
    state = new_state('r1', 'd1', 'temp', d1, T0)
    assert state['pending'] is True

    d2 = decide(rule, state, 120.0, T0 + timedelta(seconds=5))
    assert d2.action == PENDING, '未满 10s 不该触发'

    d3 = decide(rule, state, 120.0, T0 + timedelta(seconds=11))
    assert d3.action == FIRE


def test_delay_restarts_when_condition_recovers():
    rule = _rule(delay=10)
    d1 = decide(rule, None, 120.0, T0)
    state = new_state('r1', 'd1', 'temp', d1, T0)
    # 条件恢复 → 清除
    assert decide(rule, state, 50.0, T0 + timedelta(seconds=3)).action == CLEAR
    # 再次越限 → 重新挂起（计时从头开始）
    d2 = decide(rule, None, 120.0, T0 + timedelta(seconds=4))
    assert d2.action == PENDING


def test_update_increments_count_while_active():
    rule = _rule()
    state = new_state('r1', 'd1', 'temp', decide(rule, None, 120.0, T0), T0)
    d = decide(rule, state, 130.0, T0 + timedelta(seconds=5))
    assert d.action == UPDATE
    assert d.updates['trigger_count'] == 2


def test_clear_when_recovered():
    rule = _rule()
    state = new_state('r1', 'd1', 'temp', decide(rule, None, 120.0, T0), T0)
    assert decide(rule, state, 50.0, T0 + timedelta(seconds=5)).action == CLEAR


def test_non_finite_value_changes_nothing():
    """NaN/Inf = 读数无效，既不清除也不触发（fail-safe）。"""
    rule = _rule()
    state = new_state('r1', 'd1', 'temp', decide(rule, None, 120.0, T0), T0)
    for bad in (float('nan'), float('inf'), float('-inf'), None, 'abc'):
        assert decide(rule, state, bad, T0 + timedelta(seconds=1)).action == NONE


def test_unusable_timestamp_fails_open():
    """时间戳不可比时按"已到期"处理 —— 宁可多报一次，也不静默永不报警。"""
    rule = _rule(delay=10)
    d1 = decide(rule, None, 120.0, T0)
    state = new_state('r1', 'd1', 'temp', d1, T0)
    state['confirm_time'] = 'not-a-datetime'
    assert decide(rule, state, 120.0, T0 + timedelta(seconds=1)).action == FIRE


# ---------------------------------------------------------------------------
# 4. 与 AlarmManager 的一致性（防平行实现）
# ---------------------------------------------------------------------------

def test_manager_uses_same_engine():
    """同一序列，manager 的状态变化必须与 `decide()` 推演的一致。"""
    from unittest.mock import MagicMock
    from 报警层.alarm_manager import AlarmManager

    db = MagicMock()
    db.get_alarm_records.return_value = []
    mgr = AlarmManager(db)
    rule = _rule(delay=0, deadband=2.0)
    mgr.rules['r1'] = rule
    mgr._deadbands['r1'] = 2.0
    mgr._rebuild_rules_index()

    values = [120.0, 130.0, 99.0, 97.0, 120.0, 50.0]
    state = None
    for i, v in enumerate(values):
        ts = T0 + timedelta(seconds=5 * i)
        expected = decide(rule, state, v, ts)
        mgr.check_alarm('d1', 'temp', v, ts)

        actual_state = mgr.alarm_states.get(('r1', 'd1', 'temp'))
        if expected.action == CLEAR:
            assert actual_state is None, f'第 {i} 步：应清除却仍有状态'
            state = None
        else:
            assert actual_state is not None, f'第 {i} 步：应有状态却没有'
            state = dict(actual_state)


# ---------------------------------------------------------------------------
# 5. 真实数据回放（有库才跑）
# ---------------------------------------------------------------------------

REAL_DB = Path(__file__).resolve().parents[1] / 'data' / 'scada.db'


@pytest.mark.skipif(not REAL_DB.exists(), reason='没有真实历史库')
def test_replay_reduces_alarm_count_on_real_data():
    """用真实历史数据回放：加 delay + deadband 后报警数必须显著下降。

    这条不是"性能测试"，是**判据**：如果加了抑制报警数没降，说明抑制没生效
    （或者配置没被读到）—— 两者都是必须当场发现的缺陷。
    """
    import sqlite3
    import sys
    sys.path.insert(0, str(REAL_DB.parents[1] / 'tools'))
    from alarm_replay import replay_rule

    conn = sqlite3.connect(f'file:{REAL_DB.as_posix()}?mode=ro', uri=True)
    try:
        rule = {'id': 'temp_low', 'device_id': 'siemens_1500_01',
                'register_name': 'boiler_temperature',
                'condition': 'less_than', 'threshold': 50.0,
                'delay': 0, 'deadband': 0}
        baseline = replay_rule(conn, rule)
        damped = replay_rule(conn, rule, delay=10, deadband=2.0)
    finally:
        conn.close()

    assert baseline['points'] > 1000, '历史数据太少，回放没有意义'
    assert baseline['fires'] > 0, '基线都没报警，说明规则/数据不匹配'
    assert damped['fires'] < baseline['fires'] * 0.5, (
        f'加了 10s 延迟 + 2℃ 死区后报警只从 {baseline["fires"]} 降到 '
        f'{damped["fires"]} —— 抑制没生效')


@pytest.mark.skipif(not REAL_DB.exists(), reason='没有真实历史库')
def test_replay_is_deterministic():
    """同一份数据回放两次结果必须一致（回放本身不能带随机/时间依赖）。"""
    import sqlite3
    import sys
    sys.path.insert(0, str(REAL_DB.parents[1] / 'tools'))
    from alarm_replay import replay_rule

    conn = sqlite3.connect(f'file:{REAL_DB.as_posix()}?mode=ro', uri=True)
    try:
        rule = {'id': 'ph_low', 'device_id': 'schneider_m340_01',
                'register_name': 'ph_value', 'condition': 'less_than',
                'threshold': 6.5, 'delay': 15, 'deadband': 0.1}
        a = replay_rule(conn, rule)
        b = replay_rule(conn, rule)
    finally:
        conn.close()
    assert a == b
