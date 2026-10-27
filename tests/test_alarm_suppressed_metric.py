# -*- coding: utf-8 -*-
"""被抑制的告警必须**可计数**（round 290）。

缺陷形态（缺陷族：**状态不可观测 → 失败静默**）
--------------------------------------------
`AlarmManager` 有两条抑制路径，两条都只留一行日志：

* **洪水抑制**（ISA-18.2）：报警照旧落库/统计/升级，只压掉声光、广播、前端、邮件；
  日志一行 WARNING，**没有任何计数**；
* **去重抑制**：冷却窗口 / 已确认窗口内重复出现的报警，连前端推送都不发；
  日志只有一行 DEBUG。

后果不是功能坏掉，而是**看板说了假话**：抑制期里报警率骤降，界面上一切安静。
"现场真的安静"和"两百条报警被抑制吞了"在看板上长得**一模一样** ——
运维没有任何聚合口径能区分这两件事，值班交接时也无从复核。

判据
----
1. 每一次"压掉"都要有一条计数，且**按原因分桶**（flood / dedup_cooldown /
   dedup_acked）：只知道"被压了 200 条"却不知道压在哪一环，等于没排查入口；
2. 计数必须跟着**真实分支**走：正常播报的报警不许计入（否则指标变成"报警总数"
   的重复，失去"被压掉多少"的语义）；
3. 不改变原有行为：抑制照样抑制、播报照样播报 —— 本轮只加可观测性。
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest

KEY = ('r_info', 'dev1', 'temp')


def _rule(level='info', rule_id=KEY[0]):
    return {
        'id': rule_id,
        'level': level,
        'name': '温度偏高',
        'condition': 'greater_than',
        'threshold': 80.0,
        'delay': 0,
        'deadband': 0,
        'area': 'all',
    }


@pytest.fixture
def db(tmp_path):
    from 存储层.database import Database

    database = Database(str(tmp_path / 'suppressed.db'))
    yield database
    database.close()


@pytest.fixture
def am(db):
    from 报警层.alarm_manager import AlarmManager

    mgr = AlarmManager(db)
    mgr.alarm_output = MagicMock()
    mgr.alarm_output.enabled = True
    mgr.broadcast_system = MagicMock()
    mgr.broadcast_system.enabled = True
    mgr._websocket_emit = MagicMock()
    mgr.notification = MagicMock()
    mgr.rules[KEY[0]] = _rule('info')
    mgr._rules_index[('dev1', 'temp')] = [(KEY[0], mgr.rules[KEY[0]])]
    yield mgr
    try:
        mgr.stop()
    except Exception:       # noqa: BLE001
        pass


def _count(level: str, reason: str) -> float:
    """读某条计数器的当前值（Prometheus 计数器是进程级全局，用例一律取**差值**）。"""
    from core.metrics import ALARMS_SUPPRESSED

    return ALARMS_SUPPRESSED.labels(level=level, reason=reason)._value.get()


def _snapshot(level: str) -> dict[str, float]:
    return {r: _count(level, r) for r in ('flood', 'dedup_cooldown', 'dedup_acked')}


def _activate_flood(am, threshold=3):
    """把洪水检测器推进到激活态（用 critical 填窗口，它本身不会被抑制）。"""
    am._flood_detector.threshold = threshold
    for _ in range(threshold):
        am._flood_detector.record_alarm('critical')
    assert am._flood_detector._flood_active, '前提不成立：洪水抑制没有激活'


def _fire(am, level='info', when=None):
    rule = _rule(level)
    am.rules[KEY[0]] = rule
    am._rules_index[('dev1', 'temp')] = [(KEY[0], rule)]
    am.check_alarm('dev1', 'temp', 95.0, when or datetime(2026, 10, 27, 12, 0, 0))


def _trigger(am, level='info', when=None, value=95.0):
    """直接走 `_trigger_alarm`。

    去重类用例必须用它：同一条规则在 `check_alarm` 里**状态已经是活跃**，
    再喂同一个值不会重新触发（连日志都不出）—— 用例会静默空转成假绿。
    `_trigger_alarm` 才是包含抑制分支的那个函数。
    """
    rule = _rule(level)
    am.rules[KEY[0]] = rule
    am._rules_index[('dev1', 'temp')] = [(KEY[0], rule)]
    am._trigger_alarm(rule, 'dev1', 'temp', value, when or datetime(2026, 10, 27, 12, 0, 0))


# ---------------------------------------------------------------------------
# 1. 洪水抑制：压掉播报 → 计到 reason='flood'
# ---------------------------------------------------------------------------

def test_flood_suppression_is_counted(am):
    _activate_flood(am)
    before = _snapshot('info')

    _fire(am, 'info')

    after = _snapshot('info')
    assert after['flood'] - before['flood'] == 1, (
        '洪水抑制压掉了一条播报，但 `scada_alarms_suppressed_total{reason="flood"}` 没动 —— '
        '看板上就只剩"报警率下降"，看不出是被抑制吞掉的')
    # 不能把 dedup 的桶也带上（原因必须分开，否则失去排查入口）
    assert after['dedup_cooldown'] == before['dedup_cooldown']
    assert after['dedup_acked'] == before['dedup_acked']


def test_flood_does_not_annunciate_but_counts(am):
    """反向对照的一半：同一次抑制里，播报确实被压住 —— 计数不是凭空来的。"""
    _activate_flood(am)
    _fire(am, 'info')

    assert not am.alarm_output.trigger_alarm.called
    assert not am._websocket_emit.called


def test_critical_during_flood_is_not_counted(am):
    """反向对照：洪水期 critical 照常播报，**不许**计入抑制。"""
    _activate_flood(am)
    before = _snapshot('critical')

    _fire(am, 'critical')

    after = _snapshot('critical')
    assert after['flood'] == before['flood'], (
        'critical 在洪水期是照常播报的，却计成了"被抑制" —— 指标会虚高，'
        '看板上"压掉多少"从此不可信')
    assert am.alarm_output.trigger_alarm.called, '前提不成立：critical 应该照常播报'


def test_normal_alarm_not_counted(am):
    """反向对照：没有任何抑制时，三个桶都不许动。"""
    before = _snapshot('info')

    _fire(am, 'info')

    after = _snapshot('info')
    assert after == before, f'正常播报的报警被计成了抑制：{before} -> {after}'
    assert am._websocket_emit.called, '前提不成立：正常报警应该推送前端'


# ---------------------------------------------------------------------------
# 2. 去重抑制：连前端推送都压掉 → 计到具体原因
# ---------------------------------------------------------------------------

def test_dedup_cooldown_is_counted(am):
    _trigger(am, 'info')                                # 这条正常推送
    before = _snapshot('info')

    _trigger(am, 'info', value=96.0)                     # 同 key 冷却窗口内再来一次
    after = _snapshot('info')

    assert after['dedup_cooldown'] - before['dedup_cooldown'] == 1, (
        '冷却窗口压掉了一次前端推送，但计数没动 —— 前端少了一条报警，'
        '而"为什么少"在指标里查不到')
    assert am._websocket_emit.call_count == 1, '前提不成立：冷却窗口本应只放过第一条'


def test_dedup_acked_is_counted(am):
    am._record_acknowledge(KEY[0], KEY[1], KEY[2])
    before = _snapshot('info')

    _trigger(am, 'info')

    after = _snapshot('info')
    assert after['dedup_acked'] - before['dedup_acked'] == 1, (
        '确认抑制窗口压掉了推送，但计数没动')
    assert after['dedup_cooldown'] == before['dedup_cooldown'], (
        '确认抑制被记成了冷却抑制 —— 两种原因的处理方式完全不同，不能混桶')


def test_suppressed_counter_exported_in_metrics_text():
    """计数器必须真的出现在 /metrics 的导出文本里（否则加了等于没加）。

    用 `generate_latest()` 读**真实导出面**，而不是看 `_name`：
    prometheus_client 的 Counter `_name` 不带 `_total` 后缀（导出时才补），
    照着 `_name` 断言会得到一条"看着很严格、其实测错对象"的用例。
    """
    from prometheus_client import generate_latest

    from core.metrics import ALARMS_SUPPRESSED

    ALARMS_SUPPRESSED.labels(level='info', reason='flood').inc(1)
    text = generate_latest().decode('utf-8', 'replace')

    assert 'scada_alarms_suppressed_total{' in text,         '计数器没进导出面 —— /metrics 上根本看不到它'
    assert 'reason="flood"' in text, '导出缺 reason 标签，无法区分抑制原因'
    assert '# HELP scada_alarms_suppressed_total' in text, '导出缺 HELP，看板无法解释这条指标' 
