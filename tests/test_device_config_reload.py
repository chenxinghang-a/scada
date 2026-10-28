# -*- coding: utf-8 -*-
"""设备配置热重载：有方法没人能调，调了会留下孤儿采集任务（round 283）。

缺陷形态
--------
`DeviceManager.reload_config()` 一直存在（还带一段很详细的"原子替换"注释），
但：

1. **没有任何调用方** —— 全仓库只有测试在调。改 `配置/devices.yaml`
   只能重启进程，而这是 24/7 的现场系统；
2. 就算有人调它，它**只替换配置表** —— 采集器的定时器不会跟着变：

   * 配置里**删掉 / 禁用**的设备：定时器照旧每 N 秒跑一次，而
     `get_client()` 已经找不到它的配置 → 每次采集都失败 → 退避 →
     **无限重试**。日志刷 `设备 xxx 配置不存在`，还给一台不存在的设备
     累计失败计数、可能触发「设备离线」报警；
   * 配置里**新增**的设备：没有定时器，**永远不采集** ——
     界面上有配置、没有数据，看起来像设备坏了，实际是没人拉它。

判据
----
"配置生效" = **配置表 + 运行期任务**两件事都对齐。只改前者，等于让
配置与运行时**静默分叉**：没有任何报错，只表现为"设备一直采不到"。

修法
----
1. `DeviceManager.reload_config()` 返回差集 `{'added','removed','kept'}`；
2. 新增 `DataCollector.sync_device_tasks()`：按 `get_all_devices()`（只算
   `enabled`）与 `self.tasks` 做差集，缺的补、多的停；
3. 新增 `POST /api/devices/reload`：reload 配置 + 同步采集任务，
   两个差集都回给调用方（运维要看到"到底动了什么"）。

⚠️ 差集必须在**锁外**执行 start/stop：`start_device_task` 与
`remove_device_task` 自己也会拿 `_tasks_lock`，而它是**非可重入**的 Lock ——
持锁调用会把自己锁死，表现为整个采集器静默停摆。
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# DataCollector.sync_device_tasks：差集 + 委派
# ---------------------------------------------------------------------------

def _bare_collector(running=True):
    """只装配同步所需的字段（不起线程、不连设备）。"""
    from 采集层.data_collector import DataCollector

    c = DataCollector.__new__(DataCollector)
    c.tasks = {}
    c._tasks_lock = threading.Lock()
    c._tracking_lock = threading.Lock()
    c._cancelled_devices = set()
    c._last_values = {}
    c._last_times = {}
    c._circuit_state = {}
    c._failure_counts = {}
    c._device_intervals = {}
    c.stats = {}
    c._stats_lock = threading.Lock()
    c.running = running

    class _QueueStub:
        def qsize(self):
            return 0

    c.data_queue = _QueueStub()
    return c


def _fake_manager(devices):
    mgr = MagicMock()
    mgr.get_all_devices.return_value = devices
    return mgr


def _record_calls(c):
    """把 start/stop 换成记录器（真实现会起 Timer，测试里不该有副作用）。"""
    calls = {'started': [], 'stopped': []}

    def _start(device_id, config):
        calls['started'].append(device_id)
        c.tasks[device_id] = MagicMock()      # 模拟"已挂上定时器"
        # round 308：`start_device_task` 的契约改成返回 bool（是否真的挂上），
        # `sync_device_tasks` 据此区分 started / failed。这个 fake 既然真的
        # 挂上了定时器，就必须 `return True` —— 否则它自己会被记进 failed。
        return True

    def _stop(device_id):
        calls['stopped'].append(device_id)
        c.tasks.pop(device_id, None)
        c._cancelled_devices.add(device_id)

    c.start_device_task = _start
    c.remove_device_task = _stop
    return calls


def test_sync_starts_new_devices():
    c = _bare_collector()
    c.tasks = {'a': MagicMock()}
    c.device_manager = _fake_manager({
        'a': {'enabled': True},
        'c': {'enabled': True},       # 配置里新增
    })
    calls = _record_calls(c)

    result = c.sync_device_tasks()

    assert calls['started'] == ['c'], f'新增设备没被拉起: {calls}'
    assert result['started'] == ['c']
    assert result['unchanged'] == ['a']
    assert result['skipped'] is False


def test_sync_stops_removed_devices():
    c = _bare_collector()
    c.tasks = {'a': MagicMock(), 'b': MagicMock()}
    c.device_manager = _fake_manager({'a': {'enabled': True}})   # b 被删了
    calls = _record_calls(c)

    result = c.sync_device_tasks()

    assert calls['stopped'] == ['b'], (
        f'配置里已删除的设备还在采集（会无限重试 + 记失败计数）: {calls}')
    assert result['stopped'] == ['b']


def test_sync_stops_disabled_devices():
    """`enabled: false` 也是"不该采集" —— 否则运维关了它却还在被采集。"""
    c = _bare_collector()
    c.tasks = {'a': MagicMock(), 'b': MagicMock()}
    c.device_manager = _fake_manager({
        'a': {'enabled': True},
        'b': {'enabled': False},
    })
    calls = _record_calls(c)

    result = c.sync_device_tasks()

    assert calls['stopped'] == ['b']
    assert 'b' in result['stopped']


def test_sync_is_idempotent():
    c = _bare_collector()
    c.tasks = {'a': MagicMock()}
    c.device_manager = _fake_manager({'a': {'enabled': True}})
    calls = _record_calls(c)

    result = c.sync_device_tasks()

    assert calls == {'started': [], 'stopped': []}
    assert result['started'] == [] and result['stopped'] == []
    assert result['unchanged'] == ['a']


def test_sync_does_nothing_when_collector_stopped():
    """采集器没跑时不该偷偷拉起任务（启动流程自己会全量拉起）。"""
    c = _bare_collector(running=False)
    c.device_manager = _fake_manager({'a': {'enabled': True}})
    calls = _record_calls(c)

    result = c.sync_device_tasks()

    assert result['skipped'] is True
    assert calls == {'started': [], 'stopped': []}


def test_sync_does_not_hold_task_lock_while_calling_start_stop():
    """⚠️ 差集在锁内算，start/stop 必须在**锁外**调用。

    `_tasks_lock` 是非可重入 Lock，持锁调用 `start_device_task` 会自锁死
    （整个采集器静默停摆，且没有任何报错）。这里用"能否在回调里再拿一次锁"
    来验证：真实现里 start_device_task 第一件事就是拿这把锁。
    """
    c = _bare_collector()
    c.device_manager = _fake_manager({'c': {'enabled': True}})

    def _start(device_id, config):
        acquired = c._tasks_lock.acquire(timeout=1)
        try:
            assert acquired, (
                'sync_device_tasks 持着 _tasks_lock 调用了 start_device_task —— '
                '非可重入锁会把自己锁死')
        finally:
            if acquired:
                c._tasks_lock.release()
        return True       # round 308：契约是 bool（是否真的挂上）

    c.start_device_task = _start
    c.remove_device_task = lambda device_id: None

    c.sync_device_tasks()      # 不抛异常 / 不死锁即通过


# ---------------------------------------------------------------------------
# DeviceManager.reload_config：要返回差集
# ---------------------------------------------------------------------------

DEVICES_YAML = """devices:
  - id: dev_a
    name: 设备A
    protocol: modbus_tcp
    host: 127.0.0.1
    port: 502
    slave_id: 1
    enabled: true
    registers:
      - name: temp
        address: 0
        data_type: uint16
  - id: dev_b
    name: 设备B
    protocol: modbus_tcp
    host: 127.0.0.1
    port: 502
    slave_id: 1
    enabled: true
    registers:
      - name: temp
        address: 0
        data_type: uint16
