#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描「被声明、但没有任何代码读」的环境变量。

缺陷类别：**接线状态（声明了，但不通电）**
==========================================
和 round 193-205 查的「写了但没接线」是同一族，只是对象换成了**环境变量**：

    运维/容器/启动脚本声明了 ``SCADA_MODE=real``，
    文档表格里也写着「运行模式」，
    然后**代码里没有任何一处读它** ——
    于是「我设了真实设备模式」是个**假信念**，系统照旧跑模拟数据。

在 SCADA 场景里这类假信念尤其危险：操作员以为在看真实设备，其实在看仿真。

本轮实测到的（2026-10-09，round 206）
------------------------------------
| 变量 | 谁声明的 | 后果 |
|---|---|---|
| ``SCADA_MODE`` | README 表格 / Dockerfile / docker-compose / .env.example | 设了不生效，以为自己接了真设备 |
| ``SCADA_HOST`` | Dockerfile / .env.example | 容器里仍只监听 127.0.0.1 → 端口发布打不到，容器「健康」但外部连不上 |
| ``SCADA_PORT`` | Dockerfile / .env.example / tools/multi_instance.py | 多实例全部挤 5000 |
| ``SCADA_DATA_DIR`` | tools/multi_instance.py | 多实例共享同一个 SQLite 库 |
| ``SCADA_CONFIG_DIR`` | tools/multi_instance.py | 多实例共享同一份 ``配置/`` |
| ``SCADA_INSTANCE`` | tools/multi_instance.py | 无任何用途 |
| ``SCADA_DB_PATH`` | .env.example | 设了不生效（库路径实际由模式派生） |
| ``DEBUG`` | .env.example | 真名是 ``FLASK_DEBUG`` |

