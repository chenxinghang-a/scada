# -*- coding: utf-8 -*-
"""round 321：CSRF 防护的两处 fail-open + 真实模型说明。

## 为什么审这个模块

用覆盖率找盲区（`coverage run` 全量 4169 测试）：
`core/csrf_protection.py` **只有 31.7% 覆盖**，而它是安全模块 ——
也就是说"防护逻辑本身没人测过"。补测试时直接暴露了两处 fail-open。

## 缺陷一：令牌的"1 小时有效期"只判上界

```python
if time.time() - timestamp > 3600:   # 只判"太旧"
    return False
```

`timestamp` 在**未来**时这个条件恒为假 → **令牌永不过期**。
实测 `ts = now + 10 年` 的令牌**通过校验**。

现实触发路径不是攻击者伪造（HMAC 签不出来），而是**服务器时钟被回拨**：
NTP 校时往回拨 / 部署到时钟慢的机器 → 此前签发的所有令牌都变成"未来"
→ "1 小时有效期"整体失效，泄露的令牌长期可用。

修法：允许 60 秒时钟偏差，超出即拒绝并告警（提示查 NTP）。

## 缺陷二：密钥缺失时 fail-open

```python
if not self._secret:
    return True          # ← 任意字符串都通过
```

CSRF 开着但 `CSRF_SECRET` 为空（配置坏了）时，**校验变成空操作**。
安全模块最不该出现的形状：配置错误导致"保护静默关闭"，而日志里只有一行 INFO。

修法：fail-closed（拒绝 + 只记一次 ERROR）。

## 附带：docstring 写错了模型

原 docstring 写「Double Submit Cookie」，**实际没有 cookie 参与**，
只有 `时间戳.HMAC`。已按真实模型改写，并把已知限制写清楚
（令牌不绑定会话，靠登录 cookie 的 `SameSite=Strict` 兜底）。
本文件最后一条用例就是那个兜底的守卫。
"""

import hashlib
import hmac
import time

import pytest

from core.csrf_protection import CSRFProtection


@pytest.fixture
def csrf():
    c = CSRFProtection()
    c._secret = b'k' * 32
    return c


