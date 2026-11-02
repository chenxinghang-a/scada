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

    def test_health_data_has_modules_and_checks(self, client, auth_headers):
        """明细（modules / checks）在**已认证**的 /status/detail 上（round 340）。

        公开的 `/api/health/status` 只回 version + global_status。
        两个视图来自同一个快照函数
        （`api_health.py::_compute_health_snapshot`），保证结论一致。
        """
        resp = client.get('/api/health/status/detail', headers=auth_headers)
        data = resp.get_json()

        health_data = data.get('data', {})
        assert 'global_status' in health_data
        assert 'modules' in health_data
        assert 'checks' in health_data


class TestClientErrorReporting:
    """round 340：免认证上报端点的**输入面**必须自我约束。

    这个端点免认证 + 写日志 = 天然放大面。原来：
      · `err.get(...)` 对非 dict 元素抛 AttributeError → 整个端点 500；
      · `url` 不限长、且 `extra={'client_error': err}` 把原始 dict 整个落日志
        → 一个请求就能把一行日志撑到几 MB。
    """

    def test_non_dict_items_do_not_crash(self, client):
        r = client.post('/api/system/client-errors',
                        json={'errors': ['not-a-dict', 123, None, []]})
        assert r.status_code == 200, r.get_data(as_text=True)[:200]
        assert r.get_json()['received'] == 0, '非 dict 元素不该被计入 received'

    def test_overlong_fields_are_clipped_not_rejected(self, client):
        r = client.post('/api/system/client-errors', json={
            'errors': [{'type': 'e' * 500, 'message': 'm' * 500,
                        'url': 'https://x/' + 'a' * 5000, 'userId': 'u' * 500}]
        })
        assert r.status_code == 200
        assert r.get_json()['received'] == 1, '合法 dict 必须被接收（截断而不是拒收）'

    def test_non_json_body_is_400_not_415(self, client):
        r = client.post('/api/system/client-errors', data='not json',
                        content_type='text/plain')
        assert r.status_code == 400, (
            f'非 JSON 体应统一按格式不对处理（400），实际 {r.status_code}')


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

    def test_alarms_endpoint_structure(self, client, auth_headers):
        """GET /api/alarms returns expected JSON structure"""
        resp = client.get('/api/alarms', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'alarms' in data

    def test_active_alarms_endpoint(self, client, auth_headers):
        """GET /api/alarms/active returns JSON"""
        resp = client.get('/api/alarms/active', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'alarms' in data

    def test_alarm_statistics_endpoint(self, client, auth_headers):
        """GET /api/alarms/statistics returns JSON"""
        resp = client.get('/api/alarms/statistics', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data is not None

    def test_alarms_accepts_filter_params(self, client, auth_headers):
        """GET /api/alarms accepts query parameters for filtering"""
        resp = client.get('/api/alarms?device_id=test&alarm_level=warning&limit=10', headers=auth_headers)
        assert resp.status_code == 200


class TestSystemAPI:
    """Tests for system information endpoints"""

    def test_database_stats_returns_json(self, client, auth_headers):
        """GET /api/system/database returns JSON"""
        resp = client.get('/api/system/database', headers=auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data is not None

    def test_simulation_mode_returns_json(self, client, auth_headers):
        """GET /api/system/simulation-mode returns JSON with simulation_mode field"""
        resp = client.get('/api/system/simulation-mode', headers=auth_headers)
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
        # 凭据错误必须 401，且绝不能是 404（端点不存在）。
        # 原断言 `in (401, 400, 403)` 把 400/403 也放过，与 docstring 说的
        # "not 404" 相比仍不够精确。
        assert resp.status_code == 401


class TestMetricsAPI:
    """Tests for /metrics Prometheus endpoint"""

    def test_metrics_endpoint_returns_200(self, client):
        """GET /metrics returns 200"""
        resp = client.get('/metrics')
        assert resp.status_code == 200

    def test_metrics_content_type(self, client):
        """GET /metrics returns Prometheus content type"""
        resp = client.get('/metrics')
        assert 'text/plain' in resp.content_type
        assert 'charset=utf-8' in resp.content_type

    def test_metrics_contains_scada_info(self, client):
        """Metrics output contains scada_info metric"""
        resp = client.get('/metrics')
        body = resp.data.decode('utf-8')
        assert 'scada_info' in body
        assert 'version' in body

    def test_metrics_contains_device_gauges(self, client):
        """Metrics output contains device gauge definitions"""
        resp = client.get('/metrics')
        body = resp.data.decode('utf-8')
        assert 'scada_devices_connected' in body
        assert 'scada_devices_fault' in body

    def test_metrics_contains_alarm_gauges(self, client):
        """Metrics output contains alarm gauge definitions"""
        resp = client.get('/metrics')
        body = resp.data.decode('utf-8')
        assert 'scada_alarms_active' in body

    def test_metrics_contains_queue_gauge(self, client):
        """Metrics output contains data queue gauge"""
        resp = client.get('/metrics')
        body = resp.data.decode('utf-8')
        assert 'scada_data_queue_size' in body

    def test_metrics_contains_http_counters(self, client):
        """Metrics output contains HTTP request counter definitions"""
        resp = client.get('/metrics')
        body = resp.data.decode('utf-8')
        assert 'scada_http_requests_total' in body
        assert 'scada_http_request_duration_seconds' in body

    def test_metrics_endpoint_is_get_only(self, client):
        """POST /metrics returns 405 Method Not Allowed"""
        resp = client.post('/metrics')
        assert resp.status_code == 405

    def test_metrics_no_auth_required(self, client):
        """GET /metrics does not require authentication"""
        # No auth_headers passed — should still return 200
        resp = client.get('/metrics')
        assert resp.status_code == 200


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
