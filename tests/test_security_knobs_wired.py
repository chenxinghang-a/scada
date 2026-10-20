# -*- coding: utf-8 -*-
"""三个「安全旋钮是装饰品」的回归测试（round 218）。

缺陷类别（MEMORY 硬规则 17）：**算了但没人读**
==============================================
`tools/scan_config_readers.py` 实测（2026-10-19）报出 53 项配置在生产代码里
没有读取点。其中三项**直接关系安全**，且都是「设了不生效」：

1. `SecurityConfig.RATE_LIMIT_ENABLED / RATE_LIMIT_DEFAULT / RATE_LIMIT_LOGIN`
   —— 限流强度实际写在 `core/rate_limiter.py` 的 `RATE_LIMIT_CONFIG` 里，
   运维改环境变量（甚至以为关掉了限流）**完全无效**。
2. `FlaskConfig.MAX_CONTENT_LENGTH` —— 定义在 config.py，但从未交给 Flask，
   Werkzeug 用默认（不限），一个超大 JSON body 就能顶到 OOM。
3. `AuthConfig.MIN_PASSWORD_LENGTH` —— 配置写 6、代码硬编码 8，
   两个数字对不上且配置侧没人读；调大调小都不生效。

本文件钉住「接上之后不许再断」，同时钉住两条**方向性约束**：
- 密码长度**只能收紧不能放宽**（地板 8，等保2.0）；
- 限流默认值与历史行为**逐字一致**（接旋钮不许顺手改强度）。
"""

from unittest.mock import MagicMock, patch

import pytest

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from config import AuthConfig, FlaskConfig, SecurityConfig  # noqa: E402


# --------------------------------------------------------------------------
# 1. 限流旋钮
# --------------------------------------------------------------------------

def test_rate_limit_config_uses_security_config_values():
    """`RATE_LIMIT_CONFIG` 的 default / login 必须来自 SecurityConfig。"""
    from core.rate_limiter import RATE_LIMIT_CONFIG

    assert RATE_LIMIT_CONFIG['default'][0] == SecurityConfig.RATE_LIMIT_DEFAULT, (
        "default 限流第一段没有取 SecurityConfig.RATE_LIMIT_DEFAULT —— "
        "环境变量 RATE_LIMIT_DEFAULT 又不生效了"
    )
    assert RATE_LIMIT_CONFIG['login'][0] == SecurityConfig.RATE_LIMIT_LOGIN, (
        "login 限流第一段没有取 SecurityConfig.RATE_LIMIT_LOGIN —— "
        "环境变量 RATE_LIMIT_LOGIN 又不生效了"
    )


def test_rate_limit_defaults_unchanged_from_history():
    """接旋钮不许顺手改强度：默认值必须仍是历史的 200/min 与 5/min。"""
    from core.rate_limiter import RATE_LIMIT_CONFIG

    assert RATE_LIMIT_CONFIG['default'] == ['200 per minute', '50 per second']
    assert RATE_LIMIT_CONFIG['login'] == ['5 per minute', '10 per hour']


def test_rate_limit_enabled_flag_is_honored():
    """`RATE_LIMIT_ENABLED=false` 必须真的停用限流（而不是只写日志）。"""
    from flask import Flask

    from core import rate_limiter as rl

    with patch.object(rl.SecurityConfig, 'RATE_LIMIT_ENABLED', False):
        limiter = rl.create_limiter(Flask(__name__))
        assert limiter.enabled is False, (
            "RATE_LIMIT_ENABLED=false 时 Limiter 仍处于启用状态 —— 旋钮是装饰品"
        )

    with patch.object(rl.SecurityConfig, 'RATE_LIMIT_ENABLED', True):
        limiter = rl.create_limiter(Flask(__name__))
        assert limiter.enabled is True


def test_limiter_instance_still_registered_when_disabled():
    """停用限流时 `get_limiter()` 仍要拿到实例，否则装饰器会 AttributeError。"""
    from flask import Flask

    from core import rate_limiter as rl

    with patch.object(rl.SecurityConfig, 'RATE_LIMIT_ENABLED', False):
        rl.create_limiter(Flask(__name__))
        assert rl.get_limiter() is not None, (
            "限流停用时没有登记全局实例 —— rate_limit_* 装饰器一用就 AttributeError"
        )


