# -*- coding: utf-8 -*-
"""设备配置「写盘失败却回成功」的回归测试（round 218）。

缺陷
----
`采集层/device_manager.py::_save_config` 吞掉异常、**返回 None**；
`展示层/api/api_devices.py::update_device` 又直接丢掉返回值、无条件回
「设备 X 更新成功」。

净效果：配置文件所在目录只读 / 磁盘满 / 文件被占用时 ——
运行时配置改了、界面提示成功、**重启后改动全部消失**，且现场没有任何迹象。
这是本项目反复出现的招牌病「静默假成功」在设备配置路径上的实例。

为什么现在才发现
----------------
同仓另外两个 `_save_config`（`报警层/alarm_manager.py`、`智能层/energy_manager.py`）
**早就改成返回 bool 并在调用处检查**，只有 `device_manager` 这一份漏了 ——
「同类实现口径不一致」本身就是缺陷温床。

本文件钉三件事：
  ① `_save_config()` 成功返回 True、失败返回 False（不再返回 None）；
  ② 失败时 PUT /api/devices/<id> **不再回 200 成功**，而是 500 + `persisted: false`；
  ③ 成功路径不受影响（防止「修假成功」修成「全都不成功」）。
"""

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# ① manager 层：返回值语义
# ---------------------------------------------------------------------------

def _bare_device_manager(tmp_path):
    """绕开 __init__ 的重活，只装配 _save_config 需要的属性。"""
    from 采集层.device_manager import DeviceManager

    mgr = DeviceManager.__new__(DeviceManager)
    import threading
    mgr._lock = threading.Lock()
    mgr.devices = {'d1': {'id': 'd1', 'protocol': 'modbus_tcp', 'name': 'old'}}
    mgr.config_path = str(tmp_path / 'devices.yaml')
    return mgr


def test_save_config_returns_true_on_success(tmp_path):
    mgr = _bare_device_manager(tmp_path)
    assert mgr._save_config() is True, "写盘成功时 _save_config 必须返回 True"
    assert (tmp_path / 'devices.yaml').exists(), "文件没生成"


def test_save_config_returns_false_on_failure(tmp_path, monkeypatch):
    """写盘失败必须返回 False（旧实现返回 None，调用方无从判断）。"""
    mgr = _bare_device_manager(tmp_path)

    import 采集层.device_manager as dm

    def boom(*_a, **_kw):
        raise OSError('disk full (模拟)')

    monkeypatch.setattr(dm.yaml, 'dump', boom)
    assert mgr._save_config() is False, (
        "写盘失败时 _save_config 仍返回真值 —— 调用方会把失败当成功"
    )


def test_save_config_is_atomic_no_tmp_left(tmp_path):
    """成功路径不该留下 .tmp 残骸（原子写的约定）。"""
    mgr = _bare_device_manager(tmp_path)
    mgr._save_config()
    leftovers = list(tmp_path.glob('*.tmp'))
    assert not leftovers, f"留下了临时文件: {leftovers}"


# ---------------------------------------------------------------------------
# ② API 层：写盘失败不许回 200
# ---------------------------------------------------------------------------

def test_update_device_reports_persistence_failure(client, auth_headers, app):
    app.device_manager.devices = {'dev1': {'protocol': 'modbus_tcp', 'name': 'old'}}
    app.device_manager._save_config = MagicMock(return_value=False)

    resp = client.put('/api/devices/dev1', json={'name': 'new_name'},
                      headers=auth_headers)

    assert resp.status_code == 500, (
        f"写盘失败却返回 {resp.status_code} —— 用户会以为改动已保存"
    )
    body = resp.get_json()
    assert body['success'] is False
    assert body.get('persisted') is False, "响应里没有明确标出「未持久化」"
    assert body.get('runtime_applied') is True, "应说明运行时已生效（避免用户重复操作）"


def test_update_device_still_succeeds_when_saved(client, auth_headers, app):
    """防止修假成功修成「全都不成功」。"""
    app.device_manager.devices = {'dev1': {'protocol': 'modbus_tcp', 'name': 'old'}}
    app.device_manager._save_config = MagicMock(return_value=True)

    resp = client.put('/api/devices/dev1', json={'name': 'new_name'},
                      headers=auth_headers)

    assert resp.status_code == 200
    assert resp.get_json()['success'] is True


def test_update_device_runtime_change_is_kept_on_failure(client, auth_headers, app):
    """写盘失败时，内存里的配置**仍然**要更新（不要为了报错把功能回滚掉）。"""
    app.device_manager.devices = {'dev1': {'protocol': 'modbus_tcp', 'name': 'old'}}
    app.device_manager._save_config = MagicMock(return_value=False)

    client.put('/api/devices/dev1', json={'name': 'new_name'}, headers=auth_headers)

    assert app.device_manager.devices['dev1']['name'] == 'new_name', (
        "写盘失败不应撤销运行时改动 —— 响应已明确告知「已生效但未持久化」"
    )