"""

DEVICES_YAML_2 = """devices:
  - id: dev_b
    name: 设备B
    protocol: modbus_tcp
    host: 127.0.0.1
    port: 502
    slave_id: 1
    enabled: true
    registers:
      - name: temp
        address: 0
        data_type: uint16
  - id: dev_c
    name: 设备C
    protocol: modbus_tcp
    host: 127.0.0.1
    port: 502
    slave_id: 1
    enabled: true
    registers:
      - name: temp
        address: 0
        data_type: uint16
"""


def test_reload_config_returns_diff(tmp_path):
    from 采集层.device_manager import DeviceManager

    cfg = tmp_path / 'devices.yaml'
    cfg.write_text(DEVICES_YAML, encoding='utf-8')
    mgr = DeviceManager(config_path=str(cfg))
    assert 'dev_a' in mgr.get_all_devices()

    cfg.write_text(DEVICES_YAML_2, encoding='utf-8')
    diff = mgr.reload_config()

    assert diff['added'] == ['dev_c'], f'差集不对: {diff}'
    assert diff['removed'] == ['dev_a'], f'差集不对: {diff}'
    assert diff['kept'] == ['dev_b'], f'差集不对: {diff}'
    # 配置表本身也要真的换掉
    assert set(mgr.get_all_devices()) == {'dev_b', 'dev_c'}


# ---------------------------------------------------------------------------
# API：POST /api/devices/reload
# ---------------------------------------------------------------------------

def test_reload_endpoint_reloads_and_syncs(app, client, auth_headers):
    """接口必须**两步都做**：reload 配置 + 同步采集任务。"""
    app.device_manager = MagicMock()
    app.device_manager.reload_config.return_value = {
        'added': ['dev_c'], 'removed': ['dev_a'], 'kept': ['dev_b']}
    app.data_collector = MagicMock()
    app.data_collector.sync_device_tasks.return_value = {
        'started': ['dev_c'], 'stopped': ['dev_a'],
        'unchanged': ['dev_b'], 'skipped': False}

    resp = client.post('/api/devices/reload', headers=auth_headers)

    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body['success'] is True
    assert body['config_diff']['added'] == ['dev_c']
    assert body['task_sync']['started'] == ['dev_c']
    assert app.device_manager.reload_config.called, '没有重载配置表'
    assert app.data_collector.sync_device_tasks.called, (
        '只重载了配置表、没同步采集任务 —— 删掉的设备会一直重试采集，'
        '新增的设备永远不采')


def test_reload_endpoint_reports_broken_yaml(app, client, auth_headers):
    """配置文件语法坏了要回 400 并说清楚，不能静默 500。"""
    app.device_manager = MagicMock()
    app.device_manager.reload_config.side_effect = ValueError('yaml 解析失败')
    app.data_collector = MagicMock()

    resp = client.post('/api/devices/reload', headers=auth_headers)

    assert resp.status_code == 400
    body = resp.get_json()
    assert body['success'] is False
    assert 'yaml' in body['message'] or '重载失败' in body['message']


def test_reload_endpoint_reports_sync_failure(app, client, auth_headers):
    """配置已重载但同步失败：必须如实报，并带上配置差集。"""
    app.device_manager = MagicMock()
    app.device_manager.reload_config.return_value = {
        'added': ['dev_c'], 'removed': [], 'kept': []}
    app.data_collector = MagicMock()
    app.data_collector.sync_device_tasks.side_effect = RuntimeError('线程池已关闭')

    resp = client.post('/api/devices/reload', headers=auth_headers)

    assert resp.status_code == 500
    body = resp.get_json()
    assert body['success'] is False
    assert body['config_diff']['added'] == ['dev_c']
