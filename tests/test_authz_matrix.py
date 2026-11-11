"""越权矩阵：角色 × 端点。

## 为什么要单独写这一份

追溯表里 SF-04「未授权/越权用户不得执行控制写入」一直是 `partial`，
说明写的是「有鉴权用例，但缺少系统化的越权矩阵（角色 × 端点）」。

零散用例只能证明「某个接口对某个角色拦住了」，证明不了「所有该拦的都拦住了」。
矩阵的价值是**穷举组合**：少拦一个格子就是一条越权路径，表格里一眼能看到。

## 安全设计（重要）

所有请求都指向**不存在的资源**或**无效 payload**：

  · `DELETE /api/devices/__authz_probe__` —— 设备不存在，即便鉴权被绕过也是 404
  · `POST /api/alarms/__authz_probe__/acknowledge` —— 报警不存在
  · `POST /api/devices` 空 body —— 参数校验先拦
  · `PUT /api/config` 空 body —— 同上

这样「鉴权失效」的最坏后果只是 404/400，不会真的删设备、改配置。

## 判定口径

  · 期望拒绝 → **必须 403**。返回 200/400/404 都算越权（说明根本没走到鉴权）；
    401 也不行（token 是有效的，只是权限不够，应是 403 而非「未认证」）。
  · 期望允许 → **不得 403**。具体 200/400/404 都无所谓
    （资源不存在/参数无效导致的失败是正常的，只要不是被权限拦下）。
"""

from __future__ import annotations

import pytest

from 用户层.auth import ROLES

# (方法, 路径, 期望被允许的角色集合, 说明)
MATRIX = [
    ('GET', '/api/devices', {'admin', 'engineer', 'operator', 'viewer'},
     '只读接口：所有登录角色都能看设备列表'),
    ('GET', '/api/auth/users', {'admin'},
     '用户管理：仅 admin'),
    ('POST', '/api/devices', {'admin', 'engineer'},
     '新增设备：engineer 及以上'),
    ('PUT', '/api/devices/__authz_probe__', {'admin', 'engineer'},
     '改设备：engineer 及以上'),
    ('DELETE', '/api/devices/__authz_probe__', {'admin', 'engineer'},
     '删设备：engineer 及以上'),
    ('POST', '/api/devices/__authz_probe__/start', {'admin', 'engineer'},
     '启设备：engineer 及以上'),
    ('POST', '/api/devices/__authz_probe__/stop', {'admin', 'engineer'},
     '停设备：engineer 及以上'),
    ('POST', '/api/devices/__authz_probe__/inject-fault', {'admin', 'engineer'},
     '故障注入：engineer 及以上'),
    ('POST', '/api/alarms/__authz_probe__/acknowledge',
     {'admin', 'engineer', 'operator'},
     '确认报警：operator 及以上（viewer 只能看）'),
    ('POST', '/api/export/device/__authz_probe__', {'admin', 'engineer'},
     '导出：需要 export_data 权限（operator/viewer 没有）'),
    ('PUT', '/api/config', {'admin', 'engineer'},
     '改配置：engineer 及以上（viewer 绝不能改配置）'),
    ('GET', '/api/config', {'admin', 'engineer', 'operator', 'viewer'},
     '读配置：所有登录角色（页面级另有限制，见 routes.py 的 _PAGE_ROLES）'),
]

ALL_ROLES = ['admin', 'engineer', 'operator', 'viewer']


@pytest.fixture()
def as_role(app):
    """把 app.auth_manager.verify_token 的返回值切到指定角色。

    conftest 的 auth_headers 固定是 admin；越权矩阵需要每个角色都试一遍，
    所以这里直接改 mock 的返回值（真实 token 解析不在本用例的验证范围内）。
    """
    def _switch(role: str) -> dict:
        assert role in ROLES, f'未知角色 {role}'
        app.auth_manager.verify_token.return_value = {
            'username': f'{role}_probe',
            'role': role,
            'display_name': ROLES[role]['name'],
            'permissions': list(ROLES[role]['permissions']),
        }
        return {'Authorization': f'Bearer authz-probe-{role}'}
    return _switch


@pytest.mark.parametrize('method,path,allowed,desc', MATRIX,
                         ids=[f'{m} {p}' for m, p, _, _ in MATRIX])
@pytest.mark.parametrize('role', ALL_ROLES)
def test_role_matrix(client, as_role, role, method, path, allowed, desc):
    headers = as_role(role)
    resp = client.open(path, method=method, headers=headers, json={})
    code = resp.status_code

    if role in allowed:
        # 允许的角色：只要不是被权限拦下就算通过。
        # 404（资源不存在）/ 400（参数无效）都是预期内的正常失败。
        assert code != 403, (
            f'{role} 应被允许访问 {method} {path}，却被 403 拒绝 —— {desc}\n'
            f'响应: {resp.get_data(as_text=True)[:300]}'
        )
    else:
        # 该拦的角色：必须是 403。
        # 200/400/404 都意味着请求**进了业务逻辑**，也就是越权；
        # 401 意味着 token 被判无效（角色信息丢了），同样不对。
        assert code == 403, (
            f'{role} 不应访问 {method} {path}，实际 {code} —— {desc}\n'
            f'200/400/404 = 越权（没走到鉴权）；401 = 角色信息丢失\n'
            f'响应: {resp.get_data(as_text=True)[:300]}'
        )


def test_matrix_covers_all_roles():
    """矩阵必须真的枚举了 4 个角色，不能因为漏写少测一半。"""
    covered = set()
    for _m, _p, allowed, _d in MATRIX:
        covered |= allowed
    assert covered == set(ALL_ROLES), f'矩阵未覆盖角色: {set(ALL_ROLES) - covered}'


#: viewer 只有 read —— 这些写接口对 viewer 必须全部 403。
#
# 单独再列一遍（不复用 MATRIX）是故意的：MATRIX 哪天被改了，
# 这条兜底清单仍然钉着「viewer 不许写」这条最硬的约束。
VIEWER_WRITE_ENDPOINTS = [
    ('POST', '/api/devices'),
    ('PUT', '/api/devices/__authz_probe__'),
    ('DELETE', '/api/devices/__authz_probe__'),
    ('POST', '/api/devices/__authz_probe__/start'),
    ('POST', '/api/devices/__authz_probe__/stop'),
    ('POST', '/api/devices/__authz_probe__/inject-fault'),
    ('POST', '/api/alarms/__authz_probe__/acknowledge'),
    ('POST', '/api/export/device/__authz_probe__'),
    ('PUT', '/api/config'),
    ('GET', '/api/auth/users'),
]


@pytest.mark.parametrize('method,path', VIEWER_WRITE_ENDPOINTS)
def test_viewer_blocked_on_every_write(client, as_role, method, path):
    resp = client.open(path, method=method, headers=as_role('viewer'), json={})
    assert resp.status_code == 403, (
        f'viewer 对 {method} {path} 返回 {resp.status_code}，应为 403'
    )
