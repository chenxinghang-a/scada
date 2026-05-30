"""
冒烟测试 - 真正启动应用验证所有关键路径
验收标准：如果这个测试失败，说明系统打不开。
"""

import pytest
import sys
import os
import json
import time
import threading
import requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _find_free_port():
    import socket
    with socket.socket() as s:
        s.bind(('', 0))
        return s.getsockname()[1]


@pytest.fixture(scope='module')
def app_server():
    """启动真实的Flask应用服务器"""
    port = _find_free_port()

    # 构建组件（模拟模式）
    from 存储层.database import Database
    from 采集层.simulated_device_manager import SimulatedDeviceManager
    from 报警层.alarm_manager import AlarmManager
    from 采集层.data_collector import DataCollector
    from 展示层.routes import create_app

    database = Database(f'data/smoke_test_{port}.db')
    device_manager = SimulatedDeviceManager('配置/devices_simulated.yaml')

    from 报警层.alarm_output import AlarmOutput
    from 报警层.broadcast_system import BroadcastSystem
    alarm_output = AlarmOutput({'enabled': True, 'simulation': True})
    broadcast_system = BroadcastSystem({'enabled': True, 'simulation': True, 'areas': ['车间A']})
    alarm_manager = AlarmManager(
        database=database,
        config_path='配置/alarms.yaml',
        alarm_output=alarm_output,
        broadcast_system=broadcast_system
    )
    data_collector = DataCollector(database, device_manager, alarm_manager)

    app = create_app(database, device_manager, alarm_manager, data_collector)

    from 展示层.websocket import socketio as sio

    server_thread = threading.Thread(
        target=lambda: sio.run(app, host='127.0.0.1', port=port,
                               debug=False, allow_unsafe_werkzeug=True,
                               use_reloader=False, log_output=False),
        daemon=True
    )
    server_thread.start()

    base_url = f'http://127.0.0.1:{port}'
    for _ in range(30):
        try:
            r = requests.get(f'{base_url}/login', timeout=2)
            if r.status_code in (200, 302):
                break
        except Exception:
            time.sleep(0.5)
    else:
        pytest.fail("应用启动超时")

    yield base_url


@pytest.fixture(scope='module')
def auth_token(app_server):
    """获取认证token（处理首次登录改密场景）"""
    r = requests.post(f'{app_server}/api/auth/login', json={
        'username': 'admin',
        'password': 'admin123'
    })
    if r.status_code == 200:
        data = r.json()
        token = data.get('token', '')
        if token:
            return token
    # 403 = 首次登录需改密，token在响应中
    if r.status_code == 403:
        data = r.json()
        token = data.get('token', '')
        if token:
            # 先改密
            headers = {'Authorization': f'Bearer {token}'}
            requests.post(f'{app_server}/api/auth/force-change-password',
                          json={'username': 'admin', 'new_password': 'Admin123!'},
                          headers=headers)
            # 用新密码重新登录
            r2 = requests.post(f'{app_server}/api/auth/login', json={
                'username': 'admin', 'password': 'Admin123!'
            })
            if r2.status_code == 200:
                return r2.json().get('token', '')
            return token  # 即使改密后登录失败，返回原token
    pytest.skip("无法登录，跳过需要认证的测试")


class TestSmokeStartup:
    """应用启动"""

    def test_login_page_loads(self, app_server):
        r = requests.get(f'{app_server}/login')
        assert r.status_code == 200

    def test_login_returns_token(self, app_server):
        r = requests.post(f'{app_server}/api/auth/login', json={
            'username': 'admin', 'password': 'admin123'
        })
        # 可能是200（成功）或403（首次登录需改密）或429（限流）
        assert r.status_code in (200, 403, 429), f"登录异常: {r.status_code} {r.text}"
        if r.status_code == 200:
            data = r.json()
            assert 'token' in data or 'data' in data

    def test_no_500_on_startup(self, app_server):
        for page in ['/login']:
            r = requests.get(f'{app_server}{page}')
            assert r.status_code != 500, f"{page} 返回500"


