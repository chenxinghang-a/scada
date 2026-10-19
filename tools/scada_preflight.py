#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""开工前置检查（清单 P2-6）—— 一条命令核仓库实况。

背景：硬规则「开工先核仓库实况」原来靠人记得。这个脚本把它变成一条命令：

    python tools/scada_preflight.py

输出 PASS / WARN / FAIL 三类。有 FAIL 时退出码为 1，便于将来接 CI 或 git hook。
只做**只读**检查，不修改任何文件、不 fetch 网络（除 git 读远端引用时用本地缓存）。

注意：前端的版本 lockstep 在**另一个仓库**里校验（`verify:version`），
本脚本只核本仓（后端）的实况。
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
results: list[tuple[str, str, str]] = []


def add(level: str, title: str, detail: str = "") -> None:
    results.append((level, title, detail))


def run(args: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        return p.returncode, (p.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001
        return 127, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- 1. 分支与同步
rc, branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
if rc != 0:
    add(FAIL, "git 仓库可读", branch[:80])
    branch = ""
else:
    add(PASS, "当前分支", branch)

if branch and branch != "HEAD":
    rc, ab = run(["git", "rev-list", "--left-right", "--count", f"origin/{branch}...HEAD"])
    if rc == 0 and ab:
        parts = ab.split()
        if len(parts) == 2:
            behind, ahead = parts[0], parts[1]
            if behind == "0" and ahead == "0":
                add(PASS, "与远端同步", f"origin/{branch} 一致")
            elif behind != "0":
                add(FAIL, "落后远端", f"{behind} 个提交未合并，先 pull")
            else:
                add(WARN, "有未推送提交", f"领先 origin/{branch} {ahead} 个")
    else:
        add(WARN, "无远端跟踪分支", f"origin/{branch} 不存在？")

# ---------------------------------------------------------------- 2. 工作区
rc, st = run(["git", "status", "--porcelain"])
if rc == 0:
    lines = [l for l in st.splitlines() if l.strip()]
    if not lines:
        add(PASS, "工作区干净", "")
    else:
        staged = sum(1 for l in lines if l[:2].strip() and l[0] not in " ?")
        untracked = sum(1 for l in lines if l.startswith("??"))
        add(FAIL, "工作区有未提交改动",
            f"共 {len(lines)} 项（已暂存 {staged}，未跟踪 {untracked}）"
            " —— 别人可能正在改，先 git status 看清再动手")

# ---------------------------------------------------------------- 3. VERSION
ver_file = ROOT / "VERSION"
if ver_file.exists():
    version = ver_file.read_text(encoding="utf-8").strip()
    add(PASS, "VERSION", version)
else:
    version = ""
    add(FAIL, "VERSION 缺失", "版本唯一源不见了")

# ---------------------------------------------------------------- 4. 测试临时目录积压
tmp_dirs = glob.glob(str(ROOT / ".pytest_tmp*"))
if not tmp_dirs:
    add(PASS, ".pytest_tmp 残留", "0 个")
elif len(tmp_dirs) <= 10:
    add(PASS, ".pytest_tmp 残留", f"{len(tmp_dirs)} 个（保留策略上限内）")
else:
    add(WARN, ".pytest_tmp 残留积压",
        f"{len(tmp_dirs)} 个 —— 保留策略每次只清 10 个，"
        "积压要靠外部脚本一次性扫（见 round 216 报告）")

# ---------------------------------------------------------------- 5. 敏感文件是否被忽略
for name in (".env", ".env.local"):
    p = ROOT / name
    if not p.exists():
        continue
    rc, _ = run(["git", "check-ignore", "-q", name])
    if rc == 0:
        add(PASS, f"{name} 已被 gitignore", "")
    else:
        add(FAIL, f"{name} 未忽略", "密钥可能被提交，立刻加进 .gitignore")

# ---------------------------------------------------------------- 6. stash / 未合并分支
rc, stash = run(["git", "stash", "list"])
if rc == 0:
    n = len([l for l in stash.splitlines() if l.strip()])
    add(PASS if n == 0 else WARN, "stash",
        "无" if n == 0 else f"{n} 条未处理 —— 可能有人做到一半")

rc, heads = run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"])
if rc == 0:
    strays = []
    for b in [x for x in heads.splitlines() if x.strip() and x.strip() != branch]:
        rc2, cnt = run(["git", "rev-list", "--count", f"origin/main..{b}"])
        if rc2 == 0 and cnt.strip() not in ("", "0"):
            strays.append(f"{b}(+{cnt.strip()})")
    add(PASS if not strays else WARN, "其它分支",
        "无未合并" if not strays else "未合并且领先 main: " + ", ".join(strays))

# ---------------------------------------------------------------- 7. 最近提交
rc, log = run(["git", "log", "--oneline", "-5"])
if rc == 0 and log:
    add(PASS, "最近 5 条提交", "\n" + "\n".join("        " + l for l in log.splitlines()))

# ---------------------------------------------------------------- 8. 已安装构建（可选）
launch = Path(os.environ.get("USERPROFILE", "")) / ".workbuddy" / "last-launch.json"
add(PASS, "（参考）本仓为后端源码", f"版本源 VERSION={version}；前端 lockstep 在另一仓校验")

# ---------------------------------------------------------------- 输出
print("=" * 66)
print(" SCADA 开工前置检查（清单 P2-6）")
print("=" * 66)
for level, title, detail in results:
    mark = {"PASS": "[ OK ]", "WARN": "[WARN]", "FAIL": "[FAIL]"}[level]
    print(f" {mark} {title}" + (f"  ->  {detail}" if detail else ""))

n_fail = sum(1 for r in results if r[0] == FAIL)
n_warn = sum(1 for r in results if r[0] == WARN)
print("-" * 66)
print(f" 合计: PASS {len(results) - n_fail - n_warn} / WARN {n_warn} / FAIL {n_fail}")
if n_fail:
    print(" 结论: 有 FAIL，先处理再动手。")
    sys.exit(1)
print(" 结论: 可以开工。")