口径（不收紧就不能用）
----------------------
* **声明来源**：Dockerfile（``ENV X=``）、docker-compose（``- X=`` / ``X:`` / ``${X:-}``）、
  ``.env`` / ``.env.example``（``X=``）、``tools/**`` 与 ``.github/**`` 脚本
  （``env['X'] =`` / ``set X=`` / ``export X=``）、**文档表格**（``| `X` | ... |``）。
* **读取来源**：源码（py/js/mjs/ts/vue）里的读取点，以及 ``配置/*.yaml`` 里的
  ``${X}`` 插值（这是真读：采集层会把它们代入设备清单）。
  只认**严格的读取姿势**（见 ``READ_PATTERNS``），不认「文件里随便出现过一次」——
  否则一句注释就能把守卫喂饱（round 191/197/199/203 踩过 5 次）。
* **白名单**：``EXTERNAL_ALLOW`` —— 给**别的进程**读的变量（如 Grafana 的 ``GF_*``）。
  必须写明理由，且理由要能被复核。
* 反过来（「读了但没声明」）本轮**不做** —— 那更接近文档完整性问题，
  且容易因为「读到的是拼接出来的名字」产生假红。

用法
----
    python tools/scan_doc_env_vars.py [仓库根]
"""

from __future__ import annotations

import collections
import os
import pathlib
import re
import sys

#: 遍历时按目录名剪枝。
#: ⚠️ 必须剪：``Path.rglob`` 会走进 ``.venv``（本仓 7716 个 .py），
#: 整仓遍历会把命令拖到被环境 SIGTERM 掉（2026-10-09 实测）。
SKIP_DIRS = {
    "node_modules", ".venv", "venv", "dist", "dist-iso", "release",
    "build", ".git", "__pycache__", "instances", ".pytest_cache",
}
SKIP_PREFIXES = (".pytest_tmp",)

CODE_EXTS = {".py", ".js", ".mjs", ".ts", ".vue"}
YAML_EXTS = {".yaml", ".yml"}
DOC_EXTS = {".md"}

#: 给**别的进程 / 别的解释器**读的变量，本项目代码不读它们是正常的。
#: 理由必须可复核（点名「谁」在读）。
EXTERNAL_ALLOW: dict[str, str] = {
    "GF_SECURITY_ADMIN_USER": "Grafana 容器自己读（grafana 镜像），本项目代码不读",
    "GF_SECURITY_ADMIN_PASSWORD": "Grafana 容器自己读（grafana 镜像），本项目代码不读",
    "GF_INSTALL_PLUGINS": "Grafana 容器自己读（grafana 镜像），本项目代码不读",
    "EMQX_NAME": "EMQX 容器自己读（emqx 镜像），本项目代码不读",
    "EMQX_HOST": "EMQX 容器自己读（emqx 镜像），本项目代码不读",
    "TAOS_FQDN": "TDengine 容器自己读（tdengine 镜像），本项目代码不读",
    "PYTHONUTF8": "CPython 解释器自己读（UTF-8 模式开关），本项目代码不读",
    "PYTHONIOENCODING": "CPython 解释器自己读（stdio 编码），本项目代码不读",
}

#: 严格的「读取点」姿势。``%s`` 会被替换成变量名。
READ_PATTERNS = (
    r"os\.environ\.get\(\s*['\"]%s['\"]",
    r"os\.environ\[\s*['\"]%s['\"]",
    r"\bgetenv\(\s*['\"]%s['\"]",
    r"process\.env\.%s\b",
    r"process\.env\[\s*['\"]%s['\"]",
    r"import\.meta\.env\.%s\b",
    # config.py 的取值助手：_get_int('X', ...) / _get_bool('X', ...) /
    # _get_secret('X', 'X') / paths._env_path('X', ...)
    r"_get_(?:int|bool|secret|str|float)\(\s*['\"]%s['\"]",
    r"_get_secret\(\s*[^,)]+,\s*['\"]%s['\"]",
    r"_env_path\(\s*['\"]%s['\"]",
    # 配置 YAML 里的 ${X} / ${X:-默认} 插值（采集层会代入）
    r"\$\{%s(?::[^}]*)?\}",
)

#: 间接读取：把变量名先存进常量再拿去读，例如
#:     _FRONTEND_API_DIR_ENV = "SCADA_FRONTEND_API_DIR"
#:     env = os.environ.get(_FRONTEND_API_DIR_ENV)
#: 这是**真读**，但上面的字面量正则看不见它。
#: 不处理就会假红（round 206 实测：`SCADA_FRONTEND_API_DIR` 就是这样被误报的）。
#: 做法刻意收紧：常量必须**恰好等于**变量名，且必须出现在读取调用里。
ENV_CONST_RE = re.compile(
    r"^\s*([A-Z_][A-Z0-9_]*)\s*(?::\s*str\s*)?=\s*['\"]([A-Z][A-Z0-9_]{2,})['\"]", re.M
)
#: 这些调用里出现常量名 = 间接读取。``%s`` 换成**常量名**。
CONST_READER_PATTERNS = (
    r"os\.environ\.get\(\s*%s\b",
    r"os\.environ\[\s*%s\b",
    r"\bgetenv\(\s*%s\b",
    r"process\.env\[\s*%s\b",
    r"process\.env\.%s\b",
    r"import\.meta\.env\[\s*%s\b",
    r"_get_(?:int|bool|secret|str|float)\(\s*%s\b",
    r"_env_path\(\s*%s\b",
)

#: 文档表格里的环境变量名：``| `VAR` | ... |``
DOC_TABLE_VAR_RE = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]{2,})`\s*\|", re.M)
#: Dockerfile 的 ``ENV VAR=...``
DOCKERFILE_ENV_RE = re.compile(r"^\s*ENV\s+([A-Z][A-Z0-9_]{2,})\s*=", re.M)
#: ``- VAR=...`` / ``VAR=...`` 行（docker-compose / .env）
ASSIGN_RE = re.compile(r"^\s*-?\s*([A-Z][A-Z0-9_]{2,})\s*=", re.M)
#: YAML 映射形态的 ``VAR: 值``（CI 工作流的 ``env:`` 块就是这么写的）。
#: 只在 YAML 声明源上使用 —— 键名全大写是「这是环境变量」的强信号，
#: 而 YAML 里的服务名/字段名（``scada:`` / ``version:``）都是小写。
YAML_KEY_RE = re.compile(r"^\s*([A-Z][A-Z0-9_]{2,})\s*:\s*\S", re.M)
#: ``${VAR}`` / ``${VAR:-x}``（compose 插值 = 声明来源）
INTERP_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]{2,})(?::[^}]*)?\}")
#: 脚本里的 ``env['VAR'] =`` / ``set VAR=`` / ``export VAR=`` / ``os.environ['VAR'] =``
SCRIPT_SET_RE = re.compile(
    r"(?:env\[['\"]([A-Z][A-Z0-9_]{2,})['\"]\]\s*="
    r"|os\.environ\[['\"]([A-Z][A-Z0-9_]{2,})['\"]\]\s*="
    r"|^\s*(?:set|export)\s+([A-Z][A-Z0-9_]{2,})=)",
    re.M,
)


def _walk(root: pathlib.Path, exts: set[str]):
    """按目录剪枝地遍历指定扩展名的文件。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and not d.startswith(SKIP_PREFIXES)
        ]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in exts:
                yield pathlib.Path(dirpath) / fn


def _read(p: pathlib.Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def collect_declared(root: pathlib.Path) -> dict[str, set[str]]:
    """收集「谁声明了哪些环境变量」。"""
    declared: dict[str, set[str]] = collections.defaultdict(set)

    def add(var: str | None, where: str):
        if var:
            declared[var].add(where)

    # --- Dockerfile* ---
    for p in root.glob("Dockerfile*"):
        txt = _read(p)
        for m in DOCKERFILE_ENV_RE.finditer(txt):
            add(m.group(1), p.name)
        for m in INTERP_RE.finditer(txt):
            add(m.group(1), p.name)

    # --- docker-compose* / *.env 模板 ---
    for p in list(root.glob("docker-compose*")) + list(root.glob(".env*")):
        txt = _read(p)
        for m in ASSIGN_RE.finditer(txt):
            add(m.group(1), p.name)
        for m in YAML_KEY_RE.finditer(txt):
            add(m.group(1), p.name)
        for m in INTERP_RE.finditer(txt):
            add(m.group(1), p.name)

    # --- CI 工作流（env: 块 / ${VAR} 插值）---
    # 目前 ci.yml 里没有 env 声明，但这是**将来最可能**新增声明源的地方
    # （往 workflow 里加一个 SCADA_* 覆盖 CI 行为，然后没人读）。
    workflows = root / ".github" / "workflows"
    if workflows.is_dir():
        for p in _walk(workflows, YAML_EXTS):
            txt = _read(p)
            rel = str(p.relative_to(root)).replace("\\", "/")
            for m in ASSIGN_RE.finditer(txt):
                add(m.group(1), rel)
            for m in YAML_KEY_RE.finditer(txt):
                add(m.group(1), rel)
            for m in INTERP_RE.finditer(txt):
                add(m.group(1), rel)

    # --- 脚本（tools/、.github/、根目录 *.py 里的 env[...] =）---
    for p in list(_walk(root / "tools", CODE_EXTS)) + list(_walk(root / ".github", CODE_EXTS)):
        for m in SCRIPT_SET_RE.finditer(_read(p)):
            add(next(g for g in m.groups() if g), str(p.relative_to(root)).replace("\\", "/"))

    # --- 文档表格 ---
    for p in _walk(root, DOC_EXTS):
        txt = _read(p)
        rel = str(p.relative_to(root)).replace("\\", "/")
        for m in DOC_TABLE_VAR_RE.finditer(txt):
            add(m.group(1), rel)

    return declared


def collect_read_corpus(root: pathlib.Path) -> str:
    """拼出「可能含读取点」的语料：源码 + 配置 YAML。"""
    parts = []
    for p in _walk(root, CODE_EXTS):
        parts.append(_read(p))
    for p in _walk(root / "配置", YAML_EXTS):
        parts.append(_read(p))
    return "\n".join(parts)


def collect_indirectly_read(root: pathlib.Path) -> set[str]:
    """识别「常量间接读取」的变量名集合。

    形态（``tests/test_frontend_contract.py`` 里就是这么写的）::

        _FRONTEND_API_DIR_ENV = "SCADA_FRONTEND_API_DIR"
        ...
        env = os.environ.get(_FRONTEND_API_DIR_ENV)

    按文件判定：常量必须**恰好等于**变量名，且必须出现在读取调用里。
    这样「注释里提了一句 + 同文件里别处有 os.environ」不会误判成读取。
    """
    found: set[str] = set()
    for p in _walk(root, CODE_EXTS):
        text = _read(p)
        consts = {m.group(1): m.group(2) for m in ENV_CONST_RE.finditer(text)}
        if not consts:
            continue
        for const, var in consts.items():
            if any(
                re.search(pat.replace("%s", re.escape(const)), text)
                for pat in CONST_READER_PATTERNS
            ):
                found.add(var)
    return found


def find_unread(root: pathlib.Path) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """返回 (无读取点的变量 -> 声明来源, 白名单放行的变量 -> 声明来源)。"""
    declared = collect_declared(root)
    corpus = collect_read_corpus(root)
    indirect = collect_indirectly_read(root)

    unread: dict[str, set[str]] = {}
    allowed: dict[str, set[str]] = {}
    for var, where in sorted(declared.items()):
        hit = var in indirect or any(
            re.search(pat.replace("%s", re.escape(var)), corpus)
            for pat in READ_PATTERNS
        )
        if hit:
            continue
        if var in EXTERNAL_ALLOW:
            allowed[var] = where
        else:
            unread[var] = where
    return unread, allowed


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    unread, allowed = find_unread(root)

    print(f"=== 被声明、但没有任何读取点（{len(unread)} 个）===")
    for var, where in unread.items():
        print(f"  {var:28s} <- {', '.join(sorted(where))}")
    print(f"=== 白名单放行（别的进程读，{len(allowed)} 个）===")
    for var, where in allowed.items():
        print(f"  {var:28s} <- {', '.join(sorted(where))}  | {EXTERNAL_ALLOW[var]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