# --------------------------------------------------------------------------
# 2. 请求体上限
# --------------------------------------------------------------------------

def _make_app():
    """用真实 create_app 建应用（只 mock 数据库/设备这类外部依赖）。

    ⚠️ **不要 mock `create_limiter`**：`apply_endpoint_limits` 会用它把每个端点
    重新包一层（`limiter.limit(...)(view)`）。如果传入 MagicMock，被包过的视图
    直接返回 MagicMock 响应 —— 所有请求都是「200 + 空 body」，
    于是任何行为断言（413 / 400 / 401）都会**假绿**。
    """
    with patch('展示层.routes.AuthManager') as MockAuth:
        MockAuth.return_value = MagicMock()
        from 展示层.routes import create_app
        return create_app(
            database=MagicMock(),
            device_manager=MagicMock(),
            alarm_manager=MagicMock(),
            data_collector=MagicMock(),
        )


def test_max_content_length_applied_to_flask():
    """`FlaskConfig.MAX_CONTENT_LENGTH` 必须真的写进 app.config。"""
    app = _make_app()
    assert app.config.get('MAX_CONTENT_LENGTH') == FlaskConfig.MAX_CONTENT_LENGTH, (
        f"app.config['MAX_CONTENT_LENGTH'] = {app.config.get('MAX_CONTENT_LENGTH')!r}，"
        f"期望 {FlaskConfig.MAX_CONTENT_LENGTH} —— 配置没接线，请求体不受限"
    )


def test_oversized_body_is_rejected_413():
    """行为验证：超过上限的请求体必须被拒（413），而不是先读进内存。"""
    app = _make_app()
    limit = FlaskConfig.MAX_CONTENT_LENGTH
    client = app.test_client()
    resp = client.post(
        '/api/auth/login',
        data=b'x' * (limit + 1024),
        content_type='application/json',
    )
    assert resp.status_code == 413, (
        f"超大请求体返回 {resp.status_code}，期望 413 —— 上限没生效"
    )


# --------------------------------------------------------------------------
# 3. 密码长度
# --------------------------------------------------------------------------

def _validator():
    from 用户层.auth import AuthManager
    return AuthManager.__new__(AuthManager)._validate_password_strength


def test_password_length_follows_config_when_tightened():
    """调大 MIN_PASSWORD_LENGTH 必须生效（此前硬编码 8，调大也没用）。"""
    validate = _validator()
    with patch.object(AuthConfig, 'MIN_PASSWORD_LENGTH', 12):
        ok, msg = validate('Abcdefg1')          # 8 位
        assert not ok, "配置要求 12 位，8 位密码却通过了"
        assert '12' in msg, msg
        ok, _ = validate('Abcdefghij1')          # 11 位
        assert not ok
        ok, _ = validate('Abcdefghijk1')         # 12 位
        assert ok, "12 位密码应通过"


def test_password_length_has_floor_of_8():
    """配置调小时**不许**低于 8（等保2.0 地板，防止配一下就绕过合规）。"""
    validate = _validator()
    with patch.object(AuthConfig, 'MIN_PASSWORD_LENGTH', 4):
        ok, msg = validate('Abc1')               # 4 位
        assert not ok, "配置写 4 位就放行 4 位密码 —— 合规地板被绕过了"
        assert '8' in msg, msg


def test_password_strength_still_requires_all_classes():
    """长度接上之后，大小写/数字三类字符的要求不能被顺手改掉。"""
    validate = _validator()
    with patch.object(AuthConfig, 'MIN_PASSWORD_LENGTH', 8):
        assert not validate('abcdefg1')[0], '缺大写字母却通过'
        assert not validate('ABCDEFG1')[0], '缺小写字母却通过'
        assert not validate('Abcdefgh')[0], '缺数字却通过'
        assert validate('Abcdefg1')[0]
