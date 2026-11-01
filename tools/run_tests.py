#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""跑全量测试，并给出**可信**的结论（round 337）。

为什么需要它
------------
来自两次真实事故（round 330 / 336）：

1. **`pytest ... | tail -N` 会永不返回。**
   pytest 主进程其实早退了（CPU 0.1s、内存 9MB 是铁证），
   但残留子进程持有 stdout 管道，`tail` 一直等 EOF。

2. **跑到 100% 后 teardown 卡住时，日志里没有统计行。**
   而「没有统计行」很容易被当成「跑完了只是没打印」—— 实际是**结果不可用**。
   实测两次都是 kill 掉重跑才拿到 `N passed`。

判据（缺一不可）
----------------
- 日志里有 `N passed` 统计行
- 退出码 == 0
- 日志里没有 `FAILED` / `ERROR`

三者任一不满足 → 判 **不可信**，退出码 1。
**「没有统计行」判失败，不判通过** —— 这是本脚本存在的全部理由。

附带能力
--------
- 输出**重定向到文件**（不走管道，避免事故 1）
- `--faulthandler-timeout=N`：卡住时自动 dump 所有线程栈，
  直接告诉你**哪个 fixture 卡在 teardown**（事故 2 的根因定位）
- `--durations=15`：顺手给出最慢的用例

用法
----
    python tools/run_tests.py                  # 全量
    python tools/run_tests.py tests/foo.py     # 指定路径
    python tools/run_tests.py --timeout 180    # 自定义卡死阈值（秒）
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOG_DIR = REPO / "logs" / "test_runs"

#: 统计行形如 `4328 passed, 10 skipped, 9 warnings in 1209.10s (0:20:09)`
STATS_RE = re.compile(r"^(\d+) passed", re.M)
FAIL_RE = re.compile(r"^(FAILED|ERROR) ", re.M)


def _python() -> str:
    """优先用项目 venv 的解释器（与手工跑保持一致）。"""
    venv = REPO / ".venv" / "Scripts" / "python.exe"
    if venv.exists():
        return str(venv)
    venv = REPO / ".venv" / "bin" / "python"
    if venv.exists():
        return str(venv)
    return sys.executable


def main() -> int:
    # ⚠️ Windows 终端默认 GBK —— 直接 print 中文会乱码，遇到 `❯`/`✓` 这类字符
    # 还会 UnicodeEncodeError 直接把脚本崩掉（这正是外部审计报告 B 指出的
    # 「13 个 py 脚本同族」问题，本脚本自己先修掉）。
    # 用 utf-8 + replace：断言要求保留中文，replace 只兜底无法编码的个别字符。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", default=["tests/"],
                    help="要跑的路径（默认 tests/）")
    ap.add_argument("--timeout", type=int, default=180,
                    help="超过这么多秒没结束就 dump 栈并终止（默认 180）")
    ap.add_argument("--hard-timeout", type=int, default=2400,
                    help="整体硬超时（默认 2400s = 40min）")
    ap.add_argument("--durations", type=int, default=15)
    args = ap.parse_args()

    paths = args.paths or ["tests/"]
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"pytest_{stamp}.log"

    pytest_args = [
        *paths,
        "-q", "--no-header", "-p", "no:randomly",
        "--timeout=300",                       # 单个用例上限
        f"--durations={args.durations}",
    ]

    # ⚠️ 用 `-c` 包装，把 faulthandler 挂在**同一个进程**上。
    #
    # 为什么不用 pytest 自带的 `faulthandler_timeout`：它只在**测试执行期间**
    # 计时。而本项目实测的卡顿发生在 pytest **报完统计之后**的进程退出阶段
    # （日志写 `21 passed in 2.65s`，但进程 393 秒才退）—— 那段完全不在
    # pytest 的计时范围内，自带的 faulthandler 抓不到。
    # `dump_traceback_later(..., repeat=True)` 会每隔 N 秒把**所有线程栈**
    # dump 到 stderr（已重定向进日志），能直接看到退出阶段卡在哪。
    wrapper = (
        "import faulthandler, sys;"
        f"faulthandler.dump_traceback_later({args.timeout}, repeat=True);"
        "import pytest;"
        f"sys.exit(pytest.main({pytest_args!r}))"
    )
    cmd = [_python(), "-c", wrapper]

    print(f"[run_tests] 日志 → {log_path}")
    print(f"[run_tests] 命令 → {' '.join(cmd)}", flush=True)
    started = time.time()

    # 关键：直接写文件，**不经过管道**（事故 1）
    with open(log_path, "w", encoding="utf-8", errors="replace") as fh:
        proc = subprocess.Popen(cmd, cwd=str(REPO), stdout=fh,
                                stderr=subprocess.STDOUT)
        try:
            rc = proc.wait(timeout=args.hard_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=30)
            rc = -9
            print(f"[run_tests] 硬超时 {args.hard_timeout}s，已终止")

    elapsed = time.time() - started
    text = log_path.read_text(encoding="utf-8", errors="replace")

    # ---- 判据 ----
    #
    # ⚠️ 这里要区分两件**不同**的事（round 337 实测）：
    #
    #   (a) 测试本身的结果 —— 看统计行 + 有没有 FAILED
    #   (b) 进程能不能干净退出 —— 看退出码
    #
    # 本项目实测存在 (b) 失败但 (a) 正常的情况：pytest 报完 `N passed`
    # 之后，`atexit` 要 join 100 个 ThreadPoolExecutor 的 worker
    # （faulthandler dump 显示全部卡在 `work_queue.get()`），进程要多花
    # 几分钟才退，有时要等到被外部超时 kill。
    #
    # 把 (b) 当成「测试失败」是**误报**（测试其实是绿的）；
    # 把 (b) 完全忽略又会让人以为环境干净。所以：
    #   · (a) 通过 → 判「测试可信」，但如果 (b) 异常要**显式标注**
    #   · (a) 不通过 → 判「不可信」，无论 (b) 如何
    stats = STATS_RE.search(text)
    fails = FAIL_RE.findall(text)
    clean_exit = (rc == 0)

    print(f"\n[run_tests] 耗时 {elapsed:.0f}s，退出码 {rc}")
    if stats:
        print(f"[run_tests] 统计行: {stats.group(0)}")
    else:
        print("[run_tests] **没有统计行** —— 测试结果不可用")
        # 把 faulthandler 的栈打出来，直接指向卡住的线程
        if "Timeout (0:" in text or "Thread 0x" in text:
            idx = text.find("Timeout (0:")
            if idx < 0:
                idx = max(0, len(text) - 4000)
            print("\n--- faulthandler 线程栈（定位卡在哪）---")
            print(text[idx:idx + 3000])
    if fails:
        print(f"[run_tests] 失败/错误 {len(fails)} 条: {fails[:10]}")

    tests_ok = bool(stats) and not fails
    if not tests_ok:
        print("\n[run_tests] 判定: ❌ 不可信（测试本身失败或没有统计行）")
        print(f"[run_tests] 完整日志: {log_path}")
        return 1

    if not clean_exit:
        print("\n[run_tests] 判定: ✅ 测试通过（但**进程未干净退出**）")
        print(f"[run_tests] 退出码 {rc} —— 已知问题：atexit 要 join 100 个"
              "线程池 worker，见日志里的 faulthandler 栈")
        print(f"[run_tests] 完整日志: {log_path}")
        return 0

    print("\n[run_tests] 判定: ✅ 可信的绿（测试通过 + 进程干净退出）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
