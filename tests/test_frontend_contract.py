"""前端 API 声明 ↔ 后端 Flask 路由 的**静态**契约测试。

审计要求（Phase 2）：
    - 「前端声明 API 与后端实现匹配率 100%」
    - 「关键端点意外 404 = 0 / 意外 500 = 0」
    - 集成测试必须能从"默认排除"变成**可独立运行**

历史问题：`src/api/*.ts` 里声明的端点与 `展示层/api/*.py` 的实际路由之间
**没有任何自动化校验**，只能靠人肉发现。此前 `performance.ts` 里三个接口写成了
`/api/performance/...`（axios 实例 baseURL 已含 `/api`），必然落到
`/api/api/performance/...` → 稳定 404，且没有任何测试会拦住它。

本模块的做法：**纯静态解析两侧源码**，不起 Flask、不连数据库、不依赖任何运行中
的服务，因此可以在任意环境独立运行（`pytest tests/test_frontend_contract.py`）。

产出：
    - `api-contract-report.json`（仓库根目录，见 `_report_location_rationale()`）
    - 正向断言：前端声明的每个端点都能在后端找到 (method, path) 路由，否则失败并列出
    - 反向断言：后端存在但前端从未调用的端点，**必须全部落进显式的前缀分类**
      （`UNUSED_BACKEND_PREFIX_RULES`）；新出现的未分类端点 → 失败。
      2026-10-08 round 193 从「只报告不失败」升级而来：一份没人看的清单不构成闸门，
      而实测确实有本该被前端调用的端点躺在那份清单里（`/api/system/client-errors`）。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 路径定位
# ---------------------------------------------------------------------------

BACKEND_ROOT = Path(__file__).resolve().parent.parent
BACKEND_API_DIR = BACKEND_ROOT / "展示层" / "api"
# `展示层/routes.py` 里挂的是 app 级路由（`@app.route`），不在任何 Blueprint 中。
# 前端的 `request.ts` 会请求 `/api/csrf-token`，该路由只存在于这里，
# 所以必须一并解析，否则会产生假的 missing_backend。
BACKEND_APP_ROUTES_FILE = BACKEND_ROOT / "展示层" / "routes.py"

REPORT_PATH = BACKEND_ROOT / "api-contract-report.json"

# axios 实例（`src/api/request.ts` 中的 default 导出）的 baseURL 已包含 `/api`。
# 前端源码里写的路径都是相对 baseURL 的，无需再写 `/api`。
API_BASE_PREFIX = "/api"

# 前端仓库位置：优先环境变量，其次常见相对位置，最后回落到本机的默认检出路径。
_FRONTEND_API_DIR_ENV = "SCADA_FRONTEND_API_DIR"
_FRONTEND_API_DIR_CANDIDATES = (
    BACKEND_ROOT.parent / "scada-app" / "src" / "api",
    BACKEND_ROOT.parent.parent / "scada-app" / "src" / "api",
    Path.home() / "scada-app" / "src" / "api",
)


def _resolve_frontend_api_dir() -> Path:
    env = os.environ.get(_FRONTEND_API_DIR_ENV)
    if env:
        return Path(env)
    for candidate in _FRONTEND_API_DIR_CANDIDATES:
        if candidate.is_dir():
            return candidate
    # 故意 fail 而不是 skip：静默跳过会让"匹配率 100%"变成一个永远为真的假信号，
    # 正是本测试要消灭的问题。找不到前端源码就应该是一次显式的红灯。
    pytest.fail(
        "未找到前端 api 目录，无法做前后端契约比对。已尝试：\n"
        + "\n".join(f"  - {p}" for p in _FRONTEND_API_DIR_CANDIDATES)
        + f"\n可通过环境变量 {_FRONTEND_API_DIR_ENV} 指定 scada-app/src/api 的绝对路径。"
    )


#: 前端源码根（`_resolve_frontend_api_dir()` 的上一级）。
def _resolve_frontend_src_dir() -> Path:
    return _resolve_frontend_api_dir().parent


def _iter_frontend_source_files() -> list[Path]:
    """前端**全部**源码文件（`src/**/*.{ts,vue}`），而不只是 `src/api/*.ts`。

    为什么必须扫全（2026-10-08，round 193）
    ------------------------------------
    第一版只扫 `src/api/*.ts`，于是**在 `src/api/` 之外发起的调用对报告完全不可见**，
    后果是「后端存在但前端从未调用」这份清单**混入假阳性**。实测漏掉两处：

    * ``src/composables/useErrorLogger.ts`` —— 原生 ``fetch`` 调
      ``/system/client-errors``（用 fetch 是因为要 ``keepalive``，**有意为之**）；
    * ``src/views/Devices.vue`` —— ``api.post('/devices/presets/add')``
      与 ``api.post('/devices/presets/add-all')``。

    报告因此把 ``/api/system/client-errors`` 报成「前端从未调用」——
    而它**明明在用**。这份清单是要当依据用的，**口径不收紧就不能用**。

    ⚠️ 与 round 189 的教训同源：那次的 `path.endswith(...)` 没归一化反斜杠，
    导致每个文件都算自己的引用者，报出「零引用 0 个」的假绿。
    扫描类排查的第一件事永远是**确认口径覆盖了全部应当覆盖的对象**。
    """
    src = _resolve_frontend_src_dir()
    files: list[Path] = []
    for pattern in ("*.ts", "*.vue"):
        files.extend(src.rglob(pattern))
    # 排除类型声明与测试替身：它们不是「发起调用」的代码
    return sorted(
        f
        for f in files
        if f.name != "env.d.ts"
        and "node_modules" not in f.parts
        and not f.name.endswith(".d.ts")
    )


def _report_location_rationale() -> str:
    """报告落点说明（审计要求"二选一并在报告说明"）。

    选择**仓库根目录** `industrial_scada/api-contract-report.json`，而不是
    `evidence/<version>/`，理由：
      1. 该文件是"每次跑契约测试都会重算"的活产物，版本号（VERSION 文件）
         变化时应始终反映最新一次比对结果，固定在语义化版本目录下会很快过期；
      2. 报告内已记录生成时间与两侧仓库路径，可独立复现；
      3. 需要归档进证据包时，由 `tools/collect_evidence.py` 按版本号复制即可。
    """
    return (
        "仓库根目录 industrial_scada/api-contract-report.json（活产物，每次运行重算；"
        "归档证据包时由 tools/collect_evidence.py 按 VERSION 复制到 evidence/<version>/）"
    )


# ---------------------------------------------------------------------------
# 例外清单（每条都必须写明理由）
# ---------------------------------------------------------------------------

# 后端：整体排除的文件
BACKEND_EXCLUDED_FILES: dict[str, str] = {
    # flask_restx 生成的 OpenAPI 文档蓝图。其中的 Resource 方法体全部是 `pass`，
    # 只用于渲染 /api/v1/docs 文档页，返回空响应，不是真实业务端点；
    # 其 namespace path（/api/auth、/api/devices…）与真实 Blueprint 路由重复，
    # 纳入比对会对同一条业务路由产生重复计数。
    "swagger.py": "flask_restx OpenAPI 文档蓝图，Resource 方法体均为 pass，仅供 /api/v1/docs 渲染",
}

# 后端：不进入"未使用端点"报告的路径
BACKEND_UNUSED_REPORT_EXCLUDE: dict[str, str] = {
    "/metrics": "Prometheus 抓取端点，由 Prometheus Server 消费，本就不应被浏览器前端调用",
}
# 非 `/api/` 前缀的路由是服务给浏览器的 HTML 页面路由（/dashboard、/login…），
# 不属于 JSON API 契约范围，统一不计入"未使用端点"。
BACKEND_UNUSED_REPORT_PATH_PREFIX = "/api/"

# 前端：不参与契约比对的静态资源路径
FRONTEND_EXCLUDED_PREFIXES: dict[str, str] = {
    "/static/": "由 Flask/nginx 直接托管的静态文件，不是 API",
    "/uploads/": "上传文件回读路径，非 API 端点",
    "/favicon.ico": "浏览器自动请求的站点图标，非 API",
}

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

STATUS_MATCH = "match"
STATUS_MISSING_BACKEND = "missing_backend"
STATUS_PREFIXED_MISMATCH = "prefixed_mismatch"
STATUS_PARAM_MISMATCH = "param_mismatch"

#: `request.ts` 中对 axios 实例（baseURL 含 /api）的调用对象名
_AXIOS_INSTANCE_NAMES = ("api", "request")
#: 直接使用 axios 原型的调用（未经过实例，不套 baseURL）
_AXIOS_RAW_NAMES = ("axios",)


@dataclass(frozen=True)
class FrontendEndpoint:
    """前端声明的一个端点。"""

    method: str
    #: 后端实际会收到的绝对路径（已把 baseURL 的 /api 计入）
    effective_path: str
    #: 源码里字面写的路径（模板变量已转成 :param）
    source_path: str
    source_file: str
    line: int
    #: 该调用是否经过带 baseURL 的 axios 实例
    via_instance: bool

    def key(self) -> tuple[str, str]:
        return (self.method, self.effective_path)


@dataclass(frozen=True)
class BackendRoute:
    """后端注册的一个路由（method 粒度）。"""

    method: str
    path: str
    source_file: str
    line: int

    def key(self) -> tuple[str, str]:
        return (self.method, self.path)


@dataclass
class Contract:
    frontend: list[FrontendEndpoint] = field(default_factory=list)
    backend: list[BackendRoute] = field(default_factory=list)
    frontend_excluded: list[dict] = field(default_factory=list)
    backend_excluded_files: list[dict] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 归一化工具
# ---------------------------------------------------------------------------

_TEMPLATE_VAR_RE = re.compile(r"\$\{([^}]*)\}")
_HTTP_ORIGIN_RE = re.compile(r"^https?://[^/]+", re.I)
_ENCODE_URI_RE = re.compile(r"encodeURIComponent\(\s*([^)]*?)\s*\)")


def _param_name(expr: str) -> str:
    """从 `${...}` 表达式里取一个稳定的参数名，用于生成 `:name` 占位。"""
    inner = _ENCODE_URI_RE.sub(r"\1", expr.strip()) or "param"
    inner = re.sub(r"[^0-9A-Za-z_]", "", inner) or "param"
    return inner


def _templatize(path: str) -> str:
    """把模板字面量里的 `${...}` 统一转成 Flask 风格的 `:param` 占位。"""
    return _TEMPLATE_VAR_RE.sub(lambda m: ":" + _param_name(m.group(1)), path)


def _normalize_path(path: str) -> str:
    """把任意写法的路径规整成 `/a/b/:c` 形式。

    - 去掉协议+域名前缀
    - `${...}` → `:param`
    - 去掉 query string（`?limit=10`、`?a=1&b=2` 都不影响路由匹配）
    - 折叠重复斜杠、补齐前导斜杠、去掉尾部斜杠
    """
    path = path.strip()
    path = _HTTP_ORIGIN_RE.sub("", path)
    path = _templatize(path)
    path = path.split("?", 1)[0].split("#", 1)[0]
    path = re.sub(r"/{2,}", "/", path)
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


def _is_param_segment(seg: str) -> bool:
    """判断一段路径是否为路径参数。

    前端模板变量归一后是 `:id`（Express/Vue 风格），后端 Flask 是 `<device_id>`
    （可能带 converter，如 `<int:page>`）。两种写法都算参数位。
    """
    if seg.startswith(":"):
        return True
    return seg.startswith("<") and seg.endswith(">")


def _structural(path: str) -> tuple[str, ...]:
    """结构签名：把路径参数位统一成 `*`，静态段小写，用于形状比较。

    这是"前端 `:id` ↔ 后端 `<device_id>`"能对齐的关键 —— 参数**名字**不同
    不影响契约，只有参数**位置**不同才是真不一致。
    """
    return tuple(
        "*" if _is_param_segment(seg) else seg.lower()
        for seg in path.split("/")
        if seg
    )


def _join_prefix(prefix: str, path: str) -> str:
    """拼接 Blueprint 的 url_prefix 与路由 path，行为对齐 Flask。"""
    prefix = (prefix or "").rstrip("/")
    if not path:
        return prefix or "/"
    if not path.startswith("/"):
        path = "/" + path
    joined = prefix + path
    joined = re.sub(r"/{2,}", "/", joined)
    return joined or "/"


# ---------------------------------------------------------------------------
# 前端解析
# ---------------------------------------------------------------------------

#: 匹配 `api.get('/x')` / `request.post(`/y/${z}`, ...)` / `axios.get(`...`)`
_FRONTEND_CALL_RE = re.compile(
    r"\b(?P<obj>[A-Za-z_$][\w$]*)\s*\.\s*"
    r"(?P<method>get|post|put|delete|patch|head|options)\s*\(\s*"
    r"(?P<quote>['\"`])(?P<path>.*?)(?P=quote)",
    re.S,
)

#: 匹配原生 `fetch(<url>, { … })`。
#:
#: 为什么必须认它（2026-10-08，round 193）
#: ------------------------------------
#: `src/composables/useErrorLogger.ts` 用 **fetch 而不是 axios** 调
#: `/system/client-errors` —— 因为需要 `keepalive: true`（页面卸载时也要发得出去），
#: axios 没有这个能力，所以那是**有意为之**，不该改。
#: 但第一版解析器只认 axios 调用，于是这条调用对报告**不可见**，
#: 后端对应端点被误报成「前端从未调用」。
#: 这正是「**口径不收紧，结果就不能用**」—— 一份带假阳性的清单没法拿来当依据。
_FRONTEND_FETCH_RE = re.compile(
    r"\bfetch\s*\(\s*"
    r"(?P<quote>['\"`])(?P<url>.*?)(?P=quote)"
    r"\s*,\s*\{(?P<opts>[\s\S]{0,400}?)\}\s*\)",
    re.S,
)

#: `fetch(..., { method: 'POST' })` 里的 method；缺省按 HTTP 语义取 GET。
_FETCH_METHOD_RE = re.compile(r"method\s*:\s*['\"](\w+)['\"]", re.I)


def _resolve_fetch_url(raw_url: str) -> str | None:
    """把 ``fetch()`` 的 URL 解析成**相对 /api 的路径**；解析不出来返回 ``None``。

    只认两种**能确证**的形状（实测于 `useErrorLogger.ts`）：

    1. ``${base}/system/client-errors`` —— ``base`` 在 dev 是 ``/api``、
       prod 是 ``http://host:port/api``（两种同构），故 ``${…}`` 之后的部分
       就是 API 相对路径；
    2. 字面量里直接带 ``/api/``。

    **解析不出来就返回 None 并跳过，绝不猜** —— 猜错会往报告里灌假数据，
    而这份报告的用途正是「当依据」。
    """
    m = re.match(r"^\$\{[^}]*\}(/.*)$", raw_url)
    if m:
        return m.group(1)
    if raw_url.startswith(API_BASE_PREFIX + "/"):
        return raw_url[len(API_BASE_PREFIX):]
    return None


def _fetch_method(opts: str) -> str:
    m = _FETCH_METHOD_RE.search(opts)
    return m.group(1).upper() if m else "GET"


def _parse_frontend_file(py_path: Path) -> list[FrontendEndpoint]:
    text = py_path.read_text(encoding="utf-8")
    out: list[FrontendEndpoint] = []
    for m in _FRONTEND_CALL_RE.finditer(text):
        obj = m.group("obj")
        raw_path = m.group("path")
        if "${" in raw_path and "}" not in raw_path:
            continue  # 跨行的模板字面量，无法可靠解析
        is_instance = obj in _AXIOS_INSTANCE_NAMES
        is_raw_axios = obj in _AXIOS_RAW_NAMES
        if not (is_instance or is_raw_axios):
            continue
        if obj == "axios":
            # 直接走 axios 原型时不套 baseURL，前端必须写全路径：
            # 常见写法是 `${getApiBaseURL()}/csrf-token`，其中 getApiBaseURL()
            # 在 dev 返回 '/api'、在 prod 返回 'http://host:port/api'，两者同构。
            raw_path = raw_path.replace("${getApiBaseURL()}", API_BASE_PREFIX)
        normalized = _normalize_path(raw_path)
        if is_instance:
            effective = _join_prefix(API_BASE_PREFIX, normalized)
        else:
            effective = normalized
        out.append(
            FrontendEndpoint(
                method=m.group("method").upper(),
                effective_path=effective,
                source_path=normalized,
                source_file=py_path.name,
                line=text.count("\n", 0, m.start()) + 1,
                via_instance=is_instance,
            )
        )

    # --- 原生 fetch() ---
    for m in _FRONTEND_FETCH_RE.finditer(text):
        resolved = _resolve_fetch_url(m.group("url"))
        if resolved is None:
            continue  # 解析不出来就跳过，不猜
        normalized = _normalize_path(resolved)
        out.append(
            FrontendEndpoint(
                method=_fetch_method(m.group("opts")),
                effective_path=_join_prefix(API_BASE_PREFIX, normalized),
                source_path=normalized,
                source_file=py_path.name,
                line=text.count("\n", 0, m.start()) + 1,
                # fetch 不套 baseURL，URL 里已经写全了；置 False 以免被
                # 误判成 prefixed_mismatch（那条判定专治「实例 + 又写 /api」）
                via_instance=False,
            )
        )
    return out


# ---------------------------------------------------------------------------
# 后端解析
# ---------------------------------------------------------------------------

_BLUEPRINT_ASSIGN_RE = re.compile(r"(\w+)\s*=\s*Blueprint\s*\(")
_ROUTE_DECORATOR_RE = re.compile(
    r"@(?P<obj>\w+)\.route\(\s*(?P<q>['\"])(?P<path>.*?)(?P=q)(?P<kw>[^)]*)\)",
    re.S,
)
_METHODS_KW_RE = re.compile(r"methods\s*=\s*\[([^\]]*)\]", re.S)
_URL_PREFIX_KW_RE = re.compile(r"url_prefix\s*=\s*(['\"])(?P<prefix>.*?)\1", re.S)


def _scan_blueprint_prefixes(text: str) -> dict[str, str]:
    """取出 `xxx_bp = Blueprint(..., url_prefix='...')` 的变量名 → 前缀映射。

    Blueprint 的参数里可能嵌套函数调用（如 `__name__`），所以用括号配对
    扫描而不是正则懒惰匹配到第一个 `)`。
    """
    prefixes: dict[str, str] = {}
    for m in _BLUEPRINT_ASSIGN_RE.finditer(text):
        start = m.end()  # 指向第一个 `(` 之后
        depth = 1
        i = start
        while i < len(text) and depth:
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            i += 1
        args = text[start : i - 1]
        pm = _URL_PREFIX_KW_RE.search(args)
        prefixes[m.group(1)] = pm.group("prefix") if pm else ""
    return prefixes


def _parse_backend_file(py_path: Path) -> list[BackendRoute]:
    text = py_path.read_text(encoding="utf-8")
    prefixes = _scan_blueprint_prefixes(text)
    # app 级路由（`@app.route`）不带前缀
    prefixes.setdefault("app", "")

    routes: list[BackendRoute] = []
    for m in _ROUTE_DECORATOR_RE.finditer(text):
        obj = m.group("obj")
        if obj not in prefixes:
            continue  # 不是本文件里定义的 Blueprint/app（例如 flask_restx 的 namespace）
        prefix = prefixes[obj]
        path = _join_prefix(prefix, m.group("path"))
        kw = m.group("kw") or ""
        mm = _METHODS_KW_RE.search(kw)
        if mm:
            methods = [
                tok.strip().strip("'\"").upper()
                for tok in mm.group(1).split(",")
                if tok.strip()
            ]
        else:
            methods = ["GET"]  # Flask 默认 GET
        line = text.count("\n", 0, m.start()) + 1
        for method in methods:
            routes.append(
                BackendRoute(
                    method=method,
                    path=path,
                    source_file=py_path.name,
                    line=line,
                )
            )
    return routes


# ---------------------------------------------------------------------------
# 契约装载
# ---------------------------------------------------------------------------


def _iter_backend_source_files() -> list[Path]:
    files = sorted(BACKEND_API_DIR.glob("*.py"))
    if BACKEND_APP_ROUTES_FILE.is_file():
        files.append(BACKEND_APP_ROUTES_FILE)
    return files


def classify_endpoints(
    frontend: list[FrontendEndpoint], backend: list[BackendRoute]
) -> list[dict]:
    """把前端声明逐条对照后端路由分类。

    状态取值：
      - ``match``              方法+路径结构都能对上（参数名可以不同）
      - ``prefixed_mismatch``  前端多写了一层 baseURL 已含的 `/api` → 必然 404
      - ``param_mismatch``     段数相同但参数位与静态段错位 → 命中错误 handler 或 404
      - ``missing_backend``    后端根本没有对应方法/路径 → 404（或 405）

    独立成函数是为了让下面的合成用例能直接调用它，**证明这些检测器不是摆设**
    （否则"全部 match"也可能只是分类逻辑写死的）。
    """
    records: list[dict] = []
    by_key = {(r.method, r.path): r for r in backend}
    #: (method, 结构签名) → 后端路由，用于"参数名不同但结构一致"的对齐
    structural_index: dict[tuple[str, tuple[str, ...]], list[BackendRoute]] = {}
    #: (method, 段数) → 后端路由，用于找"近失配"：段数相同、静态段可对齐
    same_len_index: dict[tuple[str, int], list[BackendRoute]] = {}
    #: 段数 → 所有方法的后端路由，用于区分"参数错位"与"方法不匹配"
    len_index: dict[int, list[BackendRoute]] = {}
    for r in backend:
        structural_index.setdefault((r.method, _structural(r.path)), []).append(r)
        same_len_index.setdefault((r.method, len(_structural(r.path))), []).append(r)
        len_index.setdefault(len(_structural(r.path)), []).append(r)

    def _compatible(other: tuple[str, ...]) -> bool:
        """段数相同，且每个位置的静态段互不冲突（任一为 `*` 即视为可对齐）。"""
        return len(other) == len(struct) and all(
            a == "*" or b == "*" or a == b for a, b in zip(other, struct)
        )

    for ep in frontend:
        struct = _structural(ep.effective_path)
        record = {
            "method": ep.method,
            "frontend_path": ep.effective_path,
            "source_path": ep.source_path,
            "backend_path": None,
            "status": None,
            "detail": "",
            "source_file": ep.source_file,
            "source_line": ep.line,
        }

        # 1) 字面完全一致
        exact = by_key.get(ep.key())
        if exact is not None:
            record["status"] = STATUS_MATCH
            record["backend_path"] = exact.path
            record["detail"] = f"精确匹配 {exact.source_file}:{exact.line}"
            records.append(record)
            continue

        # 2) 前端多写了一层 baseURL 已有前缀 → 请求会落到 /api/api/... → 必然 404
        if ep.via_instance and ep.source_path.startswith(API_BASE_PREFIX):
            raw_struct = _structural(ep.source_path)
            hit = structural_index.get((ep.method, raw_struct))
            if hit:
                record["status"] = STATUS_PREFIXED_MISMATCH
                record["backend_path"] = hit[0].path
                record["detail"] = (
                    f"前端源码路径 '{ep.source_path}' 已含 '{API_BASE_PREFIX}' 前缀，"
                    f"而 axios 实例 baseURL 也含 '{API_BASE_PREFIX}'，"
                    f"实际请求 {ep.effective_path} → 必然 404；"
                    f"后端真实路由为 {hit[0].path}"
                )
                records.append(record)
                continue

        # 3) 结构一致（参数名可不同，如前端 `:id` ↔ 后端 `<device_id>`）→ 契约成立
        same_method = structural_index.get((ep.method, struct))
        if same_method:
            record["status"] = STATUS_MATCH
            record["backend_path"] = same_method[0].path
            record["detail"] = (
                f"结构匹配（参数名归一）{same_method[0].source_file}:{same_method[0].line}"
            )
            records.append(record)
            continue

        # 4) 段数一致、静态段能对齐，但参数位与静态段错位
        #    典型：前端 /devices/protocols 撞上后端 /devices/<device_id>，
        #    路由能匹配但会命中错误的 handler，或反之直接 404。
        loose = [
            r
            for r in same_len_index.get((ep.method, len(struct)), [])
            if _compatible(_structural(r.path))
        ]
        if loose:
            record["status"] = STATUS_PARAM_MISMATCH
            record["backend_path"] = loose[0].path
            record["detail"] = (
                f"段数相同但参数位与静态段错位：{loose[0].path}"
                f"（{loose[0].source_file}:{loose[0].line}）"
            )
            records.append(record)
            continue

        # 5) 路径形状存在但 HTTP 方法不匹配（后端会返回 405 而非 404）
        candidates = [
            r
            for r in len_index.get(len(struct), [])
            if r.method != ep.method and _compatible(_structural(r.path))
        ]
        if candidates:
            record["status"] = STATUS_MISSING_BACKEND
            record["backend_path"] = candidates[0].path
            record["detail"] = (
                "路径存在但 HTTP 方法不匹配（后端仅注册："
                + ", ".join(sorted({c.method for c in candidates}))
                + "）"
            )
        else:
            record["status"] = STATUS_MISSING_BACKEND
            record["detail"] = f"后端无任何结构匹配的路由（段数 {len(struct)}）"
        records.append(record)

    return records


@lru_cache(maxsize=1)
def load_contract() -> Contract:
    contract = Contract()

    # --- 前端 ---
    # 扫**整个 src/**，不只是 src/api/（见 _iter_frontend_source_files 的说明）
    for ts_file in _iter_frontend_source_files():
        for ep in _parse_frontend_file(ts_file):
            if any(
                ep.effective_path.startswith(p) or ep.effective_path == p.rstrip("/")
                for p in FRONTEND_EXCLUDED_PREFIXES
            ):
                contract.frontend_excluded.append(
                    {
                        "method": ep.method,
                        "frontend_path": ep.effective_path,
                        "source_file": ep.source_file,
                        "line": ep.line,
                        "reason": next(
                            r
                            for p, r in FRONTEND_EXCLUDED_PREFIXES.items()
                            if ep.effective_path.startswith(p)
                            or ep.effective_path == p.rstrip("/")
                        ),
                    }
                )
                continue
            contract.frontend.append(ep)

    # --- 后端 ---
    for py_file in _iter_backend_source_files():
        if py_file.name in BACKEND_EXCLUDED_FILES:
            contract.backend_excluded_files.append(
                {
                    "file": str(py_file.relative_to(BACKEND_ROOT)).replace("\\", "/"),
                    "reason": BACKEND_EXCLUDED_FILES[py_file.name],
                }
            )
            continue
        contract.backend.extend(_parse_backend_file(py_file))

    # --- 比对 ---
    contract.records = classify_endpoints(contract.frontend, contract.backend)

    return contract


def unused_backend_routes(contract: Contract) -> list[dict]:
    """后端存在、但前端从未声明的端点（只报告，不断言）。

    比对用**结构签名**而非字面路径：前端写 `/alarm-rules/:id`、后端写
    `/alarm-rules/<rule_id>`，是同一个端点，不能因为参数名不同就被误判成"未使用"。
    """
    used = {(ep.method, _structural(ep.effective_path)) for ep in contract.frontend}
    out = []
    for r in contract.backend:
        if (r.method, _structural(r.path)) in used:
            continue
        if not r.path.startswith(BACKEND_UNUSED_REPORT_PATH_PREFIX):
            continue  # HTML 页面路由，不属于 JSON API 契约
        if r.path in BACKEND_UNUSED_REPORT_EXCLUDE:
            continue
        if r.method in ("HEAD", "OPTIONS"):
            continue  # Flask 自动补齐的辅助方法
        out.append(
            {
                "method": r.method,
                "backend_path": r.path,
                "source_file": r.source_file,
                "source_line": r.line,
            }
        )
    return sorted(out, key=lambda x: (x["backend_path"], x["method"]))


# ---------------------------------------------------------------------------
# 报告生成
# ---------------------------------------------------------------------------


def build_report(contract: Contract) -> dict:
    counts = {
        STATUS_MATCH: 0,
        STATUS_MISSING_BACKEND: 0,
        STATUS_PREFIXED_MISMATCH: 0,
        STATUS_PARAM_MISMATCH: 0,
    }
    for rec in contract.records:
        counts[rec["status"]] += 1

    total = len(contract.records)
    unmatched = total - counts[STATUS_MATCH]
    unused = unused_backend_routes(contract)

    return {
        "report_schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generated_by": "tests/test_frontend_contract.py",
        "report_location": str(REPORT_PATH.relative_to(BACKEND_ROOT)).replace("\\", "/"),
        "report_location_rationale": _report_location_rationale(),
        "method": (
            "纯静态源码解析：解析前端 HTTP 调用（axios 实例 / axios 原型 / **原生 fetch**）"
            "的 (method, path) 与后端 @bp.route + url_prefix 解析出的真实路由后归一化比对；"
            "不启动 Flask、不连数据库、不依赖任何运行中的服务。"
            "前端侧扫描范围是**整个 src/**（不只是 src/api/）—— 口径太窄会让"
            "「后端存在但前端从未调用」清单混入假阳性（2026-10-08 round 193 实测）。"
        ),
        "normalization": {
            "frontend_baseurl": (
                "src/api/request.ts 中 axios 实例 baseURL 已含 /api，"
                "故源码内相对路径自动前置 /api；axios 原型直调（axios.get）不套 baseURL"
            ),
            "template_vars": "`${expr}` → `:expr`（模板变量归一为路径参数）",
            "query_string": "已剔除 `?a=1&b=2`，query 不影响路由匹配",
            "trailing_slash": "已统一去掉尾部斜杠（根路径除外）",
            "backend_params": "Flask `<device_id>` 与前端 `:id` 归一为同一参数位",
            "fetch_url": (
                "原生 fetch 的 URL 只认两种能确证的形状：`${base}/x`（base 在 dev 是 /api、"
                "prod 是 http://host:port/api，两者同构）与字面量里直接带 /api/；"
                "**解析不出来就跳过，不猜**"
            ),
        },
        "sources": {
            "backend_repo": str(BACKEND_ROOT),
            "backend_api_dir": str(BACKEND_API_DIR.relative_to(BACKEND_ROOT)).replace("\\", "/"),
            "backend_app_routes": str(BACKEND_APP_ROUTES_FILE.relative_to(BACKEND_ROOT)).replace("\\", "/"),
            "frontend_api_dir": str(_resolve_frontend_api_dir()),
            "frontend_src_dir": str(_resolve_frontend_src_dir()),
            "frontend_files": sorted(
                {ep.source_file for ep in contract.frontend}
            ),
            "frontend_scanned_files": len(_iter_frontend_source_files()),
        },
        "summary": {
            "frontend_endpoints": total,
            "backend_routes": len(contract.backend),
            "matched": counts[STATUS_MATCH],
            "missing_backend": counts[STATUS_MISSING_BACKEND],
            "prefixed_mismatch": counts[STATUS_PREFIXED_MISMATCH],
            "param_mismatch": counts[STATUS_PARAM_MISMATCH],
            "unmatched": unmatched,
            "match_rate": f"{(counts[STATUS_MATCH] / total * 100) if total else 0:.2f}%",
            "unused_backend_endpoints": len(unused),
            "excluded_frontend_endpoints": len(contract.frontend_excluded),
            "result": "pass" if unmatched == 0 else "fail",
        },
        "endpoints": sorted(
            contract.records, key=lambda r: (r["status"] != STATUS_MATCH, r["frontend_path"], r["method"])
        ),
        "unused_backend_endpoints": unused,
        "exclusions": {
            "frontend": [
                {"prefix": p, "reason": r} for p, r in FRONTEND_EXCLUDED_PREFIXES.items()
            ],
            "backend_files": contract.backend_excluded_files,
            "backend_unused_report": [
                {"path": p, "reason": r} for p, r in BACKEND_UNUSED_REPORT_EXCLUDE.items()
            ]
            + [
                {
                    "path": "非 " + BACKEND_UNUSED_REPORT_PATH_PREFIX + " 前缀",
                    "reason": "服务给浏览器的 HTML 页面路由（/dashboard、/login…），不属于 JSON API 契约",
                }
            ],
        },
        "notes": [
            "unused_backend_endpoints 按**前缀分类**（UNUSED_BACKEND_PREFIX_RULES）"
            "并受双向守卫约束：未分类的新端点会让测试失败，分类腐烂（无命中）也会失败。"
            "后端路由先于前端存在（resilience / ops 运维接口）是合理设计，"
            "但**必须是显式声明的**，不能靠一份没人看的清单兜着。",
            "本报告由测试运行自动覆盖重写，不手工编辑。",
        ],
    }


@pytest.fixture(scope="module", autouse=True)
def contract_report():
    """模块级：解析两侧源码并把最新结果写入报告 json。

    放在 autouse fixture 里而不是某个用例内，保证只要本模块被收集到，
    报告就一定是最新的（也避免"只跑单个用例时报告不更新"的坑）。
    """
    contract = load_contract()
    report = build_report(contract)
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


# ---------------------------------------------------------------------------
# 解析器自检（防止正则静默失效导致"假绿"）
# ---------------------------------------------------------------------------


def test_frontend_backend_sources_are_reachable():
    assert BACKEND_API_DIR.is_dir(), f"后端 API 目录不存在：{BACKEND_API_DIR}"
    frontend_dir = _resolve_frontend_api_dir()
    assert frontend_dir.is_dir(), f"前端 api 目录不存在：{frontend_dir}"


def test_parsers_found_a_plausible_number_of_endpoints(contract_report):
    """回归护栏：正则一旦失效会解析出 0 条，此时"全部匹配"是假绿。

    这里用下界断言把这种情况变成红灯。
    """
    summary = contract_report["summary"]
    assert summary["frontend_endpoints"] >= 100, (
        f"前端仅解析出 {summary['frontend_endpoints']} 个端点，疑似解析器失效"
    )
    assert summary["backend_routes"] >= 100, (
        f"后端仅解析出 {summary['backend_routes']} 条路由，疑似解析器失效"
    )


def test_report_json_is_written(contract_report):
    assert REPORT_PATH.is_file()
    on_disk = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    assert on_disk["summary"] == contract_report["summary"]
    assert on_disk["report_location"] == "api-contract-report.json"


# ---------------------------------------------------------------------------
# 检测器有效性自证（防止"因为判据写错所以全绿"）
# ---------------------------------------------------------------------------

_SYNTHETIC_FRONTEND_TS = """
import api from './request'

