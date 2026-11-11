"""可审计的全量测试跑法（替代"按 glob 分块 + 数 summary 行"）。

为什么需要它（"假绿"复盘）：
* 按文件 glob 分块时，**收集数与执行数不一致**不会被发现 —— 少跑几十条，
  汇总行照样是 "N passed"；
* `-q` 汇总行在进程被 OOM/超时杀掉时可能只输出半行，肉眼很容易当成通过；
* skip / xfail / 收集错误混在汇总里，看不出"到底哪些没真跑"。

本脚本的做法：
1. 先用 `--collect-only` 拿到**权威节点清单**（file::test 前缀 → 每文件条数）；
2. 按文件分片，每片输出 JUnit XML（唯一可靠的逐条结果）；
3. 聚合 XML：统计 passed/failed/error/skipped，并按文件比对
   "收集数 vs 执行数"，任何缺失直接判失败；
4. 打印 skip 原因分布与最慢用例，输出退出码（0 = 真全绿）。

用法：
    python tools/run_full_suite.py --shards 6
    python tools/run_full_suite.py --shards 6 --only 采集层     # 只跑名字含该片段的文件
"""

from __future__ import annotations

import argparse
import collections
import glob
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.safe_numeric import try_finite_float      # noqa: E402


def _run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout)


def collect_nodeids() -> tuple[list[str], str]:
    proc = _run([sys.executable, '-m', 'pytest', '--collect-only', '-q',
                 '-p', 'no:cacheprovider'])
    out = proc.stdout + proc.stderr
    nodeids = []
    for line in out.splitlines():
        line = line.strip()
        if '::' in line and not line.startswith(('=', '_', 'rootdir')):
            nodeids.append(line)
    return nodeids, out


def per_file_counts(nodeids: list[str]) -> collections.Counter:
    counter: collections.Counter = collections.Counter()
    for node in nodeids:
        counter[node.split('::', 1)[0]] += 1
    return counter


def shard_files(files: list[str], counts: collections.Counter, shards: int):
    """按用例数做贪心均衡，避免某个分片特别慢。"""
    buckets = [[] for _ in range(shards)]
    load = [0] * shards
    for path in sorted(files, key=lambda p: -counts[p]):
        idx = load.index(min(load))
        buckets[idx].append(path)
        load[idx] += counts[path]
    return [b for b in buckets if b]


def _norm(path: str) -> str:
    """统一成 POSIX 分隔符再比较：pytest 给 `tests/x.py`，normpath 在
    Windows 会给 `tests\\x.py`，直接用会让"收集 vs 执行"永远对不上。"""
    return path.replace('\\', '/')


def _file_for_case(case, known_files: set[str]) -> str:
    file_attr = case.get('file')
    if file_attr:
        return _norm(file_attr)
    classname = case.get('classname') or ''
    best = None
    for path in known_files:
        dotted = path[:-3].replace('\\', '.').replace('/', '.')
        if classname == dotted or classname.startswith(dotted + '.'):
            if best is None or len(dotted) > len(best[0]):
                best = (dotted, path)
    return best[1] if best else (classname or '?')


