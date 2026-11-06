# -*- coding: utf-8 -*-
"""声明式自定义接口适配器（round 375）。

覆盖三层：
1. `AdapterSpec` 解析/校验（未知字段、重复点位、环境变量、scale）；
2. `map_payload` 取值/映射（数组路径、enum、bool、required、空记录）；
3. `CustomAdapterClient` 的 HTTP 行为（认证头、records 列表、write 模板）。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from 采集层 import custom_adapter
from 采集层.adapter_spec import (
    AdapterConfigError,
    AdapterDataError,
    AdapterSpec,
    dig,
    map_payload,
)


def _adapter(**overrides):
    adapter = {
        'transport': 'http',
        'base_url': 'https://mes.example.com/api',
        'request': {'method': 'GET', 'path': '/lines/{device_id}/points'},
        'response': {'records': 'data.points'},
        'points': [
            {'name': 'temperature', 'path': 'value', 'type': 'float',
             'scale': 0.1, 'unit': '°C'},
            {'name': 'state', 'path': 'state', 'type': 'string',
             'enum': {'RUNNING': 1, 'IDLE': 0, 'FAULT': -1}},
        ],
    }
    adapter.update(overrides)
    return {'id': 'custom_device', 'name': '自定义接口设备',
            'protocol': 'custom', 'adapter': adapter}


def _spec(**overrides) -> AdapterSpec:
    return AdapterSpec.from_device_config(_adapter(**overrides))


# ---------------------------------------------------------------------------
# 1. 配置解析
# ---------------------------------------------------------------------------

class TestSpecParsing:
    def test_minimal(self):
        spec = _spec()
        assert spec.base_url == 'https://mes.example.com/api'
        assert spec.records_path == 'data.points'
        assert [p.name for p in spec.points] == ['temperature', 'state']
        assert spec.points[0].scale == 0.1

    def test_missing_adapter_section(self):
        with pytest.raises(AdapterConfigError, match='adapter'):
            AdapterSpec.from_device_config({'id': 'x', 'protocol': 'custom'})

    def test_unknown_field_is_rejected(self):
        """`records` 拼成 `record` 必须报错 —— 否则会静默采集 0 个点。"""
        with pytest.raises(AdapterConfigError, match='未知字段'):
            _spec(response={'record': 'data.points'})

    def test_unknown_point_field_is_rejected(self):
        with pytest.raises(AdapterConfigError, match='未知字段'):
            _spec(points=[{'name': 'x', 'path': 'v', 'scael': 0.1}])

    def test_duplicate_point_names(self):
        with pytest.raises(AdapterConfigError, match='重名'):
            _spec(points=[
                {'name': 'x', 'path': 'a'},
                {'name': 'x', 'path': 'b'},
            ])

    def test_bad_type(self):
        with pytest.raises(AdapterConfigError, match='type'):
            _spec(points=[{'name': 'x', 'path': 'a', 'type': 'number'}])

    def test_zero_scale_rejected(self):
        with pytest.raises(AdapterConfigError, match='scale'):
            _spec(points=[{'name': 'x', 'path': 'a', 'scale': 0}])

    def test_base_url_required(self):
        with pytest.raises(AdapterConfigError, match='base_url'):
            _spec(base_url='')

    def test_env_var_expansion(self, monkeypatch):
        monkeypatch.setenv('MES_TOKEN', 'secret-token')
        spec = _spec(auth={'type': 'bearer', 'token': '${MES_TOKEN}'})
        assert spec.auth['token'] == 'secret-token'

    def test_missing_env_var_is_explicit(self, monkeypatch):
        monkeypatch.delenv('MES_TOKEN', raising=False)
        with pytest.raises(AdapterConfigError, match='MES_TOKEN'):
            _spec(auth={'type': 'bearer', 'token': '${MES_TOKEN}'})

    def test_poll_interval_must_be_positive(self):
        with pytest.raises(AdapterConfigError, match='poll_interval'):
            _spec(poll_interval=0)

    def test_write_spec_parsed(self):
        spec = _spec(write={'method': 'POST', 'path': '/points/{name}',
                            'json': {'value': '{value}'}})
        assert spec.write.method == 'POST'
        assert spec.write.json == {'value': '{value}'}


# ---------------------------------------------------------------------------
# 2. 取值 / 映射
# ---------------------------------------------------------------------------

class TestDig:
    def test_nested_and_array(self):
        payload = {'data': {'points': [{'v': 1}, {'v': 2}]}}
        assert dig(payload, 'data.points[0].v') == 1
        assert dig(payload, 'data.points[1].v') == 2

    def test_missing_returns_sentinel(self):
        assert dig({'a': 1}, 'b') is not None
        assert dig({'a': [1]}, 'a[3]') is not None
        # 缺失与"值为 None"在业务上都算没有值
        assert dig({'a': None}, 'a') is None


class TestMapPayload:
    def _payload(self):
        return {'data': {'points': [
            {'value': 500, 'state': 'running'},
            {'value': 510, 'state': 'IDLE'},
        ]}}

    def test_list_mapping_and_enum(self):
        mapped = map_payload(self._payload(), _spec(),
                             now=datetime(2026, 1, 1, 0, 0, 0))
        # 两条记录按顺序合并，后一条覆盖前一条：510 * 0.1
        assert mapped['temperature']['value'] == 51.0
        assert mapped['temperature']['unit'] == '°C'
        assert mapped['state']['value'] == 0            # 后一条记录覆盖
        assert mapped['temperature']['timestamp'] == '2026-01-01T00:00:00'

    def test_single_object_without_records(self):
        spec = _spec(response={}, points=[
            {'name': 'v', 'path': 'data.value', 'type': 'float'},
        ])
        assert map_payload({'data': {'value': 1.5}}, spec)['v']['value'] == 1.5

    @pytest.mark.parametrize('raw, expected', [
        ('on', 1.0), ('OFF', 0.0), (True, 1.0), (False, 0.0),
    ])
    def test_bool_conversion(self, raw, expected):
        spec = _spec(response={}, points=[
            {'name': 'run', 'path': 'run', 'type': 'bool'},
        ])
        assert map_payload({'run': raw}, spec)['run']['value'] == expected

    def test_int_conversion(self):
        spec = _spec(response={}, points=[
            {'name': 'count', 'path': 'count', 'type': 'int', 'scale': 2},
        ])
        assert map_payload({'count': '21'}, spec)['count']['value'] == 42.0

    def test_auto_keeps_string(self):
        spec = _spec(response={}, points=[
            {'name': 'mode', 'path': 'mode', 'type': 'auto'},
        ])
        assert map_payload({'mode': 'AUTO'}, spec)['mode']['value'] == 'AUTO'

    def test_required_missing_raises(self):
        spec = _spec(response={}, points=[
            {'name': 'v', 'path': 'nope', 'required': True},
        ])
        with pytest.raises(AdapterDataError, match='必填'):
            map_payload({'a': 1}, spec)

    def test_optional_bad_value_is_skipped(self):
        spec = _spec(response={}, points=[
            {'name': 'good', 'path': 'good', 'type': 'float'},
            {'name': 'bad', 'path': 'bad', 'type': 'float'},
        ])
        mapped = map_payload({'good': 1, 'bad': 'abc'}, spec)
        assert 'good' in mapped and 'bad' not in mapped

    def test_non_finite_required_raises(self):
        spec = _spec(response={}, points=[
            {'name': 'v', 'path': 'v', 'type': 'float', 'required': True},
        ])
        with pytest.raises(AdapterDataError):
            map_payload({'v': float('nan')}, spec)

    def test_empty_records_raises(self):
        payload = {'data': {'points': []}}
        with pytest.raises(AdapterDataError, match='空列表'):
            map_payload(payload, _spec())

    def test_bool_cannot_be_numeric(self):
        spec = _spec(response={}, points=[
            {'name': 'v', 'path': 'v', 'type': 'float', 'required': True},
        ])
        with pytest.raises(AdapterDataError, match='布尔'):
            map_payload({'v': True}, spec)


# ---------------------------------------------------------------------------
# 3. HTTP 客户端
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {'Content-Type': 'application/json'}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')

    def json(self):
        return self._payload


class _FakeSession:
    """最小 requests.Session 替身：固定返回 payload，并记录每次调用。"""

    payload = {'data': {'points': [{'value': 500, 'state': 'RUNNING'}]}}
    instances: list['_FakeSession'] = []

    def __init__(self):
        self.headers: dict[str, str] = {}
        self.auth = None
        self.calls: list[dict] = []
        self.closed = False
        _FakeSession.instances.append(self)

    def request(self, method, url, **kwargs):
        self.calls.append({'method': method, 'url': url, **kwargs})
        return _FakeResponse(_FakeSession.payload)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_session(monkeypatch):
    _FakeSession.instances = []
    _FakeSession.payload = {'data': {'points': [{'value': 500, 'state': 'RUNNING'}]}}
    monkeypatch.setattr(custom_adapter.requests, 'Session', _FakeSession)
    return _FakeSession


def _client(**overrides):
    return custom_adapter.CustomAdapterClient(_adapter(**overrides))


class TestCustomAdapterClient:
    def test_connect_probes_and_maps(self, fake_session):
        client = _client(auth={'type': 'bearer', 'token': 'tok'})
        try:
            assert client.connect() is True
            session = fake_session.instances[-1]
            assert session.headers['Authorization'] == 'Bearer tok'
            first = session.calls[0]
            assert first['method'] == 'GET'
            assert first['url'] == 'https://mes.example.com/api/lines/custom_device/points'
            assert client.latest_data['temperature']['value'] == 50.0
        finally:
            client.disconnect()

    def test_connect_fails_when_records_mismatch(self, fake_session):
        fake_session.payload = {'data': {'items': []}}     # 配置写的是 data.points
        client = _client()
        assert client.connect() is False
        assert client.connected is False
        assert 'records' in (client.stats['last_error'] or '')

    def test_invalid_config_fails_without_raising(self):
        client = custom_adapter.CustomAdapterClient({
            'id': 'x', 'protocol': 'custom',
            'adapter': {'base_url': 'https://x',
                        'request': {'path': '/points'},
                        'points': []},
        })
        assert client.connect() is False
        assert 'points' in client.stats['last_error']

    def test_write_point_uses_template(self, fake_session):
        client = _client(write={
            'method': 'POST', 'path': '/points/{name}',
            'json': {'value': '{value}', 'name': '{name}'},
        })
        try:
            assert client.connect() is True
            assert client.write_point('temperature', 12.5) is True
            last = fake_session.instances[-1].calls[-1]
            assert last['method'] == 'POST'
            assert last['url'].endswith('/points/temperature')
            assert last['json'] == {'value': 12.5, 'name': 'temperature'}
        finally:
            client.disconnect()

    def test_write_without_config_returns_false(self, fake_session):
        client = _client()
        try:
            assert client.connect() is True
            assert client.write_point('temperature', 1) is False
        finally:
            client.disconnect()

    def test_disconnect_stops_thread_and_closes_session(self, fake_session):
        client = _client()
        assert client.connect() is True
        session = fake_session.instances[-1]
        client.disconnect()
        assert client.connected is False
        assert session.closed is True


def test_factory_returns_custom_client():
    from 采集层.device_manager import _create_client

    client = _create_client(_adapter(), simulation_mode=True)
    assert isinstance(client, custom_adapter.CustomAdapterClient)


# ---------------------------------------------------------------------------
# 4. API 配置入口
# ---------------------------------------------------------------------------

class TestApiDeviceValidation:
    def test_validate_accepts_good_adapter(self):
        from 展示层.api.api_devices import _validate_protocol_fields

        _validate_protocol_fields('custom', _adapter())   # 不抛即通过

    def test_validate_rejects_bad_adapter_with_reason(self):
        from 展示层.api.api_devices import _validate_protocol_fields

        with pytest.raises(ValueError, match='record'):
            _validate_protocol_fields('custom', _spec_bad_records())

    def test_build_config_keeps_adapter_and_interval(self):
        from 展示层.api.api_devices import _build_device_config

        data = _adapter(poll_interval=7)
        data.update({'name': 'MES', 'collection_interval': 5})
        config = _build_device_config('custom', data)

        assert config['adapter']['response']['records'] == 'data.points'
        assert config['poll_interval'] == 7

    def test_build_config_keeps_device_metadata(self):
        from 展示层.api.api_devices import _build_device_config

        data = _adapter()
        data.update({
            'device_class': 'meter',
            'autonomy': 'hitl',
            'electrical': {'rated_voltage': 380, 'phases': 3},
        })
        config = _build_device_config('custom', data)
        assert config['device_class'] == 'meter'
        assert config['autonomy'] == 'hitl'
        assert config['electrical']['phases'] == 3

    @pytest.mark.parametrize('field, value', [
        ('autonomy', 'robot'),
        ('device_class', 'spaceship'),
        ('electrical', ['x']),
    ])
    def test_invalid_device_metadata_rejected(self, field, value):
        from 展示层.api.api_devices import _validate_protocol_fields

        data = _adapter()
        data[field] = value
        with pytest.raises(ValueError):
            _validate_protocol_fields('custom', data)

    def test_connection_test_uses_custom_client(self):
        """设备页的"测试连接"必须走真实适配器，而不是当成不支持的协议。"""
        from 展示层.api.api_devices import _create_test_client

        client = _create_test_client(_adapter(), simulation_mode=True)
        assert isinstance(client, custom_adapter.CustomAdapterClient)

    def test_devices_page_supports_custom_and_device_metadata(self):
        """设备页必须能选 custom、编辑 adapter、填设备类别/自主等级。"""
        from pathlib import Path

        html = (Path(__file__).resolve().parent.parent
                / '模板' / 'devices.html').read_text(encoding='utf-8')
        for token in (
            'value="custom"',
            'id="custom-fields"',
            'id="custom-adapter-json"',
            'id="custom-plugin-select"',
            '/api/devices/adapters',
            '/adapter-config',
            'id="device-class"',
            'id="device-autonomy"',
            'id="device-rated-voltage"',
        ):
            assert token in html, f'设备页缺少 {token}'

    def test_devices_yaml_validation_catches_bad_adapter(self, tmp_path):
        """启动/保存阶段的配置校验必须能发现 adapter 结构错误（round 376）。"""
        import yaml
        from core.config_validator import validate_config

        bad = _spec_bad_records()
        path = tmp_path / 'devices.yaml'
        path.write_text(yaml.safe_dump({'devices': [bad]}, allow_unicode=True),
                        encoding='utf-8')

        ok, errors = validate_config(str(path), 'devices')
        assert ok is False
        assert any('adapter' in e and 'record' in e for e in errors)


# ---------------------------------------------------------------------------
# 5. MQTT 复用同一套映射引擎（round 376）
# ---------------------------------------------------------------------------

class TestMqttDeclarativeMapping:
    def _client(self, points, **topic_over):
        from 采集层.mqtt_client import MQTTClient

        topic = {'topic': 'factory/line1/state', 'points': points}
        topic.update(topic_over)
        return MQTTClient({'id': 'mqtt_dev', 'topics': [topic]})

    def test_topic_points_map_multiple_values(self):
        """一个主题出多个点位，含 enum / scale / 显式 device_id。"""
        client = self._client(
            [
                {'name': 'temperature', 'path': 'temp', 'type': 'float',
                 'scale': 0.1, 'unit': 'C'},
                {'name': 'run_state', 'path': 'state', 'type': 'string',
                 'enum': {'RUNNING': 1, 'IDLE': 0}},
            ],
            device_id='line1',
        )
        seen = []
        client.add_data_callback(lambda d, n, v, u: seen.append((d, n, v, u)))

        client._process_message('factory/line1/state',
                                {'temp': 500, 'state': 'running'})

        assert client.latest_data['temperature']['value'] == 50.0
        assert client.latest_data['temperature']['unit'] == 'C'
        assert client.latest_data['run_state']['value'] == 1
        assert ('line1', 'temperature', 50.0, 'C') in seen

    def test_topic_records_list(self):
        """`records` 指向列表：与 HTTP 适配器同口径，后一条覆盖前一条。"""
        client = self._client(
            [{'name': 'value', 'path': 'v', 'type': 'float'}],
            records='data.points',
        )
        client._process_message('factory/line1/state',
                                {'data': {'points': [{'v': 1}, {'v': 2}]}})
        assert client.latest_data['value']['value'] == 2.0

    def test_invalid_points_config_is_explicit(self):
        """点位配置写错 → 该主题明确不采集，不能回退成按整包猜点位名。"""
        client = self._client([{'name': 'broken'}])      # 缺 path
        assert client._declared_topic_map()['factory/line1/state'] is None
        client._process_message('factory/line1/state', {'broken': 1, 'temp': 25})
        assert client.latest_data == {}
        assert client.stats['parse_errors'] == 0        # 编译期已记 ERROR，不重复计数

    def test_legacy_json_path_still_works(self):
        """没有 points 的主题继续走旧的 json_path 路径（别修过头）。"""
        client = self._client([])
        client.topics_config = [{'topic': 'legacy/t', 'name': 'vibration_x',
                                 'json_path': 'value', 'unit': 'mm/s'}]
        client._process_message('legacy/t', {'value': 0.12, 'ts': 1})
        assert client.latest_data['vibration_x']['value'] == 0.12


def _spec_bad_records():
    """response 里写错字段名（record 而不是 records）。"""
    data = _adapter()
    data['adapter']['response'] = {'record': 'data.points'}
    return data
