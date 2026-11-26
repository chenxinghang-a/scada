# -*- coding: utf-8 -*-
"""Partition B5 回归测试：API / Auth / CSRF / Alarm / Data Export 状态联动。"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from core.csrf_protection import CSRFProtection
from 报警层.alarm_engine import evaluate_condition
from 报警层.alarm_manager import AlarmManager
from 报警层.alarm_output import AlarmOutput
from 存储层.data_export import DataExport


def test_csrf_exempts_client_errors_endpoint():
    from flask import Flask
    csrf_app = Flask(__name__)
    csrf_app.config['SECRET_KEY'] = 'b5-csrf-secret'

    @csrf_app.route('/api/system/client-errors', methods=['POST'])
    def _ce():
        return {'ok': True}, 200

    @csrf_app.route('/api/other-protected', methods=['POST'])
    def _other():
        return {'ok': True}, 200

    CSRFProtection(csrf_app)
    tc = csrf_app.test_client()
    tc.set_cookie('token', 'dummy-cookie-token')
    assert tc.post('/api/other-protected', json={}).status_code == 403
    assert tc.post('/api/system/client-errors', json={}).status_code == 200


def test_auth_username_xss_and_is_active_coercion(auth_manager):
    bad = auth_manager.register('alice<script>', 'Abcdef12', role='viewer')
    assert bad['success'] is False
    assert '用户名' in bad['message']

    ok2 = auth_manager.register('b5_user_01', 'Abcdef12', role='operator')
    assert ok2['success'] is True, ok2

    # update_user 字符串 'false' 必须落成 0 并阻断登录与 verify_token
    auth_manager.update_user('b5_user_01', is_active='false')
    users_after = {u['username']: u for u in auth_manager.get_users()}
    assert users_after['b5_user_01']['is_active'] in (0, False)

    res_login = auth_manager.login('b5_user_01', 'Abcdef12')
    assert res_login['success'] is False

    # 先激活拿 token，再置 '0'，verify_token 必须拒绝
    auth_manager.update_user('b5_user_01', is_active=1)
    res_login2 = auth_manager.login('b5_user_01', 'Abcdef12')
    assert res_login2['success'] is True
    token = res_login2['token']
    assert auth_manager.verify_token(token) is not None

    auth_manager.update_user('b5_user_01', is_active='0')
    assert auth_manager.verify_token(token) is None


def test_api_control_write_endpoint_respects_estop_and_lock(client, auth_headers, app):
    dc = MagicMock()
    dc.estop_active = True
    dc.get_estop_status.return_value = {'active': True, 'reason': '急停测试'}
    app.device_control = dc
    app.device_manager = MagicMock()
    app.device_manager.get_all_devices.return_value = {
        'rest_dev': {
            'id': 'rest_dev', 'name': 'R', 'protocol': 'rest', 'device_category': 'mechanical',
            'endpoints': [{'name': 'cmd', 'path': '/cmd', 'method': 'POST', 'access': 'rw'}],
        }
    }

    resp = client.post('/api/devices/rest_dev/write-endpoint',
                       json={'endpoint': 'cmd', 'value': 1},
                       headers=auth_headers)
    assert resp.status_code == 403
    body = resp.get_json()
    assert body['success'] is False


def test_api_data_export_returns_500_on_disk_error_and_404_when_empty(client, auth_headers, app, monkeypatch):
    app.database.get_alarm_records.return_value = [{'id': 1, 'message': 'x'}]

    def fail_csv(self, data, filename=None):
        self.last_error = 'permission denied'
        return None

    monkeypatch.setattr(DataExport, 'export_csv', fail_csv)
    resp_err = client.post('/api/export/alarms', json={'format': 'csv'}, headers=auth_headers)
    assert resp_err.status_code == 500

    app.database.get_alarm_records.return_value = []
    resp_empty = client.post('/api/export/alarms', json={'format': 'csv'}, headers=auth_headers)
    assert resp_empty.status_code == 404


def test_api_alarms_masks_secrets_and_validates_manual_duration(client, auth_headers, app, tmp_path, monkeypatch):
    import yaml as _yaml
    from 展示层.api import api_alarms

    alarms_yaml = tmp_path / 'alarms.yaml'
    alarms_yaml.write_text(
        "alarm_rules: []\n"
        "notification:\n"
        "  email:\n"
        "    password: top_secret_smtp_pass\n"
        "broadcast:\n"
        "  mqtt:\n"
        "    password: mqtt_secret_pass\n",
        encoding='utf-8',
    )
    monkeypatch.setattr(
        api_alarms,
        'load_yaml_config',
        lambda rel: _yaml.safe_load(alarms_yaml.read_text(encoding='utf-8')) or {},
    )
    saved = {}
    monkeypatch.setattr(
        api_alarms,
        'save_yaml_config',
        lambda rel, cfg: saved.update(cfg) or True,
    )

    resp_get = client.get('/api/alarm-rules', headers=auth_headers)
    assert resp_get.status_code == 200
    assert resp_get.get_json()['notification']['email']['password'] == '***'

    resp_put = client.put('/api/alarm-rules/notification',
                          json={'email': {'password': '***', 'host': 'smtp.example.com'}},
                          headers=auth_headers)
    assert resp_put.status_code == 200
    assert saved['notification']['email']['password'] == 'top_secret_smtp_pass'

    app.alarm_output = AlarmOutput()
    resp_bad = client.post('/api/alarm-output/manual',
                           json={'red': True, 'duration': True},
                           headers=auth_headers)
    assert resp_bad.status_code == 400
    resp_big = client.post('/api/alarm-output/manual',
                           json={'red': True, 'duration': 9999},
                           headers=auth_headers)
    assert resp_big.status_code == 400


def test_alarm_engine_and_manager_quality_and_output_timer(tmp_path):
    assert evaluate_condition({'condition': 'greater_than', 'threshold': 0.5}, True, active=False) is False
    assert evaluate_condition({'condition': 'greater_than', 'threshold': float('nan')}, 10.0, active=False) is False
    assert evaluate_condition({'condition': 'greater_than', 'threshold': 5.0}, 10.0, active=False) is True

    cfg = tmp_path / 'alarms.yaml'
    cfg.write_text(
        "alarm_rules:\n"
        "  - id: r1\n"
        "    name: hi\n"
        "    device_id: d1\n"
        "    register_name: temp\n"
        "    condition: greater_than\n"
        "    threshold: 50\n"
        "    level: high\n"
        "    enabled: true\n",
        encoding='utf-8',
    )
    am = AlarmManager(MagicMock(), str(cfg))
    am.alarm_output = None
    am.broadcast_system = None
    try:
        # Bad/Uncertain 质量码 fail-closed，不得触发或误清除状态
        now = datetime.now()
        assert not am.check_alarm('d1', 'temp', 999.0, now, quality=0)
        assert not am.check_alarm('d1', 'temp', 999.0, now, quality=64)
        assert ('r1', 'd1', 'temp') not in am.alarm_states
        am.check_alarm('d1', 'temp', 999.0, now, quality=192)
        assert ('r1', 'd1', 'temp') in am.alarm_states
        # 质量坏回落后原有 ACTIVE 报警不得被清掉
        am.check_alarm('d1', 'temp', 0.0, now, quality=0)
        assert ('r1', 'd1', 'temp') in am.alarm_states
    finally:
        am.stop()

    ao = AlarmOutput()
    ao.manual_control(red=True, duration=10)
    assert ao._manual_timer is not None
    ao.manual_control(red=False, duration=0)
    assert ao._manual_timer is None


def test_api_devices_put_category_rtu_and_topology_no_mutate(client, auth_headers, app):
    devices_map = {
        'd1': {'id': 'd1', 'name': 'RTU1', 'protocol': 'modbus_rtu', 'serial_port': 'COM3', 'baudrate': 9600,
               'device_category': 'mechanical', 'enabled': True}
    }
    orphan = {'id': 'orph', 'name': 'Orphan', 'protocol': 'mqtt', 'order': None}
    app.device_manager = MagicMock()
    app.device_manager.devices = {'d1': dict(devices_map['d1']), 'orph': orphan}
    app.device_manager.get_all_devices.return_value = app.device_manager.devices
    app.device_manager.get_device.side_effect = lambda did: app.device_manager.devices.get(did)
    app.device_manager.get_device_status.return_value = {'status': 'online', 'connected': True}

    resp_topo = client.get('/api/devices/topology', headers=auth_headers)
    assert resp_topo.status_code == 200
    assert 'stage_unknown' not in orphan

    resp_put = client.put('/api/devices/d1',
                          json={'device_category': 'safety', 'serial_port': 'COM4', 'baudrate': 19200},
                          headers=auth_headers)
    assert resp_put.status_code == 200
    app.device_manager._save_config.assert_called()
    call_cfg = app.device_manager.devices['d1']
    assert call_cfg['device_category'] == 'safety'
    assert call_cfg['serial_port'] == 'COM4'
    assert call_cfg['baudrate'] == 19200
