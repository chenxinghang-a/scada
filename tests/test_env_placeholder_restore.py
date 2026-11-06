# -*- coding: utf-8 -*-
"""设备配置落盘时不得把 `${VAR}` 写成明文（round 377）。

`load_config()` 会把 `${DEVICE_X_PASSWORD}` 展开进内存；保存时如果直接
dump 内存，现场改一次设备名就把**全厂密码写成明文**。
"""
from __future__ import annotations

import yaml

from core.config_manager import (
    expand_env_vars,
    restore_device_placeholders,
    restore_env_placeholders,
)


class TestRestoreHelper:
    def test_unchanged_placeholder_is_restored(self, monkeypatch):
        monkeypatch.setenv('T_PWD', 'secret')
        raw = {'password': '${T_PWD}', 'nested': {'token': '${T_PWD}'}}
        runtime = expand_env_vars(raw)

        restored = restore_env_placeholders(runtime, raw)
        assert restored['password'] == '${T_PWD}'
        assert restored['nested']['token'] == '${T_PWD}'

    def test_changed_value_is_kept(self, monkeypatch):
        monkeypatch.setenv('T_PWD', 'secret')
        raw = {'password': '${T_PWD}'}
        runtime = expand_env_vars(raw)
        runtime['password'] = 'new-password'

        restored = restore_env_placeholders(runtime, raw)
        assert restored['password'] == 'new-password'

    def test_plain_values_untouched(self):
        raw = {'name': '设备1', 'port': 502}
        assert restore_env_placeholders(dict(raw), raw) == raw

    def test_placeholder_with_default(self, monkeypatch):
        monkeypatch.delenv('T_OPT', raising=False)
        raw = {'value': '${T_OPT:-fallback}'}
        runtime = expand_env_vars(raw)
        assert runtime['value'] == 'fallback'
        assert restore_env_placeholders(runtime, raw)['value'] == '${T_OPT:-fallback}'

    def test_devices_matched_by_id_not_index(self, monkeypatch):
        monkeypatch.setenv('T_PWD', 'secret')
        raw_devices = [
            {'id': 'a', 'password': '${T_PWD}'},
            {'id': 'b', 'password': 'plain'},
        ]
        runtime = [dict(raw_devices[0], password='secret'), dict(raw_devices[1])]

        restored = restore_device_placeholders(runtime, raw_devices)
        assert restored[0]['password'] == '${T_PWD}'
        assert restored[1]['password'] == 'plain'


class TestManagerSaveRoundTrip:
    def test_save_keeps_env_placeholder(self, tmp_path, monkeypatch):
        from 采集层.real_device_manager import RealDeviceManager

        monkeypatch.setenv('DEV_PWD', 's3cret')
        path = tmp_path / 'devices_real.yaml'
        path.write_text(yaml.safe_dump({'devices': [{
            'id': 'plc_1', 'name': 'PLC', 'protocol': 'modbus_tcp',
            'host': '192.168.1.10', 'port': 502,
            'password': '${DEV_PWD}',
            'adapter': {'auth': {'type': 'bearer', 'token': '${DEV_PWD}'}},
        }]}, allow_unicode=True), encoding='utf-8')

        mgr = RealDeviceManager(str(path))
        assert mgr.devices['plc_1']['password'] == 's3cret'

        assert mgr._save_config() is True
        saved = yaml.safe_load(path.read_text(encoding='utf-8'))['devices'][0]
        assert saved['password'] == '${DEV_PWD}'
        assert saved['adapter']['auth']['token'] == '${DEV_PWD}'

        # 用户在界面上真改了密码 → 写新值（不能顽固还原成旧占位符）
        mgr.devices['plc_1']['password'] = 'changed'
        assert mgr._save_config() is True
        saved = yaml.safe_load(path.read_text(encoding='utf-8'))['devices'][0]
        assert saved['password'] == 'changed'
