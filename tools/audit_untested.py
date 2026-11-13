"""找"从没被任何测试引用过"的函数。

## 为什么

前几轮发现两个零测试覆盖的关键逻辑：
  · `simulated_client._estop_override`（急停归零）
  · `data_collector` 的 `dropped_register_short` 计数（短帧丢弃）

它们都是**撞上了才发现**的。这个脚本反过来：先列出所有"测试里从没出现过
名字"的函数，再按风险排（写操作 > 安全逻辑 > 只读查询）。

## 方法

  1. 从生产代码里抽出 `def name(` 和 `def _name(`
  2. 把 tests/ 全部拼成一个大文本
  3. 函数名在其中**一次都没出现** → 零引用
  4. 按模块分组，标出可疑度高的（含 write/stop/delete/estop/save 等动词）

## 局限（必须知道）

  · 名字被间接调用（getattr / 回调注册）时，测试里可能只出现字符串，
    这种情况算"出现过"，不会误报成零引用；
  · 反之，名字恰好是常用词（`run`、`get`）时会被算作"出现过"，会漏报。
    所以阈值定在名字长度 >= 8，太短的不看。
"""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict

PROD_ROOTS = ('采集层', '存储层', '智能层', '展示层', '报警层', 'core')
SKIP_DIRS = {'dist', 'build', 'build-scada-1.3.1091', 'dist-scada-1.3.1091',
             'dist-installer', '__pycache__', '.venv', 'node_modules',
             'backup', 'backups', 'tests', '.git'}

#: 名字里带这些词，说明是"会改变状态"的函数，零覆盖风险更高
RISKY_WORDS = ('write', 'save', 'delete', 'remove', 'stop', 'start', 'estop',
               'bypass', 'update', 'add', 'create', 'reset', 'clear', 'apply',
               'persist', 'commit', 'rollback', 'restore', 'upload')


def _iter_py(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith('.py'):
                yield os.path.join(dirpath, fn)


def _tests_blob() -> str:
    parts = []
    if not os.path.isdir('tests'):
        return ''
    for dirpath, dirnames, filenames in os.walk('tests'):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith('.py'):
                try:
                    parts.append(open(os.path.join(dirpath, fn),
                                      encoding='utf-8', errors='ignore').read())
                except OSError:
                    pass
    return '\n'.join(parts)


def main() -> int:
    blob = _tests_blob()
    if not blob:
        print('没有 tests/ 目录，无法比对')
        return 2

    total = 0
    zero: dict[str, list[str]] = defaultdict(list)
    risky: list[tuple[str, str]] = []

    for root in PROD_ROOTS:
        if not os.path.isdir(root):
            continue
        for path in _iter_py(root):
            try:
                text = open(path, encoding='utf-8', errors='ignore').read()
            except OSError:
                continue
            for m in re.finditer(r'^\s*def\s+(\w+)\s*\(', text, re.MULTILINE):
                name = m.group(1)
                if len(name) < 8:          # 太短的名字比对没有意义
                    continue
                if name.startswith('__'):
                    continue
                total += 1
                if re.search(r'\b' + re.escape(name) + r'\b', blob):
                    continue
                zero[path].append(name)
                if any(w in name.lower() for w in RISKY_WORDS):
                    risky.append((path, name))

    n_zero = sum(len(v) for v in zero.values())
    print(f'生产代码函数（名字>=8字符）: {total}')
    print(f'测试里零引用:                {n_zero}'
          f'（{len(zero)} 个文件）\n')

    print('=== 高风险零覆盖（名字含写/删/停/急停等动词）===')
    if not risky:
        print('  无')
    for path, name in sorted(risky):
        print(f'  {name:38} {path}')

    print('\n=== 零覆盖最多的文件 ===')
    for path, names in sorted(zero.items(), key=lambda kv: -len(kv[1]))[:10]:
        print(f'  {len(names):3}  {path}')
        print(f'       {", ".join(names[:6])}')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
