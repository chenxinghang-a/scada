"""`create_app()` 的装配验证（round 346）。

## 为什么需要

`tests/conftest.py` 的 `app` fixture 用的是「最小 Flask + 手工注册蓝图」，
**不是** `create_app()`。而 `run.py` 生产启动走的正是 `create_app()`。

两者差异很大：`create_app()` 里还有 CSRF 挂载、JWT 初始化、静态资源、
模板目录、蓝图注册、错误处理器、`device_control` 装配……

于是出现一个覆盖盲区：**装配层的问题（漏挂蓝图、CSRF 配错、初始化顺序）
测试全绿，一上线就 500**。

这个文件补的就是那层：用 mock 的外部依赖跑一次真 `create_app()`，
断言关键端点存在、未认证会被拒、`/status` 这类探活接口可匿名访问。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope='module')
def real_app():
    """用 mock 依赖跑一次真 `create_app()`（模块级，避免重复初始化）。"""
    from 展示层.routes import create_app

    db = MagicMock()
    db.get_database_stats.return_value = {
        'realtime_count': 0, 'history_count': 0, 'alarm_count': 0,
    }
    db.get_realtime_data.return_value = []
    db.get_device_summary.return_value = []
    db.get_latest_data_all.return_value = {}
    db.get_active_alarms.return_value = []
    db.get_alarm_records.return_value = []

    mgr = MagicMock()
    mgr.simulation_mode = True
    mgr.get_all_status.return_value = []
    mgr.get_all_devices.return_value = {}
    mgr.devices = {}
    mgr.get_protocol_summary.return_value = {}

    alarm = MagicMock()
    alarm.get_active_alarms.return_value = []
    alarm.get_alarm_statistics.return_value = {}
    alarm.alarm_output = None

    collector = MagicMock()
    collector.get_stats.return_value = {'running': False}
    collector.get_all_stats.return_value = {}
    collector.tasks = {}

    app = create_app(db, mgr, alarm, collector)
    app.config['TESTING'] = True
    return app


class TestAppAssembly:
    """装配层：蓝图、认证、探活。"""

    def test_create_app_returns_app(self, real_app):
        assert real_app is not None

    @pytest.mark.parametrize('rule', [
        '/api/devices',
        '/api/auth/login',
        '/api/devices/<device_id>/write-register',
        '/api/alarms/active',
    ])
    def test_critical_endpoints_registered(self, real_app, rule):
        """关键端点必须在 `url_map` 里。

        漏挂一个蓝图的后果不是 404 那么明显 —— 前端会拿到 404 后
        静默降级（列表空白），运维以为「没有设备」。
        """
        rules = {r.rule for r in real_app.url_map.iter_rules()}
        assert rule in rules, f'{rule} 没有注册（漏挂蓝图？）实际有 {len(rules)} 条规则'

    def test_devices_requires_auth(self, real_app):
        """未认证访问设备列表必须 401/403，不能 200。"""
        client = real_app.test_client()
        resp = client.get('/api/devices')
        assert resp.status_code in (401, 403), (
            f'未认证访问 /api/devices 返回 {resp.status_code} —— 设备清单泄漏')

    def test_write_endpoint_requires_auth(self, real_app):
        """写接口更必须拦。"""
        client = real_app.test_client()
        # 注意：`control_bp` 的 url_prefix 是 `/api`（不是 `/api/control`），
        # 所以写接口的完整路径是 `/api/devices/<id>/write-register`。
        resp = client.post('/api/devices/dev1/write-register',
                           json={'address': 0, 'value': 1})
        assert resp.status_code in (401, 403), (
            f'未认证写入返回 {resp.status_code}')

    def test_health_is_reachable_without_auth(self, real_app):
        """探活接口要能匿名访问（负载均衡/容器编排要探它）。"""
        client = real_app.test_client()
        # 不同版本的路径不同，任一个能通即可
        for path in ('/api/health/status', '/status', '/api/health'):
            resp = client.get(path)
            if resp.status_code == 200:
                return
        pytest.fail('没有任何匿名可访问的探活接口 —— 负载均衡无法判断服务存活')

    def test_device_control_absent_is_explicit(self, real_app):
        """没传 `device_control` 时，属性必须是 **None**（而不是不存在）。

        写接口用 `getattr(current_app, 'device_control', None)` 判断，
        然后 **fail-closed 返回 503**。如果属性压根不存在，行为一样（None），
        但「属性不存在」和「装配漏了」在排查时很难区分 ——
        显式赋 None 至少让 `hasattr` 能回答「装配流程跑过了」。
        """
        assert hasattr(real_app, 'device_control'), \
            'create_app 没有设置 device_control 属性 —— 装配流程可能漏了一步'
        assert real_app.device_control is None, \
            '本 fixture 没传 device_control，应当是 None'

    def test_create_app_assigns_device_control(self):
        """结构守卫：`create_app` 必须把入参赋给 `app.device_control`。

        漏赋的后果：写接口永远 503 —— 「拒绝写入」本身是对的，
        但原因如果是装配漏了，整台机器就永远不能操控。
        """
        src = (REPO / '展示层' / 'routes.py').read_text(encoding='utf-8')
        code = '\n'.join(line.split('#', 1)[0] for line in src.splitlines())
        assert 'app.device_control' in code, \
            'create_app 没有把 device_control 挂到 app 上' 

    def test_csrf_does_not_block_test_client(self, real_app):
        """CSRF 不能在 `TESTING` 下误伤（否则所有 API 测试都得带 token）。"""
        client = real_app.test_client()
        resp = client.post('/api/auth/login', json={'username': 'x', 'password': 'y'})
        assert resp.status_code != 400 or 'csrf' not in resp.get_data(as_text=True).lower(), (
            'CSRF 把登录接口拦了 —— 登录本来就发生在拿到 token 之前')