def _sign(secret: bytes, ts: str) -> str:
    return hmac.new(secret, ts.encode('utf-8'), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# 令牌生成/校验
# ---------------------------------------------------------------------------

def test_generated_token_is_valid(csrf):
    token = csrf.generate_token()
    assert '.' in token
    assert csrf._validate_token(token) is True


def test_expired_token_rejected(csrf):
    """1 小时前的令牌必须拒绝（原有行为，不能因为本轮改动退化）。"""
    ts = str(int(time.time()) - 3700)
    assert csrf._validate_token(f'{ts}.{_sign(csrf._secret, ts)}') is False


def test_future_token_rejected(csrf):
    """**本轮核心**：时间戳在未来的令牌必须拒绝。

    修复前：只判 `now - ts > 3600`，未来时间戳恒为负 → 永不过期。
    实测十年后的令牌通过校验。
    """
    ts = str(int(time.time()) + 86400 * 365 * 10)
    token = f'{ts}.{_sign(csrf._secret, ts)}'
    assert csrf._validate_token(token) is False, (
        '未来时间戳的令牌通过了 —— "1 小时有效期"失效，泄露的令牌长期可用')


def test_small_clock_skew_is_tolerated(csrf):
    """反向对照：60 秒以内的偏差要放行，否则时钟略慢的客户端全部 403。"""
    ts = str(int(time.time()) + 30)
    assert csrf._validate_token(f'{ts}.{_sign(csrf._secret, ts)}') is True


def test_tampered_signature_rejected(csrf):
    ts = str(int(time.time()))
    assert csrf._validate_token(f'{ts}.{"0" * 64}') is False


def test_malformed_token_rejected(csrf):
    for bad in ('', 'nodot', 'a.b', '123.', '.' + '0' * 64):
        assert csrf._validate_token(bad) is False, f'{bad!r} 被接受了'


def test_missing_secret_fails_closed(csrf, caplog):
    """**本轮核心**：密钥为空时**拒绝**（原来返回 True，任意串都通过）。"""
    csrf._secret = None
    import logging
    with caplog.at_level(logging.ERROR):
        assert csrf._validate_token('garbage') is False, (
            'CSRF 开着但密钥为空时放行了 —— 保护静默失效')
    assert any('CSRF 已启用但密钥为空' in r.getMessage() for r in caplog.records)


def test_missing_secret_logs_only_once(csrf, caplog):
    """日志只打一次 —— 否则每个请求刷一行 ERROR，把真正的错误淹掉。"""
    csrf._secret = None
    import logging
    with caplog.at_level(logging.ERROR):
        for _ in range(5):
            csrf._validate_token('x')
    hits = [r for r in caplog.records if 'CSRF 已启用但密钥为空' in r.getMessage()]
    assert len(hits) == 1, f'刷了 {len(hits)} 条'


# ---------------------------------------------------------------------------
# before_request 中间件
# ---------------------------------------------------------------------------

@pytest.fixture
def app_with_csrf(monkeypatch):
    """构造一个真的注册了 before_request 的 Flask app。"""
    from flask import Flask

    import config
    monkeypatch.setattr(config.SecurityConfig, 'CSRF_ENABLED', True, raising=False)
    monkeypatch.setattr(config.SecurityConfig, 'CSRF_SECRET', 'x' * 32, raising=False)

    app = Flask(__name__)
    CSRFProtection().init_app(app)

    @app.route('/api/devices', methods=['POST', 'GET'])
    def devices():
        return {'ok': True}

    @app.route('/api/auth/login', methods=['POST'])
    def login():
        return {'ok': True}

    return app


def _token_for_app(app):
    """用 app 里那个实例的密钥签一个令牌。

    `init_app` 从 `SecurityConfig.CSRF_SECRET` 取密钥，所以这里用同一个值签。
    """
    import config
    ts = str(int(time.time()))
    return f'{ts}.{_sign(config.SecurityConfig.CSRF_SECRET.encode(), ts)}'


def test_post_without_token_is_403(app_with_csrf):
    r = app_with_csrf.test_client().post('/api/devices')
    assert r.status_code == 403


def test_post_with_valid_token_passes(app_with_csrf):
    r = app_with_csrf.test_client().post(
        '/api/devices', headers={'X-CSRF-Token': _token_for_app(app_with_csrf)})
    assert r.status_code == 200


def test_get_is_not_checked(app_with_csrf):
    """读请求不校验 —— 否则前端所有 GET 都要带令牌。"""
    assert app_with_csrf.test_client().get('/api/devices').status_code == 200


def test_bearer_auth_is_exempt(app_with_csrf):
    """Bearer 令牌本身就是防护（跨站请求带不上自定义头）。"""
    r = app_with_csrf.test_client().post(
        '/api/devices', headers={'Authorization': 'Bearer abc'})
    assert r.status_code == 200


def test_login_is_exempt(app_with_csrf):
    """登录页还没有令牌，必须豁免 —— 否则谁都登不进来。"""
    assert app_with_csrf.test_client().post('/api/auth/login').status_code == 200


def test_future_token_is_403(app_with_csrf):
    """端到端：未来时间戳的令牌必须 403（不只是 `_validate_token` 层面）。"""
    import config
    ts = str(int(time.time()) + 86400 * 365 * 10)
    token = f'{ts}.{_sign(config.SecurityConfig.CSRF_SECRET.encode(), ts)}'
    r = app_with_csrf.test_client().post(
        '/api/devices', headers={'X-CSRF-Token': token})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 守卫：模型依赖 SameSite=Strict
# ---------------------------------------------------------------------------

def test_auth_cookie_keeps_samesite_strict():
    """**守卫**：登录 cookie 必须保持 `SameSite=Strict`。

    CSRF 令牌**不绑定用户/会话**（见模块 docstring 的"已知限制"），
    之所以现在还能接受，全靠这条 cookie 让跨站请求带不上登录态。
    一旦有人为了嵌 iframe / 桌面壳 / 跨域前端把它放宽成 `Lax` / `None`，
    CSRF 防护就真的破了 —— 这条用例专门让那种改动**红**。
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / '展示层' / 'api' / 'api_auth.py'
           ).read_text(encoding='utf-8')
    assert "set_cookie('token'" in src, '登录 cookie 的设置点变了，请同步本守卫'
    idx = src.index("set_cookie('token'")
    line = src[idx:src.index('\n', idx)]
    assert 'samesite=' in line.lower(), '登录 cookie 没有设置 SameSite'
    assert "samesite='strict'" in line.lower().replace('"', "'"), (
        '登录 cookie 的 SameSite 被放宽了 —— CSRF 令牌不绑定会话，'
        '放宽后跨站请求就能带上登录态，CSRF 防护实际失效')
