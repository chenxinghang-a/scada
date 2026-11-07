# -*- coding: utf-8 -*-
"""工厂自定义设备模板库（round 381）。"""
from __future__ import annotations

import pytest
import yaml

from core.device_templates import (
    TemplateConfigError,
    load_custom_templates,
    merge_templates,
)

VALID = {
    'templates': [
        {'id': 'meter_x', 'name': '电表', 'protocol': 'modbus_tcp',
         'host': '192.168.1.10', 'port': 502,
         'registers': [{'name': 'u', 'address': 0, 'data_type': 'float32'}]},
        {'id': 'relay_x', 'name': '继电器板', 'protocol': 'custom',
         'adapter': {
             'transport': 'http', 'base_url': 'http://192.168.1.20',
             'request': {'path': '/status'},
             'points': [{'name': 'ch1', 'path': 'on', 'type': 'bool'}],
         }},
    ]
}


def _write(tmp_path, data):
    p = tmp_path / 'templates.yaml'
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding='utf-8')
    return str(p)


class TestLoader:
    def test_valid(self, tmp_path):
        templates = load_custom_templates(_write(tmp_path, VALID))
        assert [t['id'] for t in templates] == ['meter_x', 'relay_x']

    def test_missing_file_returns_empty(self, tmp_path):
        assert load_custom_templates(str(tmp_path / 'none.yaml')) == []

    def test_yaml_error_raises(self, tmp_path):
        p = tmp_path / 'bad.yaml'
        p.write_text('templates: [', encoding='utf-8')
        with pytest.raises(TemplateConfigError):
            load_custom_templates(str(p))

    def test_mapping_templates_rejected(self, tmp_path):
        """`配置/device_templates.yaml` 是 gen_factory 的映射形态，不能混用。"""
        p = _write(tmp_path, {'templates': {'rotating_machine': {}}})
        with pytest.raises(TemplateConfigError, match='列表'):
            load_custom_templates(p)

    def test_bad_entries_skipped(self, tmp_path):
        data = {'templates': [
            {'id': 'ok', 'name': 'OK', 'protocol': 'modbus_tcp'},
            {'name': '缺 id', 'protocol': 'modbus_tcp'},
            {'id': 'bad id', 'name': 'x', 'protocol': 'modbus_tcp'},
            {'id': 'bad_proto', 'name': 'x', 'protocol': 'unknown'},
        ]}
        assert [t['id'] for t in load_custom_templates(_write(tmp_path, data))] == ['ok']


class TestMerge:
    def test_custom_overrides_builtin_in_place(self):
        builtin = [{'id': 'a', 'name': 'A'}, {'id': 'b', 'name': 'B'}]
        custom = [{'id': 'a', 'name': 'A2'}, {'id': 'c', 'name': 'C'}]
        merged = merge_templates(builtin, custom)
        assert [t['id'] for t in merged] == ['a', 'b', 'c']
        assert merged[0]['name'] == 'A2'
        assert merged[2]['name'] == 'C'

    def test_duplicate_custom_last_wins(self):
        merged = merge_templates([], [
            {'id': 'x', 'name': 'first'},
            {'id': 'x', 'name': 'last'},
        ])
        assert len(merged) == 1 and merged[0]['name'] == 'last'


class TestRepoLibrary:
    def test_repo_templates_are_valid_device_configs(self):
        from 展示层.api.api_devices import (
            _build_device_config, _validate_protocol_fields,
        )

        templates = load_custom_templates()
        assert len(templates) >= 4

        for tpl in templates:
            _validate_protocol_fields(tpl['protocol'], tpl)
            cfg = _build_device_config(tpl['protocol'], tpl)
            assert cfg['id'] == tpl['id']
            assert cfg['protocol'] == tpl['protocol']
