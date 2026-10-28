# -*- coding: utf-8 -*-
"""round 306：诚实清单最后两条（`/metrics` 抓不到、单人旁路端点）。

两条都是「功能存在、但在真实部署里不生效」：
· `/metrics` 被页面级鉴权重定向到 /login —— Prometheus 一条都采不到，且无报错；
· deprecated 的单人旁路端点仍在用 —— 任意 engineer 可永久旁路安全联锁。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. /metrics 必须可达
# ---------------------------------------------------------------------------

def test_metrics_endpoint_serves_exposition(client):
    """端点在应用层不需要登录（中间件放行由下面的源码守卫兜住）。"""
    resp = client.get('/metrics')

    assert resp.status_code == 200, f'/metrics 返回 {resp.status_code}'
    body = resp.get_data(as_text=True)
    assert 'scada_' in body, '响应里没有任何 scada_ 指标 —— 这不是 exposition 输出'


def test_page_auth_middleware_allows_metrics():
    """源码守卫：放行 `/metrics` 的分支必须存在，且在 redirect 之前。

    这个中间件是 `create_app()` 里的闭包，没法从测试里单独取出来跑；
    但它一旦被人「顺手整理」掉，Prometheus 就又只能拿到 302 ——
    而那种失效是**完全静默**的（Grafana 面板只是没数据）。
    """
    src = (REPO / '展示层' / 'routes.py').read_text(encoding='utf-8')
    seg = src.split('def check_page_auth', 1)[1]

    assert "path == '/metrics'" in seg, '页面级鉴权没有放行 /metrics'
    assert seg.index("path == '/metrics'") < seg.index("return redirect('/login')"), (
        '/metrics 的放行写在了 redirect 之后 —— 等于没放行')


def test_metrics_token_enforced_when_configured(monkeypatch, client):
    """设了 PROMETHEUS_SCRAPE_TOKEN 时，匿名抓取必须被拒。

    这里只能验证「配置了 token 就要求 header」这条语义在代码里成立
    （中间件是闭包，见上一条）；实际 403 由 create_app 装配后的应用产生。
    """
    import importlib
    routes = importlib.import_module('展示层.routes')
    src = Path(routes.__file__).read_text(encoding='utf-8')
    seg = src.split('def check_page_auth', 1)[1]

    assert 'PROMETHEUS_SCRAPE_TOKEN' in seg, '没有可选的 scrape token 加固'
    assert 'X-Prometheus-Scrape-Token' in seg, '没有校验应有的 header 名'


# ---------------------------------------------------------------------------
# 2. 联锁旁路必须走双人审批
# ---------------------------------------------------------------------------

def _dc(status=None, request_id='req1234'):
    dc = MagicMock()
    dc.get_interlock_status.return_value = status if status is not None else {'il1': {'name': '超温'}}
    dc.request_bypass.return_value = request_id
    return dc


def test_bypass_goes_through_approval(client, auth_headers, app):
    dc = _dc()
    app.device_control = dc

    resp = client.post('/api/control/interlocks/il1/bypass', headers=auth_headers,
                       json={'reason': '维护'})
    data = resp.get_json()

    assert resp.status_code == 200
    assert data['success'] is True
    assert data['applied'] is False, (
        '接口回的是「已旁路」—— 实际只是创建了审批单，联锁仍在生效')
    assert data['request_id'] == 'req1234'
    assert '批准' in data['message'], f'文案没有说明需要第二人批准：{data["message"]!r}'
    dc.bypass_interlock.assert_not_called()


def test_bypass_rejects_unknown_interlock(client, auth_headers, app):
    dc = _dc(status={})
    app.device_control = dc

    resp = client.post('/api/control/interlocks/nope/bypass', headers=auth_headers,
                       json={'reason': '维护'})

    assert resp.status_code == 404
    dc.request_bypass.assert_not_called()


def test_bypass_rejects_bad_timeout(client, auth_headers, app):
    dc = _dc()
    app.device_control = dc

    assert client.post('/api/control/interlocks/il1/bypass', headers=auth_headers,
                       json={'timeout_minutes': 'abc'}).status_code == 400
    assert client.post('/api/control/interlocks/il1/bypass', headers=auth_headers,
                       json={'timeout_minutes': -5}).status_code == 400
    assert client.post('/api/control/interlocks/il1/bypass', headers=auth_headers,
                       json={'timeout_minutes': 9999}).status_code == 400
    dc.request_bypass.assert_not_called()


def test_bypass_passes_positive_timeout(client, auth_headers, app):
    """反向对照：合法时长必须真的提交审批。"""
    dc = _dc()
    app.device_control = dc

    resp = client.post('/api/control/interlocks/il1/bypass', headers=auth_headers,
                       json={'reason': '维护', 'timeout_minutes': 45})

    assert resp.status_code == 200
    args = dc.request_bypass.call_args[0]
    assert args[0] == 'il1' and args[3] == 45
