"""安全功能追溯检查：每条安全功能必须有实现位置与可复现用例。

这是审核最常问的第一句话：「你怎么证明这条安全需求被实现了、被验证了？」
本脚本不判断"验证是否充分"，只保证**不存在"没有用例的安全功能"**这种低级缺口，
并把 `gap/partial` 如实打印出来 —— 宁可现在难看，也不要在审核现场才发现。

用法：
    python tools/check_traceability.py            # 人读
    python tools/check_traceability.py --strict   # 有 gap/partial 即非 0 退出
"""

from __future__ import annotations

import argparse
import glob
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(ROOT, '文档', '安全功能追溯.yaml')


def _load_yaml(path):
    try:
        import yaml
    except ImportError:
        raise SystemExit('需要 PyYAML：pip install pyyaml')
    with io.open(path, encoding='utf-8') as handle:
        return yaml.safe_load(handle)


def _check_code(spec: str) -> tuple[bool, str]:
    if '::' not in spec:
        return False, 'code 字段必须写成 路径::符号'
    rel, symbol = spec.split('::', 1)
    path = os.path.join(ROOT, rel.replace('/', os.sep))
    if not os.path.isfile(path):
        return False, f'实现文件不存在: {rel}'
    text = io.open(path, encoding='utf-8', errors='replace').read()
    if symbol not in text:
        return False, f'实现位置找不到符号 {symbol!r}'
    return True, ''


def _expand(pattern: str):
    path = os.path.join(ROOT, pattern.replace('/', os.sep))
    if any(ch in pattern for ch in '*?['):
        return [p for p in glob.glob(path) if os.path.isfile(p)]
    return [path] if os.path.isfile(path) else []


def _check_test(spec: str) -> tuple[bool, str]:
    if '::' in spec:
        rel, func = spec.split('::', 1)
        files = [os.path.join(ROOT, rel.replace('/', os.sep))]
    else:
        files = _expand(spec)
        func = None
    if not files:
        return False, f'用例文件不存在: {spec}'
    for path in files:
        text = io.open(path, encoding='utf-8', errors='replace').read()
        if func is None or f'def {func}(' in text:
            return True, ''
    return False, f'用例函数未找到: {spec}'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='安全功能追溯检查')
    parser.add_argument('--strict', action='store_true',
                        help='出现 gap/partial 也返回非 0（用于发布闸门）')
    args = parser.parse_args(argv)

    if not os.path.isfile(REGISTRY):
        print(f'追溯表不存在: {REGISTRY}')
        return 2

    data = _load_yaml(REGISTRY)
    functions = data.get('functions') or []
    print(f'追溯表：{len(functions)} 条安全功能\n')

    errors = 0
    partial = 0
    gaps = 0
    for item in functions:
        fid = item.get('id', '?')
        name = item.get('name', '')
        status = (item.get('status') or 'gap').lower()

        ok_code, why_code = _check_code(item.get('code') or '')
        tests = item.get('tests') or []
        test_errors = []
        if not tests:
            test_errors.append('没有登记任何用例')
        for spec in tests:
            ok, why = _check_test(spec)
            if not ok:
                test_errors.append(why)

        marker = {'verified': 'OK ', 'partial': 'PART', 'gap': 'GAP '}.get(status, 'GAP ')
        print(f'[{marker}] {fid} {name}')
        if not ok_code:
            print(f'        实现: {why_code}')
            errors += 1
        if test_errors:
            for why in test_errors:
                print(f'        用例: {why}')
            errors += 1
        if status == 'gap':
            gaps += 1
        elif status == 'partial':
            partial += 1
        note = (item.get('note') or '').strip()
        if note:
            print(f'        说明: {note}')

    # id 必须连续无缺口（SF-01..SF-NN）。
    #
    # 为什么要单独查这个：按位置切片改 yaml 时，很容易把**夹在中间的一条整段删掉**，
    # 而 status 计数只是"少一条 verified"——看起来跟"某条从 verified 变成 partial"
    # 差不多，不会报警。本轮真的发生过一次：更新 SF-09 时把 SF-10 一起切掉了，
    # 表里只剩 11 条、check 仍报"结构完整"，是靠"提交信息写的数字和实际对不上"
    # 才发现的。这条断言把那种失误变成硬错误。
    import re as _re
    _nums = []
    for item in functions:
        m = _re.fullmatch(r'SF-(\d+)', str(item.get('id') or ''))
        if m:
            _nums.append(int(m.group(1)))
    if _nums:
        _missing = sorted(set(range(1, max(_nums) + 1)) - set(_nums))
        _dup = sorted({n for n in _nums if _nums.count(n) > 1})
        if _missing:
            print('\n  [ID 缺口] 缺少: ' + ', '.join('SF-%02d' % n for n in _missing))
            errors += 1
        if _dup:
            print('\n  [ID 重复] ' + ', '.join('SF-%02d' % n for n in _dup))
            errors += 1

    print(f'\n统计：verified={len(functions) - gaps - partial} '
          f'partial={partial} gap={gaps} 结构错误={errors}')
    if errors:
        print('结论：存在"安全功能没有实现位置或没有用例"的结构缺口 —— 不可发布')
        return 1
    if args.strict and (partial or gaps):
        print('结论：strict 模式下 partial/gap 也算缺口 —— 不可发布')
        return 1
    print('结论：结构完整（partial/gap 的充分性仍需人工评审）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
