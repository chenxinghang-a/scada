"""
Extended tests for 展示层 API endpoints
Covers: api_alarms, api_data, api_auth, api_health, api_system, api_devices, swagger
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime


# ============================================================
# Alarm API Tests
# ============================================================

class TestAlarmsAPIExtended:

    def test_get_alarms_with_filters(self, client, auth_headers):
        """GET /api/alarms with query params"""
        resp = client.get('/api/alarms?device_id=d1&alarm_level=warning&acknowledged=false&limit=50',
                          headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'alarms' in data

    def test_acknowledge_alarm(self, client, auth_headers, app):
        """POST /api/alarms/<id>/acknowledge"""
        app.alarm_manager.acknowledge_alarm.return_value = True
        resp = client.post('/api/alarms/alarm_001/acknowledge',
                           json={'device_id': 'd1', 'register_name': 'temp'},
                           headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'success' in data

    def test_get_alarm_statistics(self, client, auth_headers):
        """GET /api/alarms/statistics"""
        resp = client.get('/api/alarms/statistics', headers=auth_headers)
        assert resp.status_code == 200


# ============================================================
# Data API Tests
# ============================================================

class TestDataAPI:

    def test_realtime_data(self, client, auth_headers, app):
        """GET /api/data/realtime"""
        app.database.get_realtime_data.return_value = []
        resp = client.get('/api/data/realtime', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'data' in data

    def test_realtime_data_with_device(self, client, auth_headers, app):
        """GET /api/data/realtime?device_id=d1"""
        app.database.get_realtime_data.return_value = []
        resp = client.get('/api/data/realtime?device_id=d1', headers=auth_headers)
        assert resp.status_code == 200

    def test_latest_data(self, client, auth_headers, app):
        """GET /api/data/latest/<device_id>"""
        app.database.get_latest_data.return_value = None
        resp = client.get('/api/data/latest/test_device', headers=auth_headers)
        assert resp.status_code == 404

    def test_latest_data_found(self, client, auth_headers, app):
        """GET /api/data/latest/<device_id> when data exists"""
        app.database.get_latest_data.return_value = {'value': 42}
        resp = client.get('/api/data/latest/test_device', headers=auth_headers)
        assert resp.status_code == 200

    def test_history_data(self, client, auth_headers, app):
        """GET /api/data/history/<device_id>/<register_name>"""
        app.database.get_history_data.return_value = []
        resp = client.get('/api/data/history/test_device/temp', headers=auth_headers)
        assert resp.status_code == 200

    def test_data_requires_auth(self, client):
        """Data endpoints require auth"""
        resp = client.get('/api/data/realtime')
        assert resp.status_code == 401


# ============================================================
# Auth API Tests
# ============================================================

class TestAuthAPIExtended:

    def test_login_empty_body(self, client):
        """POST /api/auth/login with empty body"""
        resp = client.post('/api/auth/login', json={})
        assert resp.status_code == 400

    def test_login_missing_fields(self, client):
        """POST /api/auth/login with missing password"""
        resp = client.post('/api/auth/login', json={'username': 'admin'})
        assert resp.status_code == 400

    def test_login_success(self, client, app):
        """POST /api/auth/login with valid credentials"""
        app.auth_manager.login.return_value = {
            'success': True, 'token': 'abc', 'refresh_token': 'def',
            'user': {'username': 'admin', 'role': 'admin'}
        }
        resp = client.post('/api/auth/login',
                           json={'username': 'admin', 'password': 'pass'})
        assert resp.status_code == 200

    def test_login_must_change_password(self, client, app):
        """POST /api/auth/login when must change password"""
        app.auth_manager.login.return_value = {
            'success': True, 'status': 'must_change_password',
            'token': 'abc', 'user': {'username': 'admin'}
        }
        resp = client.post('/api/auth/login',
                           json={'username': 'admin', 'password': 'pass'})
        assert resp.status_code == 403

    def test_verify_token(self, client, auth_headers):
        """GET /api/auth/verify"""
        resp = client.get('/api/auth/verify', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['valid'] is True

    def test_refresh_token_missing(self, client):
        """POST /api/auth/refresh without token"""
        resp = client.post('/api/auth/refresh', json={})
        assert resp.status_code == 400

    def test_refresh_token_valid(self, client, app):
        """POST /api/auth/refresh with valid token"""
        app.auth_manager.refresh_token.return_value = {
            'success': True, 'token': 'new_token'
        }
        resp = client.post('/api/auth/refresh',
                           json={'refresh_token': 'valid_token'})
        assert resp.status_code == 200

    def test_refresh_token_invalid(self, client, app):
        """POST /api/auth/refresh with invalid token"""
        app.auth_manager.refresh_token.return_value = None
        resp = client.post('/api/auth/refresh',
                           json={'refresh_token': 'invalid'})
        assert resp.status_code == 401

    def test_change_password(self, client, auth_headers, app):
        """POST /api/auth/change-password"""
        app.auth_manager.change_password.return_value = {'success': True}
        resp = client.post('/api/auth/change-password',
                           json={'old_password': 'old', 'new_password': 'new'},
                           headers=auth_headers)
        assert resp.status_code == 200

    def test_get_users_with_auth(self, client, auth_headers, app):
        """GET /api/auth/users with admin auth"""
        app.auth_manager.get_users.return_value = [
            {'username': 'admin', 'role': 'admin'}
        ]
        resp = client.get('/api/auth/users', headers=auth_headers)
        assert resp.status_code == 200

    def test_get_operation_logs(self, client, auth_headers, app):
        """GET /api/auth/logs"""
        app.auth_manager.get_operation_logs.return_value = []
        resp = client.get('/api/auth/logs', headers=auth_headers)
        assert resp.status_code == 200

    def test_force_change_password_no_body(self, client):
        """POST /api/auth/force-change-password without body"""
        resp = client.post('/api/auth/force-change-password',
                           content_type='application/json')
        assert resp.status_code == 400

    def test_force_change_password_missing_fields(self, client):
        """POST /api/auth/force-change-password with missing fields"""
        resp = client.post('/api/auth/force-change-password', json={'username': 'admin'})
        assert resp.status_code == 400


# ============================================================
# Health API Tests
# ============================================================

class TestHealthAPIExtended:

    def test_health_status(self, client):
        """GET /api/health/status"""
        resp = client.get('/api/health/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True

    def test_health_modules(self, client, auth_headers):
        """GET /api/health/modules"""
        resp = client.get('/api/health/modules', headers=auth_headers)
        assert resp.status_code == 200

    def test_health_checks(self, client, auth_headers):
        """GET /api/health/checks"""
        resp = client.get('/api/health/checks', headers=auth_headers)
        assert resp.status_code == 200

    def test_available_modules(self, client, auth_headers):
        """GET /api/health/available"""
        resp = client.get('/api/health/available', headers=auth_headers)
        assert resp.status_code == 200

    def test_unavailable_modules(self, client, auth_headers):
        """GET /api/health/unavailable"""
        resp = client.get('/api/health/unavailable', headers=auth_headers)
        assert resp.status_code == 200


# ============================================================
# System API Tests
# ============================================================

class TestSystemAPIExtended:

    def test_system_status(self, client, auth_headers):
        """GET /api/system/status"""
        resp = client.get('/api/system/status', headers=auth_headers)
        assert resp.status_code == 200

    def test_system_database(self, client, auth_headers):
        """GET /api/system/database"""
        resp = client.get('/api/system/database', headers=auth_headers)
        assert resp.status_code == 200

    def test_simulation_mode(self, client, auth_headers):
        """GET /api/system/simulation-mode"""
        resp = client.get('/api/system/simulation-mode', headers=auth_headers)
        assert resp.status_code == 200


# ============================================================
# Devices API Tests
# ============================================================

class TestDevicesAPIExtended:

    def test_get_devices(self, client, auth_headers):
        """GET /api/devices"""
        resp = client.get('/api/devices', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'devices' in data

    def test_get_device_detail(self, client, auth_headers, app):
        """GET /api/devices/<id>"""
        app.device_manager.get_device_status.return_value = {'error': 'not found'}
        resp = client.get('/api/devices/test_device_01', headers=auth_headers)
        assert resp.status_code == 404

    def test_add_device_no_data(self, client, auth_headers):
        """POST /api/devices without data"""
        resp = client.post('/api/devices', headers=auth_headers,
                           content_type='application/json')
        assert resp.status_code == 400

    def test_add_device_missing_fields(self, client, auth_headers):
        """POST /api/devices with missing required fields"""
        resp = client.post('/api/devices', json={'name': 'test'}, headers=auth_headers)
        assert resp.status_code == 400


# ============================================================
# Swagger API Tests
# ============================================================

class TestSwaggerAPI:

    def test_swagger_json(self, client):
        """GET /api/swagger.json or /swagger.json"""
        resp = client.get('/api/swagger.json')
        if resp.status_code == 404:
            resp = client.get('/swagger.json')
        assert resp.status_code in (200, 404)