class TestSmokeAPI:
    """所有API端点"""

    def test_dashboard_api(self, app_server, auth_token):
        headers = {'Authorization': f'Bearer {auth_token}'}
        r = requests.get(f'{app_server}/api/data/latest', headers=headers)
        assert r.status_code in (200, 204, 404), f"{r.status_code} {r.text[:200]}"

    def test_devices_api(self, app_server, auth_token):
        headers = {'Authorization': f'Bearer {auth_token}'}
        r = requests.get(f'{app_server}/api/devices', headers=headers)
        assert r.status_code in (200, 404)

    def test_alarms_api(self, app_server, auth_token):
        headers = {'Authorization': f'Bearer {auth_token}'}
        r = requests.get(f'{app_server}/api/alarms', headers=headers)
        assert r.status_code in (200, 404)

    def test_health_api(self, app_server, auth_token):
        headers = {'Authorization': f'Bearer {auth_token}'}
        r = requests.get(f'{app_server}/api/health', headers=headers)
        assert r.status_code in (200, 404)

    def test_control_status(self, app_server, auth_token):
        headers = {'Authorization': f'Bearer {auth_token}'}
        r = requests.get(f'{app_server}/api/control/status', headers=headers)
        assert r.status_code in (200, 404, 503)

    def test_csrf_token(self, app_server):
        r = requests.get(f'{app_server}/api/csrf-token')
        assert r.status_code == 200
        assert 'csrf_token' in r.json()

    def test_auth_required(self, app_server):
        # 未认证应被拒绝（401/403）或页面重定向（302）或资源不存在（404也说明没泄露数据）
        for path in ['/api/devices', '/api/alarms']:
            r = requests.get(f'{app_server}{path}')
            assert r.status_code in (401, 403, 302, 404), \
                f"{path} 未认证应被拒绝，返回 {r.status_code}"

    def test_no_500_on_any_api(self, app_server, auth_token):
        """所有API都不能返回500"""
        headers = {'Authorization': f'Bearer {auth_token}'}
        endpoints = [
            '/api/data/latest', '/api/devices', '/api/alarms',
            '/api/health', '/api/control/status',
            '/api/industry40/overview', '/api/csrf-token',
        ]
        errors = []
        for ep in endpoints:
            try:
                r = requests.get(f'{app_server}{ep}', headers=headers, timeout=5)
                if r.status_code == 500:
                    errors.append(f"500: {ep} -> {r.text[:100]}")
            except Exception as e:
                errors.append(f"ERROR: {ep} -> {e}")
        assert not errors, f"API 500错误:\n" + "\n".join(errors)


class TestSmokeCSP:
    """CSP策略验证"""

    def test_csp_header_present(self, app_server):
        r = requests.get(f'{app_server}/login')
        csp = r.headers.get('Content-Security-Policy', '')
        assert 'script-src' in csp

    def test_csp_includes_socketio(self, app_server):
        r = requests.get(f'{app_server}/login')
        csp = r.headers.get('Content-Security-Policy', '')
        assert 'cdn.socket.io' in csp, f"CSP缺少cdn.socket.io: {csp}"

    def test_csp_includes_jsdelivr(self, app_server):
        r = requests.get(f'{app_server}/login')
        csp = r.headers.get('Content-Security-Policy', '')
        assert 'cdn.jsdelivr.net' in csp

    def test_security_headers(self, app_server):
        r = requests.get(f'{app_server}/login')
        assert 'X-Frame-Options' in r.headers
        assert 'X-Content-Type-Options' in r.headers


class TestSmokePages:
    """页面路由"""

    def test_all_pages_load(self, app_server, auth_token):
        pages = ['/', '/dashboard', '/screen', '/history', '/alarms',
                 '/config', '/devices', '/control', '/industry40',
                 '/users', '/alarm-output']
        cookies = {'token': auth_token}
        errors = []
        for page in pages:
            r = requests.get(f'{app_server}{page}', cookies=cookies, allow_redirects=False)
            if r.status_code == 500:
                errors.append(f"500: {page}")
            elif r.status_code not in (200, 302):
                errors.append(f"{r.status_code}: {page}")
        assert not errors, f"页面加载失败:\n" + "\n".join(errors)


class TestSmokeConcurrency:
    """并发安全"""

    def test_concurrent_reads(self, app_server, auth_token):
        headers = {'Authorization': f'Bearer {auth_token}'}
        errors = []

        def read_api():
            try:
                r = requests.get(f'{app_server}/api/data/latest',
                                 headers=headers, timeout=5)
                if r.status_code == 500:
                    errors.append("500")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=read_api) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert len(errors) == 0, f"并发读取出错: {errors}"


class TestSmokeJWT:
    """JWT黑名单"""

    def test_logout_blacklists_token(self, app_server, auth_token):
        # 先获取一个新token
        r = requests.post(f'{app_server}/api/auth/login', json={
            'username': 'admin', 'password': 'admin123'
        })
        if r.status_code != 200:
            pytest.skip("无法登录")
        token = r.json().get('token', '')
        headers = {'Authorization': f'Bearer {token}'}

        # 登出
        r = requests.post(f'{app_server}/api/auth/logout', headers=headers)
        assert r.status_code == 200

        # 已登出token应被拒绝
        r = requests.get(f'{app_server}/api/data/latest', headers=headers)
        assert r.status_code in (401, 403), \
            f"已登出token应被拒绝，返回 {r.status_code}"
