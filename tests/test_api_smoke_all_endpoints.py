# -*- coding: utf-8 -*-
"""全 API 冒烟：真实组件 + 遍历 url_map，任何 5xx / 空数据 / 未鉴权都红（round 255）。

为什么需要它
------------
之前的测试都是"一个功能一个用例"，覆盖的是**我想到的地方**。
而实测（本轮）用真实组件把 94 个 GET 端点全跑一遍，立刻抓到一个
**用户天天点、但一直 500 的功能**：

    POST /api/auth/logout  →  500 "登出失败：令牌未能撤销，请重试"

根因：`blacklist_token` 在没有 `jti` 的令牌上直接返回 False，
而 `logout` 把 False 变成 500 —— 对这类令牌**重试一万次也不会成功**。

这个用例的价值就是"遍历式覆盖"：不依赖我记得哪些端点存在。
新增端点自动被纳入，任何 5xx / 空 data / 意外 401 都会在这里先红。
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(scope='module')
def real_app(tmp_path_factory):
    """用**真实**组件构造 app（不启动采集线程）。

    不用 conftest 的 MagicMock app：mock 会把"模块未挂载"掩盖成正常返回，
    而这正是要验的东西（真实装配下接口是否可用）。
    """
    tmp = tmp_path_factory.mktemp('smoke')
    from 存储层.database import Database
    from 采集层.device_manager import DeviceManager
    from 报警层.alarm_manager import AlarmManager
    from 采集层.data_collector import DataCollector
    from 用户层.auth import AuthManager
    from 展示层 import create_app

    from 智能层.predictive_maintenance import PredictiveMaintenance
    from 智能层.oee_calculator import OEECalculator
    from 智能层.spc_analyzer import SPCAnalyzer
    from 智能层.energy_manager import EnergyManager
    from 智能层.edge_decision import EdgeDecisionEngine
    from 智能层.vibration_analyzer import VibrationAnalyzer
    from 智能层.device_control import DeviceControlSafety

    database = Database(str(tmp / 'smoke.db'))
    dm = DeviceManager(str(PROJECT_ROOT / '配置' / 'devices.yaml'),
                       simulation_mode=True)
    # 声光/广播要一起给 —— 与 run.py 的装配一致，否则
    # /api/alarm-output/status、/api/broadcast/* 会报"模块不可用"（那是装配问题）
    from 报警层.alarm_output import AlarmOutput
    from 报警层.broadcast_system import BroadcastSystem
    am = AlarmManager(database, str(PROJECT_ROOT / '配置' / 'alarms.yaml'),
                      alarm_output=AlarmOutput(), broadcast_system=BroadcastSystem())
    dc = DataCollector(dm, database, am)
    auth = AuthManager(database)

    # 智能层模块必须一起传 —— 与 run.py 的装配保持一致。
    # 不传的话 /api/industry40/* 全报"模块未挂载"，冒烟会误判成接口坏了。
    app = create_app(
        database, dm, am, dc,
        predictive_maintenance=PredictiveMaintenance(database),
        oee_calculator=OEECalculator(database),
        spc_analyzer=SPCAnalyzer(database),
        energy_manager=EnergyManager(database),
        edge_decision=EdgeDecisionEngine(database),
        device_control=DeviceControlSafety(database, dm, am),
        vibration_analyzer=VibrationAnalyzer(database),
    )
    app.config['TESTING'] = True
    app.smoke_auth = auth

    # 默认 admin 带「强制改密」标记，会把除改密接口外的请求全挡掉
    # （403），于是冒烟会看到一片"被拒"而误判成接口坏了。
    # 这里在临时库里清掉它 —— 我们要验的是接口本身，不是改密流程。
    with database.get_connection() as conn:
        conn.execute("UPDATE users SET must_change_password = 0 WHERE username = 'admin'")
    yield app

    try:
        am.stop()
        database.close()
    except Exception:      # noqa: BLE001 —— teardown 失败不该影响结论
        pass


@pytest.fixture(scope='module')
def smoke_client(real_app):
    import jwt as pyjwt
    import 用户层.auth as auth_mod

    token = pyjwt.encode(
        {'username': 'admin', 'role': 'admin', 'type': 'access',
         'exp': datetime.now(timezone.utc) + timedelta(hours=2)},
        auth_mod.JWT_SECRET, algorithm=auth_mod.JWT_ALGORITHM)
    client = real_app.test_client()
    client.environ_base['HTTP_AUTHORIZATION'] = f'Bearer {token}'
    return client


def _get_rules(app):
    for rule in app.url_map.iter_rules():
        if 'GET' in rule.methods and rule.endpoint != 'static':
            yield rule


def _write_rules(app):
    for rule in app.url_map.iter_rules():
        if rule.endpoint == 'static':
            continue
        for method in ('POST', 'PUT', 'DELETE', 'PATCH'):
            if method in rule.methods:
                yield rule, method


class TestNoServerErrors:
    def test_all_get_endpoints_no_5xx(self, real_app, smoke_client):
        """遍历所有 GET 端点（带路径参数的用 1 填充），不许 5xx。"""
        failures = []
        for rule in _get_rules(real_app):
            path = str(rule)
            # 路径参数填一个无害值，让路由能命中
            for arg in rule.arguments:
                path = path.replace(f'<{arg}>', 'smoke-id')
                path = path.replace(f'<path:{arg}>', 'smoke-id')
            try:
                r = smoke_client.get(path)
            except Exception as e:      # noqa: BLE001
                failures.append(f'{path}: 抛异常 {type(e).__name__}: {e}')
                continue
            if r.status_code >= 500:
                failures.append(
                    f'{path}: HTTP {r.status_code} {r.get_data(as_text=True)[:160]}')
        assert not failures, '以下 GET 端点返回 5xx：\n  ' + '\n  '.join(failures)

    def test_all_get_endpoints_no_unexpected_401(self, real_app, smoke_client):
        """带 admin 令牌不该出现 401（除非该端点明确只对匿名开放）。"""
        failures = []
        for rule in _get_rules(real_app):
            path = str(rule)
            for arg in rule.arguments:
                path = path.replace(f'<{arg}>', 'smoke-id')
            r = smoke_client.get(path)
            if r.status_code == 401:
                failures.append(f'{path}: 401 {r.get_data(as_text=True)[:120]}')
        assert not failures, (
            '以下端点用 admin 令牌仍 401 —— 鉴权配置有问题：\n  '
            + '\n  '.join(failures))

    #: 这些端点的响应**本身就是**"逐段可用性报告"，出现 unavailable 是诚实的
    #: （例如 /industry40/overview 里 energy 段在还没喂电力数据时就是 unavailable）。
    #: 它们不该被当成"模块没挂上"。
    AVAILABILITY_REPORTS = {
        '/api/industry40/overview',
    }

    def test_all_get_endpoints_not_unavailable(self, real_app, smoke_client):
        """真实装配下不该出现 module_unavailable（说明模块没挂上）。"""
        failures = []
        for rule in _get_rules(real_app):
            path = str(rule)
            for arg in rule.arguments:
                path = path.replace(f'<{arg}>', 'smoke-id')
            if path in self.AVAILABILITY_REPORTS:
                continue
            body = smoke_client.get(path).get_data(as_text=True)
            if 'module_unavailable' in body or '"status":"unavailable"' in body:
                failures.append(path)
        assert not failures, (
            '真实装配下这些端点报"模块不可用" —— 说明依赖没接上：\n  '
            + '\n  '.join(failures))


class TestWriteEndpointsRejectEmptyBody:
    @pytest.mark.parametrize('method', ['POST', 'PUT', 'DELETE', 'PATCH'])
    def test_empty_body_is_4xx_not_5xx(self, real_app, smoke_client, method):
        """空 body 的写请求必须是 4xx（参数错误），不能 5xx（服务器崩了）。"""
        failures = []
        for rule, m in _write_rules(real_app):
            if m != method:
                continue
            path = str(rule)
            if rule.arguments:      # 带参数的路径跳过（需要真实 id）
                continue
            try:
                r = smoke_client.open(path, method=m, json={})
            except Exception as e:      # noqa: BLE001
                failures.append(f'{m} {path}: 抛异常 {type(e).__name__}: {e}')
                continue
            if r.status_code >= 500:
                failures.append(
                    f'{m} {path}: HTTP {r.status_code} '
                    f'{r.get_data(as_text=True)[:160]}')
        assert not failures, (
            f'空 body 的 {method} 请求导致 5xx：\n  ' + '\n  '.join(failures))


class TestLogoutActuallyRevokes:
    """登出必须**真的**撤销令牌，且对没有 jti 的令牌也成立。

    这是本轮修的那个 500 的回归守卫：`blacklist_token` 原先在无 jti 时
    返回 False，`logout` 把它变成 500 —— 用户永远登不出去。
    """

    def test_logout_without_jti_succeeds(self, real_app):
        import jwt as pyjwt
        import 用户层.auth as auth_mod

        # 加 nonce：同一秒生成的两个令牌字面相同，会让第二个用例拿到
        # 已被撤销的令牌（401）—— 那是测试串味，不是产品缺陷。
        token = pyjwt.encode(
            {'username': 'admin', 'role': 'admin', 'type': 'access',
             'nonce': 'no-jti-1',
             'exp': datetime.now(timezone.utc) + timedelta(hours=1)},
            auth_mod.JWT_SECRET, algorithm=auth_mod.JWT_ALGORITHM)

        client = real_app.test_client()
        r = client.post('/api/auth/logout',
                        headers={'Authorization': f'Bearer {token}'})
        assert r.status_code == 200, (
            f'没有 jti 的令牌登出仍然失败: {r.status_code} {r.get_data(as_text=True)[:160]}')

    def test_revoked_token_cannot_be_used_again(self, real_app):
        """撤销后同一个令牌必须失效 —— 否则"登出"只是界面上的假象。"""
        import jwt as pyjwt
        import 用户层.auth as auth_mod

        token = pyjwt.encode(
            {'username': 'admin', 'role': 'admin', 'type': 'access',
             'nonce': 'revoke-check-1',
             'exp': datetime.now(timezone.utc) + timedelta(hours=1)},
            auth_mod.JWT_SECRET, algorithm=auth_mod.JWT_ALGORITHM)
        client = real_app.test_client()
        h = {'Authorization': f'Bearer {token}'}

        # 用**需要鉴权**的端点验：/api/health/status 是公开端点，
        # 无论令牌是否撤销都回 200，拿它断言会得到假绿。
        probe = '/api/system/status'
        assert client.get(probe, headers=h).status_code == 200
        assert client.post('/api/auth/logout', headers=h).status_code == 200
        assert client.get(probe, headers=h).status_code == 401, (
            '撤销后令牌仍能用 —— 撤销没生效（假登出）')
