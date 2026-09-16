"""Blueprint 注册完整性回归测试。

背景（2026-09-16 代码质量审计 P2-3）：
``展示层/api/api_performance.py`` 里定义了 ``performance_bp``（``/api/performance``），
但**从未被 ``展示层/api/__init__.py`` 导入，也不在 ``ALL_BLUEPRINTS`` 里** ——
于是 ``/api/performance/metrics/{realtime,history,summary}`` 三个接口全部 404，
而前端 ``src/views/PerformanceMonitor.vue`` → ``src/api/performance.ts`` 正好调这三个。
**整个性能监控页面在功能上是死的，但没有任何测试会发现它。**

这类"文件写好了、忘了挂上去"的缺陷不会报错、不会让任何既有测试变红，
只能靠静态扫描兜住。本文件就做这件事。
"""
import importlib
import re
from pathlib import Path

import pytest

API_DIR = Path(__file__).resolve().parent.parent / '展示层' / 'api'

_BP_RE = re.compile(r'^(\w+)\s*=\s*Blueprint\(', re.M)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        return path.read_text(encoding='utf-8', errors='replace')


def _declared_blueprints():
    """返回 [(模块名, 蓝图变量名)]，即所有在 api 包内定义过 Blueprint 的模块"""
    found = []
    for f in sorted(API_DIR.glob('*.py')):
        if f.name == '__init__.py':
            continue
        for var in _BP_RE.findall(_read(f)):
            found.append((f.stem, var))
    return found


def test_declared_blueprints_found():
    """确保扫描确实读到了蓝图，避免正则失效导致测试假通过"""
    declared = _declared_blueprints()
    assert len(declared) > 5, f'只扫到 {len(declared)} 个蓝图定义，解析可能有问题'


def test_every_declared_blueprint_is_registered():
    """定义了 Blueprint 就必须注册进 ALL_BLUEPRINTS，否则接口静默 404"""
    api_pkg = importlib.import_module('展示层.api')
    registered = list(api_pkg.ALL_BLUEPRINTS)

    missing = []
    for module_name, var_name in _declared_blueprints():
        module = importlib.import_module(f'展示层.api.{module_name}')
        bp = getattr(module, var_name)
        if not any(bp is b for b in registered):
            missing.append(
                f'{module_name}.py:{var_name} (url_prefix={bp.url_prefix!r})')

    assert not missing, (
        '以下 Blueprint 已定义但未加入 ALL_BLUEPRINTS，其接口会静默 404：\n  '
        + '\n  '.join(missing)
    )


def test_all_registered_blueprints_routes_are_mounted(app):
    """注册过的蓝图，其每条路由都必须真的出现在应用的 url_map 里"""
    rules = {r.rule for r in app.url_map.iter_rules()}

    missing = []
    for module_name, var_name in _declared_blueprints():
        module = importlib.import_module(f'展示层.api.{module_name}')
        bp = getattr(module, var_name)
        for route in _blueprint_routes(module, var_name):
            if route not in rules:
                missing.append(f'{module_name}.py:{var_name} → {route}')

    assert not missing, '以下路由未挂载到应用：\n  ' + '\n  '.join(missing)


def _blueprint_routes(module, var_name: str):
    """从模块源码里抠出该蓝图声明的路由（拼接 url_prefix）"""
    source = _read(Path(module.__file__))
    bp = getattr(module, var_name)
    prefix = bp.url_prefix or ''
    routes = []
    for m in re.finditer(rf'@{var_name}\.route\(\s*[\'"]([^\'"]+)[\'"]', source):
        routes.append(prefix + m.group(1))
    return routes


def test_performance_api_is_reachable(app):
    """回归：性能监控接口必须存在（前端 PerformanceMonitor.vue 依赖它）"""
    rules = {r.rule for r in app.url_map.iter_rules()}
    for route in ('/api/performance/metrics/realtime',
                  '/api/performance/metrics/history',
                  '/api/performance/metrics/summary'):
        assert route in rules, f'{route} 未注册 —— 前端性能监控页面会 404'

    resp = app.test_client().get('/api/performance/metrics/realtime')
    assert resp.status_code in (401, 403), (
        f'性能接口未鉴权（期望 401/403，实际 {resp.status_code}）'
    )
