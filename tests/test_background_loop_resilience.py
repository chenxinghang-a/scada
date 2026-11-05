# -*- coding: utf-8 -*-
"""后台循环元测试：`while` 里必须有 try/except，否则一次异常线程静默死亡。

为什么需要（round 374）
------------------------
这类故障在 SCADA 现场极难排查：进程还在、HTTP 还通、登录也正常，
只是**某个循环不再干活**了 ——

* 连接池清理线程被一次健康检查异常打死 → 空闲连接永不回收，池被占满；
* 仿真推进线程被一拍异常打死 → 所有设备数据同时冻住；
* 模拟客户端推送线程异常 → 看板"在线但无数据"；
* 蜂鸣器/闪灯线程异常 → 报警时现场不响不亮。

线程异常不会传播到主线程，Python 只把 traceback 打到 stderr（守护线程
甚至可能被吞掉），所以必须在循环内自己兜住。
"""
from __future__ import annotations

import ast
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules",
    "build", "dist", "legacy", "tests", "测试", "tools",
}

#: 函数名里出现这些词，才当作"后台循环"检查
LOOP_NAME_HINTS = ("loop", "run", "publish", "listen", "poll")

#: 允许没有 try 的循环：(相对路径, 函数名) -> 理由
ALLOWED_WITHOUT_TRY: dict[tuple[str, str], str] = {
    (
        "core/scheduled_tasks.py",
        "_run_loop",
    ): "循环体只有 `Event.wait()` 与 `_execute_task()`；后者内部统一 try/except，"
       "把任务异常记进 last_error 并继续。",
    (
        "报警层/alarm_output.py",
        "_buzzer_pulse_loop",
    ): "`_write_do()` 内部捕获全部异常并返回 False，循环里没有其它可抛点；"
       "写失败已有显式的停机+关 DO 清理路径。",
    (
        "报警层/alarm_output.py",
        "_flash_loop",
    ): "同蜂鸣器：`_write_do()` 自兜底，失败时 stop_flash 并熄灭 DO。",
}


def _iter_py():
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(root, name)


def _collect_unguarded_loops():
    """返回 (违规列表, 命中的豁免键集合)。"""
    violations = []
    used = set()
    for path in _iter_py():
        rel = os.path.relpath(path, REPO_ROOT).replace(os.sep, "/")
        try:
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
        except (OSError, SyntaxError):
            continue

        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            if not any(h in fn.name.lower() for h in LOOP_NAME_HINTS):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.While):
                    continue
                if any(isinstance(inner, ast.Try) for inner in ast.walk(node)):
                    continue
                key = (rel, fn.name)
                if key in ALLOWED_WITHOUT_TRY:
                    used.add(key)
                else:
                    violations.append(f"{rel}:{node.lineno} {fn.name}")
    return violations, used


def test_background_loops_have_exception_guard():
    violations, _ = _collect_unguarded_loops()
    assert not violations, (
        "以下后台循环没有 try/except —— 一次未预期异常就会让线程静默退出，"
        "进程外表正常但功能永久停止：\n  " + "\n  ".join(sorted(violations)) +
        "\n修法：循环体内 `try: ... except Exception: logger.exception(...); continue`。"
    )


def test_allowlist_entries_still_match():
    _, used = _collect_unguarded_loops()
    stale = sorted(set(ALLOWED_WITHOUT_TRY) - used)
    assert not stale, f"以下豁免已不再命中，请删除：{stale}"
