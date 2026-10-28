# -*- coding: utf-8 -*-
"""round 308：把「采集起来了没有」和「联锁生效了几条」变成可判定的返回值。

## 本轮的起点是一处**改了一半**的修复

`_start_device_collection()` 此前**没有任何 return**（恒 None）。
调用方想判断成败只能写 `if not self._start_device_collection(...)` ——
而 `not None` 恒为真，于是**每台设备每次启动都会打印一条
「采集任务启动失败」的 ERROR**，现场日志被假警报淹没，
真失败反而看不出来。

这一类「修了一半」比不修更糟：它把一个静默失败换成了一个持续误报。
所以本组用例的第一条就是钉住**返回契约本身**。

## 第二处：联锁部分丢弃

`interlocks.yaml` 里 12 条规则、5 条写错（缺 `condition` / 缩进吃掉
`action`），现场看到的是「已加载 7 条」，**没有任何地方提示还有 5 条被丢了**。
那 5 条对应的设备保护等于不存在，而系统显示「联锁正常」。
"""

import logging
import threading
from unittest.mock import MagicMock

import pytest

from 采集层.data_collector import DataCollector


# ---------------------------------------------------------------------------
# 1. 采集任务启动的返回契约
# ---------------------------------------------------------------------------

@pytest.fixture
def collector():
    dm = MagicMock()
    dm.get_client.return_value = MagicMock(connected=True)
    dm.get_device_status.return_value = {}
    c = DataCollector(dm, MagicMock())
    c.running = True
    yield c
    c.running = False
    with c._tasks_lock:
        for t in c.tasks.values():
            t.cancel()
        c.tasks.clear()
    c._pool.shutdown(wait=False)


def _cfg(device_id='d1', interval=600):
    return {'id': device_id, 'protocol': 'modbus_tcp',
            'collection_interval': interval, 'enabled': True}


def test_start_device_collection_returns_true_when_armed(collector):
    """正常启动 → True，且定时器真的挂上了。

    没有这条，「恒 None」这个契约缺陷会一直藏着 ——
    因为 `if not None:` 恒真，任何"失败"判断都表现为"永远失败"。
    """
    assert collector._start_device_collection('d1', _cfg()) is True
    assert 'd1' in collector.tasks


def test_start_device_collection_returns_false_when_not_running(collector):
    """采集器没在跑时挂定时器是**假启动** —— 必须返回 False。

    `collect_task` 第一行就 `if not self.running: return`，定时器不会重排，
    这台设备永远不采集，而调用方以为起来了。
    """
    collector.running = False
    assert collector._start_device_collection('d1', _cfg()) is False
    assert 'd1' not in collector.tasks, (
        '采集器未运行时仍挂了定时器 —— 这台设备会永远沉默')


def test_start_device_collection_returns_false_on_timer_error(collector, monkeypatch):
    """定时器创建失败（线程资源耗尽）→ False，且不把异常抛给调用方。

    现场几十台设备 + 频繁增删会走到这里。异常若冒到 `start_all()`，
    一台设备就能把整轮启动打断，其余设备全部起不来。
    """
    class _Boom(threading.Timer):
        def start(self):
            raise RuntimeError('can\'t start new thread')

    monkeypatch.setattr(threading, 'Timer', _Boom)
    assert collector._start_device_collection('d1', _cfg()) is False


def test_start_device_task_reports_not_running(collector):
    """`start_device_task` 也要如实返回 False（接口层据此提示用户）。"""
    collector.running = False
    assert collector.start_device_task('d1', _cfg()) is False


def test_start_device_task_idempotent_is_success(collector):
    """任务已存在 = 幂等成功，不能返回 False（否则"重复添加"被误报失败）。"""
    assert collector.start_device_task('d1', _cfg()) is True
    assert collector.start_device_task('d1', _cfg()) is True
    assert 'd1' in collector.tasks


# ---------------------------------------------------------------------------
# 2. 接口层：采集没起来必须说清楚
# ---------------------------------------------------------------------------

def test_api_helper_reports_collection_failure(monkeypatch):
    """添加设备后采集没起来，接口层必须能拿到 False。"""
    from flask import Flask

    from 展示层.api import api_devices

    app = Flask(__name__)
    dc = MagicMock()
    dc.start_device_task.return_value = False
    dm = MagicMock()
    dm.get_all_devices.return_value = {'d1': _cfg()}
    app.data_collector = dc
    app.device_manager = dm

    with app.app_context():
        assert api_devices._start_device_collection('d1') is False


def test_api_helper_ok_when_collection_started():
    from flask import Flask

    from 展示层.api import api_devices

    app = Flask(__name__)
    app.data_collector = MagicMock()
    app.data_collector.start_device_task.return_value = True
    app.device_manager = MagicMock()
    app.device_manager.get_all_devices.return_value = {'d1': _cfg()}

    with app.app_context():
        assert api_devices._start_device_collection('d1') is True


