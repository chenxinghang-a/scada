"""轻量变异抽查：检验测试能不能"杀死"人为改坏的代码。

依据：Just et al., "Are Mutants a Valid Substitute for Real Faults in
Software Testing?", FSE 2014 —— 变异分数与真实缺陷检出能力高度相关，
可以用来发现"断言太松/根本没断言"的测试。

为什么自己写而不是装 mutmut/cosmic-ray：
* 仓库零新增依赖，CI 不受影响；
* 只对**指定文件的指定几类运算符**做变异，结果可解释；
* 单文件、单测试跑，几十秒出结论，适合提交前抽查。

用法：
    python tools/mutation_spotcheck.py 采集层/modbus_client.py \\
        tests/test_modbus_client_decode.py --max 12

退出码：0 = 抽查执行完成（哪怕有存活变异）；2 = 参数/环境错误。
"""

from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: (正则, 替换, 标签)。只做"语义相邻"的替换，避免制造不可执行代码。
MUTATIONS: list[tuple[str, str, str]] = [
    (r'==', '!=', 'eq→ne'),
    (r'!=', '==', 'ne→eq'),
    (r'>=', '>', 'ge→gt'),
    (r'<=', '<', 'le→lt'),
    (r'(?<![<>=!])\s>\s', ' >= ', 'gt→ge'),
    (r'(?<![<>=!])\s<\s', ' <= ', 'lt→le'),
    (r'\band\b', 'or', 'and→or'),
    (r'\bor\b', 'and', 'or→and'),
    (r'\bTrue\b', 'False', 'True→False'),
    (r'\bFalse\b', 'True', 'False→True'),
    (r'\bNone\b', '0', 'None→0'),
]


@dataclass
class Mutant:
    line_no: int
    label: str
    original: str
    mutated: str


def _is_code_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith('#'):
        return False
    # 跳过 docstring 分隔行（粗糙但足够：不改变语义、只减少噪声）
    if stripped.startswith(('"""', "'''")):
        return False
    return True


def find_mutants(source: str, max_mutants: int = 20) -> list[Mutant]:
    """在源码里找出可变异点（每个运算符只取第一次出现，保证覆盖面）。"""
    mutants: list[Mutant] = []
    used: set[str] = set()
    for idx, line in enumerate(source.splitlines(), 1):
        if not _is_code_line(line):
            continue
        for pattern, repl, label in MUTATIONS:
            if label in used:
                continue
            new_line, count = re.subn(pattern, repl, line, count=1)
            if count and new_line != line:
                mutants.append(Mutant(idx, label, line, new_line))
                used.add(label)
        if len(mutants) >= max_mutants:
            break
    return mutants


def apply_mutant(source: str, mutant: Mutant) -> str:
    lines = source.splitlines(keepends=True)
    lines[mutant.line_no - 1] = mutant.mutated + (
        '' if mutant.mutated.endswith('\n') else '\n')
    return ''.join(lines)


def run_tests(test_target: str, timeout: int = 120) -> int:
    cmd = [sys.executable, '-m', 'pytest', test_target, '-q', '--tb=no',
           '-p', 'no:cacheprovider', '--timeout=60']
    return subprocess.run(cmd, cwd=ROOT, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=timeout).returncode


def _write_text(path: str, text: str) -> None:
    """原子写：先写临时文件再 replace，避免中断留下半截文件。"""
    tmp = path + '.mut-tmp'
    with io.open(tmp, 'w', encoding='utf-8', newline='') as handle:
        handle.write(text)
    os.replace(tmp, path)


def spotcheck(module_path: str, test_target: str, max_mutants: int = 12) -> dict:
    """执行抽查，返回 {total, killed, survived, timed_out}。原文件保证被还原。"""
    abs_module = module_path if os.path.isabs(module_path) else os.path.join(ROOT, module_path)
    # newline='' 保留原换行：否则 CRLF 文件被读成 LF，还原后整份文件变脏
    with io.open(abs_module, 'r', encoding='utf-8', newline='') as handle:
        original = handle.read()
    mutants = find_mutants(original, max_mutants=max_mutants)
    survived: list[dict] = []
    timed_out: list[dict] = []
    killed = 0
    try:
        for mutant in mutants:
            _write_text(abs_module, apply_mutant(original, mutant))
            try:
                rc = run_tests(test_target)
            except subprocess.TimeoutExpired:
                # 超时不等于"被测试杀死"：单列，否则分数会虚高
                timed_out.append({'line': mutant.line_no, 'label': mutant.label})
                continue
            if rc == 0:
                survived.append({'line': mutant.line_no, 'label': mutant.label,
                                 'code': mutant.original.strip()[:120]})
            else:
                killed += 1
    finally:
        _write_text(abs_module, original)

    effective = len(mutants) - len(timed_out)
    return {'module': module_path, 'total': len(mutants),
            'killed': killed, 'survived': survived, 'timed_out': timed_out,
            'score': (killed / effective) if effective else 0.0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='变异抽查（零依赖）')
    parser.add_argument('module', help='被测模块路径')
    parser.add_argument('tests', help='对应测试文件或 -k 表达式')
    parser.add_argument('--max', type=int, default=12, dest='max_mutants')
    args = parser.parse_args(argv)

    result = spotcheck(args.module, args.tests, max_mutants=args.max_mutants)
    print(f"module={result['module']} mutants={result['total']} "
          f"killed={result['killed']} score={result['score']:.0%}")
    for item in result.get('timed_out', []):
        print(f"  TIMEOUT  L{item['line']:<5} {item['label']:<10}（不计入分数）")
    for item in result['survived']:
        print(f"  SURVIVED L{item['line']:<5} {item['label']:<10} {item['code']}")
    if result['survived']:
        print('存活变异 = 测试没有覆盖到这条语义，建议补断言。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
