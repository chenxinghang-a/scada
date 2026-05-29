"""
Tests for REST API endpoints
"""

import pytest
from unittest.mock import patch, MagicMock


class TestHealthAPI:
    """Tests for /api/health endpoints"""

    def test_health_status_returns_200(self, client):
        """GET /api/health/status returns 200"""
        resp = client.get('/api/health/status')
        assert resp.status_code == 200

    def test_health_status_json(self, client):
        """Health endpoint returns JSON with expected structure"""
        resp = client.get('/api/health/status')
        data = resp.get_json()

        assert data is not None
        assert 'success' in data
        assert data['success'] is True
        assert 'data' in data

    def test_health_data_has_modules_and_checks(self, client):
        """Health data contains modules and checks info"""
        resp = client.get('/api/health/status')
        data = resp.get_json()

        health_data = data.get('data', {})
        assert 'global_status' in health_data
        assert 'modules' in health_data
        assert 'checks' in health_data


class TestDevicesAPI:
    """Tests for device-related endpoints"""

    def test_devices_endpoint_requires_auth(self, client):
        """GET /api/devices requires authentication (returns 401 without token)"""
        resp = client.get('/api/devices')
        assert resp.status_code == 401

    def test_protected_device_endpoints_require_token(self, client):
        """All device management endpoints require auth"""
        endpoints = ['/api/devices', '/api/devices/test_device_01']
        for ep in endpoints:
            resp = client.get(ep)
            assert resp.status_code == 401, f"{ep} should require auth"


class TestAlarmsAPI:
    """Tests for alarm-related endpoints"""

    def test_alarms_endpoint_structure(self, client):
        """GET /api/alarms returns expected JSON structure"""
        resp = client.get('/api/alarms')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'alarms' in data

    def test_active_alarms_endpoint(self, client):
        """GET /api/alarms/active returns JSON"""
        resp = client.get('/api/alarms/active')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'alarms' in data

    def test_alarm_statistics_endpoint(self, client):
        """GET /api/alarms/statistics returns JSON"""
        resp = client.get('/api/alarms/statistics')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data is not None

    def test_alarms_accepts_filter_params(self, client):
        """GET /api/alarms accepts query parameters for filtering"""
        resp = client.get('/api/alarms?device_id=test&alarm_level=warning&limit=10')
        assert resp.status_code == 200


class TestSystemAPI:
    """Tests for system information endpoints"""

    def test_database_stats_returns_json(self, client):
        """GET /api/system/database returns JSON"""
        resp = client.get('/api/system/database')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data is not None

    def test_simulation_mode_returns_json(self, client):
        """GET /api/system/simulation-mode returns JSON with simulation_mode field"""
        resp = client.get('/api/system/simulation-mode')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'simulation_mode' in data


class TestAuthAPI:
    """Tests for authentication endpoints"""

    def test_protected_endpoint_requires_token(self, client):
        """Protected endpoints return 401 without auth token"""
        # The system/status endpoint is protected by @jwt_required
        resp = client.get('/api/system/status')
        # Without a valid JWT, should get 401
        assert resp.status_code == 401

    def test_login_endpoint_exists(self, client, app):
        """POST /api/auth/login endpoint exists and handles bad credentials"""
        # Configure mock auth_manager to return a proper dict for login
        app.auth_manager.login.return_value = {
            'success': False, 'message': 'Invalid credentials'
        }
        resp = client.post('/api/auth/login',
                           json={'username': 'admin', 'password': 'wrong'})
        # Should return 401 (bad credentials) not 404 (endpoint not found)
        assert resp.status_code in (401, 400, 403)


class TestAPIRoot:
    """Tests for general API behavior"""

    def test_unknown_route_returns_404(self, client):
        """Unknown route returns 404"""
        resp = client.get('/api/nonexistent')
        assert resp.status_code == 404

    def test_health_endpoint_is_get_only(self, client):
        """Health endpoint only accepts GET"""
        resp = client.post('/api/health/status')
        assert resp.status_code == 405

    def test_content_type_json(self, client):
        """API endpoints return JSON content type"""
        resp = client.get('/api/health/status')
        assert 'application/json' in resp.content_type