export const demo = {
  exact() { return api.get('/alarms') },
  paramOk() { return api.get(`/alarms/${id}/acknowledge`) },
  duplicatedPrefix() { return api.get('/api/performance/metrics/realtime') },
  staticCollidesWithParam() { return api.post('/devices/protocols') },
  absent() { return api.delete('/totally/absent/endpoint') },
  queryIgnored() { return api.get(`/alarms?limit=${limit}`) },
}
"""

_SYNTHETIC_BACKEND_PY = """
from flask import Blueprint

demo_bp = Blueprint('demo', __name__, url_prefix='/api')


@demo_bp.route('/alarms', methods=['GET'])
def alarms():
    return {}


@demo_bp.route('/alarms/<alarm_id>/acknowledge', methods=['GET'])
def ack():
    return {}


@demo_bp.route('/performance/metrics/realtime', methods=['GET'])
def perf():
    return {}


@demo_bp.route('/devices/<device_id>', methods=['POST'])
def device_detail():
    return {}
"""


def test_classifier_detects_every_status(tmp_path):
    """用合成源码验证 4 种分类**都会真实触发**。

    没有这条用例，"全绿"可能只是分类逻辑本身写错了（能力上无法失败）。
    这里对每种状态都构造一个样本，证明检测器有牙齿。
    """
    ts_file = tmp_path / "synthetic.ts"
    ts_file.write_text(_SYNTHETIC_FRONTEND_TS, encoding="utf-8")
    py_file = tmp_path / "synthetic_backend.py"
    py_file.write_text(_SYNTHETIC_BACKEND_PY, encoding="utf-8")

    frontend = _parse_frontend_file(ts_file)
    backend = _parse_backend_file(py_file)
    records = classify_endpoints(frontend, backend)
    by_source = {r["source_path"]: r for r in records}

    assert len(frontend) == 6, "合成前端应解析出 6 个端点"
    assert len(backend) == 4, "合成后端应解析出 4 条路由"

    # 字面一致 → match
    assert by_source["/alarms"]["status"] == STATUS_MATCH

    # 参数名不同（前端 :id ↔ 后端 <alarm_id>）但结构一致 → match
    assert by_source["/alarms/:id/acknowledge"]["status"] == STATUS_MATCH
    assert by_source["/alarms/:id/acknowledge"]["backend_path"] == "/api/alarms/<alarm_id>/acknowledge"

    # query string 不影响路由匹配 → match
    assert by_source["/alarms"]["status"] == STATUS_MATCH

    # 前端重复书写 /api 前缀 → prefixed_mismatch（performance.ts 那类事故）
    dup = by_source["/api/performance/metrics/realtime"]
    assert dup["status"] == STATUS_PREFIXED_MISMATCH
    assert dup["frontend_path"] == "/api/api/performance/metrics/realtime"

    # 静态段撞上参数段 → param_mismatch（会命中错误的 handler）
    collide = by_source["/devices/protocols"]
    assert collide["status"] == STATUS_PARAM_MISMATCH
    assert collide["backend_path"] == "/api/devices/<device_id>"

    # 后端完全没有 → missing_backend
    assert by_source["/totally/absent/endpoint"]["status"] == STATUS_MISSING_BACKEND


def test_classifier_detects_method_mismatch(tmp_path):
    """路径存在但方法不同 → missing_backend（线上表现是 405 而非 404）。"""
    ts_file = tmp_path / "synthetic.ts"
    ts_file.write_text("import api from './request'\napi.delete('/alarms')\n", encoding="utf-8")
    py_file = tmp_path / "synthetic_backend.py"
    py_file.write_text(_SYNTHETIC_BACKEND_PY, encoding="utf-8")

    records = classify_endpoints(_parse_frontend_file(ts_file), _parse_backend_file(py_file))
    assert records[0]["status"] == STATUS_MISSING_BACKEND
    assert "方法不匹配" in records[0]["detail"]
    assert records[0]["backend_path"] == "/api/alarms"


def test_frontend_parser_normalizes_templates_and_query():
    """解析器归一化：模板变量 → :param，query 剥离，baseURL 的 /api 前置。"""
    assert _normalize_path("/alarms?limit=10&offset=2") == "/alarms"
    assert _normalize_path("/data/history/${deviceId}/${encodeURIComponent(reg)}") == (
        "/data/history/:deviceId/:reg"
    )
    assert _normalize_path("devices//") == "/devices"
    assert _join_prefix("/api/health", "/status") == "/api/health/status"
    assert _structural("/api/devices/<int:device_id>") == ("api", "devices", "*")
    assert _structural("/api/devices/:id") == ("api", "devices", "*")


# ---------------------------------------------------------------------------
# 正向断言：前端声明的每个端点都必须在后端存在
# ---------------------------------------------------------------------------


def test_every_frontend_endpoint_exists_in_backend():
    """前端 `src/api/**` 声明的每个 (method, path) 都必须能在后端找到路由。

    失败时列出全部不一致项（路径 + 状态 + 出处），便于直接定位。
    """
    contract = load_contract()
    bad = [r for r in contract.records if r["status"] != STATUS_MATCH]
    if bad:
        lines = [
            f"  [{r['status']}] {r['method']} {r['frontend_path']}"
            f"  ← {r['source_file']}:{r['source_line']}  {r['detail']}"
            for r in bad
        ]
        pytest.fail(
            f"{len(bad)}/{len(contract.records)} 个前端声明端点在后台找不到对应路由：\n"
            + "\n".join(lines)
        )


def test_no_prefixed_mismatch(contract_report):
    """专项回归：前端不得重复书写 baseURL 已含的 `/api` 前缀。

    这正是 `performance.ts` 三个接口必现 404 的成因，单独留一条断言，
    失败信息比总断言更直观。
    """
    bad = [r for r in contract_report["endpoints"] if r["status"] == STATUS_PREFIXED_MISMATCH]
    assert not bad, "存在多余的 /api 前缀（会请求到 /api/api/... → 必然 404）：\n" + "\n".join(
        f"  {r['method']} {r['source_path']} ← {r['source_file']}:{r['source_line']}"
        for r in bad
    )


def test_no_param_mismatch(contract_report):
    """专项回归：路径参数位必须与后端一致，不允许"静态段被参数段吃掉"。"""
    bad = [r for r in contract_report["endpoints"] if r["status"] == STATUS_PARAM_MISMATCH]
    assert not bad, "路径参数与后端不一致：\n" + "\n".join(
        f"  [{r['status']}] {r['method']} {r['frontend_path']} ← {r['source_file']}:{r['source_line']}"
        for r in bad
    )


def test_match_rate_is_100_percent(contract_report):
    summary = contract_report["summary"]
    assert summary["result"] == "pass"
    assert summary["unmatched"] == 0
    assert summary["match_rate"] == "100.00%"


# ---------------------------------------------------------------------------
# 反向断言：后端存在但前端未调用 → 只报告不失败
# ---------------------------------------------------------------------------


def test_unused_backend_endpoints_are_reported_only(contract_report, capsys):
    """**只报告，不断言失败**。

    后端路由先行或供运维/外部系统调用（resilience、ops、Prometheus 等）是
    合理设计，不构成缺陷；把它们强制"必须被前端调用"会逼出为了过关而
    删除可用能力的反效果。这里只把清单打进报告 json 和测试输出。
    """
    unused = contract_report["unused_backend_endpoints"]
    with capsys.disabled():
        print(f"\n[契约报告] 未使用后端端点 {len(unused)} 个（仅报告，不判失败）：")
        for item in unused:
            print(f"  - {item['method']:6s} {item['backend_path']}  ({item['source_file']}:{item['source_line']})")
    assert isinstance(unused, list)


# ---------------------------------------------------------------------------
# 后端「无人调用」端点的**分类**守卫（round 193 新增）
# ---------------------------------------------------------------------------

#: 后端存在、前端不调用的端点，按**前缀**分类并给出理由。
#:
#: 为什么从「只报告」升级成「显式分类 + 守卫」
#: ------------------------------------------
#: 上面那条 `..._are_reported_only` 的理由（后端路由先行是合理设计）本身没错，
#: 但它让**任何**新增的未调用端点都能静默溜进来 —— **包括本该被前端调用的那种**。
#: 一份「只报告不失败」的清单没人会去看，于是它不构成闸门。
#:
#: 实测（2026-10-08，round 193）就抓到一个反例：`/api/system/client-errors`
#: 明明是给前端上报错误用的，却躺在「前端从未调用」清单里。
#: （那条其实是**扫描口径**的假阳性 —— 只扫 `src/api/` 且不认 `fetch`，已修。
#:  但「清单不可信」这件事本身说明：只报告不失败是不够的。）
#:
#: 现在：每条未调用端点必须落进下面某个前缀规则；**新出现的**未分类端点 → 红。
UNUSED_BACKEND_PREFIX_RULES: dict[str, str] = {
    "/api/resilience/": (
        "韧性工程接口（混沌实验 / 熔断器 / 降级 / 故障注入 / 限流配置），"
        "供 SRE 与演练脚本使用，**不面向浏览器 UI**"
    ),
    "/api/ops/": (
        "运维工具接口（数据库维护 / 配置中心 / 清理 / 诊断导出 / 维护任务 / 运维审计），"
        "供运维脚本与运维人员使用，**不面向浏览器 UI**"
    ),
    "/api/alarms/escalation/": (
        "告警升级规则接口（后端有真实的 AlarmEscalationManager，规则是**列表**模型）。"
        "⚠️ 前端**没有**接它 —— 配置页的「报警升级」表单走了另一条**且是坏的**路径，见 D14"
    ),
    "/api/health/": (
        "健康检查扩展接口（详情 / 异步任务状态），供探活与运维脚本使用；UI 只用 /status"
    ),
}


def test_unused_backend_endpoints_are_all_classified(contract_report):
    """后端未调用端点必须**全部**落进已声明的前缀分类里。

    新出现一条没分类的未调用端点 → 红：要么把它接上前端，
    要么在 `UNUSED_BACKEND_PREFIX_RULES` 里写清它为什么**不该**被前端调用。
    """
    unused = contract_report["unused_backend_endpoints"]
    unclassified = [
        f"{e['method']:6s} {e['backend_path']}  ({e['source_file']}:{e['source_line']})"
        for e in unused
        if not any(e["backend_path"].startswith(p) for p in UNUSED_BACKEND_PREFIX_RULES)
    ]
    assert not unclassified, (
        "以下后端端点前端从不调用，且没有落进任何已声明的前缀分类 —— "
        "要么接上前端，要么在 UNUSED_BACKEND_PREFIX_RULES 里说明原因：\n  "
        + "\n  ".join(unclassified)
    )


def test_unused_backend_prefix_rules_are_not_stale(contract_report):
    """分类规则不许腐烂：写了某前缀却一条端点都没命中 → 红。"""
    unused = contract_report["unused_backend_endpoints"]
    stale = [
        p for p in UNUSED_BACKEND_PREFIX_RULES
        if not any(e["backend_path"].startswith(p) for e in unused)
    ]
    assert not stale, (
        f"UNUSED_BACKEND_PREFIX_RULES 里这些前缀已无对应端点，请删掉：{stale}"
    )


def test_unused_backend_prefix_rules_have_reasons():
    """每条分类规则都必须写清理由（不能只写个前缀）。"""
    thin = [p for p, r in UNUSED_BACKEND_PREFIX_RULES.items() if len(r.strip()) < 15]
    assert not thin, f"以下前缀分类没写清理由：{thin}"


# ---------------------------------------------------------------------------
# 前端「写入一个不存在的配置段」守卫（round 193 新增）
# ---------------------------------------------------------------------------

#: 前端 `systemApi.saveConfig(<section>, …)` 里的 section 字面量。
#:
#: ⚠️ 必须带 `systemApi.` 前缀。第一版只写 `saveConfig(...)`，结果把
#: **模板里按钮的本地包装调用**也算了进来 —— `@click="saveConfig('energy')"`
#: 调的是 `Config.vue` 内部的同名函数，而那个函数对 `energy` 有**特判**
#: （走 `industry40Api.setEnergyTariff`，不是 config 接口）。
#: 于是一个正确的按钮被判成「保存到不存在的段」，守卫假红。
#: **判据必须落在「真正发请求的那一层」**，不能落在同名的包装层上。
_CONFIG_SECTION_CALL_RE = re.compile(
    r"\bsystemApi\s*\.\s*saveConfig\s*\(\s*['\"]([^'\"]+)['\"]"
)

#: `PUT /api/config` 读写的那份配置文件（后端 `展示层/api/api_system.py` 硬编码的路径）。
_SYSTEM_YAML_REL = Path("配置") / "system.yaml"

#: 已知**当前就是坏的**配置段：前端在保存它们，而 `配置/system.yaml` 里没有这些段。
#:
#: 后端 `PUT /api/config` 的实现是：
#:     section 存在且是 dict → 更新
#:     否则（section 非空）  → **400**「配置段 X 不存在或不是字典」
#: 所以写一个不存在的段**必然 400** —— 前端会弹一句「保存失败」，
#: 用户根本不知道原因，而那个按钮等于装饰品。
#:
#: 进这张表 = 「已知且已判断过」，**不是**「允许」。
#: 表是**双向**断言的：这里列了但已经修好 → 红（强制清理）；
#: 没列但实际坏了 → 红（强制新增，不允许再出现隐性的）。
KNOWN_MISSING_CONFIG_SECTIONS: dict[str, str] = {
    "alarm_escalation": (
        "配置页「报警升级」保存按钮。后端**有**专门的 /api/alarms/escalation/rules"
        "（规则**列表**模型 + 真实的 AlarmEscalationManager），但前端表单是**扁平对象**，"
        "形状不兼容 → 不是换个 URL 就完事。见 D14。"
    ),
    "archive": (
        "配置页「保存归档策略」按钮。归档策略在前端是一份扁平配置，"
        "后端没有对应配置段（归档由后台维护任务驱动）→ 需产品口径。见 D14。"
    ),
    "archive_trigger": (
        "配置页「立即归档」按钮。它把一个**命令**（action=archive_now）当成配置段写，"
        "方向就不对 —— 命令不该走配置接口。见 D14。"
    ),
}


def _frontend_config_sections() -> dict[str, list[str]]:
    """扫前端源码，取 `saveConfig('<字面量>', …)` 里的段名 → 出处列表。"""
    out: dict[str, list[str]] = {}
    for f in _iter_frontend_source_files():
        text = f.read_text(encoding="utf-8")
        for m in _CONFIG_SECTION_CALL_RE.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            out.setdefault(m.group(1), []).append(
                f"{f.relative_to(_resolve_frontend_src_dir()).as_posix()}:{line}"
            )
    return out


def _system_yaml_sections() -> set[str]:
    import yaml  # 仅测试期依赖，仓库已依赖 PyYAML

    path = BACKEND_ROOT / _SYSTEM_YAML_REL
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assert isinstance(data, dict), f"{path} 顶层不是映射"
    return set(data.keys())


def test_frontend_config_sections_exist_in_system_yaml():
    """前端 `saveConfig('<section>', …)` 的 section 必须真实存在于 `配置/system.yaml`。

    这是一条**必然成立**的约束：后端对不存在的段一律 400（见
    `KNOWN_MISSING_CONFIG_SECTIONS` 的说明）。写一个不存在的段没有任何意义。

    实测（2026-10-08，round 193）：配置页有 **3 个按钮**踩了这个坑
    —— 「报警升级」「保存归档策略」「立即归档」，点了必然报错。
    """
    sections = _frontend_config_sections()
    assert sections, "没扫到任何 saveConfig('<段名>') 调用 —— 扫描口径可能坏了"

    known = _system_yaml_sections()
    missing = {s: src for s, src in sections.items() if s not in known}

    undeclared = {s: src for s, src in missing.items() if s not in KNOWN_MISSING_CONFIG_SECTIONS}
    assert not undeclared, (
        "以下配置段前端在保存，但 system.yaml 里没有 → 后端必然 400。"
        "要么补上该段，要么在 KNOWN_MISSING_CONFIG_SECTIONS 里登记原因：\n  "
        + "\n  ".join(f"{s}  <- {', '.join(src)}" for s, src in undeclared.items())
    )


def test_known_missing_config_sections_are_still_missing():
    """登记表不许腐烂：已登记的坏段若被修好 → 红，强制把它从表里删掉。"""
    sections = _frontend_config_sections()
    known = _system_yaml_sections()

    fixed = [s for s in KNOWN_MISSING_CONFIG_SECTIONS if s in known]
    assert not fixed, (
        f"这些段已经在 system.yaml 里了，请从 KNOWN_MISSING_CONFIG_SECTIONS 删掉：{fixed}"
    )

    gone = [s for s in KNOWN_MISSING_CONFIG_SECTIONS if s not in sections]
    assert not gone, (
        f"这些段前端已经不再保存了，请从 KNOWN_MISSING_CONFIG_SECTIONS 删掉：{gone}"
    )


def test_known_missing_config_sections_have_reasons():
    """每条登记都必须写清原因与处置方向。"""
    thin = [s for s, r in KNOWN_MISSING_CONFIG_SECTIONS.items() if len(r.strip()) < 20]
    assert not thin, f"以下坏段登记没写清原因：{thin}"
