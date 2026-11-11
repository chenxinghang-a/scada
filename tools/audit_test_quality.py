"""审计"测试本身有没有用"：找弱断言、自证式 mock、无条件跳过。

动机：全绿不等于有效。一套只会 `assert x is not None` 的测试，把实现改坏
也照样绿（见 Just et al., FSE 2014：测试强度要用"能否杀死变异"衡量）。

本脚本只做**静态可疑度排序**，不下结论；可疑度高的文件再用
`tools/mutation_spotcheck.py` 做实证（把实现改坏，看测试红不红）。

用法：
    python tools/audit_test_quality.py            # 输出 Top 25
    python tools/audit_test_quality.py --json out.json
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, 'tests')

#: (正则, 权重, 标签, 说明)
RULES = [
    (r'^\s*assert\s+True\s*$', 5, 'assert-true', '恒真断言'),
    (r'^\s*assert\s+[01]\s*$', 5, 'assert-literal', '断言常量'),
    (r'assert\s+\w+\s+is\s+not\s+None\s*$', 2, 'not-none', '只校验非空'),
    (r'assert\s+len\([^)]*\)\s*(>|>=)\s*[01]\s*$', 2, 'len-gt0', '只校验"非空集合"'),
    (r'assert\s+isinstance\([^)]*\)\s*$', 2, 'isinstance', '只校验类型'),
    (r'assert\s+\w+\.assert_called(_once)?(\(\s*\))?\s*$', 3, 'called-only',
     '只校验"被调用"，不校验参数'),
    (r'assert\s+\w+\s*$', 1, 'truthy', '只校验真值'),
    (r'pytest\.raises\(Exception\)', 4, 'raises-broad', '捕获过宽'),
    (r'except\s+Exception[^:]*:\s*$', 3, 'swallow', '测试内吞异常'),
    (r'pytest\.skip\(', 2, 'skip', '运行期跳过'),
    (r'@pytest\.mark\.(skip|xfail)', 4, 'mark-skip', '整条跳过/预期失败'),
    (r'if\s+not\s+\w+:\s*\n\s*pytest\.skip', 3, 'cond-skip', '条件式跳过'),
]

#: 只验证"不抛异常"的测试：整个函数里没有 assert / pytest.raises
EMPTY_BODY = re.compile(r'^\s*(assert\b|pytest\.raises|with pytest\.raises)')


def iter_test_files():
    for name in sorted(os.listdir(TESTS)):
        if name.startswith('test_') and name.endswith('.py'):
            yield os.path.join(TESTS, name)


def split_functions(src: str):
    """按 def test_ 切函数体（够用即可，不追求完整 AST）。"""
    lines = src.splitlines()
    funcs = []
    current = None
    for idx, line in enumerate(lines):
        match = re.match(r'^\s*def (test_\w+)', line)
        if match:
            if current:
                funcs.append(current)
            current = {'name': match.group(1), 'start': idx, 'lines': []}
        elif current is not None:
            current['lines'].append(line)
    if current:
        funcs.append(current)
    return funcs


def audit_file(path: str) -> dict:
    src = io.open(path, encoding='utf-8').read()
    hits = defaultdict(list)
    score = 0
    for idx, line in enumerate(src.splitlines(), 1):
        for pattern, weight, tag, _desc in RULES:
            if re.search(pattern, line):
                hits[tag].append(idx)
                score += weight

    for func in split_functions(src):
        body = '\n'.join(func['lines'])
        if not EMPTY_BODY.search(body) and 'assert' not in body:
            hits['no-assert'].append(func['start'] + 1)
            score += 6

    # 自证式 mock：patch 的正是本测试要验证的模块
    module = os.path.basename(path)[:-3]
    if re.search(r"patch\(\s*['\"]" + re.escape(module) + r"\.", src):
        hits['self-mock'].append(0)
        score += 5

    return {'file': os.path.relpath(path, ROOT).replace('\\', '/'),
            'score': score,
            'hits': {k: len(v) for k, v in sorted(hits.items())},
            'lines': {k: v[:5] for k, v in sorted(hits.items())}}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='测试有效性静态审计')
    parser.add_argument('--json', default=None)
    parser.add_argument('--top', type=int, default=25)
    args = parser.parse_args(argv)

    results = [audit_file(p) for p in iter_test_files()]
    results.sort(key=lambda r: -r['score'])

    tag_totals = defaultdict(int)
    for item in results:
        for tag, num in item['hits'].items():
            tag_totals[tag] += num

    print(f'扫描 {len(results)} 个测试文件')
    print('弱模式总计:', dict(sorted(tag_totals.items(), key=lambda kv: -kv[1])))
    print('\n可疑度 Top:')
    for item in results[:args.top]:
        print(f"  {item['score']:4d}  {item['file']:<58} {item['hits']}")

    if args.json:
        with io.open(args.json, 'w', encoding='utf-8') as handle:
            json.dump(results, handle, ensure_ascii=False, indent=2)
        print('已写入', args.json)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
