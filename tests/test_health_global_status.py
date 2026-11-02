# -*- coding: utf-8 -*-
"""顶层 global_status 必须反映健康检查的结论（round 168d）。

背景
----
`/api/health/status` 的 docstring 写明「无需认证，供负载均衡器探活」——
也就是说**顶层 `global_status` 就是结论本身**，探活方只看这一个字段，
不会去翻 `checks` 明细。

round 340 起：公开端点只回 `version` + `global_status`，明细
（`modules` / `checks` / `unhealthy_*`）迁移到**已认证**的
`/api/health/status/detail`。所以本文件里「验证结论与明细是否一致」
的用例统一打 detail —— 那是现在唯一能同时拿到两者的地方；
公开端点的形状由文件末尾那条用例单独钉住。

但原实现只统计 `ModuleRegistry`（模块注册表的 error / disabled / unavailable），
**健康检查的结果完全不参与计算**。于是磁盘写满、内存打爆、数据库探测失败时：

    data.checks.checks.disk.status == 'unhealthy'
    data.global_status             == 'healthy'      # ← 结论与事实相反

这套健康检查对外等于不存在。本文件锁住修复后的行为。

同时锁住：模块注册表出问题也要如实反映（原先只降到 degraded）。
"""


def _patch_health(monkeypatch, checks, global_status='healthy'):
    """把 HealthChecker.get_status 换成固定返回。"""
    from core.health_checker import HealthChecker

    def fake(cls):
        return {
            'global_status': global_status,
            'checks': {n: {'status': s, 'last_check': None} for n, s in checks.items()},
            'total_checks': len(checks),
        }

    monkeypatch.setattr(HealthChecker, 'get_status', classmethod(fake))


def _patch_modules(monkeypatch, modules):
    """把 ModuleRegistry.get_status 换成固定返回。"""
    from core.module_registry import ModuleRegistry
    monkeypatch.setattr(ModuleRegistry, 'get_status', classmethod(lambda cls, name=None: modules))


def test_healthy_everything_stays_healthy(app, client, auth_headers, monkeypatch):
    """全部健康 → healthy。"""
    _patch_modules(monkeypatch, {'database': {'status': 'running'}})
    _patch_health(monkeypatch, {'disk': 'healthy', 'memory': 'healthy'})

    r = client.get('/api/health/status/detail', headers=auth_headers)
    assert r.status_code == 200
    data = r.get_json()['data']
    assert data['global_status'] == 'healthy', data['global_status']
    assert data['unhealthy_checks'] == []


def test_unhealthy_check_must_make_global_unhealthy(app, client, auth_headers, monkeypatch):
    """**核心回归**：检查项 unhealthy 时顶层必须 unhealthy。

    修复前这里会拿到 'healthy' —— 因为模块注册表里没有任何模块，
    `unhealthy_modules` 为空，直接走 else 分支。
    """
    _patch_modules(monkeypatch, {})
    _patch_health(monkeypatch, {'disk': 'unhealthy', 'memory': 'healthy'},
                  global_status='unhealthy')

    r = client.get('/api/health/status/detail', headers=auth_headers)
    data = r.get_json()['data']
    assert data['global_status'] == 'unhealthy', \
        f"检查项 unhealthy 但顶层是 {data['global_status']}"
    assert 'disk' in data['unhealthy_checks']


def test_degraded_check_makes_global_degraded(app, client, auth_headers, monkeypatch):
    """检查项 degraded → 顶层 degraded（而不是 healthy）。"""
    _patch_modules(monkeypatch, {})
    _patch_health(monkeypatch, {'memory': 'degraded', 'disk': 'healthy'},
                  global_status='degraded')

    r = client.get('/api/health/status/detail', headers=auth_headers)
    data = r.get_json()['data']
    assert data['global_status'] == 'degraded', data['global_status']


def test_unhealthy_module_must_make_global_unhealthy(app, client, auth_headers, monkeypatch):
    """模块注册表里有 error/disabled/unavailable → unhealthy。

    修复前只降到 'degraded' —— 一个模块彻底起不来，探活方却只看到「降级」。
    """
    _patch_modules(monkeypatch, {'collector': {'status': 'error'}})
    _patch_health(monkeypatch, {'disk': 'healthy'})

    r = client.get('/api/health/status/detail', headers=auth_headers)
    data = r.get_json()['data']
    assert data['global_status'] == 'unhealthy', data['global_status']
    assert 'collector' in data['unhealthy_modules']


def test_unhealthy_beats_degraded(app, client, auth_headers, monkeypatch):
    """同时有 unhealthy 与 degraded 时，取更严重的。"""
    _patch_modules(monkeypatch, {})
    _patch_health(monkeypatch, {'disk': 'unhealthy', 'memory': 'degraded'},
                  global_status='unhealthy')

    r = client.get('/api/health/status/detail', headers=auth_headers)
    assert r.get_json()['data']['global_status'] == 'unhealthy'


def test_unknown_check_alone_does_not_degrade(app, client, auth_headers, monkeypatch):
    """全是 unknown（服务刚起、首次检查还没跑）不该被误报成不健康。"""
    _patch_modules(monkeypatch, {})
    _patch_health(monkeypatch, {'disk': 'unknown', 'memory': 'unknown'},
                  global_status='unknown')

    r = client.get('/api/health/status/detail', headers=auth_headers)
    data = r.get_json()['data']
    assert data['global_status'] == 'healthy', data['global_status']


# ===========================================================================
# 公开端点只回结论（round 340）
# ===========================================================================

def test_public_status_exposes_conclusion_only(app, client, monkeypatch):
    """免认证端点只回 version + global_status，**不得**带明细。

    为什么必须钉住：这个端点免认证，原来把 `modules` / `checks` 全量
    （含 `f'数据库连接失败: {e}'` 这类异常原文）一起返回 ——
    内网任何人不带凭证就能读到内部组件名与错误文本。

    反向对照（缺一不可）：`global_status` 必须**仍然**在 ——
    去敏不能把探活功能一起删了。
    """
    _patch_modules(monkeypatch, {'collector': {'status': 'error'}})
    _patch_health(monkeypatch, {'disk': 'unhealthy'}, global_status='unhealthy')

    r = client.get('/api/health/status')
    data = r.get_json()['data']

    # 必须保留：结论 + 版本（探活方真正需要的）
    assert data['global_status'] == 'unhealthy', data['global_status']
    assert 'version' in data

    # 必须消失：任何能推断内部结构的明细
    for leaked in ('modules', 'checks', 'unhealthy_modules', 'unhealthy_checks'):
        assert leaked not in data, (
            f'免认证端点泄露了 {leaked} —— 明细必须走已认证的 /status/detail')