def test_api_helper_without_collector_is_not_a_failure():
    """采集器未装配（纯配置管理 / 单测）不算失败。"""
    from flask import Flask

    from 展示层.api import api_devices

    app = Flask(__name__)          # 不设 data_collector
    app.device_manager = MagicMock()

    with app.app_context():
        assert api_devices._start_device_collection('d1') is True


# ---------------------------------------------------------------------------
# 3. 联锁加载摘要：部分丢弃必须看得见
# ---------------------------------------------------------------------------

def _dc(rules):
    from 智能层.device_control import DeviceControlSafety

    return DeviceControlSafety(MagicMock(), config={'interlocks': rules})


def _rule(rid, ok=True):
    r = {
        'id': rid,
        'name': rid,
        'condition': {'type': 'threshold', 'device_id': 'd1',
                      'register': 'temp', 'operator': '>', 'value': 90},
        'action': {'type': 'write_register', 'device_id': 'd1',
                   'register': 'status', 'value': 0},
    }
    if not ok:
        del r['action']           # 典型的手写错误：action 被缩进吃掉
    return r


def test_partial_load_is_reported(caplog):
    """3 条声明、1 条非法 → 摘要必须写明 rejected=1，且日志是 WARNING。"""
    with caplog.at_level(logging.WARNING):
        dc = _dc([_rule('ok1'), _rule('bad', ok=False), _rule('ok2')])

    summary = dc.get_interlock_load_summary()
    assert summary['declared'] == 3
    assert summary['loaded'] == 2
    assert summary['rejected'] == 1
    assert summary['loaded_once'] is True

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any('部分丢弃' in r.getMessage() for r in warnings), (
        '联锁被丢弃却没有 WARNING —— 现场只会看到「已加载 2 条」，'
        '以为配置全生效了')
    assert set(dc.get_interlock_status()) == {'ok1', 'ok2'}


def test_full_load_has_no_rejected(caplog):
    """反向对照：全部合法时 rejected=0，且不许出现"部分丢弃"告警。"""
    with caplog.at_level(logging.WARNING):
        dc = _dc([_rule('a'), _rule('b')])

    summary = dc.get_interlock_load_summary()
    assert (summary['declared'], summary['loaded'], summary['rejected']) == (2, 2, 0)
    assert not any('部分丢弃' in r.getMessage() for r in caplog.records)


def test_empty_config_summary_is_distinguishable():
    """「没配任何规则」与「配了但全被丢弃」必须可区分。"""
    from 智能层.device_control import DeviceControlSafety

    dc = DeviceControlSafety(MagicMock(), config={})
    # 避免读到仓库里真实的 配置/interlocks.yaml
    dc._load_preset_interlocks = lambda: None
    dc._interlock_rules.clear()
    dc._interlock_load_summary = None

    summary = dc.get_interlock_load_summary()
    assert summary['loaded_once'] is False
    assert summary['empty'] is True
    assert summary['declared'] == 0


def test_summary_does_not_pollute_status_contract():
    """摘要不许塞进 `get_interlock_status()` 的返回值。

    那个方法的契约是 `{rule_id: 规则状态}`；多一个魔法 key 会让
    `get_interlock_status() == {}` 的调用方变红、让遍历 key 的前端
    把摘要渲染成一条联锁规则。
    """
    dc = _dc([_rule('a')])
    status = dc.get_interlock_status()
    assert list(status) == ['a']
    assert all(not k.startswith('_') for k in status)


# ---------------------------------------------------------------------------
# 4. 配置热重载同步：启动失败的设备不许算进「已启动」
# ---------------------------------------------------------------------------

def test_sync_reports_failed_starts(collector):
    """`sync_device_tasks()` 必须区分「真的起来了」和「尝试过」。

    原实现把 `to_start` 整个当结果返回，`start_device_task` 的成败根本没看 ——
    配置热重载后接口回「已启动 3 台」，其中一台的定时器压根没挂上，
    那台设备从此没有数据，而运维刚看到「同步成功」。
    """
    collector.device_manager.get_all_devices.return_value = {
        'd_new': _cfg('d_new'),
    }
    # 让启动失败（模拟线程资源耗尽 / 采集器状态异常）
    collector.start_device_task = MagicMock(return_value=False)

    sync = collector.sync_device_tasks()

    assert sync['started'] == [], '启动失败却记进了 started'
    assert sync['failed'] == ['d_new']


def test_sync_counts_real_success(collector):
    """反向对照：真的起来了才算 started。"""
    collector.device_manager.get_all_devices.return_value = {
        'd_new': _cfg('d_new'),
    }

    sync = collector.sync_device_tasks()

    assert sync['started'] == ['d_new']
    assert sync['failed'] == []
    assert 'd_new' in collector.tasks
