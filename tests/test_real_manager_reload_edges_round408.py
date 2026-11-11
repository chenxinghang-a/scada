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
