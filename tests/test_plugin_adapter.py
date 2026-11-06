# -*- coding: utf-8 -*-
"""工厂插件适配器（round 377）。

覆盖：目录发现、路径防穿越、插件加载、客户端轮询/写入、配置解析、失败隔离。
"""
from __future__ import annotations

import pytest

from 采集层.adapter_spec import AdapterConfigError, AdapterSpec
from 采集层 import plugin_adapter as pa

PLUGIN_SOURCE = '''
PLUGIN_INFO = {'version': '2.3', 'description': '测试插件', 'author': 'QA'}

class Adapter:
    def __init__(self, settings):
        self.base = float(settings.get('base', 10))
        self._setpoint = None

    def read(self):
        return {
            'temperature': {'value': self.base, 'unit': 'C'},
            'run_state': 'RUNNING',
            'setpoint': self._setpoint if self._setpoint is not None else self.base,
        }

    def write_point(self, name, value):
        if name != 'setpoint':
            return False
        self._setpoint = float(value)
        return True
'''


@pytest.fixture
def plugin_dir(tmp_path, monkeypatch):
    (tmp_path / 'demo.py').write_text(PLUGIN_SOURCE, encoding='utf-8')
    monkeypatch.setenv(pa.PLUGIN_DIR_ENV, str(tmp_path))
    pa.clear_plugin_cache()
    yield tmp_path
    pa.clear_plugin_cache()


def _config(**adapter_over):
    adapter = {'transport': 'plugin', 'plugin': 'demo', 'settings': {'base': 21.5}}
    adapter.update(adapter_over)
    return {'id': 'p1', 'name': '插件设备', 'protocol': 'custom',
            'adapter': adapter}


# ---------------------------------------------------------------------------
# 目录 / 加载
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_lists_plugins_without_executing(self, plugin_dir):
        plugins = pa.discover_plugins()
        assert [p['name'] for p in plugins] == ['demo']
        assert plugins[0]['version'] == '2.3'
        assert plugins[0]['description'] == '测试插件'

    def test_metadata_read_is_ast_only(self, plugin_dir):
        """`PLUGIN_INFO` 用 AST 读，不执行模块顶层代码。"""
        (plugin_dir / 'boom.py').write_text(
            'raise RuntimeError("import 就会炸")\nPLUGIN_INFO = {"version": "9"}\n',
            encoding='utf-8')
        plugins = {p['name']: p for p in pa.discover_plugins()}
        assert plugins['boom']['version'] == '9'      # 仍能列出

    def test_empty_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv(pa.PLUGIN_DIR_ENV, str(tmp_path))
        assert pa.discover_plugins() == []


class TestPathSafety:
    @pytest.mark.parametrize('name', ['../evil', 'a/b', 'a.b', '', '1abc'])
    def test_bad_names_rejected(self, plugin_dir, name):
        with pytest.raises(pa.PluginError):
            pa.plugin_path(name)

    def test_missing_plugin_has_clear_error(self, plugin_dir):
        with pytest.raises(pa.PluginError, match='不存在'):
            pa.plugin_path('nope')


class TestLoad:
    def test_load_and_read(self, plugin_dir):
        plugin = pa.load_plugin('demo', {'base': 7})
        assert plugin.read()['temperature']['value'] == 7
        assert plugin.write_point('setpoint', 9) is True

    def test_import_error_is_wrapped(self, plugin_dir):
        (plugin_dir / 'broken.py').write_text('raise ValueError("boom")\n',
                                              encoding='utf-8')
        with pytest.raises(pa.PluginError, match='导入插件'):
            pa.load_plugin('broken', {})

    def test_missing_interface_rejected(self, plugin_dir):
        (plugin_dir / 'no_iface.py').write_text(
            'class Adapter:\n'
            '    def __init__(self, settings): pass\n',
            encoding='utf-8')
        with pytest.raises(pa.PluginError, match='read'):
            pa.load_plugin('no_iface', {})


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class TestPluginClient:
    def test_connect_maps_flat_payload(self, plugin_dir):
        client = pa.PluginAdapterClient(_config())
        try:
            assert client.connect() is True
            data = client.get_latest_data()
            assert data['temperature']['value'] == 21.5
            assert data['temperature']['unit'] == 'C'
            assert data['run_state']['value'] == 'RUNNING'
        finally:
            client.disconnect()

    def test_write_point(self, plugin_dir):
        client = pa.PluginAdapterClient(_config())
        try:
            assert client.connect() is True
            assert client.write_point('setpoint', 12.5) is True
            assert client.write_point('unknown', 1) is False
        finally:
            client.disconnect()

    def test_missing_plugin_fails_closed(self, plugin_dir):
        client = pa.PluginAdapterClient(_config(plugin='does_not_exist'))
        assert client.connect() is False
        assert '不存在' in client.stats['last_error']

    def test_plugin_read_exception_fails_closed(self, plugin_dir):
        (plugin_dir / 'bad_read.py').write_text(
            'class Adapter:\n'
            '    def __init__(self, settings): pass\n'
            '    def read(self): raise RuntimeError("device down")\n',
            encoding='utf-8')
        client = pa.PluginAdapterClient(_config(plugin='bad_read'))
        assert client.connect() is False
        assert 'device down' in client.stats['last_error']

    def test_points_mapping_applied(self, plugin_dir):
        """返回原始载荷 + points 映射，与 HTTP 适配器同一套规则。"""
        client = pa.PluginAdapterClient(_config(points=[
            {'name': 'temperature', 'path': 'temperature.value', 'type': 'float',
             'scale': 0.1},
        ]))
        try:
            assert client.connect() is True
            # 插件返回的 value=21.5，配了 scale=0.1 → 2.15
            assert client.get_latest_data()['temperature']['value'] == pytest.approx(2.15)
        finally:
            client.disconnect()


