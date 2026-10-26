# -*- coding: utf-8 -*-
"""设备配置文件的**自动**热重载（round 285）。

背景
----
round 283 给了 `POST /api/devices/reload`，round 284 给它加了 fail-closed 守卫。
但那仍然要**人去点一下**。现场改设备清单（加一台仪表、停一台旧设备）本质是
"改配置 → 生效"，不该要求运维记得调接口、更不该重启进程。

为什么现在才敢自动
------------------
自动重载的前提是"坏输入不会造成破坏"。round 284 之前不是：一次编辑器
中间态保存就会把整张设备表清空、停掉全部采集。现在 `reload_config()` 会
拒绝解析失败与"解析成 0 台"，所以**最坏情况只是这次改动不生效**。

本文件钉住的不变量
------------------
1. 首次调用只**建立 mtime 基线**，不重载（服务启动时配置已加载过）；
2. 同一个 mtime 只处理一次 —— 坏文件不会每 10 秒刷一遍错误日志；
3. 重载**被拒绝时绝不同步采集任务**（差集会是"全部移除"）；
4. 任何异常都不能让维护任务挂掉（它跑在后台定时器里）。
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

VALID_YAML = """devices:
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
"""

TWO_DEVICES_YAML = """devices:
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

BROKEN_YAML = """devices:
  - id: dev_a
    name: 设备A
    protocol: modbus_tcp
    host: 127.0.0.1
     port: 502        # ← 缩进错一格
"""


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / 'devices.yaml'
    p.write_text(VALID_YAML, encoding='utf-8')
    return p


@pytest.fixture
def mgr(cfg):
    from 采集层.device_manager import DeviceManager

    m = DeviceManager(config_path=str(cfg))
    assert set(m.get_all_devices()) == {'dev_a'}
    return m


def _write(cfg, text, mtime):
    """写文件并把 mtime 钉成确定值（避免文件系统时间粒度影响判断）。"""
    cfg.write_text(text, encoding='utf-8')
    os.utime(cfg, (mtime, mtime))


def _collector():
    c = MagicMock()
    c.sync_device_tasks.return_value = {
        'started': ['dev_b'], 'stopped': [], 'unchanged': ['dev_a'],
        'skipped': False}
    return c


# ---------------------------------------------------------------------------
# 基线 / 无变化
# ---------------------------------------------------------------------------

def test_first_call_only_establishes_baseline(cfg, mgr):
    from core.device_config_watch import watch_once

    dc = _collector()
    state = {}
    result = watch_once(mgr, dc, state=state)

    assert result['changed'] is False
    assert result['reloaded'] is False
    assert state['mtime'] == cfg.stat().st_mtime
    assert not dc.sync_device_tasks.called, '首次调用不该同步采集任务'


def test_unchanged_mtime_does_nothing(cfg, mgr):
    from core.device_config_watch import watch_once

    dc = _collector()
    state = {}
    watch_once(mgr, dc, state=state)          # 建基线
    dc.sync_device_tasks.reset_mock()

    result = watch_once(mgr, dc, state=state)

    assert result['changed'] is False
    assert not dc.sync_device_tasks.called


def test_missing_file_is_not_an_error(cfg, mgr, tmp_path):
    from core.device_config_watch import watch_once

    mgr.config_path = str(tmp_path / 'nope.yaml')
    result = watch_once(mgr, _collector(), state={})

    assert result['changed'] is False
    assert '不存在' in result['reason']


# ---------------------------------------------------------------------------
# 变化 → 重载 + 同步
# ---------------------------------------------------------------------------

def test_changed_mtime_reloads_and_syncs(cfg, mgr):
    from core.device_config_watch import watch_once

    dc = _collector()
    state = {}
    watch_once(mgr, dc, state=state)          # 建基线
    dc.sync_device_tasks.reset_mock()

    _write(cfg, TWO_DEVICES_YAML, cfg.stat().st_mtime + 10)
    result = watch_once(mgr, dc, state=state)

    assert result['changed'] is True
    assert result['reloaded'] is True
    assert result['rejected'] is None
    assert result['diff']['added'] == ['dev_b']
    assert 'dev_b' in mgr.get_all_devices(), '配置表没有被真的替换'
    assert dc.sync_device_tasks.called, (
        '重载后没有同步采集任务 —— 新增设备永远不采、删掉的设备一直重试')


def test_rejected_reload_never_syncs_tasks(cfg, mgr):
    """**核心**：被 fail-closed 守卫拒绝时，绝不能去同步采集任务。

    否则差集是"全部移除"，现场所有设备会被停止采集。
    """
    from core.device_config_watch import watch_once

    dc = _collector()
    state = {}
    watch_once(mgr, dc, state=state)
    dc.sync_device_tasks.reset_mock()

    _write(cfg, BROKEN_YAML, cfg.stat().st_mtime + 10)
    result = watch_once(mgr, dc, state=state)

    assert result['changed'] is True
    assert result['reloaded'] is False
    assert result['rejected'], '写坏的配置没有被拒绝'
    assert not dc.sync_device_tasks.called, (
        '配置被拒绝了还去同步采集任务 —— 差集是"全部移除"，现场会全部停采')
    assert set(mgr.get_all_devices()) == {'dev_a'}, '原设备表被改坏了'


