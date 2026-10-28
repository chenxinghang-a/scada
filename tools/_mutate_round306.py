# -*- coding: utf-8 -*-
"""round 306 变异验证：/metrics 可达 + 旁路走审批。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = REPO / '.venv' / 'Scripts' / 'python.exe'
T = 'tests/test_silent_failures_round306.py'

MUTATIONS = [
    dict(
        name='M1 中间件不放过 /metrics',
        file='展示层/routes.py',
        old="        if path == '/metrics':",
        new='        if False:      # mutated',
        tests=[f'{T}::test_page_auth_middleware_allows_metrics'],
    ),
    dict(
        name='M2 旁路端点退回单人旁路',
        file='展示层/api/api_control.py',
        old=("        request_id = device_control.request_bypass(\n"
             "            rule_id, operator, reason, int(timeout_minutes))"),
        new=("        request_id = 'x' if device_control.bypass_interlock(\n"
             "            rule_id, operator, reason) else ''      # mutated"),
        tests=[f'{T}::test_bypass_goes_through_approval'],
    ),
    dict(
        name='M3 不校验时长范围',
        file='展示层/api/api_control.py',
        old='    if not (1 <= timeout_minutes <= 480):',
        new='    if False:      # mutated',
        tests=[f'{T}::test_bypass_rejects_bad_timeout'],
    ),
    dict(
        name='M4 不校验联锁是否存在',
        file='展示层/api/api_control.py',
        old='    if rule_id not in device_control.get_interlock_status():',
        new='    if False:      # mutated',
        tests=[f'{T}::test_bypass_rejects_unknown_interlock'],
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
            print(f'{m["name"]:36s} {"红(好)" if red else "绿(假绿!)":8s} {tail[0][:50]}')
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
