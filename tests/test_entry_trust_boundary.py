# -*- coding: utf-8 -*-
"""入口信任边界的结构守卫（round 340）。

为什么需要这个文件
------------------
本项目的安全能力一直在「靠某个入口记得挂」这条路线上出问题：

  · CSRF 只挂在 `run.py`，`create_app()` 不挂 → 不走 run.py 的入口静默失去防护；
  · `/api/health/status` 免认证却回全量明细（含异常原文）；
  · `jwt_required` 接受 `?token=`，令牌落进 access log / Referer；
  · `/api/system/client-errors` 免认证，字段不限长不限类型。

这四条的形状相同：**「谁能不带凭证进来」是隐式的**——
由「某个路由碰巧忘了加装饰器」决定，而不是由一份显式契约决定。

本文件把这件事变成结构断言：
  1. 任何 `/api` 路由，要么有认证装饰器，要么出现在**显式白名单**里；
  2. 凭证不得从 query 进入；
  3. `create_app()` 必须自己把 CSRF 装上（装配自洽子）。

与 `test_paired_path_guards.py`（round 338）同族：都是**扫源码断言**，
不是场景测试。新增一条路径时漏了约束，这里会红。
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
API_DIR = REPO / "展示层" / "api"
ROUTES_PY = REPO / "展示层" / "routes.py"
AUTH_PY = REPO / "用户层" / "auth.py"


#: 视为「已做认证」的装饰器名。
#:
#: 包括各模块里的别名（`_require_auth = jwt_required` 等）——
#: 别名是既有事实，本守卫不强迫改名，但**必须把它们列全**，
#: 否则会出现「明明有认证却判成公开」的假红。
AUTH_DECORATORS = {
    "jwt_required",
    "_require_auth",
    "_require_admin",
    "_require_engineer",
    "_require_operator",
    "_require_manager",
    "role_required",
    "permission_required",
}

#: 允许**免认证**的路由：(文件名, 函数名) → 理由。
#:
#: 加新条目时必须写清「为什么它可以不带凭证」。这份表就是契约本身。
PUBLIC_ROUTES = {
    ("api_auth.py", "login"): "登录本身不能要求凭证",
    ("api_auth.py", "register"): "首个用户注册；已有用户时内部校验 admin 令牌",
    ("api_auth.py", "refresh_token"): "用 refresh 令牌换 access，凭证在 body",
    ("api_auth.py", "force_change_password"): "首次登录改密；内部校验 token 与 username 一致",
    ("api_health.py", "get_health_status"): "负载均衡器探活；只回 version + global_status",
    ("api_metrics.py", "prometheus_metrics"): "Prometheus 抓取；可选 scrape token（默认放行）",
    ("api_system.py", "report_client_errors"): "前端错误上报，可能发生在登录失败时；字段已限长",
    ("api_i18n.py", "get_current_bundle"): "界面语言词条；登录页就要显示正确语言，此时无 token",
    ("api_i18n.py", "get_bundle"): "同上（指定语言）；词条只有保存/取消这类界面文字",
    ("routes.py", "get_csrf_token"): "发放 CSRF 令牌本身不是敏感操作",
    # flask-restx 文档桩：方法体是 `pass`，不执行业务逻辑、不返回真实数据。
    # ⚠️ 后续项：生产环境应整块不注册文档蓝图（目前无开关）。
    ("swagger.py", "LoginResource"): "OpenAPI 文档桩（方法体为 pass）",
    ("swagger.py", "DeviceListResource"): "OpenAPI 文档桩（方法体为 pass）",
    ("swagger.py", "AlarmListResource"): "OpenAPI 文档桩（方法体为 pass）",
    ("swagger.py", "HealthResource"): "OpenAPI 文档桩（方法体为 pass）",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _iter_routes(path: Path):
    """产出 (函数名, 装饰器块文本)。

    只做**行级**解析：本项目的 route 装饰器都是单行，
    多行 route 会解析不到函数名 —— 此时函数名为 None，由调用方报错。
    """
    lines = _read(path).splitlines()
    i = 0
    while i < len(lines):
        if re.match(r"\s*@\w+\.route\(", lines[i]):
            block = [lines[i]]
            j = i + 1
            while j < len(lines) and lines[j].lstrip().startswith("@"):
                block.append(lines[j])
                j += 1
            func = None
            for k in range(j, min(j + 6, len(lines))):
                m = re.match(r"\s*(?:async\s+)?def\s+(\w+)", lines[k])
                if m:
                    func = m.group(1)
                    break
                # flask-restx 风格：`@ns.route(...)` 挂在 class 上，
                # 真正响应的是 class 内的 post/get —— 用 class 名做标识，
                # 否则四个资源都会解析成 `get`/`post`，白名单无法区分。
                m = re.match(r"\s*class\s+(\w+)", lines[k])
                if m:
                    func = m.group(1)
                    break
            yield func, "\n".join(block)
            i = j
        else:
            i += 1


def _all_route_files():
    return sorted(API_DIR.glob("*.py")) + [ROUTES_PY]


def _is_api_route(path: Path, block: str) -> bool:
    """判定这条路由是否属于「API 面」。

    ⚠️ 不能只看装饰器块里有没有 `/api/`：Blueprint 的 `url_prefix='/api'`
    写在**文件顶部**，不在路由装饰器里 —— 那样会把 health / devices 等
    全部漏判成「不是 API 路由」，守卫直接形同虚设（假绿）。

    所以按文件名判定：`展示层/api/*.py` 下全是 API 路由；
    `routes.py` 里只挑显式写了 `/api/` 或 `/metrics` 的。
    页面路由（`/dashboard` 等）由 `routes.py::check_page_auth` 统一保护，
    不在本守卫职责内。
    """
    if path.parent.name == "api":
        return True
    if "route('/metrics'" in block or 'route("/metrics"' in block:
        return True
    return "/api/" in block


class TestPublicEndpointContract:
    """免认证端点必须**显式登记**，不能靠「忘了加装饰器」。"""

    def test_no_unauthenticated_api_route_outside_allowlist(self):
        leaks = []
        for path in _all_route_files():
            for func, block in _iter_routes(path):
                if not _is_api_route(path, block):
                    continue
                if any(d in block for d in AUTH_DECORATORS):
                    continue
                if func is None:
                    leaks.append(f"{path.name}: 无法解析装饰器块对应的函数名")
                    continue
                if (path.name, func) not in PUBLIC_ROUTES:
                    leaks.append(f"{path.name}::{func}")

        assert not leaks, (
            "以下路由既没有认证装饰器，也不在 PUBLIC_ROUTES 白名单里：\n  "
            + "\n  ".join(leaks)
            + "\n\n两种正确做法：\n"
              "  · 加 @jwt_required / @role_required(...)；或\n"
              "  · 在 tests/test_entry_trust_boundary.py 的 PUBLIC_ROUTES 里登记，"
              "并写明「为什么它可以不带凭证」。"
        )

    def test_allowlist_entries_are_still_public(self):
        """反向对照：白名单不能腐化。

        如果某个端点在白名单里、但源码里已经加了认证装饰器，
        说明白名单过期了 —— 过期的白名单会让守卫放行真正的裸奔端点。
        """
        existing = {}
        for path in _all_route_files():
            for func, block in _iter_routes(path):
                if func:
                    existing[(path.name, func)] = block

        stale = []
        for key in PUBLIC_ROUTES:
            block = existing.get(key)
            if block is None:
                stale.append(f"{key[0]}::{key[1]} 已不存在")
            elif any(d in block for d in AUTH_DECORATORS):
                stale.append(f"{key[0]}::{key[1]} 已加认证，请从白名单移除")
        assert not stale, "PUBLIC_ROUTES 已过期：\n  " + "\n  ".join(stale)


class TestCredentialEntryPoints:
    """凭证只能从 header / cookie 进入，不能进 URL。"""

    def test_jwt_required_does_not_accept_query_token(self):
        """`?token=` 会落进 nginx access log、浏览器历史、Referer。

        WebSocket 握手走 `展示层/websocket.py` 自己的 `request.args`，
        不经过 `jwt_required` —— 所以移除这条通道不影响 WS。
        """
        text = _read(AUTH_PY)
        # 去掉注释再判断：注释里出现 `request.args.get('token')` 会造成假绿
        body = re.sub(r"#[^\n]*", "", text)

        # 只检查 jwt_required 的装饰器体内
        idx = body.find("def jwt_required")
        assert idx != -1, "找不到 jwt_required，守卫失效"
        tail = body[idx:]
        end = tail.find("\ndef ", 10)
        decorator_body = tail if end == -1 else tail[:end]

        assert "request.args" not in decorator_body, (
            "jwt_required 仍然从查询参数取令牌 —— 令牌会进入 URL 与访问日志。\n"
            "凭证只应从 Authorization header 进入（页面导航另有 httpOnly cookie）。"
        )


class TestAppAssemblyIsSelfContained:
    """`create_app()` 返回的 app 必须**安全能力自洽**，不依赖入口脚本补挂。"""

    def test_create_app_initialises_csrf(self):
        text = re.sub(r"#[^\n]*", "", _read(ROUTES_PY))
        assert re.search(r"csrf\w*\.init_app\(", text), (
            "create_app() 里没有挂 CSRF —— 任何不走 run.py 的入口"
            "（WSGI / 嵌入式 / 测试夹具 / 新启动方式）都会静默失去 CSRF 防护，"
            "而 SecurityConfig.CSRF_ENABLED 仍是 true、日志也不会报。"
        )

    def test_csrf_init_is_idempotent(self):
        """`create_app` 与 `run.py` 都会调，必须幂等，否则重复注册 before_request。"""
        text = _read(REPO / "core" / "csrf_protection.py")
        assert "csrf_initialized" in text, (
            "CSRFProtection.init_app 缺少幂等标记：两处调用会注册两个 before_request，"
            "每个请求校验两遍。"
        )
