"""三份设备管理器的回滚语义必须一致（追溯表 SF-07）。

## 背景

仓库里套着**三份**设备管理器，各自实现了 `add_device` / `remove_device`：

    采集层/device_manager.py            DeviceManager        （测试与遗留路径）
    采集层/real_device_manager.py       RealDeviceManager    （--real）
    采集层/simulated_device_manager.py  SimulatedDeviceManager（默认模拟 / --sim-live）

追溯表 SF-07 一直标 partial，note 写着「real/sim/device_manager 三份实现回滚语义
不完全一致，需统一后再判定」。本文件把「一致」写成可执行的断言。

## 不变量（用户口径）

**落盘失败必须回滚内存**，且回滚的是三样：设备表 `devices`、客户端 `clients`、
原始表 `_raw_devices`。

只回滚 `devices` 会留下「设备在表里、但连接已断」的半死状态 ——
重连循环救不回来，采集永远拿不到数据，而界面上设备显示正常。

### 关于 `_raw_devices`（查过之后确认不需要回滚）

只有 `real` / `simulated` 两份有这个表，`device_manager` 没有。它在
`add_device` 里是**落盘成功之后**才更新的，落盘失败时压根没被改到；
删除路径完全不碰它。而 `_save_config` 走
`restore_device_placeholders(runtime_devices, raw_devices)` —— **按内存表遍历**、
按 id 从原始表里取对应条目还原 `${VAR}`，所以删掉的设备不会因为原始表里
还留着旧条目而被写回配置文件。

结论：`_raw_devices` 不需要回滚，本文件不断言它（避免写出一个看起来
更严格、实际在钉错语义的用例）。

## 已知差异（本文件钉住的）

1. `DeviceManager.remove_device` 落盘失败时只把 `devices[device_id]` 放回去，
   `clients[device_id]` 直接丢了；另外两份都恢复了。
2. `DeviceManager.disconnect_device` 会 `clients.pop()`，另两份只 `get` 不 pop
   —— 所以「先 disconnect 再取 client」永远是 None。
3. `SimulatedDeviceManager.add_device` 算了 `previous` 却没用，覆盖场景落盘失败
   时无条件 `pop`，旧配置直接丢失。
4. 「删不存在的设备」返回值：`real` 返回 False，另两份返回 True（幂等）。
   两份既有用例都断言 True，real 是唯一偏离。
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from 采集层.device_manager import DeviceManager
from 采集层.real_device_manager import RealDeviceManager
from 采集层.simulated_device_manager import SimulatedDeviceManager


def _build_dm(path: str) -> DeviceManager:
    return DeviceManager(config_path=path, simulation_mode=True,
                         use_enhanced_simulation=False)


def _build_real(path: str) -> RealDeviceManager:
    return RealDeviceManager(config_path=path)


def _build_sim(path: str) -> SimulatedDeviceManager:
    return SimulatedDeviceManager(config_path=path)


MANAGERS = [
    pytest.param(_build_dm, id='DeviceManager'),
    pytest.param(_build_real, id='RealDeviceManager'),
    pytest.param(_build_sim, id='SimulatedDeviceManager'),
]

DEV = {
    'id': 'rollback_probe_01',
    'name': '回滚探针',
    'protocol': 'modbus_tcp',
    'host': '127.0.0.1',
    'port': 1502,
    'enabled': False,          # 不真的去连
    'registers': [],
}


@pytest.fixture()
def mgr(request, tmp_path):
    path = str(tmp_path / 'devices_probe.yaml')
    m = request.param(path)
    yield m
    try:
        m.disconnect_device(DEV['id'])
    except Exception as e:  # noqa: BLE001
        print(f'  [warn] 清理连接失败（忽略）: {type(e).__name__}: {e}')


def _seed(m, dev_id=DEV['id']):
    """直接把设备塞进内存表（不落盘），再挂一个假客户端。"""
    with m._lock:
        m.devices[dev_id] = dict(DEV)
        m.clients[dev_id] = MagicMock(name=f'client-{dev_id}')


@pytest.mark.parametrize('mgr', MANAGERS, indirect=True)
def test_remove_rollback_restores_devices(mgr):
    """落盘失败时设备必须回到内存表，并如实返回 False。"""
    _seed(mgr)
    mgr._save_config = lambda: False
    assert mgr.remove_device(DEV['id']) is False
    assert DEV['id'] in mgr.devices, '落盘失败后设备没有回到 devices'
    assert mgr.devices[DEV['id']]['name'] == DEV['name']


@pytest.mark.parametrize('mgr', MANAGERS, indirect=True)
def test_remove_rollback_restores_clients(mgr):
    """落盘失败时**客户端也必须一起回来**。

    只还原 devices 会得到「设备在表里、但连接已断」的半死状态：
    界面上设备正常，采集却永远拿不到数据，重连循环也不会重建它
    （它认为客户端存在）。这是 SF-07 记录的那条差异。
    """
    _seed(mgr)
    before = mgr.clients[DEV['id']]
    mgr._save_config = lambda: False
    assert mgr.remove_device(DEV['id']) is False
    assert DEV['id'] in mgr.clients, (
        '落盘失败后客户端没恢复 —— 设备在表里但没有 client，'
        '采集会永远拿不到数据（半死状态）'
    )
    assert mgr.clients[DEV['id']] is before, '恢复的应是原来那个客户端实例'


@pytest.mark.parametrize('mgr', MANAGERS, indirect=True)
def test_add_rollback_removes_new_device(mgr):
    """新增落盘失败：设备必须从内存表消失，客户端也不留。"""
    mgr._save_config = lambda: False
    assert mgr.add_device(dict(DEV)) is False
    assert DEV['id'] not in mgr.devices, '落盘失败后新设备仍留在 devices'
    assert DEV['id'] not in mgr.clients


@pytest.mark.parametrize('mgr', MANAGERS, indirect=True)
def test_add_rollback_restores_previous_config_on_overwrite(mgr):
    """覆盖同名设备落盘失败：必须还原**旧配置**，不能一删了之。"""
    old = dict(DEV, name='旧名字')
    with mgr._lock:
        mgr.devices[DEV['id']] = old

    mgr._save_config = lambda: False
    assert mgr.add_device(dict(DEV, name='新名字')) is False

    assert DEV['id'] in mgr.devices, '覆盖失败时旧设备被弄丢了'
    assert mgr.devices[DEV['id']]['name'] == '旧名字', (
        '覆盖场景回滚应还原旧配置，实际留的是新配置'
    )


@pytest.mark.parametrize('mgr', MANAGERS, indirect=True)
def test_remove_missing_device_does_not_write_config(mgr):
    """删一台不存在的设备：不该产生写盘（无谓 IO）。"""
    calls = {'n': 0}

    def _counting_save():
        calls['n'] += 1
        return True

    mgr._save_config = _counting_save
    result = mgr.remove_device('__never_exists__')
    assert calls['n'] == 0, (
        f'删除不存在的设备触发了 {calls["n"]} 次落盘 —— '
        '配置没变化时不该写文件'
    )
    # 返回值必须是 True：三份实现统一走**幂等语义**（删不存在不算失败）。
    # 两份既有用例（test_device_manager / test_simulated_device_manager）
    # 也都断言 True，real 原来返回 False 是唯一的偏离。
    assert result is True, f'删不存在的设备应返回 True（幂等），实际 {result!r}'
