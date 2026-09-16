# -*- coding: utf-8 -*-
"""静默吞异常静态守卫（P1-4）。

背景
----
本项目最危险的失效模式不是崩溃，而是"**静默假成功**"：代码不报错，只是
给出错误或陈旧的数据。审计发现生产代码里散布着大量 ``except XXX: pass`` /
``except XXX: continue``，异常被完全吞掉，运维侧看不到任何痕迹。

本测试用 AST 静态扫描（而不是正则 —— 正则会被注释和 docstring 误伤）锁死
两类退化：

1. **裸 ``except:``** —— 会连 ``KeyboardInterrupt`` / ``SystemExit`` 一起吞掉，
   导致 Ctrl-C 杀不掉进程。一律禁止。
2. **``except Exception: pass`` / ``except BaseException: pass``** —— 宽异常被
   静默丢弃，任何"预期外"的故障都不会留痕。一律禁止，必须至少
   ``logger.debug(...)``。

窄异常（``queue.Empty`` / ``socket.timeout`` / ``sqlite3.Error`` 等）**允许**
继续忽略 —— 那是明确的、可枚举的预期分支。但按项目约定，应在 ``pass`` /
``continue`` 上方写一行中文注释说明为什么安全忽略；本测试不强制校验注释
（注释不是可机检的契约），由 code review 把关。

扫描范围
--------
仓库内全部 ``.py``，排除：
- ``tests/``（测试里的 try/except 有自己的语义，且经常故意吞异常）
- ``tools/``（运维脚本，不是常驻服务代码）
- ``.venv`` / ``node_modules`` / ``build`` / ``dist`` / ``__pycache__``
- ``测试``（旧的中文名测试目录，已被 pyproject 排除）
"""

from __future__ import annotations

import ast
import os

import pytest

# ---------------------------------------------------------------------------
# 扫描范围
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "build",
    "dist",
    "managed_components",
    "tests",      # 测试自身允许吞异常
    "tools",      # 运维脚本，非常驻服务代码
    "测试",        # 旧中文名测试目录
    "pytest-quarantine",
    "legacy",     # 归档区：历史死代码与一次性脚本，不参与构建/打包，已明确标记为废弃
}

#: 宽异常类型 —— 这些被 ``pass`` 掉就是静默失效
BROAD_EXCEPTIONS = {"Exception", "BaseException"}

#: 允许的例外：(相对路径, 行号) -> 理由
#: 目前为空。若确需豁免，必须写明理由，且理由要能说服 reviewer。
ALLOWED_SILENT_BROAD: dict[tuple[str, int], str] = {}


def _iter_production_py():
    """遍历生产代码里的全部 .py 文件，产出 (相对路径, 绝对路径)。"""
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for name in files:
            if not name.endswith(".py"):
                continue
            abs_path = os.path.join(root, name)
            rel = os.path.relpath(abs_path, REPO_ROOT).replace(os.sep, "/")
            # 仓库根目录下的临时脚本（下划线开头）跳过
            if "/" not in rel and name.startswith("_"):
                continue
            yield rel, abs_path


def _parse(rel: str, abs_path: str) -> ast.Module:
    with open(abs_path, encoding="utf-8") as fh:
        source = fh.read()
    return ast.parse(source, filename=rel)


def _is_bare(handler: ast.ExceptHandler) -> bool:
    return handler.type is None


