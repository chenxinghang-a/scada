"""精确判定「return jsonify(...) 是否带状态码」——按括号配对，支持跨行。

第一版用行内正则，把 `}), 503` 这种跨行写法全判成了"没状态码"，
278 处里大量误报。这里改成：
  1. 找到 `return jsonify(`
  2. 从 `(` 开始做括号配对（跳过字符串里的括号）
  3. 配对结束后看紧跟的是 `, 4xx/5xx` 还是换行/`#`

只统计**真的没有状态码**的调用，再从中筛出 `success: False` 的（潜在假成功）。
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter

ROOTS = ('采集层', '存储层', '智能层', '展示层', '报警层', 'core')
SKIP_DIRS = {'dist', 'build', 'build-scada-1.3.1091', 'dist-scada-1.3.1091',
             'dist-installer', '__pycache__', '.venv', 'node_modules',
             'backup', 'backups', 'tests', '.git'}


def _iter_py():
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if fn.endswith('.py'):
                    yield os.path.join(dirpath, fn)


def _match_paren(text: str, open_idx: int) -> int:
    """从 text[open_idx] == '(' 开始配对，返回对应 ')' 的下标；失败返回 -1。"""
    depth = 0
    i = open_idx
    n = len(text)
    quote = ''
    while i < n:
        ch = text[i]
        if quote:
            if ch == '\\':
                i += 2
                continue
            if ch == quote:
                quote = ''
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            i += 1
            continue
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def main(argv=None) -> int:
    import sys as _sys
    strict = '--strict' in (argv if argv is not None else _sys.argv[1:])
    total_calls = 0
    no_status = []          # 真的没状态码
    risk = []               # 没状态码 + success:False

    for path in _iter_py():
        try:
            text = open(path, encoding='utf-8', errors='ignore').read()
        except OSError:
            continue
        for m in re.finditer(r'return\s+jsonify\s*\(', text):
            total_calls += 1
            open_idx = text.index('(', m.start())
            close_idx = _match_paren(text, open_idx)
            if close_idx < 0:
                continue
            after = text[close_idx + 1: close_idx + 40]
            # 紧跟 `, 200` / `, 400` / `, 503` … 视为有状态码
            has_status = bool(re.match(r'\s*,\s*[1-5]\d\d\b', after))
            if has_status:
                continue
            lineno = text[:m.start()].count('\n') + 1
            body = text[open_idx:close_idx + 1]
            rec = {
                'file': path,
                'line': lineno,
                'has_false': bool(re.search(r"['\"]success['\"]\s*:\s*False", body)),
                'body_head': ' '.join(body.split())[:150],
            }
            no_status.append(rec)
            if rec['has_false']:
                risk.append(rec)

    print(f'jsonify 调用总数: {total_calls}')
    print(f'真的没带状态码:   {len(no_status)}')
    print(f'其中 success:False（潜在假成功）: {len(risk)}\n')

    print('=== 潜在假成功 —— 按文件 ===')
    for f, n in Counter(r['file'] for r in risk).most_common(20):
        print(f'  {n:3}  {f}')

    print('\n=== 明细 ===')
    for r in risk:
        print(f"{r['file']}:{r['line']}")
        print(f"    {r['body_head']}")

    os.makedirs(os.path.join('build', 'audit'), exist_ok=True)
    out = os.path.join('build', 'audit', 'false_success.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump({'total_jsonify': total_calls,
                   'no_status': no_status, 'risk': risk},
                  f, ensure_ascii=False, indent=1)
    print(f'\n明细: {out}')

    if strict:
        if risk:
            print(f'\n[FAIL] 存在 {len(risk)} 处「success:false 却回 200」—— '
                  f'前端只看 response.ok，会把失败当成功')
            return 1
        print('\n[PASS] 没有假成功接口')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
