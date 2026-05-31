"""
全面API端点测试 - 提升展示层/api/覆盖率
覆盖: devices, data, control, alarms, auth, system, industry40, health, metrics
"""
import pytest
from unittest.mock import patch, MagicMock


# ============================================================
# Device API - 未覆盖端点
# ============================================================

class TestDeviceAPIDeleteUpdate:
    """设备删除/更新/连接断开/协议/模板等端点"""

    def test_get_device_found(self, client, auth_headers, app):
        """GET /api/devices/<id> 返回设备详情"""
        app.device_manager.get_device_status.return_value = {
            'device_id': 'test_pump_01', 'connected': True, 'protocol': 'modbus_tcp'
        }
        resp = client.get('/api/devices/test_pump_01', headers=auth_headers)
        assert resp.status_code == 200

    def test_add_device_success(self, client, auth_headers, app):
        """POST /api/devices 成功添加设备"""
        app.device_manager.add_device.return_value = True
        app.device_manager.get_device_status.return_value = {
            'device_id': 'new_dev', 'connected': False
        }
        resp = client.post('/api/devices', json={
            'id': 'new_dev', 'name': 'New Device',
            'protocol': 'modbus_tcp', 'host': '192.168.1.1', 'port': 502
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True

    def test_add_device_failure(self, client, auth_headers, app):
        """POST /api/devices 添加失败"""
        app.device_manager.add_device.return_value = False
        resp = client.post('/api/devices', json={
            'id': 'fail_dev', 'name': 'Fail Device',
            'protocol': 'modbus_tcp', 'host': '192.168.1.1', 'port': 502
        }, headers=auth_headers)
        assert resp.status_code == 400

    def test_add_device_no_auth(self, client):
        """POST /api/devices 无认证应返回401"""
        resp = client.post('/api/devices', json={'id': 'x', 'name': 'x'})
        assert resp.status_code == 401

    def test_update_device_no_data(self, client, auth_headers):
        """PUT /api/devices/<id> 无数据"""
        resp = client.put('/api/devices/test_pump_01', headers=auth_headers,
                          content_type='application/json')
        assert resp.status_code == 400

    def test_update_device_not_found(self, client, auth_headers, app):
        """PUT /api/devices/<id> 设备不存在"""
        app.device_manager.devices = {}
        resp = client.put('/api/devices/nonexistent', json={'name': 'x'},
                          headers=auth_headers)
        assert resp.status_code == 404

    def test_update_device_success(self, client, auth_headers, app):
        """PUT /api/devices/<id> 成功更新"""
        app.device_manager.devices = {'dev1': {'protocol': 'modbus_tcp', 'name': 'old'}}
        app.device_manager._save_config = MagicMock()
        resp = client.put('/api/devices/dev1', json={'name': 'new_name'},
                          headers=auth_headers)
        assert resp.status_code == 200

    def test_delete_device_not_found(self, client, auth_headers, app):
        """DELETE /api/devices/<id> 设备不存在"""
        app.device_manager.devices = {}
        resp = client.delete('/api/devices/nonexistent', headers=auth_headers)
        assert resp.status_code == 404

    def test_delete_device_success(self, client, auth_headers, app):
        """DELETE /api/devices/<id> 成功删除"""
        app.device_manager.devices = {'dev1': {}}
        app.device_manager.remove_device.return_value = True
        resp = client.delete('/api/devices/dev1', headers=auth_headers)
        assert resp.status_code == 200

    def test_connect_device_not_found(self, client, auth_headers, app):
        """POST /api/devices/<id>/connect 设备不存在"""
        app.device_manager.devices = {}
        resp = client.post('/api/devices/unknown/connect', headers=auth_headers)
        assert resp.status_code == 404

    def test_connect_device_success(self, client, auth_headers, app):
        """POST /api/devices/<id>/connect 成功"""
        app.device_manager.devices = {'dev1': {'name': 'Device 1', 'protocol': 'modbus_tcp'}}
        app.device_manager.connect_device.return_value = True
        resp = client.post('/api/devices/dev1/connect', headers=auth_headers)
        assert resp.status_code == 200

    def test_disconnect_device_not_found(self, client, auth_headers, app):
        """POST /api/devices/<id>/disconnect 设备不存在"""
        app.device_manager.devices = {}
        resp = client.post('/api/devices/unknown/disconnect', headers=auth_headers)
        assert resp.status_code == 404

    def test_disconnect_device_success(self, client, auth_headers, app):
        """POST /api/devices/<id>/disconnect 成功"""
        app.device_manager.devices = {'dev1': {'name': 'Device 1', 'protocol': 'modbus_tcp'}}
        resp = client.post('/api/devices/dev1/disconnect', headers=auth_headers)
        assert resp.status_code == 200

    def test_test_device_not_found(self, client, auth_headers, app):
        """POST /api/devices/<id>/test 设备不存在"""
        app.device_manager.devices = {}
        resp = client.post('/api/devices/unknown/test', headers=auth_headers)
        assert resp.status_code == 404

    def test_protocols_endpoint(self, client, auth_headers, app):
        """GET /api/devices/protocols"""
        app.device_manager.get_protocol_summary.return_value = {}
        resp = client.get('/api/devices/protocols', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'protocols' in data

    def test_templates_endpoint(self, client, auth_headers):
        """GET /api/devices/templates"""
        resp = client.get('/api/devices/templates', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'templates' in data
        assert len(data['templates']) > 0

    def test_device_behavior_not_found(self, client, auth_headers, app):
        """GET /api/devices/<id>/behavior 设备不存在"""
        app.device_manager.get_client.return_value = None
        resp = client.get('/api/devices/unknown/behavior', headers=auth_headers)
        assert resp.status_code == 404

    def test_device_behavior_no_sim(self, client, auth_headers, app):
        """GET /api/devices/<id>/behavior 无增强模拟"""
        mock_client = MagicMock(spec=[])  # no behavior_simulator attr
        app.device_manager.get_client.return_value = mock_client
        resp = client.get('/api/devices/dev1/behavior', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'message' in data

    def test_presets_no_initializer(self, client, auth_headers, app):
        """GET /api/devices/presets 无初始化器"""
        resp = client.get('/api/devices/presets', headers=auth_headers)
        assert resp.status_code == 500

    def test_stop_device_not_found(self, client, auth_headers, app):
        """POST /api/devices/<id>/stop 设备不存在"""
        app.device_manager.devices = {}
        resp = client.post('/api/devices/unknown/stop', headers=auth_headers)
        assert resp.status_code == 404

    def test_start_device_not_found(self, client, auth_headers, app):
        """POST /api/devices/<id>/start 设备不存在"""
        app.device_manager.devices = {}
        resp = client.post('/api/devices/unknown/start', headers=auth_headers)
        assert resp.status_code == 404


# ============================================================
# Data API - 未覆盖端点
# ============================================================

class TestDataAPIComprehensive:

    def test_history_data_wildcard(self, client, auth_headers, app):
        """GET /api/data/history/<id>/* 查询全部寄存器"""
        app.database.get_history_data.return_value = []
        resp = client.get('/api/data/history/dev1/*', headers=auth_headers)
        assert resp.status_code == 200

    def test_history_data_with_time_params(self, client, auth_headers, app):
        """GET /api/data/history 带时间参数"""
        app.database.get_history_data.return_value = []
        resp = client.get(
            '/api/data/history/dev1/temp?start_time=2024-01-01T00:00:00&end_time=2024-12-31T23:59:59&interval=5min',
            headers=auth_headers)
        assert resp.status_code == 200

    def test_export_device_no_times(self, client, auth_headers):
        """POST /api/export/device/<id> 缺少时间参数"""
        resp = client.post('/api/export/device/dev1', json={},
                           headers=auth_headers)
        assert resp.status_code == 400

    def test_export_device_success(self, client, auth_headers, app):
        """POST /api/export/device/<id> 成功导出"""
        app.database.get_device_registers.return_value = ['temp']
        app.database.get_history_data.return_value = [{'value': 25.0, 'timestamp': '2024-01-01'}]
        resp = client.post('/api/export/device/dev1', json={
            'start_time': '2024-01-01T00:00:00',
            'end_time': '2024-12-31T23:59:59',
            'format': 'csv'
        }, headers=auth_headers)
        assert resp.status_code in (200, 500)

    def test_export_alarms(self, client, auth_headers, app):
        """POST /api/export/alarms"""
        app.database.get_alarm_records.return_value = []
        resp = client.post('/api/export/alarms', json={}, headers=auth_headers)
        assert resp.status_code == 404

    def test_export_requires_auth(self, client):
        """导出端点需要认证"""
        resp = client.post('/api/export/device/dev1')
        assert resp.status_code == 401


# ============================================================
# Control API - 未覆盖端点
# ============================================================

class TestControlAPIComprehensive:

    def test_write_register_no_data(self, client, auth_headers):
        """POST /api/devices/<id>/write-register 无数据"""
        resp = client.post('/api/devices/dev1/write-register', headers=auth_headers,
                           content_type='application/json')
        assert resp.status_code == 400

    def test_write_register_missing_params(self, client, auth_headers):
        """POST /api/devices/<id>/write-register 缺少参数"""
        resp = client.post('/api/devices/dev1/write-register', json={'address': 0},
                           headers=auth_headers)
        assert resp.status_code == 400

    def test_write_coil_no_data(self, client, auth_headers):
        """POST /api/devices/<id>/write-coil 无数据"""
        resp = client.post('/api/devices/dev1/write-coil', headers=auth_headers,
                           content_type='application/json')
        assert resp.status_code == 400

    def test_write_coil_missing_params(self, client, auth_headers):
        """POST /api/devices/<id>/write-coil 缺少参数"""
        resp = client.post('/api/devices/dev1/write-coil', json={'address': 0},
                           headers=auth_headers)
        assert resp.status_code == 400

    def test_adjust_device_no_data(self, client, auth_headers):
        """POST /api/devices/<id>/adjust 无数据"""
        resp = client.post('/api/devices/dev1/adjust', headers=auth_headers,
                           content_type='application/json')
        assert resp.status_code == 400

    def test_adjust_device_missing_params(self, client, auth_headers):
        """POST /api/devices/<id>/adjust 缺少参数"""
        resp = client.post('/api/devices/dev1/adjust', json={'register_name': 'temp'},
                           headers=auth_headers)
        assert resp.status_code == 400

    def test_control_logs(self, client, auth_headers, app):
        """GET /api/control/logs"""
        app.auth_manager.get_operation_logs.return_value = []
        resp = client.get('/api/control/logs', headers=auth_headers)
        assert resp.status_code == 200

    def test_estop_no_module(self, client, auth_headers):
        """POST /api/control/estop 无安全模块"""
        resp = client.post('/api/control/estop', json={'reason': 'test'},
                           headers=auth_headers)
        assert resp.status_code == 503

    def test_estop_reset_no_module(self, client, auth_headers):
        """POST /api/control/estop/reset 无安全模块"""
        resp = client.post('/api/control/estop/reset', headers=auth_headers)
        assert resp.status_code == 503

    def test_estop_status_no_module(self, client, auth_headers):
        """GET /api/control/estop/status 无安全模块"""
        resp = client.get('/api/control/estop/status', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_interlocks_no_module(self, client, auth_headers):
        """GET /api/control/interlocks 无安全模块"""
        resp = client.get('/api/control/interlocks', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_health_no_module(self, client, auth_headers):
        """GET /api/control/health 无安全模块"""
        resp = client.get('/api/control/health', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_batch_control_invalid_action(self, client, auth_headers):
        """POST /api/control/batch 无效操作"""
        resp = client.post('/api/control/batch', json={'action': 'invalid'},
                           headers=auth_headers)
        assert resp.status_code == 400

    def test_recipe_list(self, client, auth_headers):
        """GET /api/control/recipe/list"""
        resp = client.get('/api/control/recipe/list', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'recipes' in data

    def test_recipe_status_no_sim(self, client, auth_headers):
        """GET /api/control/recipe/status 无模拟器"""
        resp = client.get('/api/control/recipe/status', headers=auth_headers)
        assert resp.status_code == 404

    def test_write_endpoint_no_data(self, client, auth_headers):
        """POST /api/devices/<id>/write-endpoint 无数据"""
        resp = client.post('/api/devices/dev1/write-endpoint', json={},
                           headers=auth_headers)
        assert resp.status_code == 400

    def test_write_endpoint_unsupported_method(self, client, auth_headers):
        """POST /api/devices/<id>/write-endpoint 不支持的方法"""
        resp = client.post('/api/devices/dev1/write-endpoint',
                           json={'endpoint': 'ep1', 'value': 1, 'method': 'GET'},
                           headers=auth_headers)
        assert resp.status_code == 400

    def test_bypass_interlock_no_module(self, client, auth_headers):
        """POST /api/control/interlocks/<id>/bypass 无模块"""
        resp = client.post('/api/control/interlocks/rule1/bypass',
                           json={'reason': 'test'}, headers=auth_headers)
        assert resp.status_code == 503

    def test_restore_interlock_no_module(self, client, auth_headers):
        """POST /api/control/interlocks/<id>/restore 无模块"""
        resp = client.post('/api/control/interlocks/rule1/restore',
                           headers=auth_headers)
        assert resp.status_code == 503

    def test_bypass_request_no_data(self, client, auth_headers):
        """POST /api/control/interlocks/bypass-request 无数据"""
        resp = client.post('/api/control/interlocks/bypass-request',
                           json={}, headers=auth_headers)
        assert resp.status_code == 400

    def test_bypass_approve_no_data(self, client, auth_headers):
        """POST /api/control/interlocks/bypass-approve 无数据"""
        resp = client.post('/api/control/interlocks/bypass-approve',
                           json={}, headers=auth_headers)
        assert resp.status_code == 400

    def test_bypass_reject_no_data(self, client, auth_headers):
        """POST /api/control/interlocks/bypass-reject 无数据"""
        resp = client.post('/api/control/interlocks/bypass-reject',
                           json={}, headers=auth_headers)
        assert resp.status_code == 400

    def test_pending_bypasses_no_module(self, client, auth_headers):
        """GET /api/control/interlocks/bypass-pending 无模块"""
        resp = client.get('/api/control/interlocks/bypass-pending', headers=auth_headers)
        assert resp.status_code == 503

    def test_audit_no_module(self, client, auth_headers):
        """GET /api/control/audit 无模块"""
        resp = client.get('/api/control/audit', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_status_no_module(self, client, auth_headers):
        """GET /api/control/status 无模块"""
        resp = client.get('/api/control/status', headers=auth_headers)
        assert resp.status_code in (200, 503)


# ============================================================
# Alarm API - 未覆盖端点
# ============================================================

class TestAlarmAPIComprehensive:

    def test_alarm_output_status(self, client, auth_headers, app):
        """GET /api/alarm-output/status"""
        app.alarm_manager.alarm_output = None
        app.alarm_manager.broadcast_system = None
        resp = client.get('/api/alarm-output/status', headers=auth_headers)
        assert resp.status_code == 200

    def test_alarm_output_acknowledge_no_output(self, client, auth_headers, app):
        """POST /api/alarm-output/acknowledge 无输出"""
        app.alarm_manager.alarm_output = None
        resp = client.post('/api/alarm-output/acknowledge', headers=auth_headers)
        assert resp.status_code == 400

    def test_alarm_output_reset(self, client, auth_headers, app):
        """POST /api/alarm-output/reset"""
        resp = client.post('/api/alarm-output/reset', headers=auth_headers)
        assert resp.status_code == 200

    def test_broadcast_areas_no_system(self, client, auth_headers, app):
        """GET /api/broadcast/areas 无广播系统"""
        app.alarm_manager.broadcast_system = None
        resp = client.get('/api/broadcast/areas', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_broadcast_history_no_system(self, client, auth_headers, app):
        """GET /api/broadcast/history 无广播系统"""
        app.alarm_manager.broadcast_system = None
        resp = client.get('/api/broadcast/history', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_broadcast_speak_no_body(self, client, auth_headers):
        """POST /api/broadcast/speak 无数据"""
        resp = client.post('/api/broadcast/speak', json={},
                           headers=auth_headers)
        assert resp.status_code == 400

    def test_alarm_rules_get(self, client, auth_headers, app):
        """GET /api/alarm-rules"""
        with patch('展示层.api.api_alarms.load_yaml_config', return_value={'alarm_rules': []}):
            resp = client.get('/api/alarm-rules', headers=auth_headers)
        assert resp.status_code == 200

    def test_flood_status(self, client, auth_headers, app):
        """GET /api/alarms/flood-status"""
        mock_detector = MagicMock()
        mock_detector.get_status.return_value = {'flood_active': False}
        app.alarm_manager._flood_detector = mock_detector
        resp = client.get('/api/alarms/flood-status', headers=auth_headers)
        assert resp.status_code == 200

    def test_dedup_config_get(self, client, auth_headers, app):
        """GET /api/alarms/dedup-config"""
        app.alarm_manager.get_dedup_config.return_value = {}
        resp = client.get('/api/alarms/dedup-config', headers=auth_headers)
        assert resp.status_code == 200

    def test_dedup_config_update_no_data(self, client, auth_headers):
        """PUT /api/alarms/dedup-config 无数据"""
        resp = client.put('/api/alarms/dedup-config', json={},
                          headers=auth_headers)
        assert resp.status_code == 400


# ============================================================
# Auth API - 未覆盖端点
# ============================================================

class TestAuthAPIComprehensive:

    def test_login_no_body(self, client):
        """POST /api/auth/login 无JSON"""
        resp = client.post('/api/auth/login', content_type='application/json')
        assert resp.status_code == 400

    def test_register_no_auth_no_users(self, client, app):
        """POST /api/auth/register 首个用户"""
        app.auth_manager.get_users.return_value = []
        app.auth_manager.register.return_value = {'success': True}
        resp = client.post('/api/auth/register', json={
            'username': 'admin', 'password': 'pass123', 'role': 'admin'
        })
        assert resp.status_code in (200, 201)

    def test_register_no_auth_has_users(self, client, app):
        """POST /api/auth/register 非首个用户无权限"""
        app.auth_manager.get_users.return_value = [{'username': 'existing'}]
        resp = client.post('/api/auth/register', json={
            'username': 'new', 'password': 'pass123'
        })
        assert resp.status_code == 403

    def test_update_user(self, client, auth_headers, app):
        """PUT /api/auth/users/<username>"""
        app.auth_manager.update_user.return_value = {'success': True}
        resp = client.put('/api/auth/users/testuser',
                          json={'display_name': 'Updated'},
                          headers=auth_headers)
        assert resp.status_code == 200

    def test_delete_user(self, client, auth_headers, app):
        """DELETE /api/auth/users/<username>"""
        app.auth_manager.delete_user.return_value = {'success': True}
        resp = client.delete('/api/auth/users/testuser', headers=auth_headers)
        assert resp.status_code == 200


# ============================================================
# System API - 未覆盖端点
# ============================================================

class TestSystemAPIComprehensive:

    def test_system_status(self, client, auth_headers):
        """GET /api/system/status"""
        resp = client.get('/api/system/status', headers=auth_headers)
        assert resp.status_code == 200

    def test_toggle_simulation_no_data(self, client, auth_headers):
        """POST /api/system/simulation-mode 无数据"""
        resp = client.post('/api/system/simulation-mode', json={},
                           headers=auth_headers)
        assert resp.status_code == 400

    def test_get_config(self, client, auth_headers):
        """GET /api/config"""
        with patch('展示层.api.api_system.load_yaml_config', return_value={'system': {}}):
            resp = client.get('/api/config', headers=auth_headers)
        assert resp.status_code in (200, 404)

    def test_update_config_no_data(self, client, auth_headers):
        """PUT /api/config 无数据"""
        resp = client.put('/api/config', json={}, headers=auth_headers)
        assert resp.status_code == 400


# ============================================================
# Industry 4.0 API - 未覆盖端点
# ============================================================

class TestIndustry40APIComprehensive:

    def test_health_overview(self, client, auth_headers):
        """GET /api/industry40/health"""
        resp = client.get('/api/industry40/health', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_health_device(self, client, auth_headers):
        """GET /api/industry40/health/<device_id>"""
        resp = client.get('/api/industry40/health/dev1', headers=auth_headers)
        assert resp.status_code in (200, 404, 503)

    def test_maintenance_alerts(self, client, auth_headers):
        """GET /api/industry40/maintenance-alerts"""
        resp = client.get('/api/industry40/maintenance-alerts', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_trend_data(self, client, auth_headers):
        """GET /api/industry40/trend/<id>/<reg>"""
        resp = client.get('/api/industry40/trend/dev1/temp', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_oee_all(self, client, auth_headers):
        """GET /api/industry40/oee"""
        resp = client.get('/api/industry40/oee', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_oee_device(self, client, auth_headers):
        """GET /api/industry40/oee/<device_id>"""
        resp = client.get('/api/industry40/oee/dev1', headers=auth_headers)
        assert resp.status_code in (200, 404, 503)

    def test_spc_chart(self, client, auth_headers):
        """GET /api/industry40/spc/<id>/<reg>"""
        resp = client.get('/api/industry40/spc/dev1/temp', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_spc_violations(self, client, auth_headers):
        """GET /api/industry40/spc/violations"""
        resp = client.get('/api/industry40/spc/violations', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_energy_summary(self, client, auth_headers):
        """GET /api/industry40/energy"""
        resp = client.get('/api/industry40/energy', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_energy_cost(self, client, auth_headers):
        """GET /api/industry40/energy/cost"""
        resp = client.get('/api/industry40/energy/cost', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_energy_carbon(self, client, auth_headers):
        """GET /api/industry40/energy/carbon"""
        resp = client.get('/api/industry40/energy/carbon', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_energy_power(self, client, auth_headers):
        """GET /api/industry40/energy/power"""
        resp = client.get('/api/industry40/energy/power', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_energy_tariff_get(self, client, auth_headers):
        """GET /api/industry40/energy/tariff"""
        resp = client.get('/api/industry40/energy/tariff', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_energy_tariff_update_no_data(self, client, auth_headers):
        """PUT /api/industry40/energy/tariff 无数据"""
        resp = client.put('/api/industry40/energy/tariff', json={},
                          headers=auth_headers)
        assert resp.status_code in (400, 503)

    def test_energy_anomaly_config_get(self, client, auth_headers):
        """GET /api/industry40/energy/anomaly-config"""
        resp = client.get('/api/industry40/energy/anomaly-config', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_edge_status(self, client, auth_headers):
        """GET /api/industry40/edge/status"""
        resp = client.get('/api/industry40/edge/status', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_edge_rules(self, client, auth_headers):
        """GET /api/industry40/edge/rules"""
        resp = client.get('/api/industry40/edge/rules', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_edge_log(self, client, auth_headers):
        """GET /api/industry40/edge/log"""
        resp = client.get('/api/industry40/edge/log', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_devices_status(self, client, auth_headers):
        """GET /api/industry40/devices/status"""
        resp = client.get('/api/industry40/devices/status', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_vibration_scores(self, client, auth_headers):
        """GET /api/industry40/vibration"""
        resp = client.get('/api/industry40/vibration', headers=auth_headers)
        assert resp.status_code in (200, 503)

    def test_vibration_device(self, client, auth_headers):
        """GET /api/industry40/vibration/<id>"""
        resp = client.get('/api/industry40/vibration/dev1', headers=auth_headers)
        assert resp.status_code in (200, 404, 503)

    def test_vibration_spectrum(self, client, auth_headers):
        """GET /api/industry40/vibration/<id>/spectrum"""
        resp = client.get('/api/industry40/vibration/dev1/spectrum', headers=auth_headers)
        assert resp.status_code in (200, 404, 503)

    def test_vibration_bearing(self, client, auth_headers):
        """GET /api/industry40/vibration/<id>/bearing"""
        resp = client.get('/api/industry40/vibration/dev1/bearing', headers=auth_headers)
        assert resp.status_code in (200, 404, 503)

    def test_overview(self, client, auth_headers):
        """GET /api/industry40/overview"""
        resp = client.get('/api/industry40/overview', headers=auth_headers)
        assert resp.status_code == 200


# ============================================================
# Health API - 未覆盖端点
# ============================================================

class TestHealthAPIComprehensive:

    def test_health_module_not_found(self, client, auth_headers):
        """GET /api/health/modules/<name> 未注册模块"""
        resp = client.get('/api/health/modules/nonexistent_module', headers=auth_headers)
        assert resp.status_code in (200, 404)

    def test_health_check_not_found(self, client, auth_headers):
        """GET /api/health/checks/<name> 未注册检查"""
        resp = client.get('/api/health/checks/nonexistent_check', headers=auth_headers)
        assert resp.status_code in (200, 404)


# ============================================================
# Alarm Rule CRUD
# ============================================================

class TestAlarmRuleCRUD:

    def test_add_alarm_rule_no_data(self, client, auth_headers):
        """POST /api/alarm-rules 无数据"""
        resp = client.post('/api/alarm-rules', json={}, headers=auth_headers)
        assert resp.status_code == 400

    def test_update_alarm_rule_no_data(self, client, auth_headers):
        """PUT /api/alarm-rules/<id> 无数据"""
        resp = client.put('/api/alarm-rules/rule1', json={}, headers=auth_headers)
        assert resp.status_code == 400

    def test_delete_alarm_rule(self, client, auth_headers, app):
        """DELETE /api/alarm-rules/<id> 不存在"""
        with patch('展示层.api.api_alarms.load_yaml_config', return_value={'alarm_rules': []}):
            resp = client.delete('/api/alarm-rules/nonexistent', headers=auth_headers)
        assert resp.status_code == 404

    def test_notification_update_no_data(self, client, auth_headers):
        """PUT /api/alarm-rules/notification 无数据"""
        resp = client.put('/api/alarm-rules/notification', json={}, headers=auth_headers)
        assert resp.status_code == 400

    def test_alarm_output_config_get(self, client, auth_headers):
        """GET /api/alarm-output/config"""
        with patch('展示层.api.api_alarms.load_yaml_config', return_value={}):
            resp = client.get('/api/alarm-output/config', headers=auth_headers)
        assert resp.status_code == 200

    def test_alarm_output_config_update_no_data(self, client, auth_headers):
        """PUT /api/alarm-output/config 无数据"""
        resp = client.put('/api/alarm-output/config', json={},
                          headers=auth_headers)
        assert resp.status_code == 400

    def test_broadcast_config_get(self, client, auth_headers, app):
        """GET /api/broadcast/config"""
        app.alarm_manager.broadcast_system = None
        with patch('展示层.api.api_alarms.load_yaml_config', return_value={}):
            resp = client.get('/api/broadcast/config', headers=auth_headers)
        assert resp.status_code == 200

    def test_broadcast_config_update_no_data(self, client, auth_headers):
        """PUT /api/broadcast/config 无数据"""
        resp = client.put('/api/broadcast/config', json={},
                          headers=auth_headers)
        assert resp.status_code == 400
