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

扫描范围（round 162 调整）
------------------------
两条规则的扫描范围**不一样**，这是刻意的：

- **禁裸 ``except:``** —— 覆盖几乎全库（只排除第三方 / 构建产物 / 归档区）。
  裸 except 会吞掉 ``KeyboardInterrupt`` / ``SystemExit``，导致 Ctrl-C 杀不掉进程，
  这个危害**不区分生产代码还是测试代码**。
- **禁 ``except Exception/BaseException: pass|continue``** —— 只扫生产代码，
  额外排除 ``tests/`` / ``测试/``（测试里故意吞异常是常见写法，且测试崩了会直接变红）。
  **``tools/`` 已在 round 162 纳入管辖** —— 此前它整目录被排除，结果藏了 11 处，
  其中 ``security_scan.py`` 一家 7 处，表现为"扫描器读不了文件就静默跳过，
  然后报告『未发现问题』"。

永久排除：``.venv`` / ``node_modules`` / ``build`` / ``dist`` / ``__pycache__`` /
``legacy``（round 161 建立的归档区：历史死代码与一次性脚本）。
"""

from __future__ import annotations

import ast
import os

import pytest

# ---------------------------------------------------------------------------
# 扫描范围
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 任何规则都不扫的目录：第三方依赖 / 构建产物 / 归档区。
#: ``legacy/`` 是 round 161 建立的归档区（历史死代码与一次性脚本），
#: 按定义是废弃代码，不适用生产标准。
ALWAYS_SKIP = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "build",
    "dist",
    "managed_components",
    "pytest-quarantine",
    "legacy",
}

#: 「宽异常被静默丢弃」规则的额外排除集。
#:
#: 只排除测试目录 —— 测试里故意吞异常是常见写法（而且测试崩了会直接变红，
#: 不存在"静默"问题）。
#:
#: **``tools/`` 已在 round 162 从本集合移除。** 此前它整目录被排除，
#: 结果 11 处 `except Exception: pass` 藏在运维脚本里躲过守卫 ——
#: 其中 `security_scan.py` 一家占 7 处，表现为"扫描器读不了文件就静默跳过，
#: 然后报告『未发现问题』"。这些已全部修掉，现在 `tools/` 纳入管辖。
BROAD_RULE_SKIP = ALWAYS_SKIP | {"tests", "测试"}

#: 「裸 except」规则的排除集：**只排除 ALWAYS_SKIP，不额外排除测试目录**。
#:
#: 理由：裸 ``except:`` 会连 ``KeyboardInterrupt`` / ``SystemExit`` 一起吞掉，
#: 导致脚本跑起来 Ctrl-C 杀不掉进程 —— 这个危害**不区分生产代码还是测试代码**。
#: round 162 已把全库裸 except 清零（含 `测试/实验测试.py` 的 3 处）。
BARE_RULE_SKIP = ALWAYS_SKIP

#: 宽异常类型 —— 这些被 ``pass`` 掉就是静默失效
BROAD_EXCEPTIONS = {"Exception", "BaseException"}

#: 允许的例外：(相对路径, 行号) -> 理由
#: 豁免必须写明理由，且理由要能说服 reviewer。**豁免集只应收缩，不应增长** ——
#: `tests/test_core_regressions.py::test_silent_broad_allowlist_does_not_grow`
#: 会盯着这里，防止有人图省事把新违规加进白名单。
ALLOWED_SILENT_BROAD: dict[tuple[str, int], str] = {
    ("core/health_checker.py", 548): (
        "已登记的存量负债（未修复）。"
        "`_check_data_freshness()` 里 `data_collector.data_queue.qsize()` 的 "
        "`except Exception: pass`：队列深度读不出来时会静默跳过"
        "「内存队列为空但磁盘队列有积压 → 消费链路停摆」这条交叉信号，"
        "健康检查于是可能报 HEALTHY。"
        "修法：改绑 `as e` 并 `logger.warning('数据新鲜度检查: 队列深度读取失败，"
        "该交叉信号已跳过: %s', e)`，同时把 `queue_in_memory` 置为 None 而非 0，"
        "避免被当成「内存队列是空的」。"
        "本轮（core 静默失败专项）文件边界明确排除 core/health_checker.py"
        "（该文件刚被其它改动触碰），故仅登记、不修改；"
        "请在后续针对 health_checker 的变更中一并修掉并删除本豁免。"
    ),
}


def _iter_py(skip_dirs: set[str]):
    """遍历仓库里的全部 .py 文件，产出 (相对路径, 绝对路径)。

    Args:
        skip_dirs: 要跳过的目录名集合（按**目录名**匹配，不做路径前缀匹配）。
    """
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
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
    for rel, abs_path in _iter_py(BARE_RULE_SKIP):
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
    for rel, abs_path in _iter_py(BROAD_RULE_SKIP):
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
    files = list(_iter_py(BROAD_RULE_SKIP))
    assert len(files) > 100, f"只扫到 {len(files)} 个文件，跳过集可能写错了"
    rels = {rel for rel, _ in files}
    # 抽查几个必须被扫到的核心模块
    for must in ("run.py", "config.py", "core/module_registry.py",
                 "采集层/data_collector.py", "存储层/database.py"):
        assert must in rels, f"{must} 没有被扫到"


def test_tools_directory_is_scanned():
    """``tools/`` 必须在管辖范围内（round 162 把它从排除集里移除了）。

    回归背景：``tools/`` 曾整目录被排除，结果 11 处 ``except Exception: pass``
    藏在运维脚本里躲过守卫，其中 ``security_scan.py`` 一家占 7 处 ——
    表现是"扫描器读不了文件就静默跳过，然后报告『未发现问题』"。
    这条测试防止有人图省事再把 ``tools/`` 加回排除集。
    """
    files = list(_iter_py(BROAD_RULE_SKIP))
    tools_files = [rel for rel, _ in files if rel.startswith("tools/")]
    assert len(tools_files) >= 15, (
        f"只扫到 {len(tools_files)} 个 tools/ 下的文件，tools/ 可能又被排除了"
    )
    # 这几个是 round 162 实际修过的，必须被扫到
    for must in ("tools/security_scan.py", "tools/deploy.py",
                 "tools/diagnostics.py", "tools/performance_baseline.py",
                 "tools/auto_metrics.py"):
        assert must in tools_files, f"{must} 没有被扫到"


def test_bare_except_rule_covers_tests_and_tools():
    """裸 except 规则要覆盖 tests/ 与 tools/ —— 它的危害不区分生产/测试。

    裸 ``except:`` 会吞掉 ``KeyboardInterrupt`` / ``SystemExit``，导致 Ctrl-C
    杀不掉进程。实测 `测试/实验测试.py` 曾有 3 处，round 162 已清零。
    """
    rels = {rel for rel, _ in _iter_py(BARE_RULE_SKIP)}
    assert any(r.startswith("tools/") for r in rels), "tools/ 未纳入裸 except 扫描"
    assert any(r.startswith("tests/") for r in rels), "tests/ 未纳入裸 except 扫描"
    assert any(r.startswith("测试/") for r in rels), "测试/ 未纳入裸 except 扫描"
    assert not any(r.startswith("legacy/") for r in rels), "legacy/ 归档区不应被扫"


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
