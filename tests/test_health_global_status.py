# -*- coding: utf-8 -*-
"""顶层 global_status 必须反映健康检查的结论（round 168d）。

背景
----
`/api/health/status` 的 docstring 写明「无需认证，供负载均衡器探活」——
也就是说**顶层 `global_status` 就是结论本身**，探活方只看这一个字段，
不会去翻 `checks` 明细。

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


def test_healthy_everything_stays_healthy(app, client, monkeypatch):
    """全部健康 → healthy。"""
    _patch_modules(monkeypatch, {'database': {'status': 'running'}})
    _patch_health(monkeypatch, {'disk': 'healthy', 'memory': 'healthy'})

    r = client.get('/api/health/status')
    assert r.status_code == 200
    data = r.get_json()['data']
    assert data['global_status'] == 'healthy', data['global_status']
    assert data['unhealthy_checks'] == []


def test_unhealthy_check_must_make_global_unhealthy(app, client, monkeypatch):
    """**核心回归**：检查项 unhealthy 时顶层必须 unhealthy。

    修复前这里会拿到 'healthy' —— 因为模块注册表里没有任何模块，
    `unhealthy_modules` 为空，直接走 else 分支。
    """
    _patch_modules(monkeypatch, {})
    _patch_health(monkeypatch, {'disk': 'unhealthy', 'memory': 'healthy'},
                  global_status='unhealthy')

    r = client.get('/api/health/status')
    data = r.get_json()['data']
    assert data['global_status'] == 'unhealthy', \
        f"检查项 unhealthy 但顶层是 {data['global_status']}"
    assert 'disk' in data['unhealthy_checks']


def test_degraded_check_makes_global_degraded(app, client, monkeypatch):
    """检查项 degraded → 顶层 degraded（而不是 healthy）。"""
    _patch_modules(monkeypatch, {})
    _patch_health(monkeypatch, {'memory': 'degraded', 'disk': 'healthy'},
                  global_status='degraded')

    r = client.get('/api/health/status')
    data = r.get_json()['data']
    assert data['global_status'] == 'degraded', data['global_status']


def test_unhealthy_module_must_make_global_unhealthy(app, client, monkeypatch):
    """模块注册表里有 error/disabled/unavailable → unhealthy。

    修复前只降到 'degraded' —— 一个模块彻底起不来，探活方却只看到「降级」。
    """
    _patch_modules(monkeypatch, {'collector': {'status': 'error'}})
    _patch_health(monkeypatch, {'disk': 'healthy'})

    r = client.get('/api/health/status')
    data = r.get_json()['data']
    assert data['global_status'] == 'unhealthy', data['global_status']
    assert 'collector' in data['unhealthy_modules']


def test_unhealthy_beats_degraded(app, client, monkeypatch):
    """同时有 unhealthy 与 degraded 时，取更严重的。"""
    _patch_modules(monkeypatch, {})
    _patch_health(monkeypatch, {'disk': 'unhealthy', 'memory': 'degraded'},
                  global_status='unhealthy')

    r = client.get('/api/health/status')
    assert r.get_json()['data']['global_status'] == 'unhealthy'


def test_unknown_check_alone_does_not_degrade(app, client, monkeypatch):
    """全是 unknown（服务刚起、首次检查还没跑）不该被误报成不健康。"""
    _patch_modules(monkeypatch, {})
    _patch_health(monkeypatch, {'disk': 'unknown', 'memory': 'unknown'},
                  global_status='unknown')

    r = client.get('/api/health/status')
    data = r.get_json()['data']
    assert data['global_status'] == 'healthy', data['global_status']
