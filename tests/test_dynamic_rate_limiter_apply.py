"""动态限流「下发到 Flask-Limiter」必须真的生效。

## 为什么需要这条用例

`DynamicRateLimiter._apply_to_flask_limiter()` 原来用
`Limit(limit_provider=..., key_func=...)` 构造限流对象。flask-limiter 4.0
把该形参改名成 `key_function`，旧名直接 TypeError —— 而异常被方法内的
`except Exception` 吞掉，只留一行 warning「动态限流下发失败」。

表现是最坏的一种：**配置改了、日志说"动态限流调整 low → medium"、
实际阈值纹丝不动**。压测/被刷时运维调阈值，看日志以为生效了。

这条用例把「下发后 default_limits 真的变了」钉死：只要参数名再次对不上
（升/降 flask-limiter 大版本），它立刻红。
"""

from __future__ import annotations

import pytest
from flask import Flask
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from core.dynamic_rate_limiter import LoadLevel, dynamic_rate_limiter


def _make_limiter(default_limit: str = "200 per minute") -> Limiter:
    app = Flask(__name__)
    app.config["RATELIMIT_ENABLED"] = True
    return Limiter(key_func=get_remote_address, app=app,
                   default_limits=[default_limit])


def _current_defaults(limiter: Limiter) -> list[str]:
    return [str(l.limit_provider) for l in limiter.limit_manager._default_limits]


@pytest.fixture()
def attached():
    """注入一个干净的 Limiter，用例结束后把单例恢复原状。"""
    limiter = _make_limiter()
    old_limiter = dynamic_rate_limiter._flask_limiter
    old_level = dynamic_rate_limiter._current_level
    old_profile = dict(dynamic_rate_limiter._current_profile)
    dynamic_rate_limiter.set_flask_limiter(limiter)
    try:
        yield limiter
    finally:
        dynamic_rate_limiter.set_flask_limiter(old_limiter)
        dynamic_rate_limiter._current_level = old_level
        dynamic_rate_limiter._current_profile = old_profile


def test_apply_reports_success(attached):
    """下发成功必须返回 True —— 返回 False 时上层会打「未下发」错误日志。"""
    assert dynamic_rate_limiter._apply_to_flask_limiter() is True


def test_apply_really_changes_default_limits(attached):
    """核心断言：下发后 Flask-Limiter 的 default_limits 必须换成新值。

    只断言返回值不够 —— 曾经返回值也是「成功」，但阈值没变。
    """
    before = _current_defaults(attached)
    assert dynamic_rate_limiter._apply_to_flask_limiter() is True
    after = _current_defaults(attached)

    assert after != before, f"下发后 default_limits 没变，仍是 {before}"
    profile = dynamic_rate_limiter._current_profile
    expected = f"{profile['rpm']} per minute; {profile['rps']} per second"
    assert expected in after, f"期望 {expected!r}，实际 {after}"


def test_apply_follows_load_level_switch(attached):
    """从默认档切到 CRITICAL，阈值必须跟着变严。"""
    assert dynamic_rate_limiter._apply_to_flask_limiter() is True
    normal = _current_defaults(attached)

    dynamic_rate_limiter._current_level = LoadLevel.CRITICAL
    dynamic_rate_limiter._current_profile = {"rpm": 30, "rps": 1, "name": "critical"}
    assert dynamic_rate_limiter._apply_to_flask_limiter() is True
    critical = _current_defaults(attached)

    assert critical != normal
    assert "30 per minute; 1 per second" in critical


def test_apply_without_limiter_returns_false():
    """没注入 Limiter 时必须如实返回 False，不能假装成功。"""
    old = dynamic_rate_limiter._flask_limiter
    dynamic_rate_limiter.set_flask_limiter(None)
    try:
        assert dynamic_rate_limiter._apply_to_flask_limiter() is False
    finally:
        dynamic_rate_limiter.set_flask_limiter(old)
