# -*- coding: utf-8 -*-
"""round 312：设备删除事件的**第二条路径** —— 配置热重载。

## 缺口

round 310 给 9 个模块加了 `forget_device()`，并在 `api_devices.delete_device()`
里通过 `_forget_device_everywhere()` 调用。但那**只覆盖「运维在界面上删设备」**。

设备还会因为**配置文件热重载**消失：

    POST /api/devices/reload  →  DeviceManager.reload_config()
    core/device_config_watch  →  维护任务每 10s 检查 devices.yaml 的 mtime

两条都走 `reload_config()` 的差集（`removed`），**完全不经过 api_devices** ——
所以智能层状态照样残留：

    yaml 里删掉一台设备
    → 配置表没了、采集停了、界面也没了
    → 但 OEE / 能耗 / 振动 / 预测维护报表里它还在（`forget_device` 从没被调）
    → 重新加回同名设备时继承旧的连续失败计数 → 立刻判离线

## 修法

`core/device_lifecycle.py` 提供**进程级**的「设备已删除」事件：
`add_device_remove_listener()` / `notify_device_removed()`。

为什么用模块级注册表而不是 `DeviceManager` 的实例字段：三个设备管理器是
**平行实现**（`DeviceManager` / `RealDeviceManager` / `SimulatedDeviceManager`），
各有自己的 `remove_device()`。实例字段要在三个 `__init__` 里各初始化一次、
三处 `remove_device` 里各通知一次 —— 漏一个就是同一个 bug 换个类再来一遍
（round 302 的 `_save_config` 就是这么漏的）。模块级只有一份状态。

调用点仍是 4 处（3 个 `remove_device` + 1 个 `reload_config`），
本文件末尾的**源码守卫**保证一个都不能漏。
"""

from unittest.mock import MagicMock

import pytest

from core import device_lifecycle as dl


@pytest.fixture(autouse=True)
def _clean_listeners():
    """每个用例都从空注册表开始（生产里监听器只注册一次、永不撤销）。"""
    dl.clear_device_remove_listeners()
    yield
    dl.clear_device_remove_listeners()


# ---------------------------------------------------------------------------
# 1. 事件总线本身
# ---------------------------------------------------------------------------

def test_notify_calls_all_listeners():
    seen = []
    dl.add_device_remove_listener(lambda did: seen.append(('a', did)))
    dl.add_device_remove_listener(lambda did: seen.append(('b', did)))

    assert dl.notify_device_removed('d1') == {}
    assert seen == [('a', 'd1'), ('b', 'd1')]


def test_register_is_idempotent():
    """同一个函数注册两次只应生效一次（`create_app` 可能被调用多次）。"""
    calls = []

    def fn(did):
        calls.append(did)

    dl.add_device_remove_listener(fn)
    dl.add_device_remove_listener(fn)
    assert dl.get_device_remove_listener_count() == 1
    dl.notify_device_removed('d1')
    assert calls == ['d1']


def test_listener_error_is_swallowed_and_reported():
    """监听器抛异常不许影响删除结果，但必须被记下来。"""
    def boom(did):
        raise RuntimeError('清理炸了')

    dl.add_device_remove_listener(boom)
    failures = dl.notify_device_removed('d1')

    assert len(failures) == 1
    assert '清理异常' in next(iter(failures.values()))


def test_no_listener_is_not_an_error():
    assert dl.notify_device_removed('d1') == {}
    assert dl.get_device_remove_listener_count() == 0


# ---------------------------------------------------------------------------
# 2. 三条 remove_device 路径都会通知
# ---------------------------------------------------------------------------

def _sim(tmp_path):
    import yaml
    cfg = tmp_path / 'devices.yaml'
    cfg.write_text(yaml.safe_dump({'devices': [
        {'id': 'd1', 'name': 'x', 'protocol': 'modbus_tcp',
         'host': '127.0.0.1', 'port': 502, 'slave_id': 1,
         'enabled': True, 'registers': [
             {'name': 'temp', 'address': 0, 'data_type': 'uint16'}]},
    ]}, allow_unicode=True), encoding='utf-8')
    from 采集层.simulated_device_manager import SimulatedDeviceManager
    return SimulatedDeviceManager(config_path=str(cfg))


def test_simulated_remove_device_notifies(tmp_path):
    mgr = _sim(tmp_path)
    seen = []
    dl.add_device_remove_listener(lambda did: seen.append(did))

    assert mgr.remove_device('d1') is True
    assert seen == ['d1'], '模拟管理器删设备没有通知 —— 智能层状态会残留'


def test_remove_device_save_failure_does_not_notify(tmp_path, monkeypatch):
    """落盘失败会回滚设备 —— 那时**不能**通知（设备其实还在）。"""
    mgr = _sim(tmp_path)
    seen = []
    dl.add_device_remove_listener(lambda did: seen.append(did))
    monkeypatch.setattr(mgr, '_save_config', lambda: False)

    assert mgr.remove_device('d1') is False
    assert seen == [], '设备被回滚了却通知了清理 —— 一台仍在运行的设备会丢掉健康状态'


