"""配置结构校验（devices.yaml / alarms.yaml）。"""

from pathlib import Path

import pytest
import yaml

from core.config_manager import expand_env_vars
from core.config_schema import (
    format_errors,
    has_structural_errors,
    validate_alarms_document,
    validate_devices_document,
)

ROOT = Path(__file__).resolve().parents[1]


def _dev(**kw):
    base = {'id': 'dev1', 'protocol': 'modbus_tcp', 'host': '127.0.0.1',
            'port': 502,
            'registers': [{'name': 'temp', 'address': 0, 'data_type': 'uint16'}]}
    base.update(kw)
    return base


def test_valid_documents_pass():
    assert validate_devices_document({'devices': [_dev()]}) == []
    assert validate_alarms_document({'alarm_rules': [
        {'id': 'r1', 'level': 'high', 'condition': 'greater_than',
         'threshold': 80.0, 'delay': 0, 'deadband': 0, 'register_name': 'temp'},
    ]}) == []


@pytest.mark.parametrize('doc,keyword', [
    ('not-a-dict', '顶层'),
    ({}, 'devices'),
    ({'devices': 'x'}, '不是列表'),
    ({'devices': []}, '为空'),
    ({'devices': [{'protocol': 'modbus_tcp'}]}, 'id'),
    ({'devices': [_dev(protocol='mqtt2')]}, 'protocol'),
    ({'devices': [_dev(protocol=['modbus_tcp'])]}, 'protocol'),
    ({'devices': [_dev(port=70000)]}, 'port'),
    ({'devices': [_dev(enabled='yes')]}, 'enabled'),
    ({'devices': [_dev(collection_interval=0)]}, 'collection_interval'),
    ({'devices': [_dev(registers=[{'name': 'x', 'address': -1}])]}, 'address'),
    ({'devices': [_dev(registers=[{'name': 'x', 'address': 0,
                                   'data_type': 'float128'}])]}, 'data_type'),
    ({'devices': [_dev(registers=[{'name': 'x', 'address': 0,
                                   'data_type': 'Float32'}])]}, '小写'),
    ({'devices': [_dev(registers=[{'name': 'x', 'address': 0,
                                   'data_type': 'word'}])]}, 'modbus'),
    ({'devices': [_dev(nodes=['ns=2;s=Temp'])]}, '映射'),
    ({'devices': [_dev()], 'x': 1}, None),          # 额外字段不算错
])
def test_devices_errors_are_reported(doc, keyword):
    errors = validate_devices_document(doc)
    if keyword is None:
        assert errors == []
        return
    assert errors, f'应当报错: {doc!r}'
    assert any(keyword in e for e in errors), errors


@pytest.mark.parametrize('rule,keyword', [
    ({'level': 'warning'}, 'id'),
    ({'id': 'r1', 'level': 'fatal'}, 'level'),
    ({'id': 'r1', 'condition': 'vibrates'}, 'condition'),
    ({'id': 'r1', 'condition': 'greater_than'}, 'threshold'),
    ({'id': 'r1', 'threshold': 'high'}, 'threshold'),
    ({'id': 'r1', 'delay': -1}, '不能为负'),
    ({'id': 'r1', 'enabled': 1}, 'enabled'),
    ({'id': 'r1', 'level': {}}, 'level'),
])
def test_alarm_rule_errors_are_reported(rule, keyword):
    errors = validate_alarms_document({'alarm_rules': [rule]})
    assert errors
    assert any(keyword in e for e in errors), errors


def test_validator_never_raises_on_malformed_values():
    """校验器自己崩溃会被上层当成"解析失败"，必须只返回错误。"""
    bad_docs = [
        {'devices': [{'id': 'a', 'protocol': []}]},
        {'devices': [{'id': 'a', 'protocol': {}}]},
        {'devices': [{'id': 'a', 'registers': [None, 'x', 3]}]},
        {'devices': [{'id': 'a', 'nodes': [{}], 'topics': [1]}]},
    ]
    for doc in bad_docs:
        errors = validate_devices_document(doc)
        assert isinstance(errors, list)
    assert validate_alarms_document({'alarm_rules': [{'id': 'r', 'level': {}}]})
    assert validate_alarms_document({'alarm_rules': [{'id': 'r', 'condition': []}]})


def test_structural_error_detection():
    assert has_structural_errors(['顶层不是映射（实际 list）'])
    assert has_structural_errors(['`devices` 不是列表（实际 str）'])
    assert not has_structural_errors(['devices[0](a): port=70000 非法'])


def test_format_errors_truncates_but_reports_total():
    msg = format_errors('x.yaml', [f'e{i}' for i in range(20)], limit=3)
    assert '共 20 条' in msg and 'e0' in msg


@pytest.mark.parametrize('name,expected_min', [
    ('devices.yaml', 20),
    ('devices_simulated.yaml', 100),
    ('devices_real.yaml', 1),
])
def test_repo_configs_pass_validation(name, expected_min):
    """仓库自带的配置必须能过校验（走与生产一致的 expand_env_vars）。"""
    path = ROOT / '配置' / name
    if not path.exists():
        pytest.skip(f'{name} 不存在')
    raw = yaml.safe_load(path.read_text(encoding='utf-8'))
    doc = expand_env_vars(raw or {}, _source=str(path))

    errors = validate_devices_document(doc)
    assert errors == [], f'{name} 校验未通过: {errors[:5]}'
    assert len(doc['devices']) >= expected_min


def test_repo_alarms_config_passes_validation():
    path = ROOT / '配置' / 'alarms.yaml'
    doc = expand_env_vars(yaml.safe_load(path.read_text(encoding='utf-8')) or {},
                          _source=str(path))
    errors = validate_alarms_document(doc)
    assert errors == [], f'alarms.yaml 校验未通过: {errors[:5]}'


def _write_devices(path, devices):
    path.write_text(
        yaml.safe_dump({'devices': devices}, allow_unicode=True, sort_keys=False),
        encoding='utf-8')


def test_strict_mode_rejects_structural_error_but_keeps_old_table(tmp_path, monkeypatch):
    """严格模式只拦结构性错误，且拒载后必须保留旧表（不能把现场清空）。"""
    from 采集层.device_manager import DeviceManager

    cfg = tmp_path / 'devices.yaml'
    _write_devices(cfg, [_dev()])
    manager = DeviceManager(config_path=str(cfg), simulation_mode=True)
    old = dict(manager.devices)
    assert old, '初始配置未加载成功'

    cfg.write_text(yaml.safe_dump({'devices': 'oops'}), encoding='utf-8')
    monkeypatch.setenv('SCADA_STRICT_CONFIG', 'true')

    assert manager.load_config() is False
    assert manager.devices == old, '拒载后旧设备表被清空了'
    assert manager.get_config_schema_errors(), '未记录校验错误'


def test_strict_mode_allows_entry_level_errors(tmp_path, monkeypatch):
    """条目级错误（如 enabled 写成 1）不该让整表拒载。"""
    from 采集层.device_manager import DeviceManager

    cfg = tmp_path / 'devices.yaml'
    _write_devices(cfg, [_dev(enabled=1)])
    monkeypatch.setenv('SCADA_STRICT_CONFIG', 'yes')

    manager = DeviceManager(config_path=str(cfg), simulation_mode=True)

    assert manager.devices, '条目级错误把整表拒掉了'
    assert manager.get_config_schema_errors()
