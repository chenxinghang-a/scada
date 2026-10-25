# -*- coding: utf-8 -*-
"""采集中断看门狗（round 263）。

缺陷形态
--------
`core/health_checker._check_data_freshness` 早就在看数据新鲜度，但它：

1. **只报不警** —— 结论只进 `/api/health/status` 的 JSON。报警历史里查不到、
   声光不响、WebSocket 不推。不看健康页就发现不了，而"采集中断"在 SCADA 里
   是最严重的故障之一（等于现场失联）。
2. **全局口径掩盖单设备失联** —— 它查 `MAX(timestamp) FROM history_data`，
   即**全库最新一条**。25 台设备里只要还有一台在写，这个值就一直新鲜，
   **单台设备/单条链路断了完全发现不了**。而现场最常见的恰恰是这种。

本文件的第 3 组用例专门钉住第 2 条：让一台设备停、其他设备继续写，
必须报警。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest


@pytest.fixture
def db(tmp_path):
    from 存储层.database import Database
    database = Database(str(tmp_path / 'fresh.db'))
    yield database
    database.close()


@pytest.fixture
def am(db):
    from 报警层.alarm_manager import AlarmManager
    return AlarmManager(db)


def _write(db, device_id, ts, value=1.0):
    db.insert_data(device_id, 'temp', value, ts)


NOW = datetime(2026, 10, 25, 12, 0, 0)


# ---------------------------------------------------------------------------
# 1. 阈值
# ---------------------------------------------------------------------------

def test_threshold_is_factor_times_interval():
    from core.freshness_watch import stale_threshold_seconds
    assert stale_threshold_seconds(5, factor=6) == 30


def test_threshold_has_floor():
    """采集间隔很小（1s）时也要至少等 30 秒，避免抖动误报。"""
    from core.freshness_watch import stale_threshold_seconds
    assert stale_threshold_seconds(1, factor=6) == 30


def test_threshold_survives_bad_input():
    from core.freshness_watch import stale_threshold_seconds
    assert stale_threshold_seconds(None) == 30
    assert stale_threshold_seconds(0) == 30
    assert stale_threshold_seconds(float('inf')) == 30


# ---------------------------------------------------------------------------
# 2. 扫描
# ---------------------------------------------------------------------------

def test_scan_reports_age_per_device(db):
    from core.freshness_watch import scan

    _write(db, 'dev_fresh', NOW - timedelta(seconds=2))
    _write(db, 'dev_stale', NOW - timedelta(seconds=300))

    snapshot = scan(db, now=NOW, threshold_seconds=30)
    assert snapshot['dev_fresh']['stale'] is False
    assert snapshot['dev_stale']['stale'] is True
    assert snapshot['dev_stale']['age'] == pytest.approx(300, abs=2)


def test_scan_ignores_devices_that_never_reported(db):
    """从没上报过数据的设备不算失联（刚部署/刚启用，由别的检查覆盖）。"""
    from core.freshness_watch import scan

    _write(db, 'dev_a', NOW - timedelta(seconds=1))
    snapshot = scan(db, now=NOW, threshold_seconds=30)
    assert 'dev_never_seen' not in snapshot


# ---------------------------------------------------------------------------
# 3. 关键回归：单台设备失联必须被发现（全局 MAX 发现不了）
# ---------------------------------------------------------------------------

def test_single_device_outage_is_detected_while_others_are_fresh(db, am):
    from core.freshness_watch import run_once

    # 一台停了 5 分钟，其余一直在写
    _write(db, 'dev_dead', NOW - timedelta(seconds=300))
    _write(db, 'dev_alive_1', NOW - timedelta(seconds=1))
    _write(db, 'dev_alive_2', NOW - timedelta(seconds=2))

    result = run_once(db, am, now=NOW, threshold_seconds=30)
    assert result['stale'] == ['dev_dead']
    assert result['raised'] == ['dev_dead']

    active = {a['alarm_id'] for a in am.get_active_alarms()}
    assert 'data_stale_dev_dead' in active, (
        '单台设备失联没有报警 —— 说明用的还是全库 MAX(timestamp) 的全局口径')

    # 关键对照：全局口径此刻是"新鲜"的（其它设备还在写）
    with db.get_connection() as conn:
        latest = conn.execute('SELECT MAX(timestamp) FROM history_data').fetchone()[0]
    assert str(latest).startswith(str((NOW - timedelta(seconds=1)).date()))


def test_stale_alarm_is_visible_in_alarm_records(db, am):
    """报警必须落库（能在报警历史里查到），而不只是内存里有个状态。"""
    from core.freshness_watch import run_once

    _write(db, 'dev_dead', NOW - timedelta(seconds=300))
    run_once(db, am, now=NOW, threshold_seconds=30)

    records = db.get_alarm_records(limit=10)
    assert records, '采集中断没有写进报警记录'
    assert records[0]['alarm_id'] == 'data_stale_dev_dead'
    assert records[0]['alarm_level'] == 'critical'
    assert '采集中断' in (records[0]['alarm_message'] or '')


def test_stale_alarm_is_idempotent(db, am):
    """同一台设备持续失联，30 秒一次的扫描不该每轮都新开一条报警。"""
    from core.freshness_watch import run_once

    _write(db, 'dev_dead', NOW - timedelta(seconds=300))
    first = run_once(db, am, now=NOW, threshold_seconds=30)
    second = run_once(db, am, now=NOW + timedelta(seconds=30), threshold_seconds=30)

    assert first['raised'] == ['dev_dead']
    assert second['raised'] == [], '重复扫描又开了一条报警'
    assert len(db.get_alarm_records(limit=10)) == 1


def test_recovery_clears_alarm(db, am):
    from core.freshness_watch import run_once

    _write(db, 'dev_flaky', NOW - timedelta(seconds=300))
    run_once(db, am, now=NOW, threshold_seconds=30)
    assert 'data_stale_dev_flaky' in {a['alarm_id'] for a in am.get_active_alarms()}

    # 数据恢复
    _write(db, 'dev_flaky', NOW + timedelta(seconds=35))
    result = run_once(db, am, now=NOW + timedelta(seconds=40), threshold_seconds=30)

    assert result['cleared'] == ['dev_flaky']
    assert 'data_stale_dev_flaky' not in {a['alarm_id'] for a in am.get_active_alarms()}
    records = db.get_alarm_records(limit=10)
    assert records[0]['cleared'] == 1, '恢复后没有落库标记'


# ---------------------------------------------------------------------------
# 4. 维护任务接线
# ---------------------------------------------------------------------------

def test_task_registered_only_with_alarm_manager(db):
    from core.maintenance import build_maintenance_tasks

    without = [t[0] for t in build_maintenance_tasks(database=db)]
    assert 'data_freshness_watch' not in without, (
        '没有报警管理器时不该注册 —— 它只能改状态、不能报警，等于半成品')

    with_am = [t[0] for t in build_maintenance_tasks(
        database=db, alarm_manager=object())]
    assert 'data_freshness_watch' in with_am


def test_task_interval_is_30s():
    from core.maintenance import DEFAULT_INTERVALS
    assert DEFAULT_INTERVALS['data_freshness_watch'] == 30


def test_task_runs_and_returns_summary(db, am):
    """维护任务的返回值要能看出"这轮发生了什么"，便于运维排查。"""
    from core.maintenance import build_maintenance_tasks

    _write(db, 'dev_dead', NOW - timedelta(seconds=300))
    tasks = {t[0]: t[3] for t in build_maintenance_tasks(
        database=db, alarm_manager=am)}
    result = tasks['data_freshness_watch']()
    assert 'stale' in result and 'raised' in result and 'cleared' in result
