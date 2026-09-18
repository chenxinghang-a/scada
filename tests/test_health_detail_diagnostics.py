# -*- coding: utf-8 -*-
"""诊断接口探测失败时必须留下原因（round 168e）。

背景
----
`/api/health/status/detail` 是给运维看「哪个组件坏了、为什么坏」的。
它的 6 个组件探测都是「失败就降级为 unknown/unavailable，不抛异常」——
这个降级本身是对的（单个组件探不到不该让整个诊断接口 500）。

但原先 6 处全是裸 `except Exception:`：**既不记日志也不带原因**。
后果是「组件真的坏了」和「组件正常、但探测代码本身出错」在响应里长得一模一样，
运维只看到一个 `unknown`，无从下手。

这不是假想 —— 真实踩过：`from 展示层.websocket import get_connected_count`
里的函数曾因改名而不存在，ImportError 被吞掉，websocket 状态**永远是 unknown**，
直到有人读代码才发现。

本文件锁住：降级照旧，但必须带 `reason`，且日志里要有完整原因。
"""

import logging


def test_websocket_probe_failure_carries_reason(app, client, auth_headers,
                                                monkeypatch, caplog):
    """探测抛异常时：status 仍降级，但必须带上原因 + 落日志。"""
    import 展示层.websocket as ws

    def boom():
        raise RuntimeError('get_connected_count 改名了')

    # api_health 里是函数内 import，patch 模块属性即可生效
    monkeypatch.setattr(ws, 'get_connected_count', boom, raising=False)

    with caplog.at_level(logging.WARNING):
        r = client.get('/api/health/status/detail', headers=auth_headers)

    assert r.status_code == 200, f'单个组件探测失败不该让诊断接口 500: {r.status_code}'
    ws_info = r.get_json()['data']['websocket']
    assert ws_info['status'] == 'unknown'
    assert 'get_connected_count 改名了' in ws_info.get('reason', ''), \
        f"探测失败没带原因，运维无从判断: {ws_info}"
    assert any('WebSocket 状态探测失败' in rec.message for rec in caplog.records), \
        '探测失败没有落日志'


def test_successful_probe_has_no_reason_field(app, client, auth_headers, monkeypatch):
    """探测成功时不带 reason（避免让前端以为有异常）。"""
    import 展示层.websocket as ws
    monkeypatch.setattr(ws, 'get_connected_count', lambda: 3, raising=False)

    r = client.get('/api/health/status/detail', headers=auth_headers)
    ws_info = r.get_json()['data']['websocket']
    assert ws_info['status'] == 'ok'
    assert ws_info['connected_clients'] == 3
    assert 'reason' not in ws_info


def test_all_probes_degraded_still_returns_200(app, client, auth_headers, monkeypatch):
    """多个组件同时探测失败，接口仍须 200 且逐个给出原因。"""
    import 展示层.websocket as ws
    monkeypatch.setattr(ws, 'get_connected_count',
                        lambda: (_ for _ in ()).throw(RuntimeError('ws 挂了')),
                        raising=False)

    r = client.get('/api/health/status/detail', headers=auth_headers)
    assert r.status_code == 200
    data = r.get_json()['data']
    # 数据库通常是好的，不该被连累
    assert data['database']['status'] == 'ok'
    assert data['websocket']['status'] == 'unknown'
    assert 'reason' in data['websocket']