def _is_broad(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    if isinstance(handler.type, ast.Name):
        return handler.type.id in BROAD_EXCEPTIONS
    if isinstance(handler.type, ast.Tuple):
        return any(
            isinstance(elt, ast.Name) and elt.id in BROAD_EXCEPTIONS
            for elt in handler.type.elts
        )
    return False


def _is_silent(handler: ast.ExceptHandler) -> bool:
    """异常块体只有一条 pass / continue —— 完全没留痕。"""
    body = handler.body
    return len(body) == 1 and isinstance(body[0], (ast.Pass, ast.Continue))


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------

def _collect_bare_except() -> list[str]:
    hits: list[str] = []
    for rel, abs_path in _iter_production_py():
        try:
            tree = _parse(rel, abs_path)
        except SyntaxError as exc:  # pragma: no cover - 语法错误应被其它测试抓到
            pytest.fail(f"{rel} 语法错误: {exc}")
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and _is_bare(node):
                hits.append(f"{rel}:{node.lineno}")
    return hits


def _collect_silent_broad() -> list[str]:
    hits: list[str] = []
    for rel, abs_path in _iter_production_py():
        try:
            tree = _parse(rel, abs_path)
        except SyntaxError as exc:  # pragma: no cover
            pytest.fail(f"{rel} 语法错误: {exc}")
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if not (_is_broad(node) and _is_silent(node)):
                continue
            if (rel, node.lineno) in ALLOWED_SILENT_BROAD:
                continue
            hits.append(f"{rel}:{node.lineno}")
    return hits


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

def test_scan_covers_real_files():
    """先证明扫描器真的在扫东西 —— 防止 exclude 写错导致"扫了 0 个文件"的假绿。"""
    files = list(_iter_production_py())
    assert len(files) > 100, f"只扫到 {len(files)} 个文件，EXCLUDED_DIRS 可能写错了"
    rels = {rel for rel, _ in files}
    # 抽查几个必须被扫到的核心模块
    for must in ("run.py", "config.py", "core/module_registry.py",
                 "采集层/data_collector.py", "存储层/database.py"):
        assert must in rels, f"{must} 没有被扫到"


def test_no_bare_except():
    """裸 except 会吞掉 KeyboardInterrupt / SystemExit，一律禁止。"""
    hits = _collect_bare_except()
    assert not hits, (
        "发现裸 `except:`（会吞掉 KeyboardInterrupt/SystemExit，导致进程杀不掉）：\n  "
        + "\n  ".join(sorted(hits))
    )


def test_no_silently_swallowed_broad_exception():
    """宽异常被 pass/continue 静默丢弃 = 静默失效，一律禁止。"""
    hits = _collect_silent_broad()
    assert not hits, (
        "发现 `except Exception/BaseException: pass|continue` —— 异常被完全吞掉，"
        "运维侧看不到任何痕迹（这正是本项目最危险的'静默假成功'模式）。\n"
        "修法：改绑 `as e` 并至少 `logger.debug('<操作> 忽略异常: %s', e, exc_info=True)`；"
        "若意味着功能降级，用 `logger.warning` 并带上下文。\n  "
        + "\n  ".join(sorted(hits))
    )


def test_narrow_silent_except_is_tolerated():
    """反向验证：窄异常被忽略是允许的，守卫不能误伤。

    这条测试保证上面两条守卫的"宽/窄"判别逻辑是对的 —— 否则一旦判别写错，
    守卫会把所有窄异常都报成违规，那就没人会认真修了。
    """
    src = (
        "import queue\n"
        "def f(q):\n"
        "    try:\n"
        "        return q.get_nowait()\n"
        "    except queue.Empty:\n"
        "        pass\n"
        "    except (ValueError, TypeError):\n"
        "        continue\n"
    )
    tree = ast.parse(src)
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    assert len(handlers) == 2
    for h in handlers:
        assert _is_silent(h), "窄异常 + pass 应被识别为 silent"
        assert not _is_broad(h), "窄异常不应被识别为 broad"


def test_broad_detection_catches_tuple_and_bare():
    """反向验证：`except (Exception, X):` 与裸 `except:` 都要被判为宽异常。"""
    src = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except:\n"
        "        pass\n"
        "    try:\n"
        "        pass\n"
        "    except (Exception, ValueError):\n"
        "        pass\n"
        "    try:\n"
        "        pass\n"
        "    except Exception:\n"
        "        pass\n"
    )
    tree = ast.parse(src)
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    assert len(handlers) == 3
    assert all(_is_broad(h) for h in handlers)
    assert all(_is_silent(h) for h in handlers)
