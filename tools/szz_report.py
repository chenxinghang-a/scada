"""SZZ 风格报告：每个"修复提交"改掉的行，最初是谁写进来的。

依据：Śliwerski, Zimmermann, Zeller, "When Do Changes Induce Fixes?", MSR 2005
（即 SZZ 算法）。用途不是追责，而是回答两个问题：

1. 哪些文件被反复"修了又修"（高复发区，优先做同族复查）？
2. 最近这些修复，改掉的是多久之前引入的代码（引入到发现的间隔）？

实现刻意保持"可解释的最小版本"：
* 只对**删除/修改的行**做 blame（新增行没有"引入者"语义）；
* 用 `git blame -w -L start,+count <sha>^`，取该行在**父提交**里的最后修改者；
* 忽略全 0 的边界行（文件新增）。

用法：
    python tools/szz_report.py --limit 40 --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 默认排除：测试与生成物。修测试往往是在"改期望值"，把它们算进
#: 业务代码的复发统计会掩盖真正的高危文件。
DEFAULT_EXCLUDES = ('tests/', 'api-contract-report.json', '文档/', '.venv/')

HUNK_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@')
ZERO_SHA = re.compile(r'^0+$')


def _git(repo: str, *args: str, check: bool = False) -> str:
    proc = subprocess.run(
        # core.quotepath=false：否则中文路径会被转义成 `\346\212\245...`，
        # 下面 `+++ b/` 的前缀匹配永远命中不了（本仓库一半目录是中文）。
        ['git', '--no-pager', '-c', 'core.quotepath=false', *args], cwd=repo,
        capture_output=True, text=True, encoding='utf-8', errors='replace',
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f'git {" ".join(args)} 失败: {proc.stderr.strip()}')
    return proc.stdout


def is_fix_subject(subject: str) -> bool:
    s = subject.strip().lower()
    return s.startswith(('fix', 'hotfix', 'bugfix')) or '修复' in subject


def list_commits(repo: str, limit: int = 60) -> list[dict[str, str]]:
    out = _git(repo, 'log', f'-{limit}', '--pretty=format:%H%x1f%an%x1f%ad%x1f%s',
               '--date=short')
    commits = []
    for line in out.splitlines():
        parts = line.split('\x1f')
        if len(parts) != 4:
            continue
        commits.append({'sha': parts[0], 'author': parts[1],
                        'date': parts[2], 'subject': parts[3]})
    return commits


def removed_ranges(repo: str, sha: str) -> list[tuple[str, int, int]]:
    """返回该提交删改的行区间 [(path, start, count)]（count=0 表示纯插入，跳过）。"""
    diff = _git(repo, 'show', '--unified=0', '--no-color', '--format=', sha)
    ranges: list[tuple[str, int, int]] = []
    current: str | None = None
    old_path: str | None = None
    for line in diff.splitlines():
        if line.startswith('--- a/'):
            old_path = line[6:].strip()
            continue
        if line.startswith('+++ b/'):
            current = line[6:].strip()
            continue
        if line.startswith('+++ /dev/null'):
            # 整文件删除：blame 必须用**旧路径**，否则父提交里找不到该文件
            current = old_path
            continue
        match = HUNK_RE.match(line)
        if match and current:
            start = int(match.group(1))
            count = int(match.group(2) or 1)
            if count > 0:
                ranges.append((current, start, count))
    return ranges


def blame_range(repo: str, sha: str, path: str, start: int, count: int) -> str | None:
    """在父提交里 blame 该区间，返回引入提交 SHA（多数行一致时取众数）。"""
    out = _git(repo, 'blame', '-w', '-l', '-L', f'{start},+{count}', f'{sha}^', '--', path)
    votes: dict[str, int] = {}
    for line in out.splitlines():
        token = (line.split(' ', 1)[0] if line else '').lstrip('^')
        if len(token) >= 7 and not ZERO_SHA.match(token):
            votes[token] = votes.get(token, 0) + 1
    if not votes:
        return None
    return max(votes.items(), key=lambda kv: kv[1])[0]


def build_report(repo: str = ROOT, limit: int = 40, max_hunks: int = 240,
                 excludes: tuple[str, ...] = DEFAULT_EXCLUDES) -> dict[str, Any]:
    commits = list_commits(repo, limit=limit)
    fixes = [c for c in commits if is_fix_subject(c['subject'])]
    subjects = {c['sha']: c['subject'] for c in commits}
    mappings: list[dict[str, Any]] = []
    by_file: dict[str, int] = {}
    by_introducer: dict[str, int] = {}

    failed = 0
    budget = max_hunks
    for fix in fixes:
        for path, start, count in removed_ranges(repo, fix['sha']):
            if any(part in path for part in excludes):
                continue
            if budget <= 0:
                break
            budget -= 1
            intro = blame_range(repo, fix['sha'], path, start, count)
            if not intro:
                # 重命名/越界/父提交无此文件都会走到这里；计数便于发现"静默漏采"
                failed += 1
                continue
            short = intro[:8]
            # blame 可能给长 sha，而窗口内的 subjects 也用长 sha；
            # 两者不一致时按前缀兜底匹配（git 的 short sha 语义）。
            subject = subjects.get(intro)
            if subject is None:
                subject = next((v for k, v in subjects.items()
                                if k.startswith(short)), '(不在本次窗口内)')
            mappings.append({
                'fix': fix['sha'][:8], 'file': path,
                'lines': f'{start}+{count}', 'introduced_by': short,
                'introduced_subject': subject,
            })
            by_file[path] = by_file.get(path, 0) + 1
            by_introducer[short] = by_introducer.get(short, 0) + 1

    return {
        'generated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'commits_scanned': len(commits),
        'fix_commits': len(fixes),
        'failed_hunks': failed,
        'mappings': mappings,
        'hot_files': sorted(by_file.items(), key=lambda kv: -kv[1])[:10],
        'top_introducers': sorted(by_introducer.items(), key=lambda kv: -kv[1])[:10],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='SZZ 风格缺陷引入者报告')
    parser.add_argument('--repo', default=ROOT)
    parser.add_argument('--limit', type=int, default=40)
    parser.add_argument('--max-hunks', type=int, default=240, dest='max_hunks')
    parser.add_argument('--exclude', action='append', default=None,
                        help='额外排除的路径片段（可多次）')
    parser.add_argument('--json', default=None, help='输出 JSON 路径')
    args = parser.parse_args(argv)

    excludes = tuple(DEFAULT_EXCLUDES) + tuple(args.exclude or ())
    report = build_report(args.repo, limit=args.limit,
                          max_hunks=args.max_hunks, excludes=excludes)
    print(f"扫描 {report['commits_scanned']} 个提交，其中修复类 {report['fix_commits']} 个")
    print('高复发文件:')
    for path, n in report['hot_files']:
        print(f'  {n:3d}  {path}')
    print('最常被回修的引入提交:')
    for sha, n in report['top_introducers']:
        print(f'  {n:3d}  {sha}')

    if args.json:
        with open(args.json, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print('已写入', args.json)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
