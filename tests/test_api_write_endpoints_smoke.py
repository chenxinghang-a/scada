# -*- coding: utf-8 -*-
"""写接口冒烟：带路径参数的 POST/PUT/DELETE 不许 5xx（round 257）。

为什么还要再来一遍
------------------
`test_api_smoke_all_endpoints.py` 的写接口用例有一个**大盲区**：

    if rule.arguments:      # 带参数的路径跳过（需要真实 id）
        continue

于是 `/api/devices/<device_id>/control`、`/api/alarms/<alarm_id>/acknowledge`
这类**真正会改现场状态的接口一个都没被扫到** —— 而它们恰恰是最容易出
`NoneType` / `KeyError` 的地方（"资源不存在"分支往往没人走）。

本文件的做法：把路径参数统一填成**不存在的 id**（`smoke-missing-id`）。
这样既不会真的删掉/改坏测试装置，又能逼着每个接口走一遍"找不到资源"的分支。
判据只有一条：**必须 4xx，不许 5xx**。
"""

from __future__ import annotations

import pytest

# 复用冒烟用例的真实装配（不是 mock）—— 见该文件头部的说明。
from tests.test_api_smoke_all_endpoints import real_app, smoke_client  # noqa: F401

#: 这些端点会**真的删数据 / 关服务 / 改口令**，用不存在的 id 也不安全：
#: 它们不按 id 定位资源，而是直接对整个库或整个进程动手。
SKIP_PREFIXES = (
    '/api/ops/cleanup',        # 批量清理历史数据
    '/api/ops/db/vacuum',      # 整库 VACUUM
    '/api/ops/db/backup',      # 整库备份
    '/api/system/shutdown',    # 关进程
    '/api/system/restart',     # 重启
    '/api/auth/change-password',   # 会改掉 admin 口令，后续用例全挂
    '/api/auth/refresh',       # 需要真实 refresh 令牌
    '/api/db/reset',           # 清库
    '/api/devices/import',     # 批量导入
    '/api/config/reload',      # 重载配置
)

MISSING_ID = 'smoke-missing-id'


def _write_rules(app):
    for rule in app.url_map.iter_rules():
        if rule.endpoint == 'static':
            continue
        for method in ('POST', 'PUT', 'DELETE', 'PATCH'):
            if method in rule.methods:
                yield rule, method


def _fill(path: str, rule) -> str:
    """把路径参数填成不存在的 id（`<path:...>` 要先替换）。"""
    for arg in rule.arguments:
        path = path.replace(f'<path:{arg}>', MISSING_ID)
        path = path.replace(f'<{arg}>', MISSING_ID)
    return path


def _skipped(path: str) -> bool:
    return any(path.startswith(p) for p in SKIP_PREFIXES)


BODIES = [
    pytest.param({}, id='empty-dict'),
    pytest.param({'value': 1e400}, id='inf'),
    pytest.param({'value': 'NaN'}, id='nan-string'),
    pytest.param([], id='list'),
    pytest.param(None, id='null'),
]


class TestWriteEndpointsWithMissingResource:
    def test_missing_resource_is_4xx_not_5xx(self, real_app, smoke_client):
        """资源不存在时必须是 404/400，不能 500（那说明分支里在拿 None 干活）。"""
        failures = []
        unrouted = []
        visited = 0
        for rule, method in _write_rules(real_app):
            path = _fill(str(rule), rule)
            if _skipped(path):
                continue
            visited += 1
            try:
                r = smoke_client.open(path, method=method, json={})
            except Exception as e:      # noqa: BLE001
                failures.append(f'{method} {path}: 抛异常 {type(e).__name__}: {e}')
                continue
            if r.status_code >= 500:
                failures.append(
                    f'{method} {path}: HTTP {r.status_code} '
                    f'{r.get_data(as_text=True)[:200]}')
            elif 'json' not in (r.content_type or ''):
                # 路由没命中时 Flask 回的是 HTML 404 页 —— 说明路径参数没填对，
                # 这条用例就变成"什么也没验"，必须显式报出来。
                unrouted.append(f'{method} {path}: {r.status_code} '
                                f'{r.content_type}')

        assert visited >= 40, f'只扫到 {visited} 个写接口，覆盖面异常（应 >= 40）'
        assert not unrouted, (
            '以下路径没命中路由（HTML 404 = 参数没填对，等于没测）：\n  '
            + '\n  '.join(unrouted))
        assert not failures, (
            '以下写接口在「资源不存在」时 5xx（应为 404/400）：\n  '
            + '\n  '.join(failures))

    @pytest.mark.parametrize('body', BODIES)
    def test_odd_bodies_are_4xx_not_5xx(self, real_app, smoke_client, body):
        """畸形 body（inf / NaN / 列表 / null）不许把服务打成 500。"""
        failures = []
        for rule, method in _write_rules(real_app):
            path = _fill(str(rule), rule)
            if _skipped(path):
                continue
            try:
                r = smoke_client.open(path, method=method, json=body)
            except Exception as e:      # noqa: BLE001
                failures.append(f'{method} {path}: 抛异常 {type(e).__name__}: {e}')
                continue
            if r.status_code >= 500:
                failures.append(
                    f'{method} {path}: HTTP {r.status_code} '
                    f'{r.get_data(as_text=True)[:200]}')
        assert not failures, (
            f'畸形 body {body!r} 导致 5xx：\n  ' + '\n  '.join(failures))
