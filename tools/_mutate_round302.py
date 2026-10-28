# -*- coding: utf-8 -*-
"""round 302 变异验证：六组修复逐项回退，测试必须变红。

用法：.venv/Scripts/python.exe tools/_mutate_round302.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = REPO / '.venv' / 'Scripts' / 'python.exe'
T = 'tests/test_silent_failures_round302.py'

MUTATIONS = [
    dict(
        name='M1 消音不写 DO（恢复恒 True）',
        file='报警层/alarm_output.py',
        old='        ok = self._write_all_do(red=red, yellow=yellow, green=green, buzzer=False)',
        new='        ok = True      # mutated',
        tests=[f'{T}::test_acknowledge_writes_buzzer_off',
               f'{T}::test_acknowledge_reports_do_failure'],
    ),
    dict(
        name='M2 手动控制丢 DO 结果',
        file='报警层/alarm_output.py',
        old="        ok = self._write_all_do(r, y, g, b)",
        new="        self._write_all_do(r, y, g, b); ok = True      # mutated",
        tests=[f'{T}::test_manual_control_reports_do_failure'],
    ),
    dict(
        name='M3 落库异常继续往外抛',
        file='报警层/alarm_manager.py',
        old="            self._record_output_failure('database', f'报警落库失败: {e}')",
        new="            self._record_output_failure('database', f'报警落库失败: {e}')\n            raise      # mutated",
        tests=[f'{T}::test_persist_failure_does_not_stop_annunciation'],
    ),
    dict(
        name='M4 规则异常带走后续规则',
        file='报警层/alarm_manager.py',
        old=("                self._record_output_failure(\n"
             "                    'alarm_engine', f'规则 {rule_id} 判定异常: {e}')"),
        new="                raise      # mutated",
        tests=[f'{T}::test_one_bad_rule_does_not_stop_others'],
    ),
    dict(
        name='M5 旁路清理不执行（回到 return 之后的效果）',
        file='报警层/alarm_manager.py',
        old='        if now_ts - self._last_shelf_cleanup > 60:',
        new='        if False:      # mutated',
        tests=[f'{T}::test_expired_shelf_cleaned_without_matching_rules'],
    ),
    dict(
        name='M6 联锁先置位再执行（且动作在锁内）',
        file='智能层/device_control.py',
        old='                    to_execute.append((rule_id, rule))',
        new=("                    self._interlock_states[rule_id] = True      # mutated\n"
             "                    self._execute_interlock_action(rule)"),
        tests=[f'{T}::test_interlock_action_failure_keeps_state_and_retries',
               f'{T}::test_interlock_status_not_blocked_by_slow_io'],
    ),
    dict(
        name='M7 模拟模式不看运行时结果',
        file='展示层/api/api_system.py',
        old="    if not result.get('success', True):",
        new="    if False:      # mutated",
        tests=[f'{T}::test_simulation_switch_rejection_does_not_persist'],
    ),
    dict(
        name='M8 字符串 false 当 True',
        file='展示层/api/api_system.py',
        old="        new_mode = raw.strip().lower() == 'true'",
        new="        new_mode = bool(raw)      # mutated",
        tests=[f'{T}::test_simulation_switch_string_false_is_false'],
    ),
    dict(
        name='M9 加设备丢落盘结果',
        file='采集层/device_manager.py',
        old=("            if not self._save_config():\n"
             "                with self._lock:\n"
             "                    self.devices.pop(device_id, None)\n"
             "                    self.clients.pop(device_id, None)"),
        new=("            if False:      # mutated\n"
             "                with self._lock:\n"
             "                    self.devices.pop(device_id, None)\n"
             "                    self.clients.pop(device_id, None)"),
        tests=[f'{T}::test_add_device_rolls_back_when_save_fails'],
    ),
    dict(
        name='M10 删设备不回滚',
        file='采集层/device_manager.py',
        old=("            if not self._save_config():\n"
             "                if removed is not None:"),
        new=("            if False:      # mutated\n"
             "                if removed is not None:"),
        tests=[f'{T}::test_remove_device_restores_when_save_fails'],
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