def test_real_remove_device_notifies(tmp_path):
    from 采集层.real_device_manager import RealDeviceManager

    mgr = RealDeviceManager(config_path=str(tmp_path / 'devices.yaml'))
    mgr.devices = {'d1': {'id': 'd1', 'protocol': 'modbus_tcp', 'enabled': True}}
    mgr.clients = {}
    mgr.disconnect_device = lambda did: None
    mgr._save_config = lambda: True

    seen = []
    dl.add_device_remove_listener(lambda did: seen.append(did))
    assert mgr.remove_device('d1') is True
    assert seen == ['d1']


# ---------------------------------------------------------------------------
# 3. 热重载路径（本轮的核心）
# ---------------------------------------------------------------------------

def test_reload_config_notifies_each_removed_device(tmp_path):
    """`reload_config()` 删掉的设备必须逐个通知。

    这条路径**不经过 `remove_device()`** —— 它只替换配置表，
    所以是本轮修的那个缺口。
    """
    import yaml

    from 采集层.device_manager import DeviceManager

    cfg = tmp_path / 'devices.yaml'
    cfg.write_text(yaml.safe_dump({'devices': [
        {'id': 'keep', 'protocol': 'modbus_tcp', 'enabled': True},
        {'id': 'gone', 'protocol': 'modbus_tcp', 'enabled': True},
    ]}, allow_unicode=True), encoding='utf-8')

    mgr = DeviceManager(config_path=str(cfg))
    mgr.disconnect_device = lambda did: None

    seen = []
    dl.add_device_remove_listener(lambda did: seen.append(did))

    # 从 yaml 里删掉 gone
    cfg.write_text(yaml.safe_dump({'devices': [
        {'id': 'keep', 'protocol': 'modbus_tcp', 'enabled': True},
    ]}, allow_unicode=True), encoding='utf-8')

    diff = mgr.reload_config()

    assert diff['removed'] == ['gone']
    assert seen == ['gone'], (
        'yaml 里删掉的设备没有通知清理 —— OEE/能耗报表里它还在，'
        '重新加回同名设备会继承旧状态')


def test_reload_config_no_change_does_not_notify(tmp_path):
    """反向对照：配置没变就不该有任何通知。"""
    import yaml

    from 采集层.device_manager import DeviceManager

    cfg = tmp_path / 'devices.yaml'
    cfg.write_text(yaml.safe_dump({'devices': [
        {'id': 'keep', 'protocol': 'modbus_tcp', 'enabled': True},
    ]}, allow_unicode=True), encoding='utf-8')

    mgr = DeviceManager(config_path=str(cfg))
    mgr.disconnect_device = lambda did: None

    seen = []
    dl.add_device_remove_listener(lambda did: seen.append(did))
    mgr.reload_config()
    assert seen == []


# ---------------------------------------------------------------------------
# 4. 闸门：四条删除路径一个都不能漏
# ---------------------------------------------------------------------------

#: 每条「设备真的消失」的路径都必须在落盘成功之后调用 `notify_device_removed`。
#: 新增删除路径（比如将来支持"批量删除"）时，这条闸门会提醒你补上通知。
_REQUIRED_CALL_SITES = {
    '采集层/device_manager.py': 2,          # remove_device + reload_config
    '采集层/real_device_manager.py': 1,     # remove_device
    '采集层/simulated_device_manager.py': 1,  # remove_device
}


def test_all_removal_paths_notify():
    """源码守卫：四条删除路径都要通知（数量对得上，不是"有这个字符串就行"）。"""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    problems = []
    for rel, expected in _REQUIRED_CALL_SITES.items():
        src = (repo / rel).read_text(encoding='utf-8')
        # 只数**带括号的调用**。`from core.device_lifecycle import
        # notify_device_removed` 那行没有括号，不会被误计 ——
        # 第一版多减了一次，结果把"3 个文件全漏"误报成"数量不对"。
        actual = src.count('notify_device_removed(')
        if actual != expected:
            problems.append(f'{rel}: 期望 {expected} 处调用，实际 {actual} 处')

    assert not problems, (
        '设备删除路径的通知数量不对 —— 少一处就意味着某条路径删了设备但没清状态：\n  '
        + '\n  '.join(problems))


def test_guard_detects_missing_notification(tmp_path):
    """反向对照：把某一处调用删掉，计数必须对不上（闸门不是恒绿）。"""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    src = (repo / '采集层/simulated_device_manager.py').read_text(encoding='utf-8')
    actual = src.count('notify_device_removed(')
    assert actual == 1
    # 人为"删掉"那一次调用后应为 0，与期望的 1 不符 → 闸门会红
    assert (actual - 1) != _REQUIRED_CALL_SITES['采集层/simulated_device_manager.py']