# ---------------------------------------------------------------------------
# 配置 / 工厂 / 校验
# ---------------------------------------------------------------------------

class TestPluginSpec:
    def test_spec_parsed(self):
        spec = AdapterSpec.from_device_config(_config())
        assert spec.transport == 'plugin'
        assert spec.plugin == 'demo'
        assert spec.settings == {'base': 21.5}
        assert spec.request is None

    def test_plugin_name_required(self):
        with pytest.raises(AdapterConfigError, match='plugin'):
            AdapterSpec.from_device_config(_config(plugin=''))

    def test_settings_must_be_object(self):
        cfg = _config()
        cfg['adapter']['settings'] = ['x']
        with pytest.raises(AdapterConfigError, match='settings'):
            AdapterSpec.from_device_config(cfg)

    @pytest.mark.parametrize('key, value', [
        ('base_url', 'https://x'), ('request', {'path': '/x'}),
        ('auth', {'type': 'bearer', 'token': 'x'}),
    ])
    def test_http_fields_rejected(self, key, value):
        cfg = _config(**{key: value})
        with pytest.raises(AdapterConfigError, match=key):
            AdapterSpec.from_device_config(cfg)

    def test_records_requires_points(self):
        cfg = _config(response={'records': 'data.items'})
        with pytest.raises(AdapterConfigError, match='records'):
            AdapterSpec.from_device_config(cfg)

    def test_http_rejects_plugin_fields(self):
        cfg = {
            'id': 'h', 'protocol': 'custom',
            'adapter': {
                'transport': 'http', 'base_url': 'https://x',
                'request': {'path': '/x'},
                'points': [{'name': 'v', 'path': 'v'}],
                'plugin': 'demo',
            },
        }
        with pytest.raises(AdapterConfigError, match='plugin'):
            AdapterSpec.from_device_config(cfg)

    def test_factory_returns_plugin_client(self, plugin_dir):
        from 采集层.device_manager import _create_client

        client = _create_client(_config(), simulation_mode=True)
        assert isinstance(client, pa.PluginAdapterClient)

    def test_http_factory_still_returns_custom_client(self):
        from 采集层.custom_adapter import CustomAdapterClient
        from 采集层.device_manager import _create_client

        cfg = {
            'id': 'h', 'protocol': 'custom',
            'adapter': {
                'base_url': 'https://x', 'transport': 'http',
                'request': {'path': '/x'},
                'points': [{'name': 'v', 'path': 'v'}],
            },
        }
        assert isinstance(_create_client(cfg, simulation_mode=True),
                          CustomAdapterClient)

    def test_config_validator_accepts_plugin(self):
        from core.config_validator import validate_custom_adapters

        assert validate_custom_adapters({'devices': [_config()]}) == []

    def test_packaging_includes_extension_dir(self):
        """插件目录必须进产物，否则 exe 里既没有 example 也放不了自写插件。"""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        assert "'扩展'" in (root / 'build.py').read_text(encoding='utf-8')
        assert "('扩展', '扩展')" in (root / 'scada-backend.spec').read_text(
            encoding='utf-8')