def test_empty_file_is_rejected(cfg, mgr):
    from core.device_config_watch import watch_once

    dc = _collector()
    state = {}
    watch_once(mgr, dc, state=state)
    dc.sync_device_tasks.reset_mock()

    _write(cfg, '', cfg.stat().st_mtime + 10)
    result = watch_once(mgr, dc, state=state)

    assert result['rejected']
    assert not dc.sync_device_tasks.called
    assert set(mgr.get_all_devices()) == {'dev_a'}


def test_broken_then_fixed_is_retried(cfg, mgr):
    """坏文件处理一次后基线已更新；修好（mtime 再变）必须能重试成功。"""
    from core.device_config_watch import watch_once

    dc = _collector()
    state = {}
    watch_once(mgr, dc, state=state)

    _write(cfg, BROKEN_YAML, cfg.stat().st_mtime + 10)
    assert watch_once(mgr, dc, state=state)['rejected']

    _write(cfg, TWO_DEVICES_YAML, cfg.stat().st_mtime + 20)
    dc.sync_device_tasks.reset_mock()
    result = watch_once(mgr, dc, state=state)

    assert result['reloaded'] is True, '修好之后没有重试'
    assert dc.sync_device_tasks.called
    assert 'dev_b' in mgr.get_all_devices()


def test_same_mtime_is_not_retried(cfg, mgr):
    """同一个坏文件不该每轮都重试并刷日志（基线在拒绝时也要推进）。"""
    from core.device_config_watch import watch_once

    dc = _collector()
    state = {}
    watch_once(mgr, dc, state=state)

    broken_mtime = cfg.stat().st_mtime + 10
    _write(cfg, BROKEN_YAML, broken_mtime)
    assert watch_once(mgr, dc, state=state)['rejected']

    result = watch_once(mgr, dc, state=state)      # 同一 mtime
    assert result['changed'] is False, '同一个坏文件被反复处理了'


# ---------------------------------------------------------------------------
# 异常不许把维护任务弄挂
# ---------------------------------------------------------------------------

def test_reload_exception_is_contained(cfg, mgr):
    from core.device_config_watch import watch_once

    state = {}
    watch_once(mgr, _collector(), state=state)
    mgr.reload_config = MagicMock(side_effect=RuntimeError('炸了'))

    _write(cfg, TWO_DEVICES_YAML, cfg.stat().st_mtime + 10)
    result = watch_once(mgr, _collector(), state=state)

    assert result['reloaded'] is False
    assert '炸了' in (result['rejected'] or '') or '炸了' in result['reason']


def test_sync_exception_is_contained(cfg, mgr):
    from core.device_config_watch import watch_once

    dc = _collector()
    dc.sync_device_tasks.side_effect = RuntimeError('线程池已关闭')
    state = {}
    watch_once(mgr, dc, state=state)

    _write(cfg, TWO_DEVICES_YAML, cfg.stat().st_mtime + 10)
    result = watch_once(mgr, dc, state=state)

    assert result['reloaded'] is True, '配置表已经换掉了，结果要如实反映'
    assert '同步失败' in result['reason']


# ---------------------------------------------------------------------------
# 维护任务接线
# ---------------------------------------------------------------------------

def test_maintenance_task_is_registered_and_wired():
    from core.maintenance import DEFAULT_INTERVALS, build_maintenance_tasks

    assert 'device_config_watch' in DEFAULT_INTERVALS
    assert DEFAULT_INTERVALS['device_config_watch'] <= 30, (
        '热重载间隔太大（>30s），改完配置要等半天才生效')

    dm = MagicMock()
    dm.config_path = '配置/devices.yaml'
    dc = MagicMock()
    dc.sync_device_tasks.return_value = {
        'started': [], 'stopped': [], 'unchanged': [], 'skipped': False}

    tasks = build_maintenance_tasks(database=None, auth_manager=None,
                                    device_manager=dm, data_collector=dc)
    names = [t[0] for t in tasks]
    assert 'device_config_watch' in names, f'任务没注册: {names}'

    func = dict((t[0], t[3]) for t in tasks)['device_config_watch']
    func()      # 首次只建基线
    func()      # 再跑一次也不该炸
    assert not dc.sync_device_tasks.called


def test_task_skipped_without_device_manager():
    from core.maintenance import build_maintenance_tasks

    names = [t[0] for t in build_maintenance_tasks(database=None,
                                                   auth_manager=None)]
    assert 'device_config_watch' not in names, (
        '没有 device_manager 时不该注册这个任务')
