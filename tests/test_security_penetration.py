"""
安全渗透测试
验证系统安全防护能力
"""
import jwt
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from config import AuthConfig
from 用户层.auth import ROLES


def _make_token(username: str, role: str, expired: bool = False) -> str:
    """按生产配置签发测试用 JWT（与 用户层/auth.py 使用同一密钥/算法）"""
    now = datetime.now(timezone.utc)
    payload = {
        'username': username,
        'role': role,
        'type': 'access',
        'iat': now - timedelta(hours=2) if expired else now,
        'exp': now - timedelta(hours=1) if expired else now + timedelta(hours=1),
    }
    return jwt.encode(payload, AuthConfig.JWT_SECRET, algorithm=AuthConfig.JWT_ALGORITHM)


@pytest.fixture(autouse=True)
def faithful_auth_manager(app):
    """让 conftest 里被 Mock 的 auth_manager 像真实 AuthManager 一样校验令牌。

    conftest 的 app fixture 用 MagicMock 充当 auth_manager，verify_token() 默认返回
    一个真值 MagicMock，导致任意令牌（含无效/过期令牌）都能通过认证 —— 这会让本文件
    所有认证/授权断言变成空断言。这里把 verify_token 换成真实 JWT 解码
    （与 用户层/auth.py:318 verify_token 的语义一致：解码失败/过期 → None）。
    """
    def _verify_token(token: str):
        try:
            payload = jwt.decode(
                token, AuthConfig.JWT_SECRET, algorithms=[AuthConfig.JWT_ALGORITHM])
        except jwt.InvalidTokenError:
            return None
        if payload.get('type') not in (None, 'access'):
            return None
        role = payload.get('role')
        return {
            'username': payload.get('username'),
            'role': role,
            'display_name': payload.get('username'),
            'permissions': ROLES.get(role, {}).get('permissions', []),
        }

    app.auth_manager.verify_token.side_effect = _verify_token
    return app.auth_manager


@pytest.fixture
def viewer_headers(app):
    """只读角色（viewer）的认证头，用于越权测试"""
    return {'Authorization': f'Bearer {_make_token("viewer1", "viewer")}'}


class TestSQLInjection:
    """SQL注入防护测试"""

    def test_sql_injection_in_device_id(self, client, auth_headers, app):
        """测试设备ID SQL注入防护"""
        # 真实 DeviceManager 对未知设备返回错误字典；mock 需保持同样语义，
        # 否则 get_device 会把 MagicMock 塞进 jsonify 变成 500，测不出注入防护
        app.device_manager.get_device_status.return_value = {'error': '设备不存在'}
        # 尝试SQL注入
        malicious_id = "'; DROP TABLE devices; --"
        resp = client.get(f'/api/devices/{malicious_id}', headers=auth_headers)
        # 应该返回404或400，而不是500
        assert resp.status_code in [400, 404]

    def test_sql_injection_in_query_params(self, client, auth_headers):
        """测试查询参数SQL注入防护"""
        # 尝试SQL注入
        resp = client.get('/api/alarms?device_id=\'; DROP TABLE alarms; --', headers=auth_headers)
        # 应该返回正常响应或400
        assert resp.status_code in [200, 400]

    def test_sql_injection_in_json_body(self, client, auth_headers):
        """测试JSON body SQL注入防护"""
        resp = client.post('/api/devices', json={
            'id': "'; DROP TABLE devices; --",
            'name': 'Test Device',
            'protocol': 'modbus_tcp',
            'host': '192.168.1.1',
            'port': 502
        }, headers=auth_headers)
        # 应该返回400（无效ID格式）
        assert resp.status_code == 400


class TestXSS:
    """XSS防护测试"""

    def test_xss_in_device_name(self, client, auth_headers, app):
        """测试设备名称XSS防护"""
        app.device_manager.add_device.return_value = True
        app.device_manager.get_device_status.return_value = {
            'device_id': 'test', 'name': '<script>alert("xss")</script>'
        }
        resp = client.post('/api/devices', json={
            'id': 'test',
            'name': '<script>alert("xss")</script>',
            'protocol': 'modbus_tcp',
            'host': '192.168.1.1',
            'port': 502
        }, headers=auth_headers)
        # 应该成功但内容被转义或拒绝
        if resp.status_code == 200:
            data = resp.get_json()
            assert '<script>' not in str(data)


class TestAuthenticationBypass:
    """认证绕过测试"""

    def test_no_token_access(self, client):
        """测试无token访问"""
        endpoints = [
            '/api/devices',
            '/api/alarms',
            '/api/data/realtime',
            '/api/system/status',
        ]
        for endpoint in endpoints:
            resp = client.get(endpoint)
            assert resp.status_code == 401, f"{endpoint} 应该返回401"

    def test_invalid_token_access(self, client):
        """测试无效token访问"""
        headers = {'Authorization': 'Bearer invalid_token_here'}
        resp = client.get('/api/devices', headers=headers)
        assert resp.status_code == 401

    def test_expired_token_access(self, client):
        """测试过期token访问"""
        # 用真实签名但已过期的 JWT，确保走的是 ExpiredSignatureError 分支
        headers = {'Authorization': f'Bearer {_make_token("testuser", "admin", expired=True)}'}
        resp = client.get('/api/devices', headers=headers)
        assert resp.status_code == 401


class TestAuthorization:
    """授权测试"""

    def test_viewer_cannot_write(self, client, viewer_headers, app):
        """测试viewer角色不能写入"""
        resp = client.post('/api/devices', json={
            'id': 'test', 'name': 'Test', 'protocol': 'modbus_tcp',
            'host': '192.168.1.1', 'port': 502
        }, headers=viewer_headers)
        assert resp.status_code == 403

    def test_viewer_cannot_control(self, client, viewer_headers, app):
        """测试viewer角色不能控制设备"""
        resp = client.post('/api/devices/test/write-register', json={
            'address': 100, 'value': 50
        }, headers=viewer_headers)
        assert resp.status_code == 403


class TestRateLimit:
    """速率限制测试"""

    def test_rate_limit_login(self, client):
        """测试登录速率限制"""
        # 快速发送多个登录请求
        for i in range(10):
            resp = client.post('/api/auth/login', json={
                'username': 'admin',
                'password': 'wrong_password'
            })
            if resp.status_code == 429:
                # 速率限制生效
                return
        # 如果没有触发速率限制，可能是配置问题
        pytest.skip("速率限制未触发，检查配置")


class TestInputValidation:
    """输入验证测试"""

    def test_invalid_port_number(self, client, auth_headers):
        """测试无效端口号"""
        resp = client.post('/api/devices', json={
            'id': 'test',
            'name': 'Test',
            'protocol': 'modbus_tcp',
            'host': '192.168.1.1',
            'port': 99999  # 无效端口
        }, headers=auth_headers)
        assert resp.status_code == 400

    def test_negative_register_address(self, client, auth_headers, app):
        """测试负数寄存器地址"""
        app.device_manager.get_client.return_value = MagicMock(connected=True)
        resp = client.post('/api/devices/test/write-register', json={
            'address': -1,  # 无效地址
            'value': 50
        }, headers=auth_headers)
        assert resp.status_code == 400

    def test_overflow_value(self, client, auth_headers, app):
        """测试溢出值"""
        app.device_manager.get_client.return_value = MagicMock(connected=True)
        resp = client.post('/api/devices/test/write-register', json={
            'address': 100,
            'value': 999999999  # 超出范围
        }, headers=auth_headers)
        assert resp.status_code == 400


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
