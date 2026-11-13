"""把"静默失败"的角落扫出来并量化。

## 为什么写这个

前面几轮修的都是"撞上了才看见"的缺陷：改密假成功、禁用的联锁、注释被洗。
但这些都是**个案**，修完不知道还剩多少 —— 用户说"就动皮毛"是对的。

这个脚本不猜模式，直接按**语法特征**穷举六类高风险写法，给出总量和分布，
再决定先修哪一档。

## 六类特征（都是"失败却看不出来"）

  A  `except ...: pass`                    —— 异常彻底消失，连日志都没有
  B  `except` 块里 `return True/成功`       —— 失败被当成功返回
  C  `return jsonify(...)` 不带状态码       —— success:false 但 HTTP 200
  D  前端 `.catch(() => {})` / 空 catch     —— 接口错了界面不知道
  E  `except` 里只有 logger、没有 re-raise  —— 吞掉但至少留痕（低危）
  F  `if not xxx: pass` / 空分支            —— 检查了但什么都不做

每类给出：命中数、按文件分布、前若干条原文行号，方便逐个核实。
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter

#: except:pass 的预算。都是「清理临时文件/释放锁」这类无关紧要的失败，
#: 允许存在，但要盯住总量 —— 涨上去通常意味着新代码在吞异常。
A_PASS_BUDGET = 12

ROOTS = ('采集层', '存储层', '智能层', '展示层', '报警层', 'core', '静态资源')
SKIP_DIRS = {'dist', 'build', 'build-scada-1.3.1091', 'dist-scada-1.3.1091',
             'dist-installer', '__pycache__', '.venv', 'node_modules',
             'backup', 'backups', 'tests', '.git'}

CATS = {
    'A_except_pass': ('except 里直接 pass（异常彻底消失）', re.compile(
        r'except[^\n]*:\s*\n\s*pass\s*(?:#.*)?\n', re.MULTILINE)),
    'B_except_return_true': ('except 里返回成功（失败当成功）', re.compile(
        r'except[^\n]*:\s*\n(?:\s*(?:#.*\n|\s*\n))*\s*return\s+True\b', re.MULTILINE)),
    'C_no_status_code': ('return jsonify 不带状态码（可能假成功）',
                         re.compile(r'return jsonify\(')),
    'D_js_empty_catch': ('前端空 catch（接口错了界面不知道）', re.compile(
        r'\.catch\(\s*(?:\(\s*\w*\s*\)|function\s*\(\s*\w*\s*\))?\s*=>?\s*\{\s*\}'
        r'|\.catch\(\s*\(\s*\w*\s*\)\s*=>\s*\{\s*\}\s*\)')),
    'E_log_only_except': ('except 里只 log 不抛出', re.compile(
        r'except[^\n]*:\s*\n\s*logger\.(?:debug|info|warning|error)\(', re.MULTILINE)),
    'F_empty_branch': ('if 分支里什么都不做', re.compile(
        r'if[^\n]*:\s*\n\s*pass\s*\n', re.MULTILINE)),
}


def _strip_js_line_comments(text: str) -> str:
    """去掉 JS 行首注释，但**保留字符位置与换行**（行号要对得上）。

    为什么必须做：修复 `.catch(() => {})` 时在注释里引用了原代码，
    下一轮扫描立刻把这段注释又判成"空 catch" —— 自己修完自己，闸门永远红。
    只处理行首 `//`（前面只有空白），避免把 `http://` 这类误伤。
    """
    return re.sub(r'^(\s*)//[^\n]*',
                  lambda m: m.group(1) + ' ' * (len(m.group(0)) - len(m.group(1))),
                  text, flags=re.MULTILINE)


def _iter_files():
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if fn.endswith(('.py', '.js')):
                    yield os.path.join(dirpath, fn)


def main(argv=None) -> int:
    import sys as _sys
    strict = '--strict' in (argv if argv is not None else _sys.argv[1:])
    hits: dict[str, list] = {k: [] for k in CATS}
    total_files = 0

    for path in _iter_files():
        total_files += 1
        try:
            with open(path, encoding='utf-8', errors='ignore') as f:
                text = f.read()
        except OSError:
            continue
        if path.endswith('.js'):
            text = _strip_js_line_comments(text)
        lines = text.splitlines()
        for key, (_desc, pat) in CATS.items():
            for m in pat.finditer(text):
                lineno = text[:m.start()].count('\n') + 1
                snippet = lines[lineno - 1].strip()[:110] if lineno <= len(lines) else ''
                hits[key].append({'file': path, 'line': lineno, 'text': snippet})

    print(f'扫描 {total_files} 个文件\n')
    print(f'{"类别":34} {"命中":>5}  {"涉及文件":>6}')
    print('-' * 62)
    summary = []
    for key, (desc, _p) in CATS.items():
        rows = hits[key]
        nfiles = len({r['file'] for r in rows})
        print(f'{key:34} {len(rows):5}  {nfiles:6}')
        summary.append({'cat': key, 'desc': desc, 'count': len(rows),
                        'files': nfiles})

    print('\n===== 各档 Top 文件 =====')
    for key in CATS:
        rows = hits[key]
        if not rows:
            continue
        c = Counter(r['file'] for r in rows)
        print(f'\n[{key}] {CATS[key][0]}  —— 共 {len(rows)} 处')
        for f, n in c.most_common(5):
            print(f'   {n:4}  {f}')
        for r in rows[:3]:
            print(f'        L{r["line"]:<5} {r["text"]}')

    os.makedirs(os.path.join('build', 'audit'), exist_ok=True)
    out = os.path.join('build', 'audit', 'swallowed.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump({'summary': summary, 'hits': hits}, f, ensure_ascii=False, indent=1)
    print(f'\n明细: {out}')

    if strict:
        # B 类（except 里返回成功）是硬红线：任何一处都意味着"失败被当成功"。
        # A 类（except: pass）允许少量 —— 清理临时文件、释放锁这类失败无关紧要，
        # 但总量要盯住，涨上去通常意味着新代码在吞异常。
        b = len(hits['B_except_return_true'])
        a = len(hits['A_except_pass'])
        d = len(hits['D_js_empty_catch'])
        failed = False
        if d:
            print(f'\n[FAIL] {d} 处前端空 catch（接口失败界面不知道）')
            failed = True
        if b:
            print(f'\n[FAIL] {b} 处 except 里返回成功（失败被当成功）')
            failed = True
        if a > A_PASS_BUDGET:
            print(f'\n[FAIL] except:pass {a} 处，超过预算 {A_PASS_BUDGET}')
            failed = True
        if not failed:
            print(f'\n[PASS] except 返回成功 0 处；前端空 catch 0 处；'
                  f'except:pass {a} 处（预算 {A_PASS_BUDGET}）')
        return 1 if failed else 0
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
