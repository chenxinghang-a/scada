"""API 授权矩阵回归测试。

背景：2026-09 安全审计发现多个高危端点只要求登录（@jwt_required），
任何角色（含只读的 viewer）都能注入故障、强制降级、改运行时配置、
复位声光报警、导出全量数据。本测试静态扫描所有 API 路由的装饰器，
确保「变更类端点必须带角色/权限校验」，防止后续改动把权限改回去。

有意不校验角色的端点必须在 INTENTIONAL 里显式登记并写明理由。
"""
import re
from pathlib import Path

import pytest

API_DIR = Path(__file__).resolve().parent.parent / '展示层' / 'api'
MUTATING = ('POST', 'PUT', 'PATCH', 'DELETE')

# 有意只要求登录的变更类端点：(bp, route) -> 理由
INTENTIONAL = {
    ('auth_bp', '/auth/logout'):
        '登出：任何已认证用户都应能注销自己的会话',
    ('auth_bp', '/auth/change-password'):
        '自助改密：用户改自己的密码，内部会校验原密码',
    ('auth_bp', '/auth/force-change-password'):
        '强制改密：靠 body 里的令牌自鉴权，且模型层要求处于强制改密态',
    ('auth_bp', '/auth/register'):
        '注册：自行实现鉴权（无令牌时仅允许首个用户引导，否则必须 admin）',
    ('control_bp', '/control/estop'):
        '急停：原设计明确「任何角色均可触发」，属安全设计决策，'
        '已单独提给主人裁决是否收紧到 operator 以上',
}

# 有意完全不需要鉴权的端点（(bp, route) -> 理由）
INTENTIONAL_PUBLIC = {
    ('auth_bp', '/auth/login'): '登录入口本身不能要求已登录',
    ('auth_bp', '/auth/refresh'): '刷新令牌，凭 refresh_token 自鉴权',
    ('health_bp', '/status'): '健康探针，供监控系统匿名拉取',
    ('metrics_bp', '/metrics'): 'Prometheus 指标抓取端点',
    ('system_bp', '/system/client-errors'): '前端错误上报，允许匿名但已截断内容',
}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        return path.read_text(encoding='utf-8', errors='replace')


def _collect_routes():
    """返回 [(bp, route, methods, decorators, line)]"""
    rows = []
    route_re = re.compile(r"@(\w+)\.route\((.*)\)\s*$")
    for f in sorted(API_DIR.glob('api_*.py')):
        lines = _read(f).splitlines()
        for i, line in enumerate(lines):
            m = route_re.match(line.strip())
            if not m:
                continue
            bp, spec = m.group(1), m.group(2)
            quoted = re.findall(r"'([^']+)'", spec)
            route = quoted[0] if quoted else '?'
            methods = (re.findall(r"'(\w+)'", spec.split('methods=')[-1])
                       if 'methods=' in spec else ['GET'])
            decs, j = [], i + 1
            while j < len(lines) and lines[j].strip().startswith('@'):
                decs.append(lines[j].strip())
                j += 1
            rows.append((bp, route, methods, decs, i + 1))
    return rows


def _authz_level(decs):
    """返回 'role'（带角色/权限校验）/ 'jwt'（仅登录）/ 'none'（无鉴权）"""
    joined = ' '.join(decs)
    role_alias = re.search(r'@(_require_(?!auth\b)\w+)', joined)
    if ('role_required' in joined or 'permission_required' in joined
            or role_alias):
        return 'role'
    if 'jwt_required' in joined or '_require_auth' in joined:
        return 'jwt'
    return 'none'


def test_collect_routes_not_empty():
    """确保扫描确实读到了路由，避免路径写错导致测试假通过"""
    rows = _collect_routes()
    assert len(rows) > 100, f'只扫到 {len(rows)} 个路由，路径或解析可能有问题'


def test_mutating_endpoints_require_role():
    """所有变更类端点必须带角色/权限校验（有意豁免的除外）"""
    offenders = []
    for bp, route, methods, decs, line in _collect_routes():
        if not any(m in MUTATING for m in methods):
            continue
        # 豁免清单有两张：只要求登录的、以及有意完全公开的
        if (bp, route) in INTENTIONAL or (bp, route) in INTENTIONAL_PUBLIC:
            continue
        if _authz_level(decs) != 'role':
            offenders.append(f'{bp}:{line}  {"/".join(methods)} {route}')

    assert not offenders, (
        '以下变更类端点缺少角色/权限校验，存在越权风险：\n  '
        + '\n  '.join(offenders)
        + '\n如确属有意为之，请加入本文件 INTENTIONAL 并写明理由。'
    )


def test_no_unexpected_public_endpoints():
    """除登记在案的白名单外，不应存在完全无鉴权的端点"""
    offenders = []
    for bp, route, methods, decs, line in _collect_routes():
        if _authz_level(decs) != 'none':
            continue
        if (bp, route) in INTENTIONAL_PUBLIC or (bp, route) in INTENTIONAL:
            continue
        offenders.append(f'{bp}:{line}  {"/".join(methods)} {route}')

    assert not offenders, (
        '以下端点完全没有鉴权：\n  ' + '\n  '.join(offenders)
        + '\n如确属有意为之，请加入本文件 INTENTIONAL_PUBLIC 并写明理由。'
    )


@pytest.mark.parametrize('bp,route,reason', [
    (bp, route, reason) for (bp, route), reason in INTENTIONAL.items()
])
def test_intentional_exemptions_still_exist(bp, route, reason):
    """豁免清单不能腐化：被豁免的端点必须仍然真实存在"""
    routes = {(b, r) for b, r, _, _, _ in _collect_routes()}
    assert (bp, route) in routes, (
        f'豁免清单里的 {bp} {route} 已不存在，请从 INTENTIONAL 移除'
    )
