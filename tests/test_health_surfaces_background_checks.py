# -*- coding: utf-8 -*-
"""两个后台检查的结论必须出现在**健康页**（round 276）。

背景
----
round 263 加了「按设备采集中断看门狗」、round 273 加了「每日备份恢复演练」。
但它们的结论此前只出现在**日志**和**报警**里：

* 备份演练：一天跑一次，跑完只有一行日志；运维想看"最近一次演练过没过、
  是哪个备份"，只能翻日志或等报警；
* 逐设备新鲜度：看门狗只在**越界那一刻**报警，健康页的 `data_freshness`
  看的是 `MAX(timestamp)`（全库最新一条）——25 台设备只要有一台在写就永远
  "新鲜"，**单台失联在健康页看不见**。

判据：**后台检查的结论必须能被随时查询**，而不是"只在出事那一刻出现一次"。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest


@pytest.fixture
def db(tmp_path):
    from 存储层.database import Database
    database = Database(str(tmp_path / 'health.db'))
    yield database
    database.close()


def _seed(db, device_id, age_seconds):
    ts = datetime.now() - timedelta(seconds=age_seconds)
    db.insert_data(device_id, 'temp', 20.0, ts)


# ---------------------------------------------------------------------------
# 1. 备份演练结果进健康页
# ---------------------------------------------------------------------------

def test_backup_readiness_unknown_before_first_drill(monkeypatch):
    """本次进程还没跑过演练 → UNKNOWN（不是"健康"，也不是"故障"）。"""
    import core.backup_drill as bd
    from core.health_checker import HealthStatus, _check_backup_readiness

    monkeypatch.setattr(bd, '_LAST_RESULT', None)
    result = _check_backup_readiness()
    assert result['status'] == HealthStatus.UNKNOWN
    assert '尚未执行' in result['message']


def test_backup_readiness_reports_failure(monkeypatch):
    import core.backup_drill as bd
    from core.health_checker import HealthStatus, _check_backup_readiness

    monkeypatch.setattr(bd, '_LAST_RESULT', {
        'ok': False, 'backup': '/x/b.db', 'age_hours': 100.0,
        'checked_at': '2026-10-26T02:00:00',
        'problems': ['最近一次备份是 100 小时前', '定时备份可能已经停了'],
    })
    result = _check_backup_readiness()
    assert result['status'] == HealthStatus.UNHEALTHY
    assert '备份不可用' in result['message']
    assert result['details']['age_hours'] == 100.0


def test_backup_readiness_reports_success(monkeypatch):
    import core.backup_drill as bd
    from core.health_checker import HealthStatus, _check_backup_readiness

    monkeypatch.setattr(bd, '_LAST_RESULT', {
        'ok': True, 'backup': '/x/b.db', 'age_hours': 3.0,
        'checked_at': '2026-10-26T02:00:00', 'problems': [],
    })
    result = _check_backup_readiness()
    assert result['status'] == HealthStatus.HEALTHY
    assert '备份可用' in result['message']


def test_drill_caches_result(tmp_path, monkeypatch):
    """跑一次演练后，`get_last_result()` 必须能读到。"""
    import core.backup_drill as bd

    monkeypatch.setattr(bd, '_LAST_RESULT', None)
    bd.run_drill(database=None, backup_dir=str(tmp_path / 'nope'))
    last = bd.get_last_result()
    assert last is not None and last['ok'] is False


# ---------------------------------------------------------------------------
# 2. 逐设备新鲜度进健康页
# ---------------------------------------------------------------------------

def test_single_stale_device_is_visible(db):
    """一台停、其它在写 —— 全局口径看不见，逐设备必须看见。"""
    from core.health_checker import HealthStatus, _check_device_freshness

    _seed(db, 'dev_dead', 3600)
    _seed(db, 'dev_ok1', 1)
    _seed(db, 'dev_ok2', 2)

    result = _check_device_freshness(db)
    assert result['status'] == HealthStatus.DEGRADED
    assert 'dev_dead' in result['details']['stale']
    assert '1/3' in result['message']


def test_all_devices_stale_is_unhealthy(db):
    from core.health_checker import HealthStatus, _check_device_freshness

    _seed(db, 'a', 3600)
    _seed(db, 'b', 3600)

    result = _check_device_freshness(db)
    assert result['status'] == HealthStatus.UNHEALTHY


def test_all_fresh_is_healthy(db):
    from core.health_checker import HealthStatus, _check_device_freshness

    _seed(db, 'a', 1)
    _seed(db, 'b', 2)

    result = _check_device_freshness(db)
    assert result['status'] == HealthStatus.HEALTHY
    assert result['details']['stale'] == {}


def test_empty_database_is_unknown(db):
    from core.health_checker import HealthStatus, _check_device_freshness

    result = _check_device_freshness(db)
    assert result['status'] == HealthStatus.UNKNOWN


# ---------------------------------------------------------------------------
# 3. 两个检查都已注册
# ---------------------------------------------------------------------------

def test_checks_are_registered():
    from core.health_checker import HealthChecker

    HealthChecker.clear()
    HealthChecker.register_default_checks(database=object(), device_manager=None,
                                          data_collector=None)
    names = set(getattr(HealthChecker, '_checks', {}).keys())
    assert 'backup_readiness' in names, '备份可用性没有注册进健康检查'
    assert 'device_freshness' in names, '逐设备新鲜度没有注册进健康检查'
