# -*- coding: utf-8 -*-
"""round 300 变异验证：每项修复都要有「改回去就变红」的反向对照。

用法：.venv/Scripts/python.exe tools/_mutate_round300.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = REPO / '.venv' / 'Scripts' / 'python.exe'
T = 'tests/test_silent_failures_round300.py'

MUTATIONS = [
    dict(
        name='M1 SPC 不对齐子组边界（旧 base 算法）',
        file='智能层/spc_analyzer.py',
        old=("        dropped = max(0, fed - len(raw_data))       # 已被挤出的点数\n"
             "        pad = (-dropped) % subgroup_size            # 让 aligned[0] 落在子组边界\n"
             "        aligned = raw_data[pad:] if pad else raw_data\n"
             "        base = (dropped + pad) // subgroup_size     # 首个子组的绝对序号\n"
             "        return aligned, base"),
        new=("        return raw_data, max(0, fed - (len(raw_data) // subgroup_size))"),
        tests=[f'{T}::test_align_window_keeps_absolute_subgroup_identity',
               f'{T}::test_align_window_drops_partial_head'],
    ),
    dict(
        name='M1b 指纹不重复计入（滑窗后不许重入）',
        file='智能层/spc_analyzer.py',
        old="        return aligned, base",
        new=("        return aligned, max(0, fed - (len(raw_data) // subgroup_size))"),
        tests=[f'{T}::test_violations_not_flooded_while_window_slides'],
    ),
    dict(
        name='M2 未注册回调不拦（放行到分支）',
        file='智能层/edge_decision.py',
        old="        if not self._action_callbacks.get(callback_key):",
        new="        if False:      # mutated",
        tests=[f'{T}::test_unregistered_action_is_logged_as_failure'],
    ),
    dict(
        name='M3 PID 用回墙钟',
        file='智能层/edge_decision.py',
        old=("        # round 300：与 prev_time 同源，必须都用单调时钟（理由见 add_pid_controller）\n"
             "        now = time.monotonic()"),
        new="        now = time.time()",
        tests=[f'{T}::test_pid_uses_monotonic_clock'],
    ),
    dict(
        name='M4 PID 不设 dt 上限',
        file='智能层/edge_decision.py',
        old="        if dt <= 0 or dt > self._max_pid_dt:",
        new="        if dt <= 0:",
        tests=[f'{T}::test_pid_rejects_absurd_dt'],
    ),
    dict(
        name='M5 写盘返回值又被丢掉（AST 守卫）',
        file='展示层/api/api_alarms.py',
        old=("    if not save_yaml_config('配置/alarms.yaml', config):\n"
             "        return api_error('保存报警配置失败', 500)"),
        new="    save_yaml_config('配置/alarms.yaml', config)",
        occurrence=1,          # 只改第一处：其余 5 处仍正确，守卫必须照样抓到
        tests=[f'{T}::test_every_save_yaml_config_checks_return_value'],
    ),
    dict(
        name='M6 写盘失败仍回成功（不改运行时即可）',
        file='展示层/api/api_alarms.py',
        old=("    if not save_yaml_config('配置/alarms.yaml', config):\n"
             "        return api_error('保存报警配置失败', 500)"),
        new=("    if not save_yaml_config('配置/alarms.yaml', config):\n"
             "        pass"),
        occurrence=1,
        tests=[f'{T}::test_save_failure_returns_500_and_skips_runtime'],
    ),
]


def main() -> int:
    survivors = []
    print(f'{"变异":44s} {"结果":8s} 说明')
    print('-' * 96)
    for m in MUTATIONS:
        path = REPO / m['file']
        original = path.read_text(encoding='utf-8')
        hits = original.count(m['old'])
        want = m.get('occurrence')
        if (want is None and hits != 1) or (want is not None and hits < want):
            print(f'{m["name"]:44s} {"SKIP":8s} 锚点命中 {hits} 次（期望 {want or 1}）')
            survivors.append(m['name'] + ' (锚点问题)')
            continue
        try:
            if want is None:
                mutated = original.replace(m['old'], m['new'], 1)
            else:
                idx = -1
                for _ in range(want):
                    idx = original.index(m['old'], idx + 1)
                mutated = (original[:idx] + m['new']
                           + original[idx + len(m['old']):])
            path.write_text(mutated, encoding='utf-8')
            r = subprocess.run(
                [str(PY), '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *m['tests']],
                cwd=REPO, capture_output=True, text=True, encoding='utf-8', errors='replace')
            red = r.returncode != 0
            tail = [ln for ln in r.stdout.strip().splitlines() if ln.strip()][-1:] or ['']
            print(f'{m["name"]:44s} {"红(好)" if red else "绿(假绿!)":8s} {tail[0][:56]}')
            if not red:
                survivors.append(m['name'])
        finally:
            path.write_text(original, encoding='utf-8')

    print('-' * 96)
    if survivors:
        print(f'!!! {len(survivors)} 个变异存活：')
        for s in survivors:
            print('  -', s)
        return 1
    print(f'全部 {len(MUTATIONS)} 个变异被抓住，测试集有效。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
