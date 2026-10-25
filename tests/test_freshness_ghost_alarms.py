# -*- coding: utf-8 -*-
"""已删除设备的"幽灵报警"必须被清掉（round 277）。

缺陷形态
--------
`freshness_watch.scan()` 按 `history_data` 分组算每台设备的最近数据时间。
**设备删了，历史数据还在**，于是：

* 它仍会被判为"陈旧"（永远不会再有新数据）；
* `data_stale_<dev>` 报警**永远不会恢复**（清除只在"数据变新鲜"时发生）；
* 活跃报警列表里永久挂着一台不存在的设备 —— 操作员点确认也没用，
  因为它不会消失。时间一长，列表里全是这种幽灵。

判据：**"设备不存在"和"设备失联"是两回事**。前者应当直接清掉报警，
后者才是要报的故障。区分它们需要一份"当前仍存在"的设备清单。

修法：`run_once(..., known_devices=...)`。传了清单就：
* 清单外的设备不再参与"陈旧"判断；
* 已经在报警、但设备已不存在的，直接清掉（单独计 `cleared_ghosts`，
  不要混进"采集恢复"的统计里 —— 那会让恢复率虚高）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest


@pytest.fixture
def db(tmp_path):
    from 存储层.database import Database
    database = Database(str(tmp_path / 'ghost.db'))
    yield database
    database.close()


@pytest.fixture
def am(db):
    from 报警层.alarm_manager import AlarmManager
    return AlarmManager(db)


NOW = datetime(2026, 10, 26, 3, 0, 0)


def _write(db, device_id, age_seconds):
    db.insert_data(device_id, 'temp', 1.0, NOW - timedelta(seconds=age_seconds))


def _active(am):
    return {a['alarm_id'] for a in am.get_active_alarms()}


# ---------------------------------------------------------------------------
# 1. 已删除设备不再报警
# ---------------------------------------------------------------------------

def test_deleted_device_does_not_raise(db, am):
    """设备已删（历史还在、数据很旧）→ 不该再为它报警。"""
    from core.freshness_watch import run_once

    _write(db, 'dev_gone', 3600)
    _write(db, 'dev_live', 1)

    result = run_once(db, am, now=NOW, threshold_seconds=30,
                      known_devices={'dev_live'})

    assert 'dev_gone' not in result['stale']
    assert 'dev_gone' not in result['raised']
    assert 'data_stale_dev_gone' not in _active(am)


def test_live_device_still_raises(db, am):
    """反向对照：清单里的设备失联，照常报警。"""
    from core.freshness_watch import run_once

    _write(db, 'dev_live', 3600)

    result = run_once(db, am, now=NOW, threshold_seconds=30,
                      known_devices={'dev_live'})
    assert result['raised'] == ['dev_live']


# ---------------------------------------------------------------------------
# 2. 已有的幽灵报警要被清掉
# ---------------------------------------------------------------------------

def test_existing_ghost_alarm_is_cleared(db, am):
    from core.freshness_watch import run_once

    _write(db, 'dev_gone', 3600)
    # 先制造一条报警（模拟设备删除之前就已经在报警）
    run_once(db, am, now=NOW, threshold_seconds=30)     # 不传清单 → 会报警
    assert 'data_stale_dev_gone' in _active(am)

    # 设备被删除后，下一轮带上清单
    result = run_once(db, am, now=NOW + timedelta(seconds=30), threshold_seconds=30,
                      known_devices=set())

    # `cleared_ghosts` 里存的是**设备 id**（与 raised/cleared 同口径）
    assert 'dev_gone' in result['cleared_ghosts']
    assert 'data_stale_dev_gone' not in _active(am), (
        '设备已删除，但它的采集中断报警还挂在活跃列表里 —— 永远不会消失')


def test_ghost_clear_is_not_counted_as_recovery(db, am):
    """幽灵清理不能混进 `cleared`（否则恢复率虚高）。"""
    from core.freshness_watch import run_once

    _write(db, 'dev_gone', 3600)
    run_once(db, am, now=NOW, threshold_seconds=30)

    result = run_once(db, am, now=NOW + timedelta(seconds=30), threshold_seconds=30,
                      known_devices=set())
    assert result['cleared'] == []
    assert result['cleared_ghosts'] == ['dev_gone']


def test_ghost_cleanup_is_idempotent(db, am):
    from core.freshness_watch import run_once

    _write(db, 'dev_gone', 3600)
    run_once(db, am, now=NOW, threshold_seconds=30)

    first = run_once(db, am, now=NOW + timedelta(seconds=30), threshold_seconds=30,
                     known_devices=set())
    second = run_once(db, am, now=NOW + timedelta(seconds=60), threshold_seconds=30,
                      known_devices=set())

    assert first['cleared_ghosts'] == ['dev_gone']
    assert second['cleared_ghosts'] == [], '第二轮又清了一次（应当已经没了）'


# ---------------------------------------------------------------------------
# 3. 不传清单时行为不变（向后兼容）
# ---------------------------------------------------------------------------

def test_without_known_devices_behaves_as_before(db, am):
    from core.freshness_watch import run_once

    _write(db, 'dev_any', 3600)
    result = run_once(db, am, now=NOW, threshold_seconds=30)
    assert result['raised'] == ['dev_any']
    assert result['cleared_ghosts'] == []


# ---------------------------------------------------------------------------
# 4. 维护任务把设备清单传下去
# ---------------------------------------------------------------------------

def test_task_passes_device_list(db, am):
    from core.maintenance import build_maintenance_tasks

    class _DM:
        def get_all_devices(self):
            return {'dev_live': {}}

    _write(db, 'dev_gone', 3600)
    _write(db, 'dev_live', 1)

    tasks = {t[0]: t[3] for t in build_maintenance_tasks(
        database=db, alarm_manager=am, device_manager=_DM())}
    result = tasks['data_freshness_watch']()

    assert 'dev_gone' not in result['stale'], '维护任务没有把设备清单传下去'


def test_task_survives_broken_device_manager(db, am):
    """设备管理器抛异常时，看门狗不能跟着挂（退化成旧行为即可）。"""
    from core.maintenance import build_maintenance_tasks

    class _Boom:
        def get_all_devices(self):
            raise RuntimeError('boom')

    _write(db, 'dev_any', 3600)
    tasks = {t[0]: t[3] for t in build_maintenance_tasks(
        database=db, alarm_manager=am, device_manager=_Boom())}
    result = tasks['data_freshness_watch']()
    assert result['raised'] == ['dev_any']
