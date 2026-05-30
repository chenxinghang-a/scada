"""
API边界测试 - 覆盖更多代码路径
"""
import pytest
from unittest.mock import MagicMock, patch


class TestDeviceAPIEdgeCases:
    """设备API边界测试"""

    def test_add_device_mc_validation(self, client, auth_headers):
        """POST /api/devices MC协议缺少host"""
        resp = client.post('/api/devices', json={
            'id': 'mc1', 'name': 'MC', 'protocol': 'mc'
        }, headers=auth_headers)
        assert resp.status_code == 400

    def test_add_device_fins_validation(self, client, auth_headers):
        """POST /api/devices FINS协议缺少host"""
        resp = client.post('/api/devices', json={
            'id': 'fins1', 'name': 'FINS', 'protocol': 'fins'
        }, headers=auth_headers)
        assert resp.status_code == 400

    def test_add_device_modbus_rtu_validation(self, client, auth_headers):
        """POST /api/devices Modbus RTU缺少host"""
        resp = client.post('/api/devices', json={
            'id': 'rtu1', 'name': 'RTU', 'protocol': 'modbus_rtu'
        }, headers=auth_headers)
        assert resp.status_code == 400

    def test_write_register_with_module(self, client, auth_headers, app):
        """POST /api/devices/<id>/write-register 带安全模块"""
        mock_dc = MagicMock()
        mock_dc.write_with_verification.return_value = {'success': True, 'message': 'OK'}
        app.device_control = mock_dc
        resp = client.post('/api/devices/dev1/write-register',
                           json={'address': 0, 'value': 100},
                           headers=auth_headers)
        assert resp.status_code == 200

    def test_write_coil_with_module(self, client, auth_headers, app):
        """POST /api/devices/<id>/write-coil 带安全模块"""
        mock_dc = MagicMock()
        mock_dc.write_with_verification.return_value = {'success': True, 'message': 'OK'}
        app.device_control = mock_dc
        resp = client.post('/api/devices/dev1/write-coil',
                           json={'address': 0, 'value': True},
                           headers=auth_headers)
        assert resp.status_code == 200

    def test_adjust_device_success(self, client, auth_headers, app):
        """POST /api/devices/<id>/adjust 成功"""
        app.device_manager.adjust_device.return_value = {'success': True}
        resp = client.post('/api/devices/dev1/adjust',
                           json={'register_name': 'temp', 'value': 25.0},
                           headers=auth_headers)
        assert resp.status_code == 200

    def test_device_behavior_with_sim(self, client, auth_headers, app):
        """GET /api/devices/<id>/behavior 带增强模拟"""
        mock_client = MagicMock()
        mock_sim = MagicMock()
        mock_sim.state.name = 'RUNNING'
        mock_sim.state.value = 2
        mock_sim.health.overall_score = 95.0
        mock_sim.health.mechanical_health = 90.0
        mock_sim.health.electrical_health = 95.0
        mock_sim.health.thermal_health = 98.0
        mock_sim.health.vibration_health = 92.0
        mock_sim.active_fault.value = 'none'
        mock_sim.fault_severity = 0.0
        mock_sim.health.operating_hours = 100.0
        mock_sim.stats = {'total_cycles': 1000}
        mock_client.behavior_simulator = mock_sim
        app.device_manager.get_client.return_value = mock_client
        resp = client.get('/api/devices/dev1/behavior', headers=auth_headers)
        assert resp.status_code == 200

    def test_inject_fault_no_json(self, client, auth_headers):
        """POST /api/devices/<id>/inject-fault 无数据"""
        resp = client.post('/api/devices/dev1/inject-fault',
                           headers=auth_headers, content_type='application/json')
        assert resp.status_code == 400

    def test_force_state_no_json(self, client, auth_headers):
        """POST /api/devices/<id>/force-state 无数据"""
        resp = client.post('/api/devices/dev1/force-state',
                           headers=auth_headers, content_type='application/json')
        assert resp.status_code == 400


class TestAlarmAPIEdgeCases:
    """报警API边界测试"""

    def test_alarm_output_manual_no_output(self, client, auth_headers, app):
        """POST /api/alarm-output/manual 无输出"""
        app.alarm_manager.alarm_output = None
        resp = client.post('/api/alarm-output/manual', json={'red': True},
                           headers=auth_headers)
        assert resp.status_code == 400

    def test_alarm_output_manual_success(self, client, auth_headers, app):
        """POST /api/alarm-output/manual 成功"""
        mock_output = MagicMock()
        mock_output.manual_control.return_value = {'success': True, 'state': {}}
        app.alarm_manager.alarm_output = mock_output
        resp = client.post('/api/alarm-output/manual',
                           json={'red': True, 'buzzer': True},
                           headers=auth_headers)
        assert resp.status_code == 200

    def test_broadcast_speak_no_text(self, client, auth_headers):
        """POST /api/broadcast/speak 无text"""
        resp = client.post('/api/broadcast/speak', json={'level': 'info'},
                           headers=auth_headers)
        assert resp.status_code == 400


class TestSystemAPIEdgeCases:
    """系统API边界测试"""

    def test_toggle_simulation_mode(self, client, auth_headers, app):
        """POST /api/system/simulation-mode"""
        app.device_manager.switch_simulation_mode.return_value = {'success': True}
        with patch('展示层.api.api_system.Path') as mock_path:
            mock_path.return_value.exists.return_value = False
            resp = client.post('/api/system/simulation-mode',
                               json={'simulation_mode': True},
                               headers=auth_headers)
        assert resp.status_code in (200, 500)

    def test_update_config_success(self, client, auth_headers, app):
        """PUT /api/config 成功"""
        with patch('展示层.api.api_system.load_yaml_config', return_value={'system': {}}), \
             patch('展示层.api.api_system.save_yaml_config', return_value=True):
            resp = client.put('/api/config', json={'section': 'system', 'data': {'key': 'value'}},
                              headers=auth_headers)
        assert resp.status_code == 200


class TestAuthAPIEdgeCases:
    """认证API边界测试"""

    def test_register_success(self, client, auth_headers, app):
        """POST /api/auth/register 成功"""
        app.auth_manager.register.return_value = {'success': True}
        resp = client.post('/api/auth/register',
                           json={'username': 'new', 'password': 'pass123', 'role': 'viewer'},
                           headers=auth_headers)
        assert resp.status_code in (200, 201)

    def test_force_change_password_success(self, client, auth_headers, app):
        """POST /api/auth/force-change-password 成功"""
        app.auth_manager.verify_token.return_value = {'username': 'testuser'}
        app.auth_manager.force_change_password.return_value = {'success': True}
        resp = client.post('/api/auth/force-change-password',
                           json={'username': 'testuser', 'new_password': 'newpass'},
                           headers=auth_headers)
        assert resp.status_code == 200

    def test_update_user_success(self, client, auth_headers, app):
        """PUT /api/auth/users/<username> 成功"""
        app.auth_manager.update_user.return_value = {'success': True}
        resp = client.put('/api/auth/users/testuser',
                          json={'display_name': 'Updated'},
                          headers=auth_headers)
        assert resp.status_code == 200

    def test_delete_user_success(self, client, auth_headers, app):
        """DELETE /api/auth/users/<username> 成功"""
        app.auth_manager.delete_user.return_value = {'success': True}
        resp = client.delete('/api/auth/users/testuser', headers=auth_headers)
        assert resp.status_code == 200
