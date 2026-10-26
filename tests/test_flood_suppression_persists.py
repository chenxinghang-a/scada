# -*- coding: utf-8 -*-
"""洪水抑制只该压"播报"，不该压"记录"（round 282）。

缺陷形态
--------
`AlarmManager._trigger_alarm()` 里，洪水检测命中后直接：

    should_emit, reason = self._flood_detector.record_alarm(severity)
    if not should_emit:
        logger.debug(f"告警被洪水检测器抑制: {rule_id} ...")
        return

这个 `return` 在 `database.insert_alarm()` **之前**，于是被抑制的报警：

* **不落库** —— 历史/审计里根本没有这一条，而洪水期恰恰是最需要事后复盘
  的时候；报表上的"报警总数"也比现场真实发生的少；
* 不进报警统计（`record_alarm_trigger` 同样在 return 之后）；
* 但升级跟踪**已经**登记了（`_escalation_manager.record_alarm` 在洪水检测
  之前）→ 升级链照样往下走。

连锁后果（真正致命的一环）
--------------------------
`acknowledge_alarm()` 以数据库行是否被更新为准：

    success = self.database.acknowledge_alarm(...)   # → rowcount > 0

数据库里没有这条记录 → `rowcount == 0` → 确认**永远失败** →
`if success and self._escalation_manager:` 不成立 → 升级管理器收不到确认 →
一条**看不见（历史里没有）、也关不掉（确认失败）**的报警一路升到最高级，
半夜给三级主管打电话。而失败分支此前一声不响。

判据
----
ISA-18.2：洪水抑制抑制的是**播报（annunciation）**，不得丢失**记录**。
所以正确语义是：落库 + 统计 + 升级跟踪照旧，只跳过
声光 / 广播 / 前端推送 / 邮件短信。
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
        # ⚠️ `condition` 必填：`alarm_engine.evaluate_condition` 没有它就直接
        #    返回 False（"条件不成立"），于是 check_alarm 一路走到 CLEAR/NONE，
        #    用例会静默空转（断言 `not called` 恒真 —— 假绿）。
        'condition': 'greater_than',
        'threshold': 80.0,
        'delay': 0,
        'deadband': 0,
        'area': 'all',
    }


@pytest.fixture
def db(tmp_path):
    from 存储层.database import Database

    database = Database(str(tmp_path / 'flood.db'))
    yield database
    database.close()


@pytest.fixture
def am(db):
    from 报警层.alarm_manager import AlarmManager

    mgr = AlarmManager(db)
    # 声光 / 广播：用 MagicMock 顶替，断言"有没有被播报"
    mgr.alarm_output = MagicMock()
    mgr.alarm_output.enabled = True
    mgr.broadcast_system = MagicMock()
    mgr.broadcast_system.enabled = True
    mgr._websocket_emit = MagicMock()
    mgr.notification = MagicMock()
    # 把规则装进**真实索引**，让用例走 `check_alarm()` 这条生产入口
    # （而不是直接调 `_trigger_alarm`）—— `alarm_states` 是在 check_alarm 里
    # 登记的，绕过它就会漏测"活跃列表里到底有没有这条报警"。
    # ⚠️ 索引里存的是 `(rule_id, rule_config)` 二元组，不是 config 本身
    mgr.rules[KEY[0]] = _rule('info')
    mgr._rules_index[('dev1', 'temp')] = [(KEY[0], mgr.rules[KEY[0]])]
    yield mgr
    try:
        mgr.stop()
    except Exception:       # noqa: BLE001
        pass


def _activate_flood(am, threshold=3):
    """把洪水检测器推进到激活态（用 critical 填窗口，不触发抑制）。"""
    am._flood_detector.threshold = threshold
    for _ in range(threshold):
        am._flood_detector.record_alarm('critical')
    assert am._flood_detector._flood_active, '前提不成立：洪水抑制没有激活'


def _fire(am, level='info', when=None):
    """按生产路径触发一条报警（改级别时同步改规则，让 check_alarm 用它）。"""
    rule = _rule(level)
    am.rules[KEY[0]] = rule
    am._rules_index[('dev1', 'temp')] = [(KEY[0], rule)]
    am.check_alarm('dev1', 'temp', 95.0,
                   when or datetime(2026, 10, 26, 12, 0, 0))


# ---------------------------------------------------------------------------
# 核心：被抑制的报警必须落库
# ---------------------------------------------------------------------------

def test_suppressed_alarm_is_persisted(am, db):
    """洪水期被抑制的报警**必须**进历史表 —— 否则复盘时它根本不存在。"""
    _activate_flood(am)
    _fire(am, 'info')

    rows = db.get_alarm_records(limit=10)
    assert len(rows) == 1, (
        f'洪水抑制把报警记录也一起吞了：历史表里 {len(rows)} 行 —— '
        f'ISA-18.2 要求抑制播报不得丢失记录')
    assert rows[0]['alarm_id'] == KEY[0]
    assert rows[0]['acknowledged'] == 0


def test_suppressed_alarm_does_not_annunciate(am):
    """但播报必须被压住 —— 声光/广播/前端/邮件都不该动。"""
    _activate_flood(am)
    _fire(am, 'info')

    assert not am.alarm_output.trigger_alarm.called, '被抑制的报警不该驱动声光'
    assert not am.broadcast_system.speak_alarm.called, '被抑制的报警不该广播'
    assert not am._websocket_emit.called, '被抑制的报警不该推前端'


def test_critical_alarm_still_annunciates_during_flood(am):
    """反向对照：critical 在洪水期**不**被抑制，播报照常。

    没有这条，上面两条可能只是"什么都没发生"造成的假绿。
    """
    _activate_flood(am)
    _fire(am, 'critical')

    assert am.alarm_output.trigger_alarm.called, 'critical 报警在洪水期也必须驱动声光'
    assert am._websocket_emit.called


def test_no_flood_still_annunciates(am, db):
    """没进入洪水时行为不变（防止修过头把正常播报也压了）。"""
    _fire(am, 'info')

    assert am._websocket_emit.called
    assert len(db.get_alarm_records(limit=10)) == 1


# ---------------------------------------------------------------------------
# 连锁后果：确认必须能成功，升级跟踪必须能停
# ---------------------------------------------------------------------------

def test_suppressed_alarm_can_be_acknowledged(am, db):
    """**这条是整轮的关键**：被抑制的报警必须能被确认。

    修复前：没有数据库行 → `rowcount == 0` → 确认失败 →
    升级管理器收不到确认 → 一条看不见也关不掉的报警一路升级。
    """
    _activate_flood(am)
    _fire(am, 'info')

    ok = am.acknowledge_alarm(KEY[0], 'dev1', 'temp', 'operator')
    assert ok, (
        '被洪水抑制的报警确认失败 —— 数据库里没有这条记录，'
        '于是它永远无法被确认，升级跟踪会一直跑下去')

    rows = db.get_alarm_records(limit=10)
    assert rows[0]['acknowledged'] == 1


def test_suppressed_alarm_stops_escalation_after_ack(am, db):
    """确认之后，升级管理器里那条状态必须被标记为已确认（不再升级）。"""
    mgr = am._escalation_manager
    if mgr is None:
        pytest.skip('未装配升级管理器')

    _activate_flood(am)
    _fire(am, 'info')
    key = f'{KEY[0]}:dev1:temp'
    assert key in mgr._states, '前提不成立：升级跟踪没有登记'
    assert mgr._states[key].acknowledged is False

    assert am.acknowledge_alarm(KEY[0], 'dev1', 'temp', 'operator')
    assert mgr._states[key].acknowledged is True, (
        '确认成功了但升级管理器没收到 —— 报警会继续升级')


def test_suppressed_alarm_is_counted_in_statistics(am):
    """报警统计器要看到真实发生量（抑制不等于没发生）。"""
    stats = MagicMock()
    am._alarm_statistics = stats

    _activate_flood(am)
    _fire(am, 'info')

    assert stats.record_alarm_trigger.called, (
        '被抑制的报警没有进统计 —— 报警率指标会低于现场真实发生量')


def test_suppressed_alarm_leaves_trace_in_alarm_states(am):
    """活跃报警列表里仍然有它（否则运维连"正在报警"都不知道）。"""
    _activate_flood(am)
    _fire(am, 'info')

    assert ('r_info', 'dev1', 'temp') in am.alarm_states


# ---------------------------------------------------------------------------
# 失败路径也要留痕
# ---------------------------------------------------------------------------

def test_acknowledge_failure_is_logged(am, caplog):
    """确认失败（数据库没有对应行）必须打 WARNING —— 此前完全静默。"""
    import logging

    ok = am.acknowledge_alarm('not_in_db', 'dev1', 'temp', 'operator')
    assert ok is False

    with caplog.at_level(logging.WARNING, logger='报警层.alarm_manager'):
        am.acknowledge_alarm('not_in_db', 'dev1', 'temp', 'operator')

    assert any('确认失败' in r.message or '确认失败' in r.getMessage()
               for r in caplog.records), (
        '确认失败没有留痕 —— 操作员点确认毫无反应，也查不到原因')


def test_flood_suppression_is_logged_at_warning(am, caplog):
    """被抑制的报警要在日志里能查到 id（原来是 debug，等于查不到）。"""
    import logging

    _activate_flood(am)
    with caplog.at_level(logging.WARNING, logger='报警层.alarm_manager'):
        _fire(am, 'info')

    assert any('抑制播报' in r.getMessage() for r in caplog.records), (
        '抑制只用 debug 记录 —— 现场"这条报警为什么没弹窗"永远查不清')
