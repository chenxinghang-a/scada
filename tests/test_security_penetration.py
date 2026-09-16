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
        # 只允许"接受并转义"或"直接拒绝"两种结果，且响应里绝不能出现原始脚本标签。
        # 原先写成 `if resp.status_code == 200:` 才断言，接口返回 400/403/500 时
        # 断言整段被跳过，测试恒过（条件断言 = 假绿）。
        assert resp.status_code in (200, 201, 400, 403), \
            f"未预期的状态码 {resp.status_code}: {resp.get_data(as_text=True)[:200]}"
        assert '<script>' not in resp.get_data(as_text=True), \
            "响应体中原样回显了 XSS payload"


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

    def test_rate_limit_login(self, db):
        """测试登录速率限制：连续失败登录必须触发 429。

        原实现用 conftest 的 `client` fixture —— 那是一个裸 Flask app，
        不经过 create_app()，flask-limiter 从未绑定到任何视图；循环 10 次
        永远拿不到 429，最后无条件的 pytest.skip() 让这条测试永远"绿"
        （既不通过也不失败，等于没测）。

        这里改用 create_app() 构建真实应用，让 core.rate_limiter 的分级限流
        （login: 5 per minute）真正生效，并断言：
        - 前 5 次失败登录是 401（凭据错误）
        - 第 6 次必须被限流器拦下，返回 429
        """
        import sqlite3
        import tempfile

        class _TestDatabase:
            """AuthManager 只需要 database.get_connection()"""

            def __init__(self, path):
                self._path = path

            def get_connection(self):
                conn = sqlite3.connect(self._path)
                conn.row_factory = sqlite3.Row
                return conn

        tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        tmp_path = tmp.name
        tmp.close()

        from 展示层.routes import create_app

        real_app = create_app(
            database=_TestDatabase(tmp_path),
            device_manager=MagicMock(),
            alarm_manager=MagicMock(),
            data_collector=MagicMock(),
        )

        # 限流器与登录视图都必须真实存在，否则后面的断言会退化成"接口不存在"
        assert real_app.limiter is not None, "create_app 未创建限流器"
        assert real_app.limiter.enabled, "限流器未启用"
        assert 'api_auth.login' in real_app.view_functions, "登录端点未注册"

        test_client = real_app.test_client()
        codes = [
            test_client.post('/api/auth/login', json={
                'username': 'admin',
                'password': 'wrong_password',
            }).status_code
            for _ in range(6)
        ]

        assert codes[:5] == [401] * 5, f"前 5 次失败登录应为 401，实际 {codes}"
        assert codes[5] == 429, f"第 6 次失败登录应被限流为 429，实际 {codes}"


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
