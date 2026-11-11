"""真实设备管理器的边界用例（来自变异存活点）。

* 配置文件不存在：必须返回 False 且留下 `missing` 原因，不能"静默成功"；
* 从**空设备表**热重载到非空：不能被"重载后为 0 台"的空表保护误拒
  （`if not new_ids and old_ids and not allow_empty` 三个条件缺一不可）。
"""

import yaml

from 采集层.real_device_manager import RealDeviceManager


def _write(path, devices):
    path.write_text(yaml.safe_dump({'devices': devices}, allow_unicode=True),
                    encoding='utf-8')


def _dev(dev_id='dev1', host='127.0.0.1'):
    return {
        'id': dev_id, 'name': dev_id, 'protocol': 'modbus_tcp',
        'host': host, 'port': 502, 'enabled': True,
        'registers': [{'name': 'temp', 'address': 0, 'data_type': 'uint16'}],
    }


def test_missing_config_file_returns_false_with_reason(tmp_path):
    missing = tmp_path / 'nope.yaml'
    manager = RealDeviceManager(config_path=str(missing))

    assert manager.load_config() is False
    assert manager._load_failed_reason == 'missing'
    assert manager.devices == {}


def test_reload_from_empty_table_is_not_rejected(tmp_path):
    cfg = tmp_path / 'devices.yaml'
    _write(cfg, [])
    manager = RealDeviceManager(config_path=str(cfg))
    assert manager.devices == {}

    _write(cfg, [_dev()])
    diff = manager.reload_config(allow_empty=False)

    assert diff.get('rejected') is None, f'空表起步的正常重载被误拒: {diff}'
    assert 'dev1' in manager.devices
    assert 'dev1' in diff.get('added', [])


def test_reload_to_empty_is_rejected_when_not_allowed(tmp_path):
    cfg = tmp_path / 'devices.yaml'
    _write(cfg, [_dev()])
    manager = RealDeviceManager(config_path=str(cfg))
    assert 'dev1' in manager.devices

    _write(cfg, [])
    diff = manager.reload_config(allow_empty=False)

    assert diff.get('rejected'), '删空配置应被拒绝'
    assert 'dev1' in manager.devices, '被拒绝后旧表必须保留'


class _TrackedClient:
    def __init__(self):
        self.connected = True
        self.disconnected = 0

    def disconnect(self):
        self.connected = False
        self.disconnected += 1


def test_reload_with_devices_present_is_not_rejected(tmp_path):
    """正常重载（新旧都非空）绝不能被"删空保护"误判。

    这条专门杀 `not new_ids and old_ids and not allow_empty` 里
    第一个 `and` 被改成 `or` 的变异：改完后"有设备"也会被拒。
    """
    cfg = tmp_path / 'devices.yaml'
    _write(cfg, [_dev('dev1'), _dev('dev2')])
    manager = RealDeviceManager(config_path=str(cfg))

    diff = manager.reload_config(allow_empty=False)

    assert diff['rejected'] is None, f'正常重载被误拒: {diff}'
    assert diff['kept'] == ['dev1', 'dev2']


def test_reload_to_empty_allowed_when_explicit(tmp_path):
    """显式 `allow_empty=True` 时必须允许清空（删光设备是合法运维操作）。"""
    cfg = tmp_path / 'devices.yaml'
    _write(cfg, [_dev('dev1')])
    manager = RealDeviceManager(config_path=str(cfg))

    _write(cfg, [])
    diff = manager.reload_config(allow_empty=True)

    assert diff['rejected'] is None, f'显式允许清空却被拒: {diff}'
    assert manager.devices == {}


def test_reload_rebuilds_client_when_device_config_changed(tmp_path):
    """同 id 但 host 变了 → 旧客户端必须断开并摘除（否则永远按旧参数采）。"""
    cfg = tmp_path / 'devices.yaml'
    _write(cfg, [_dev('dev1', host='10.0.0.1')])
    manager = RealDeviceManager(config_path=str(cfg))

    client = _TrackedClient()
    manager.clients['dev1'] = client

    _write(cfg, [_dev('dev1', host='10.0.0.2')])
    diff = manager.reload_config(allow_empty=False)

    assert diff['rejected'] is None
    assert client.disconnected == 1, '配置变更后旧客户端未被断开'
    assert 'dev1' not in manager.clients, '旧客户端仍留在缓存里'


def test_reload_keeps_client_when_config_unchanged(tmp_path):
    """配置没变就不要乱断连接（每次重载都重连会造成采集中断）。"""
    cfg = tmp_path / 'devices.yaml'
    _write(cfg, [_dev('dev1')])
    manager = RealDeviceManager(config_path=str(cfg))

    client = _TrackedClient()
    manager.clients['dev1'] = client

    manager.reload_config(allow_empty=False)

    assert client.disconnected == 0
    assert manager.clients['dev1'] is client
