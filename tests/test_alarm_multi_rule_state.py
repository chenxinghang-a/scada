# -*- coding: utf-8 -*-
"""同一寄存器多条规则（警告/严重）必须各自独立（round 232）。

缺陷
----
`alarm_states` 原来用 `(device_id, register_name)` 做键，但
`_rules_index[(device_id, register_name)]` 是一个**列表** —— 一个寄存器同时配
「警告 80 / 严重 100」是标准用法（ISA-18.2 分级报警）。于是两条规则抢同一个槽位：

1. **严重规则在低于自己阈值时就触发**
   `_check_condition` 的迟滞分支判据是「这个寄存器**有**报警在活动」，而不是
   「**本规则**在活动」。警告已经活动时，严重规则的触发阈值被算成
   `100 - deadband(5) = 95` → 96 就报「严重」，而它配的是 100。

2. **两条规则互相覆盖状态**
   严重规则触发时把警告规则的状态整条覆盖掉（`alarm_states[key] = 新状态`）。
   下一个数据点，警告规则看不到自己的状态 → 当成新报警再触发 → 又覆盖严重规则
   → 每个数据点互相顶一次：**声光被反复驱动**，且
   `get_active_alarms()` 永远只显示其中一条。

3. **清除任意一条会把另一条一起抹掉**
   `del alarm_states[state_key]` 删的是共享槽位。

修法：键改为 `(rule_id, device_id, register_name)`，每条规则一个独立状态槽位。
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from 报警层.alarm_manager import AlarmManager

POLL = 0.05


def _config(tmp_path):
    cfg = tmp_path / 'alarms.yaml'
    cfg.write_text(
        'alarm_rules:\n'
        '  - id: warn_temp\n'
        '    name: 温度预警\n'
        '    device_id: d1\n'
        '    register_name: temp\n'
        '    condition: greater_than\n'
        '    threshold: 80\n'
        '    level: warning\n'
        '    deadband: 5\n'
        '    enabled: true\n'
        '  - id: crit_temp\n'
        '    name: 温度严重报警\n'
        '    device_id: d1\n'
        '    register_name: temp\n'
        '    condition: greater_than\n'
        '    threshold: 100\n'
        '    level: critical\n'
        '    deadband: 5\n'
        '    enabled: true\n'
        'dedup:\n'
        '  enabled: false\n',
        encoding='utf-8',
    )
    return cfg


@pytest.fixture
def mgr(tmp_path):
    db = MagicMock()
    db.insert_alarm.return_value = True
    db.acknowledge_alarm.return_value = True
    m = AlarmManager(db, config_path=str(_config(tmp_path)))
    m._config_watch_interval = POLL
    m._trigger_alarm = MagicMock()      # 不碰声光/广播，只记调用
    try:
        yield m
    finally:
        m.stop()


def _feed(mgr, value: float):
    mgr.check_alarm('d1', 'temp', value, datetime.now())


# ---------------------------------------------------------------------------
# 1. 迟滞必须按「本规则」判断，不能借用别的规则的报警状态
# ---------------------------------------------------------------------------

def test_critical_does_not_fire_below_its_own_threshold(mgr):
    """警告已活动时，严重规则仍必须等到**自己的** 100 才报。"""
    _feed(mgr, 85.0)                    # 触发警告（>80）
    assert ('warn_temp', 'd1', 'temp') in mgr.alarm_states

    _feed(mgr, 96.0)                    # 96 < 100，严重不该报
    assert ('crit_temp', 'd1', 'temp') not in mgr.alarm_states, (
        '严重规则在 96 就触发了 —— 它借用了警告规则的「已活动」状态，'
        '于是阈值被当成 100-deadband(5)=95（低于配置的 100）'
    )
    mgr._trigger_alarm.assert_called_once()   # 只有警告那一次


def test_critical_fires_at_its_own_threshold(mgr):
    _feed(mgr, 85.0)
    _feed(mgr, 101.0)
    assert ('crit_temp', 'd1', 'temp') in mgr.alarm_states


# ---------------------------------------------------------------------------
# 2. 两条规则各自保留状态
# ---------------------------------------------------------------------------

def test_both_rules_keep_independent_states(mgr):
    _feed(mgr, 85.0)
    _feed(mgr, 101.0)

    assert ('warn_temp', 'd1', 'temp') in mgr.alarm_states, (
        '严重规则触发后把警告规则的状态覆盖掉了'
    )
    assert ('crit_temp', 'd1', 'temp') in mgr.alarm_states

    active = mgr.get_active_alarms()
    ids = {a['alarm_id'] for a in active}
    assert ids == {'warn_temp', 'crit_temp'}, f'活动报警只有 {ids}，应两条都在'


def test_no_ping_pong_triggering(mgr):
    """两条规则同时成立时，连续数据点不能反复触发声光。"""
    _feed(mgr, 85.0)          # 警告 1 次
    _feed(mgr, 101.0)         # 严重 1 次
    calls_after_two = mgr._trigger_alarm.call_count

    for _ in range(5):
        _feed(mgr, 101.0)     # 条件持续成立，不应再触发

    assert mgr._trigger_alarm.call_count == calls_after_two, (
        f'又触发了 {mgr._trigger_alarm.call_count - calls_after_two} 次 —— '
        '两条规则在互相覆盖状态，每个数据点都把对方当成「新报警」'
    )


# ---------------------------------------------------------------------------
# 3. 清除只影响自己
# ---------------------------------------------------------------------------

def test_clearing_critical_keeps_warning_active(mgr):
    _feed(mgr, 85.0)
    _feed(mgr, 101.0)

    _feed(mgr, 90.0)          # 严重清除（90 < 100-5），警告仍成立（90 > 80-5）

    assert ('crit_temp', 'd1', 'temp') not in mgr.alarm_states
    assert ('warn_temp', 'd1', 'temp') in mgr.alarm_states, (
        '严重规则清除时把警告规则一起删了'
    )
    assert [a['alarm_id'] for a in mgr.get_active_alarms()] == ['warn_temp']


# ---------------------------------------------------------------------------
# 4. 确认一条不影响另一条
# ---------------------------------------------------------------------------

def test_acknowledge_one_rule_keeps_other_unacknowledged(mgr):
    _feed(mgr, 85.0)
    _feed(mgr, 101.0)

    assert mgr.acknowledge_alarm('warn_temp', 'd1', 'temp', 'operator1') is True

    by_id = {a['alarm_id']: a for a in mgr.get_active_alarms()}
    assert by_id['warn_temp']['acknowledged'] is True
    assert by_id['crit_temp']['acknowledged'] is False, (
        '确认警告规则时把严重规则也标成已确认了'
    )


# ---------------------------------------------------------------------------
# 5. 全部复位时，传给升级管理器的三元组不能错位
# ---------------------------------------------------------------------------

def test_reset_all_passes_correct_triples_to_escalation(mgr):
    """`reset_alarm()` 全量复位必须把 (rule_id, device_id, register_name) 原样传出。

    ⚠️ 改键之后这里最容易漏：原写法 `(state['alarm_id'], key[0], key[1])`
    在新键下变成 `(rule_id, rule_id, device_id)` —— 设备被当成寄存器，
    已复位的报警在升级管理器里留下垃圾键并继续被跟踪。
    """
    mgr._escalation_manager = MagicMock()
    _feed(mgr, 85.0)
    _feed(mgr, 101.0)

    assert mgr.reset_alarm() is True
    assert mgr.alarm_states == {}

    passed = {tuple(c.args) for c in mgr._escalation_manager.remove_alarm.call_args_list}
    assert ('warn_temp', 'd1', 'temp') in passed, f'升级管理器收到的键错位: {passed}'
    assert ('crit_temp', 'd1', 'temp') in passed, f'升级管理器收到的键错位: {passed}'
