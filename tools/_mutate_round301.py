# -*- coding: utf-8 -*-
"""round 301 变异验证。

M1 产品代码：`_safe_intel` 不计数 —— 失败次数统计形同虚设。
M2 守卫自身：扫描器扫不到任何文件 —— 四条守卫断言必须因此变红，
   否则它们就是「空集合上恒真」的一类空测（本轮主题的自我应用）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = REPO / '.venv' / 'Scripts' / 'python.exe'

MUTATIONS = [
    dict(
        name='M1 _safe_intel 不计数',
        file='采集层/data_collector.py',
        old="            failures[module_name] = failures.get(module_name, 0) + 1",
        new='            pass  # mutated',
        tests=['tests/test_silent_failures_round301.py'],
    ),
    dict(
        name='M2 扫描器扫不到文件（守卫必须自曝）',
        file='tools/_scan_empty_asserts.py',
        old="    for path in sorted(tests_dir.glob('*.py')):",
        new='    for path in []:      # mutated',
        tests=['tests/test_empty_assert_guard.py'],
    ),
]


def main() -> int:
    survivors = []
    print(f'{"变异":36s} {"结果":8s} 说明')
    print('-' * 88)
    for m in MUTATIONS:
        path = REPO / m['file']
        original = path.read_text(encoding='utf-8')
        hits = original.count(m['old'])
        if hits != 1:
            print(f'{m["name"]:36s} {"SKIP":8s} 锚点命中 {hits} 次（期望 1）')
            survivors.append(m['name'] + ' (锚点问题)')
            continue
        try:
            path.write_text(original.replace(m['old'], m['new'], 1), encoding='utf-8')
            r = subprocess.run(
                [str(PY), '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *m['tests']],
                cwd=REPO, capture_output=True, text=True, encoding='utf-8', errors='replace')
            red = r.returncode != 0
            tail = [ln for ln in r.stdout.strip().splitlines() if ln.strip()][-1:] or ['']
            print(f'{m["name"]:36s} {"红(好)" if red else "绿(假绿!)":8s} {tail[0][:52]}')
            if not red:
                survivors.append(m['name'])
        finally:
            path.write_text(original, encoding='utf-8')

    print('-' * 88)
    if survivors:
        print(f'!!! {len(survivors)} 个变异存活：')
        for s in survivors:
            print('  -', s)
        return 1
    print(f'全部 {len(MUTATIONS)} 个变异被抓住，测试集有效。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
