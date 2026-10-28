# -*- coding: utf-8 -*-
"""round 304 变异验证：规则表生命周期的三项修复逐项回退。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = REPO / '.venv' / 'Scripts' / 'python.exe'
T = 'tests/test_silent_failures_round304.py'

MUTATIONS = [
    dict(
        name='M1 重载只增不删（update 而非替换）',
        file='报警层/alarm_manager.py',
        old='                self.rules = new_rules',
        new='                self.rules.update(new_rules)      # mutated',
        tests=[f'{T}::test_reload_removes_deleted_rules'],
    ),
    dict(
        name='M2 外部事件又写进配置规则表',
        file='报警层/alarm_manager.py',
        old=("            if alarm_id not in self._external_alarms:\n"
             "                self._external_alarms[alarm_id] = {"),
        new=("            if alarm_id not in self.rules:      # mutated\n"
             "                self.rules[alarm_id] = {"),
        tests=[f'{T}::test_external_alarm_does_not_pollute_rules'],
    ),
    dict(
        name='M3 外部事件描述只增不减',
        file='报警层/alarm_manager.py',
        old='                self._external_alarms.pop(alarm_id, None)',
        new='                pass      # mutated',
        tests=[f'{T}::test_clear_external_alarm_reclaims_description'],
    ),
    dict(
        name='M4 索引 clear 后重建（复用同一个 dict）',
        file='报警层/alarm_manager.py',
        old='        new_index: dict[tuple, list] = {}',
        new='        new_index: dict[tuple, list] = self._rules_index      # mutated',
        tests=[f'{T}::test_index_rebuild_replaces_instead_of_clearing'],
    ),
]


def main() -> int:
    survivors = []
    print(f'{"变异":40s} {"结果":8s} 说明')
    print('-' * 92)
    for m in MUTATIONS:
        path = REPO / m['file']
        original = path.read_text(encoding='utf-8')
        hits = original.count(m['old'])
        if hits != 1:
            print(f'{m["name"]:40s} {"SKIP":8s} 锚点命中 {hits} 次（期望 1）')
            survivors.append(m['name'] + ' (锚点问题)')
            continue
        try:
            path.write_text(original.replace(m['old'], m['new'], 1), encoding='utf-8')
            r = subprocess.run(
                [str(PY), '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *m['tests']],
                cwd=REPO, capture_output=True, text=True, encoding='utf-8', errors='replace')
            red = r.returncode != 0
            tail = [ln for ln in r.stdout.strip().splitlines() if ln.strip()][-1:] or ['']
            print(f'{m["name"]:40s} {"红(好)" if red else "绿(假绿!)":8s} {tail[0][:52]}')
            if not red:
                survivors.append(m['name'])
        finally:
            path.write_text(original, encoding='utf-8')

    print('-' * 92)
    if survivors:
        print(f'!!! {len(survivors)} 个变异存活：')
        for s in survivors:
            print('  -', s)
        return 1
    print(f'全部 {len(MUTATIONS)} 个变异被抓住，测试集有效。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
