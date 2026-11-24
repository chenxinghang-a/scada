# -*- coding: utf-8 -*-
"""device_manager round 381：双栈路径隔离 / 热重载重建客户端 / 模式切换原子性。

三条 P0：

1. 默认设备清单两套栈共用 ``配置/devices.yaml`` —— 模拟层（5000）与真实层
   （5001）同目录启动会互相写配置。改为按模式落到
   ``devices_simulated.yaml`` / ``devices_real.yaml``，旧路径存在时兼容并告警。
2. ``reload_config()`` 只换配置字典，已连上的 client 仍指向旧 host/port/协议。
   改为对比连接签名，变化的断开重建、没变的保持连接。
3. ``switch_simulation_mode()`` 先建池后改标志，中间窗口并发采集会从新池拿到
   旧模式客户端。改为标志与池在同一把锁内原子替换，并给客户端打模式标记。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

import paths


def _write_devices(path: Path, devices: list[dict]) -> None:
    path.write_text(
        yaml.safe_dump({'devices': devices}, allow_unicode=True),
        encoding='utf-8',
    )


def _dev(dev_id: str = 'dev1', host: str = '127.0.0.1', port: int = 502,
         protocol: str = 'modbus_tcp', name: str = '设备1',
         enabled: bool = True) -> dict:
    return {
        'id': dev_id,
        'name': name,
        'protocol': protocol,
        'host': host,
        'port': port,
        'slave_id': 1,
        'enabled': enabled,
        'registers': [
            {'name': 'temp', 'address': 0, 'data_type': 'uint16'},
        ],
    }


@pytest.fixture
def manager(tmp_path):
    from 采集层.device_manager import DeviceManager

    cfg = tmp_path / 'devices.yaml'
    _write_devices(cfg, [_dev()])
    return DeviceManager(config_path=str(cfg), simulation_mode=True,
                         use_enhanced_simulation=False)


class _TrackedClient:
    """只记录断开次数的假客户端（不涉及真实连接）。"""

    def __init__(self):
        self.connected = True
        self.disconnect_count = 0

    def disconnect(self):
        self.connected = False
        self.disconnect_count += 1


# ---------------------------------------------------------------------------
# 1. 双栈路径隔离
# ---------------------------------------------------------------------------

class TestDualStackPaths:

    def test_default_config_path_follows_mode(self, tmp_path, monkeypatch):
        monkeypatch.delenv('SCADA_CONFIG_DIR', raising=False)
        monkeypatch.setattr(paths, 'BASE_DIR', tmp_path)
        (tmp_path / '配置').mkdir()

        from 采集层.device_manager import DeviceManager

        sim = DeviceManager(simulation_mode=True, use_enhanced_simulation=False)
        real = DeviceManager(simulation_mode=False, use_enhanced_simulation=False)

        assert Path(sim.config_path).name == 'devices_simulated.yaml'
        assert Path(real.config_path).name == 'devices_real.yaml'
        assert sim.config_path != real.config_path, (
            '模拟/真实两层仍共用同一份设备清单 —— 双栈启动会串台')

    def test_db_path_follows_mode(self, tmp_path, monkeypatch):
        monkeypatch.delenv('SCADA_CONFIG_DIR', raising=False)
        monkeypatch.setattr(paths, 'BASE_DIR', tmp_path)
        (tmp_path / '配置').mkdir()

        from 采集层.device_manager import DeviceManager

        sim = DeviceManager(simulation_mode=True, use_enhanced_simulation=False)
        assert Path(sim.db_path).name == 'scada_simulated.db'

        sim.simulation_mode = False
        assert Path(sim.db_path).name == 'scada_real.db'

    def test_legacy_path_kept_with_warning(self, tmp_path, monkeypatch, caplog):
        """旧部署只有 ``配置/devices.yaml`` 时必须继续用它，并留下告警。"""
        monkeypatch.delenv('SCADA_CONFIG_DIR', raising=False)
        monkeypatch.setattr(paths, 'BASE_DIR', tmp_path)
        cfg_dir = tmp_path / '配置'
        cfg_dir.mkdir()
        _write_devices(cfg_dir / 'devices.yaml', [_dev()])
        _write_devices(cfg_dir / 'devices_simulated.yaml', [_dev()])

        from 采集层.device_manager import DeviceManager

        with caplog.at_level(logging.WARNING, logger='采集层.device_manager'):
            dm = DeviceManager(simulation_mode=True, use_enhanced_simulation=False)

        assert Path(dm.config_path).name == 'devices.yaml', (
            '升级后静默换掉了现网设备目录')
        assert any('旧版共享路径' in r.getMessage() for r in caplog.records), (
            '沿用了旧路径却没有 warning')


# ---------------------------------------------------------------------------
# 2. 热重载：连接参数变了才重建
# ---------------------------------------------------------------------------

class TestReloadRebuildsChangedClient:

    def test_host_change_disconnects_old_client(self, manager):
        old = _TrackedClient()
        manager.clients['dev1'] = old

        cfg = Path(manager.config_path)
        _write_devices(cfg, [_dev(host='10.0.0.2')])
        diff = manager.reload_config()

        assert diff['rejected'] is None
        assert diff['changed'] == ['dev1']
        assert old.disconnect_count == 1, '改了 host 却没断开旧客户端'
        assert 'dev1' not in manager.clients, '旧客户端仍留在缓存里'

        rebuilt = manager.get_client('dev1')
        assert rebuilt is not old
        assert rebuilt.config['host'] == '10.0.0.2', (
            '重建后的客户端仍指向旧地址')

    def test_protocol_change_disconnects_old_client(self, manager):
        old = _TrackedClient()
        manager.clients['dev1'] = old

        cfg = Path(manager.config_path)
        _write_devices(cfg, [_dev(protocol='opcua')])
        diff = manager.reload_config()

        assert diff['changed'] == ['dev1']
        assert old.disconnect_count == 1

    def test_name_only_change_keeps_connection(self, manager):
        """只改展示名不该重连 —— 否则每次改名都会抖一下采集。"""
        old = _TrackedClient()
        manager.clients['dev1'] = old

        cfg = Path(manager.config_path)
        _write_devices(cfg, [_dev(name='改名后的设备')])
        diff = manager.reload_config()

        assert diff['changed'] == []
        assert old.disconnect_count == 0, '只改设备名却断开了连接'
        assert manager.clients['dev1'] is old


# ---------------------------------------------------------------------------
# 3. 模式热切换：标志与池必须一致
# ---------------------------------------------------------------------------

class TestModeSwitchAtomicity:

    def test_flag_changes_before_new_pool_is_built(self, manager, monkeypatch):
        """新池的工厂读 ``self.simulation_mode`` —— 建池时必须已是新模式。"""
        seen = {}
        orig = manager._build_connection_pool

        def hooked():
            seen['mode_when_building'] = manager.simulation_mode
            # 在建池回调里（模拟并发采集线程）取一次客户端：不能死锁，
            # 也不能拿到旧模式的客户端。
            seen['client_during_build'] = manager.get_client('dev1')
            return orig()

        monkeypatch.setattr(manager, '_build_connection_pool', hooked)
        # 不真的去连设备（否则会等真实网络超时）
        monkeypatch.setattr(manager, 'connect_all', lambda *a, **k: {})

        result = manager.switch_simulation_mode(False)

        assert result['success'] is True
        assert seen['mode_when_building'] is False, (
            '连接池在标志切换前创建 —— 并发采集会从新池拿到旧模式客户端')
        assert type(seen['client_during_build']).__name__ != 'SimulatedModbusClient'
        assert manager.simulation_mode is False

    def test_switch_drops_old_mode_clients(self, manager, monkeypatch):
        old = manager.get_client('dev1')
        assert type(old).__name__ == 'SimulatedModbusClient'

        monkeypatch.setattr(manager, 'connect_all', lambda *a, **k: {})
        manager.switch_simulation_mode(False)

        assert 'dev1' not in manager.clients, (
            '切换后旧模式客户端仍在缓存，下一次采集会继续用它')

    def test_get_client_never_returns_client_of_other_mode(self, manager):
        """模拟「标志已切、池还没换」的窗口：缓存里的旧模式客户端必须作废。"""
        sim_client = manager.get_client('dev1')

        manager.simulation_mode = False      # 直接改标志，模拟并发窗口
        client = manager.get_client('dev1')

        assert client is not sim_client
        assert type(client).__name__ != 'SimulatedModbusClient', (
            '标志已是真实模式，却仍返回模拟客户端')
        assert sim_client.connected is False, '被丢弃的旧客户端没有断开'

    def test_switch_stops_and_restores_reconnect_loop(self, manager, monkeypatch):
        manager.start_reconnect_loop(interval=60)
        assert manager._reconnect_running is True

        monkeypatch.setattr(manager, 'connect_all', lambda *a, **k: {})
        result = manager.switch_simulation_mode(False)

        assert result['reconnect_loop_restored'] is True
        assert manager._reconnect_running is True
        manager.disconnect_all()
