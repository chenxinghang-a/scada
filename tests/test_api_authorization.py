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
    # Q3（round 393）：`POST /control/estop` 已从本豁免名单移出 —— 口径由
    # 「任何角色均可触发」收紧为 operator 及以上（见下方两条断言与决策理由）。
}

# 有意完全不需要鉴权的端点（(bp, route) -> 理由）
INTENTIONAL_PUBLIC = {
    ('auth_bp', '/auth/login'): '登录入口本身不能要求已登录',
    ('auth_bp', '/auth/refresh'): '刷新令牌，凭 refresh_token 自鉴权',
    ('health_bp', '/status'): '健康探针，供监控系统匿名拉取',
    ('metrics_bp', '/metrics'): 'Prometheus 指标抓取端点',
    ('system_bp', '/system/client-errors'): '前端错误上报，允许匿名但已截断内容',
    ('i18n_bp', '/'): '界面语言词条；登录页要显示正确语言，此时没有 token',
    ('i18n_bp', '/<lang>'): '同上（指定语言）',
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


# ---------------------------------------------------------------------------
# Q3（round 393）：急停权限由「任何角色」收紧为 operator 及以上
# ---------------------------------------------------------------------------
#
# 决策理由（方案 B）：车间大屏用的只读账号本不该有写权限 —— 让 viewer 能停
# 全厂既破坏最小权限，也让停机动作无法追责；而「长按 3 秒」这类人工延迟在
# 真实险情下反而有害。现场若需软停机，应给工位机分配 operator 账号。
#
# 注意：急停仍是**无条件放行**的（不参与幂等/去重/限流），本轮只收紧角色。


def test_estop_requires_operator_or_above():
    """静态断言：急停路由必须带角色校验，且角色集合含 operator、排除 viewer。"""
    matches = [row for row in _collect_routes()
               if row[0] == 'control_bp' and row[1] == '/control/estop']
    assert matches, '急停路由 /control/estop 不存在了，请检查路由改名'
    _bp, _route, methods, decs, _line = matches[0]
    assert 'POST' in methods
    joined = ' '.join(decs)
    assert _authz_level(decs) == 'role', (
        '急停端点退回成「仅要求登录」了 —— 只读账号又能停全厂'
    )
    m = re.search(r'role_required\((.*?)\)', joined)
    assert m, f'急停端点没有显式 role_required：{joined}'
    roles = set(re.findall(r"'([^']+)'", m.group(1)))
    assert {'admin', 'engineer', 'operator'} <= roles, f'急停角色集合过窄：{roles}'
    assert 'viewer' not in roles, '只读 viewer 不得触发急停'


def _as_role(app, role: str) -> dict:
    from 用户层.auth import ROLES
    app.auth_manager.verify_token.return_value = {
        'username': f'{role}_probe',
        'role': role,
        'display_name': ROLES[role]['name'],
        'permissions': list(ROLES[role]['permissions']),
    }
    return {'Authorization': f'Bearer estop-probe-{role}'}


def test_estop_rejects_viewer(client, app):
    """功能断言：viewer 请求急停必须 403（不能进到触发逻辑）。"""
    resp = client.post('/api/control/estop', json={'reason': 'probe'},
                       headers=_as_role(app, 'viewer'))
    assert resp.status_code == 403, (
        f'viewer 触发急停返回 {resp.status_code}，应为 403'
    )


def test_estop_allows_operator(client, app):
    """功能断言：operator 不得被权限层拦下。"""
    resp = client.post('/api/control/estop', json={'reason': 'probe'},
                       headers=_as_role(app, 'operator'))
    assert resp.status_code != 403, (
        'operator 被权限层拦下了，与 Q3 决策不符'
    )


def test_estop_audit_records_operator_and_source_ip(client, app, auth_headers):
    """Q3：每次急停都要留审计 —— 操作者 ID、来源 IP、时间戳（落库时生成）。"""
    from unittest.mock import MagicMock

    dc = MagicMock()
    dc.trigger_emergency_stop.return_value = {'success': True}
    app.device_control = dc

    resp = client.post('/api/control/estop', json={'reason': '冒烟测试'},
                       headers=auth_headers,
                       environ_base={'REMOTE_ADDR': '10.9.8.7'})
    assert resp.status_code == 200

    call = app.auth_manager.log_operation.call_args
    assert call is not None, '急停没有写审计日志'
    assert call.args[0] == 'testuser', '审计里没有操作者 ID'
    assert call.args[1] == 'emergency_stop'
    assert call.kwargs.get('ip_address') == '10.9.8.7', '审计里没有来源 IP'