def parse_junit(path: str, known_files: set[str] | None = None) -> dict:
    tree = ET.parse(path)
    root = tree.getroot()
    suites = [root] if root.tag == 'testsuite' else list(root)
    stats = {'passed': 0, 'failed': 0, 'error': 0, 'skipped': 0, 'xfail': 0,
             'by_file': collections.Counter(), 'skips': collections.Counter(),
             'failures': [], 'slow': []}
    for suite in suites:
        for case in suite.iter('testcase'):
            file_key = _file_for_case(case, known_files or set())
            stats['by_file'][file_key] += 1

            failure = case.find('failure')
            error = case.find('error')
            skipped = case.find('skipped')
            if failure is not None:
                stats['failed'] += 1
                stats['failures'].append(f"{file_key}::{case.get('name')}")
            elif error is not None:
                stats['error'] += 1
                stats['failures'].append(f"{file_key}::{case.get('name')} (error)")
            elif skipped is not None:
                stats['skipped'] += 1
                skip_type = (skipped.get('type') or '').lower()
                if 'xfail' in skip_type:
                    stats['xfail'] = stats.get('xfail', 0) + 1
                reason = (skipped.get('message') or skipped.get('type') or '').strip()
                stats['skips'][reason[:120] or '(无原因)'] += 1
            else:
                stats['passed'] += 1

            duration = case.get('time')
            if duration:
                try:
                    seconds = try_finite_float(duration)
                    if seconds is None:
                        continue
                    stats['slow'].append((seconds, f"{file_key}::{case.get('name')}"))
                except ValueError:
                    pass
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='可审计的全量测试')
    parser.add_argument('--shards', type=int, default=6)
    parser.add_argument('--only', default=None, help='只跑路径含该片段的文件')
    parser.add_argument('--keep', action='store_true', help='保留各分片 XML')
    parser.add_argument('--max-skips', type=int, default=20, dest='max_skips',
                        help='允许的跳过上限（防止"跳过越堆越多"变成假绿）')
    parser.add_argument('--min-collected', type=int, default=4800,
                        dest='min_collected',
                        help='收集数下限（防止测试文件被误删/漏收集）')
    args = parser.parse_args(argv)

    started = time.time()
    nodeids, raw = collect_nodeids()
    if not nodeids:
        print('收集失败，无任何用例：')
        print(raw[-2000:])
        return 2

    counts = per_file_counts(nodeids)
    files = list(counts)
    if args.only:
        files = [f for f in files if args.only in f]
        if not files:
            print(f'没有匹配 {args.only!r} 的测试文件')
            return 2

    print(f'回收站：收集到 {len(nodeids)} 条 / {len(counts)} 个文件；'
          f'本次执行 {sum(counts[f] for f in files)} 条 / {len(files)} 个文件')

    workdir = tempfile.mkdtemp(prefix='scada-full-')
    buckets = shard_files(files, counts, max(1, args.shards))

    total = {'passed': 0, 'failed': 0, 'error': 0, 'skipped': 0, 'xfail': 0}
    by_file_all: collections.Counter = collections.Counter()
    skips_all: collections.Counter = collections.Counter()
    failures: list[str] = []
    slow: list[tuple[float, str]] = []
    shard_times: list[float] = []

    for idx, bucket in enumerate(buckets, 1):
        xml_path = os.path.join(workdir, f'shard{idx}.xml')
        t0 = time.time()
        proc = _run([sys.executable, '-m', 'pytest', *bucket, '-q',
                     '-p', 'no:cacheprovider', f'--junitxml={xml_path}'],
                    timeout=3600)
        shard_times.append(time.time() - t0)
        if proc.returncode not in (0, 1):
            # 2=中断/收集错误，3=内部错误，124 之类都是"没跑完"，不能算通过
            print(f'[shard{idx}] 异常退出 rc={proc.returncode}: '
                  f'{(proc.stdout or proc.stderr)[-800:]}')
            return 3
        if not os.path.exists(xml_path):
            print(f'[shard{idx}] 没有生成 JUnit XML，无法判定结果')
            return 3
        stats = parse_junit(xml_path, set(files))
        for key in total:
            total[key] += stats[key]
        by_file_all.update(stats['by_file'])
        skips_all.update(stats['skips'])
        failures.extend(stats['failures'])
        slow.extend(stats['slow'])
        print(f'[shard{idx}] files={len(bucket)} passed={stats["passed"]} '
              f'failed={stats["failed"]} error={stats["error"]} '
              f'skipped={stats["skipped"]} 用时={shard_times[-1]:.0f}s')

    executed = sum(by_file_all.values())
    expected = sum(counts[f] for f in files)
    missing = {f: counts[f] - by_file_all.get(_norm(f), 0)
               for f in files if counts[f] != by_file_all.get(_norm(f), 0)}

    print('\n==== 汇总 ====')
    print(f'预期执行 {expected} 条，实际执行 {executed} 条')
    print(f'passed={total["passed"]} failed={total["failed"]} '
          f'error={total["error"]} skipped={total["skipped"]} '
          f'xfail={total.get("xfail", 0)} 总耗时={time.time() - started:.0f}s')

    if missing:
        print('\n!! 收集数与执行数不一致（这些用例可能根本没跑）:')
        for path, diff in list(missing.items())[:20]:
            print(f'  {path}: 收集 {counts[path]}，执行 {by_file_all.get(_norm(path), 0)}'
                  f'（差 {diff}）')

    if skips_all:
        print('\n跳过原因分布:')
        for reason, num in skips_all.most_common(10):
            print(f'  {num:3d}  {reason}')

    if failures:
        print('\n失败用例:')
        for item in failures[:20]:
            print('  ', item)

    slow.sort(reverse=True)
    if slow:
        print('\n最慢 5 条:')
        for duration, name in slow[:5]:
            print(f'  {duration:7.2f}s  {name}')

    if not args.keep:
        for path in glob.glob(os.path.join(workdir, '*.xml')):
            try:
                os.remove(path)
            except OSError:
                pass

    policy_errors = []
    if len(nodeids) < args.min_collected:
        policy_errors.append(
            f'收集数 {len(nodeids)} 低于下限 {args.min_collected}（测试被漏收集/误删？）')
    if total['skipped'] > args.max_skips:
        policy_errors.append(
            f'跳过 {total["skipped"]} 条 > 上限 {args.max_skips}（跳过在增长，需逐个说明）')
    if total.get('xfail'):
        policy_errors.append(f'存在 {total["xfail"]} 条 xfail（已知失败不应长期留在主干）')
    for item in policy_errors:
        print('策略告警:', item)

    green = ((not failures) and (not missing) and executed == expected
             and not policy_errors)
    print('\n结论:', '真全绿' if green else '存在缺口/失败，不能算通过')
    return 0 if green else 1


if __name__ == '__main__':
    raise SystemExit(main())
