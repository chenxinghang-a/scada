"""
API契约测试
验证前后端API契约一致性
"""
import pytest
from unittest.mock import patch, MagicMock


class TestDeviceAPIContract:
    """设备API契约测试"""

    def test_get_devices_response_structure(self, client, auth_headers, app):
        """验证GET /api/devices响应结构"""
        app.device_manager.get_all_status.return_value = [
            {'device_id': 'dev1', 'name': 'Device 1', 'connected': True}
        ]
        resp = client.get('/api/devices', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'devices' in data
        assert isinstance(data['devices'], list)

    def test_get_device_response_structure(self, client, auth_headers, app):
        """验证GET /api/devices/<id>响应结构"""
        app.device_manager.get_device_status.return_value = {
            'device_id': 'dev1', 'name': 'Device 1', 'connected': True,
            'protocol': 'modbus_tcp', 'host': '192.168.1.1', 'port': 502
        }
        resp = client.get('/api/devices/dev1', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'device' in data
        device = data['device']
        assert 'device_id' in device
        assert 'name' in device
        assert 'connected' in device


class TestDataAPIContract:
    """数据API契约测试"""

    def test_get_realtime_response_structure(self, client, auth_headers, app):
        """验证GET /api/data/realtime响应结构"""
        app.database.get_realtime_data.return_value = [
            {'device_id': 'dev1', 'register_name': 'temp', 'value': 25.0}
        ]
        resp = client.get('/api/data/realtime', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'data' in data
        assert isinstance(data['data'], list)

    def test_get_history_response_structure(self, client, auth_headers, app):
        """验证GET /api/data/history响应结构"""
        app.database.get_history_data.return_value = [
            {'timestamp': '2026-01-01T00:00:00', 'value': 25.0}
        ]
        resp = client.get('/api/data/history/dev1/temp', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'data' in data


class TestAlarmAPIContract:
    """报警API契约测试"""

    def test_get_alarms_response_structure(self, client, auth_headers, app):
        """验证GET /api/alarms响应结构"""
        app.database.get_alarm_records.return_value = [
            {'alarm_id': 'a1', 'device_id': 'dev1', 'level': 'warning'}
        ]
        resp = client.get('/api/alarms', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'alarms' in data

    def test_acknowledge_alarm_response_structure(self, client, auth_headers, app):
        """验证POST /api/alarms/<id>/acknowledge响应结构"""
        app.database.acknowledge_alarm.return_value = True
        resp = client.post('/api/alarms/a1/acknowledge', json={
            'acknowledged_by': 'admin'
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'success' in data


class TestSystemAPIContract:
    """系统API契约测试"""

    def test_get_status_response_structure(self, client, auth_headers, app):
        """验证GET /api/system/status响应结构"""
        resp = client.get('/api/system/status', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'simulation_mode' in data

    def test_get_config_response_structure(self, client, auth_headers, app):
        """验证GET /api/config响应结构"""
        app.database.get_config.return_value = {'key': 'value'}
        resp = client.get('/api/config', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'config' in data


class TestHealthAPIContract:
    """健康检查API契约测试"""

    def test_health_status_response_structure(self, client):
        """验证GET /api/health/status响应结构"""
        resp = client.get('/api/health/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'success' in data
        assert 'data' in data
        assert 'global_status' in data['data']


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
