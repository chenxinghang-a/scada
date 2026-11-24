"""真实设备管理器：连接/增删/状态路径的行为验证。

这些路径此前行覆盖率为 0（模块整体仅 49%），而它们正好对应现场最容易出事的动作：
连接失败后的退避、批量连接的超时与异常隔离、覆盖安装失败后的回滚、删设备失败回滚。
"""

import yaml
import pytest

from 采集层.real_device_manager import RealDeviceManager


def _device(dev_id, **kw):
    dev = {'id': dev_id, 'name': dev_id, 'protocol': 'modbus_tcp',
           'host': '127.0.0.1', 'port': 502, 'enabled': True,
           'registers': [{'name': 'temp', 'address': 0, 'data_type': 'uint16'}]}
    dev.update(kw)
    return dev


@pytest.fixture()
def manager(tmp_path):
    cfg = tmp_path / 'devices.yaml'
    cfg.write_text(yaml.safe_dump({'devices': [_device('d1'), _device('d2')]},
                                  allow_unicode=True), encoding='utf-8')
    mgr = RealDeviceManager(config_path=str(cfg))
    mgr._save_config = lambda: True
    return mgr


class _Client:
    def __init__(self, ok=True):
        self.ok = ok
        self.connected = False
        self.disconnected = 0

    def connect(self):
        self.connected = self.ok
        return self.ok

    def disconnect(self):
        self.connected = False
        self.disconnected += 1


# --------------------------------------------------------------- 连接
def test_connect_device_returns_false_while_backing_off(manager, monkeypatch):
    monkeypatch.setattr(manager, '_should_skip_connect', lambda _id: True)
    assert manager.connect_device('d1') is False


def test_connect_device_without_client(manager, monkeypatch):
    monkeypatch.setattr(manager, 'get_client', lambda _id: None)
    assert manager.connect_device('d1') is False


def test_connect_device_records_success_and_failure(manager, monkeypatch):
    good = _Client(ok=True)
    monkeypatch.setattr(manager, 'get_client', lambda _id: good)
    assert manager.connect_device('d1') is True
    assert not manager._connection_failures.get('d1')

    bad = _Client(ok=False)
    monkeypatch.setattr(manager, 'get_client', lambda _id: bad)
    assert manager.connect_device('d1') is False
    failure = manager._connection_failures.get('d1') or {}
    assert failure.get('retry_count', 0) >= 1


def test_connect_all_marks_disabled_as_none(manager, monkeypatch):
    manager.devices['off'] = _device('off', enabled=False)
    monkeypatch.setattr(manager, 'connect_device', lambda _id: True)

    results = manager.connect_all()

    assert results['off'] is None
    assert results['d1'] is True


def test_connect_all_isolates_single_device_exception(manager, monkeypatch):
    def boom(device_id):
        if device_id == 'd1':
            raise RuntimeError('配置写坏')
        return True

    monkeypatch.setattr(manager, 'connect_device', boom)
    results = manager.connect_all()

    assert results['d1'] is False, '单台异常必须被隔离成 False'
    assert results['d2'] is True, '一台失败不能拖垮其它设备'


def test_connect_all_timeout_marks_remaining(manager, monkeypatch):
    called = []
    monkeypatch.setattr(manager, 'connect_device',
                        lambda _id: called.append(_id) or True)

    results = manager.connect_all(timeout=-1)     # 立即进入超时分支

    assert called == [], '超时后不应再连任何设备'
    assert results == {'d1': False, 'd2': False}


def test_disconnect_all_disconnects_and_stops_reconnect(manager):
    client = _Client()
    manager.clients['d1'] = client

    manager.disconnect_all()

    assert client.disconnected == 1
    assert manager._reconnect_running is False


# --------------------------------------------------------------- 增删
def test_add_device_rejects_missing_id_and_unknown_protocol(manager):
    assert manager.add_device({'protocol': 'modbus_tcp'}) is False
    assert manager.add_device({'id': 'x', 'protocol': 'quantum'}) is False


def test_add_device_rolls_back_previous_config_on_save_failure(manager):
    old = dict(manager.devices['d1'])
    manager._save_config = lambda: False

    assert manager.add_device(_device('d1', host='10.9.9.9')) is False

    assert manager.devices['d1'] == old, '覆盖失败后旧配置必须还原'


def test_add_device_rolls_back_new_device_on_save_failure(manager):
    manager._save_config = lambda: False

    assert manager.add_device(_device('new1')) is False
    assert 'new1' not in manager.devices


def test_add_device_registers_raw_entry(manager):
    assert manager.add_device(_device('new2')) is True
    ids = [d.get('id') for d in manager._raw_devices]
    assert ids.count('new2') == 1


def test_remove_unknown_device_returns_false(manager):
    assert manager.remove_device('nope') is False


def test_remove_device_rolls_back_on_save_failure(manager):
    client = _Client()
    manager.clients['d1'] = client
    manager._save_config = lambda: False

    assert manager.remove_device('d1') is False
    assert 'd1' in manager.devices, '落盘失败必须把设备放回去'
    assert 'd1' in manager.clients, '客户端也要放回去，否则设备"活着但没连接"'


def test_remove_device_success(manager):
    manager.clients['d1'] = _Client()
    manager._stopped_devices.add('d1')

    assert manager.remove_device('d1') is True
    assert 'd1' not in manager.devices
    assert 'd1' not in manager._stopped_devices


# --------------------------------------------------------------- 精简状态
def test_brief_status_accepts_bare_strings(manager):
    manager.devices['mix'] = {
        'id': 'mix', 'name': 'mix', 'protocol': 'opcua',
        'registers': ['temp', {'name': 'press', 'unit': 'kPa'}],
        'nodes': ['ns=2;s=Temp'],
        'topics': ['scada/dev/temp'],
        'endpoints': ['/api/temp'],
    }

    status = manager._get_brief_status('mix')

    assert status['registers'][0]['name'] == 'temp'
    assert status['registers'][1]['unit'] == 'kPa'
    assert status['nodes'][0]['name'] == 'ns=2;s=Temp'
    assert status['topics'][0]['name'] == 'temp'
    assert status['endpoints'][0]['name'] == '/api/temp'


def test_brief_status_reports_fault_when_estop(manager, monkeypatch):
    """E-STOP 只把 mechanical 标成 fault；仪表必须继续显示（round 380 红线）。

    原用例用 d1（只有 temp 点位 → instrument）断言 fault，固化了
    「急停把仪表也停掉」的错误语义，已改为分别断言：
    """
    monkeypatch.setattr(manager, '_estop_active', True)
    assert manager._get_brief_status('d1')['status'] != 'fault', \
        '仪表被 E-STOP 误标为 fault'

    manager.devices['pump'] = {
        'id': 'pump', 'name': 'pump', 'protocol': 'modbus_tcp',
        'device_category': 'mechanical',
    }
    status = manager._get_brief_status('pump')
    assert status['stopped'] is True
    assert status['status'] == 'fault'
