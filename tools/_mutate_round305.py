# -*- coding: utf-8 -*-
"""round 305 变异验证：缓存与死区落盘。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = REPO / '.venv' / 'Scripts' / 'python.exe'
T = 'tests/test_silent_failures_round305.py'

MUTATIONS = [
    dict(
        name='M1 缓存不生效（每次重算）',
        file='存储层/database.py',
        old=("        if not force:\n"
             "            with self._dbstats_lock:"),
        new=("        if False:      # mutated\n"
             "            with self._dbstats_lock:"),
        tests=[f'{T}::test_stats_cached_within_ttl'],
    ),
    dict(
        name='M2 force 被忽略（永远走缓存）',
        file='存储层/database.py',
        old=("        if not force:\n"
             "            with self._dbstats_lock:"),
        new=("        if True:      # mutated\n"
             "            with self._dbstats_lock:"),
        tests=[f'{T}::test_stats_force_bypasses_cache'],
    ),
    dict(
        name='M3 缓存永不过期',
        file='存储层/database.py',
        old="            if cached is not None and (time.monotonic() - cached_at) < DB_STATS_CACHE_SECONDS:",
        new="            if cached is not None and (time.monotonic() - cached_at) < 1e9:      # mutated",
        tests=[f'{T}::test_stats_recomputed_after_ttl'],
    ),
    dict(
        name='M4 死区不写进规则本体（存不下来）',
        file='报警层/alarm_manager.py',
        old="            self.rules[rule_id]['deadband'] = deadband_value",
        new='            pass      # mutated',
        tests=[f'{T}::test_set_deadband_persists_across_reload'],
    ),
    dict(
        name='M5 落盘失败不回滚',
        file='报警层/alarm_manager.py',
        old=("        if not self._save_config():\n"
             "            with self._state_lock:\n"
             "                if previous is None:\n"
             "                    self._deadbands.pop(rule_id, None)"),
        new=("        if False:      # mutated\n"
             "            with self._state_lock:\n"
             "                if previous is None:\n"
             "                    self._deadbands.pop(rule_id, None)"),
        tests=[f'{T}::test_set_deadband_rolls_back_on_save_failure'],
    ),
    dict(
        name='M6 不校验规则存在',
        file='报警层/alarm_manager.py',
        old='        if rule_id not in self.rules:',
        new='        if False:      # mutated',
        tests=[f'{T}::test_set_deadband_rejects_unknown_rule'],
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
