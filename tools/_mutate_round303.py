# -*- coding: utf-8 -*-
"""round 303 变异验证：五项修复逐项回退。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = REPO / '.venv' / 'Scripts' / 'python.exe'
T = 'tests/test_silent_failures_round303.py'

MUTATIONS = [
    dict(
        name='M1 拿锁后不复核（直接复用旧结论）',
        file='智能层/device_control.py',
        old='            recheck = self.validate_write(device_id, address, value, operator)',
        new='            recheck = validation      # mutated：等价于不复核',
        tests=[f'{T}::test_write_rechecked_after_acquiring_lock'],
    ),
    dict(
        name='M2 刷新只清界面那份计数',
        file='智能层/device_control.py',
        old=("                    self._comm_failures[device_id] = 0\n"
             "                    health['consecutive_failures'] = 0\n"
             "                    # 恢复健康后允许下一次掉线重新降级\n"
             "                    self._degraded_devices.discard(device_id)"),
        new=("                    health['consecutive_failures'] = 0      # mutated"),
        tests=[f'{T}::test_refresh_health_syncs_failure_counter'],
    ),
    dict(
        name='M3 降级不在边沿（每次失败都跑）',
        file='智能层/device_control.py',
        old='                if device_id not in self._degraded_devices:',
        new='                if True:      # mutated',
        tests=[f'{T}::test_degradation_only_on_threshold_crossing'],
    ),
    dict(
        name='M4 初始失败次数写死 3',
        file='智能层/device_control.py',
        old="                    'consecutive_failures': self._comm_failures.get(device_id, 0),",
        new="                    'consecutive_failures': 0 if connected else 3,      # mutated",
        tests=[f'{T}::test_init_health_uses_single_source'],
    ),
    dict(
        name='M5 trigger_count 又被采样点刷',
        file='报警层/alarm_engine.py',
        old="        'sample_count': int(state.get('sample_count') or 0) + 1,",
        new="        'trigger_count': int(state.get('trigger_count') or 0) + 1,      # mutated",
        tests=[f'{T}::test_active_alarm_trigger_count_is_not_sample_count'],
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
