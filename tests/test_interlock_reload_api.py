# -*- coding: utf-8 -*-
"""联锁热重载接口（round 369 / BUG-36）。

`DeviceControlSafety.reload_interlocks()` 早已实现差集重载 + 空表回滚，
但**没有任何生产调用点** —— 现场改 `配置/interlocks.yaml` 必须重启进程。
本文件钉住新接口的三条契约：

1. 正常重载：200，返回 added/removed/changed/kept；
2. 回滚保护触发（`error` 键）：**500 + rolled_back**，不能谎报成功；
3. 控制安全模块未初始化：503，不能 500 也不能静默 200。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _fake_control(diff: dict) -> MagicMock:
    control = MagicMock()
    control.reload_interlocks.return_value = diff
    return control


def test_reload_returns_rule_diff(app, client, auth_headers):
    app.device_control = _fake_control({
        'added': ['IL_NEW'],
        'removed': ['IL_OLD'],
        'changed': ['IL_TEMP'],
        'kept': ['IL_PRESS'],
        'source': '配置/interlocks.yaml',
    })

    resp = client.post('/api/system/interlocks/reload', headers=auth_headers)
    body = resp.get_json()

    assert resp.status_code == 200
    assert body['success'] is True
    assert body['added'] == ['IL_NEW']
    assert body['removed'] == ['IL_OLD']
    assert body['changed'] == ['IL_TEMP']
    assert body['kept'] == ['IL_PRESS']


def test_rollback_is_reported_as_failure(app, client, auth_headers):
    """重载后规则为 0 → 自动回滚：旧规则还在，但这次重载**没成功**。"""
    app.device_control = _fake_control({
        'added': [], 'removed': [], 'changed': [],
        'kept': ['IL_TEMP', 'IL_PRESS'],
        'source': 'rollback',
        'error': '重载后规则为 0，已回滚到重载前的规则',
    })

    resp = client.post('/api/system/interlocks/reload', headers=auth_headers)
    body = resp.get_json()

    assert resp.status_code == 500
    assert body['success'] is False
    assert body['rolled_back'] is True
    assert body['kept'] == ['IL_TEMP', 'IL_PRESS']


def test_missing_control_module_returns_503(app, client, auth_headers):
    app.device_control = None

    resp = client.post('/api/system/interlocks/reload', headers=auth_headers)

    assert resp.status_code == 503
    assert resp.get_json()['success'] is False
