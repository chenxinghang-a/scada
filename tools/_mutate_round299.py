# -*- coding: utf-8 -*-
"""round 299 变异验证：每项修复都要有「改回去就变红」的反向对照。

用法：.venv/Scripts/python.exe tools/_mutate_round299.py
跑完自动还原源码；任何变异「存活」（测试仍绿）= 该项测试是假绿。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = REPO / '.venv' / 'Scripts' / 'python.exe'
T = 'tests/test_silent_failures_round299.py'

MUTATIONS = [
    dict(
        name='M1 maintenance 漏传 data_collector',
        file='core/maintenance.py',
        old='            data_collector=data_collector):',
        new='            ):',
        tests=[f'{T}::test_register_passes_data_collector'],
    ),
    dict(
        name='M2a ALARMS_ACTIVE 丢掉 level 维度',
        file='core/metrics.py',
        old="ALARMS_ACTIVE = Gauge('scada_alarms_active', '活跃告警数', ['level'])",
        new="ALARMS_ACTIVE = Gauge('scada_alarms_active', '活跃告警数')",
        tests=[f'{T}::test_alarms_active_has_level_label'],
    ),
    dict(
        name='M2b 不按级别分组（全塞 unknown）',
        file='core/metrics.py',
        old="                by_level[level] = by_level.get(level, 0) + 1",
        new="                by_level['unknown'] = by_level.get('unknown', 0) + 1",
        tests=[f'{T}::test_alarms_active_has_level_label'],
    ),
    dict(
        name='M3 DEVICES_TOTAL offline 恒 0',
        file='core/metrics.py',
        old="                DEVICES_TOTAL.labels(protocol=proto, status='offline').set(total - online)",
        new="                DEVICES_TOTAL.labels(protocol=proto, status='offline').set(0)",
        tests=[f'{T}::test_devices_total_has_offline_status'],
    ),
    dict(
        name='M3b list 路径丢协议名（塞 unknown）',
        file='core/metrics.py',
        old=("                rows = [(s.get('protocol', 'unknown'), bool(s.get('connected')))\n"
             "                        for s in status]"),
        new=("                rows = [('unknown', bool(s.get('connected')))\n"
             "                        for s in status]"),
        tests=[f'{T}::test_devices_total_list_contract'],
    ),
    dict(
        name='M4 change_password 不清 must_change_password',
        file='用户层/auth.py',
        old='                                 password_changed_at = ?, must_change_password = 0',
        new='                                 password_changed_at = ?',
        tests=[f'{T}::test_change_password_clears_must_change_flag'],
    ),
    dict(
        name='M5 强制改密不撤旧令牌',
        file='用户层/auth.py',
        old="        _revoked = self._blacklist_user_tokens(username, 'force_password_changed')",
        new='        _revoked = True',
        tests=[f'{T}::test_force_change_password_writes_timestamp_and_revokes'],
    ),
    dict(
        name='M6 确认时先置内存再写库（旧行为）',
        file='报警层/alarm_manager.py',
        old='            state = self.alarm_states.get(state_key)\n\n        # 更新数据库',
        new=('            state = self.alarm_states.get(state_key)\n'
             '            if state is not None:\n'
             "                state['acknowledged'] = True\n\n        # 更新数据库"),
        tests=[f'{T}::test_acknowledge_failure_does_not_mark_memory'],
    ),
    dict(
        name='M7a 旁路不撤升级跟踪',
        file='报警层/alarm_manager.py',
        # 锚点必须带「已旁路」日志行：裸的 remove_alarm 调用在
        # _recover_external_alarm 里还有一处，只按调用行匹配会命中 2 次。
        old=('        if self._escalation_manager:\n'
             '            self._escalation_manager.remove_alarm(alarm_id, device_id, register_name)\n'
             '\n'
             '        logger.warning(f"报警已旁路: {alarm_id} ({device_id}/{register_name}) "'),
        new=('        if self._escalation_manager:\n'
             '            pass  # mutated\n'
             '\n'
             '        logger.warning(f"报警已旁路: {alarm_id} ({device_id}/{register_name}) "'),
        tests=[f'{T}::test_shelve_removes_escalation_tracking'],
    ),
    dict(
        name='M7b duration=0 退回 falsy 判断',
        file='报警层/alarm_manager.py',
        old='        if duration_minutes is not None:',
        new='        if duration_minutes:',
        tests=[f'{T}::test_shelve_rejects_zero_duration'],
    ),
    dict(
        name='M7c 看板不输出 shelved 字段',
        file='报警层/alarm_manager.py',
        old="                'shelved': state.get('shelved', False),",
        new='',
        tests=[f'{T}::test_shelved_flag_reaches_dashboard'],
    ),
    dict(
        name='M7d 解除旁路不清状态位',
        file='报警层/alarm_manager.py',
        old=("                state = self.alarm_states.get(alarm_key)\n"
             "                if state is not None:\n"
             "                    state['shelved'] = False\n"
             "                logger.info(f\"报警旁路已解除: {alarm_id} ({device_id}/{register_name})\")"),
        new="                logger.info(f\"报警旁路已解除: {alarm_id} ({device_id}/{register_name})\")",
        tests=[f'{T}::test_unshelve_clears_shelved_flag'],
    ),
    dict(
        name='M7e 到期解除不清状态位',
        file='报警层/alarm_manager.py',
        old=("                state = self.alarm_states.get(key)\n"
             "                if state is not None:\n"
             "                    state['shelved'] = False\n"
             "                logger.info(f\"报警旁路已过期自动解除: {':'.join(key)}\")"),
        new="                logger.info(f\"报警旁路已过期自动解除: {':'.join(key)}\")",
        tests=[f'{T}::test_expired_shelve_clears_flag'],
    ),
    dict(
        name='M8 历史记录不合并运行时旁路',
        file='展示层/api/api_alarms.py',
        old="            rec['shelved'] = shelved.get(key, False)",
        new='            pass  # mutated',
        tests=[f'{T}::test_alarm_records_merge_runtime_shelved'],
    ),
]


def main() -> int:
    survivors = []
    print(f'{"变异":40s} {"结果":8s} 说明')
    print('-' * 90)
    for m in MUTATIONS:
        path = REPO / m['file']
        original = path.read_text(encoding='utf-8')
        if original.count(m['old']) != 1:
            print(f'{m["name"]:40s} {"SKIP":8s} 锚点命中 {original.count(m["old"])} 次（期望 1）')
            survivors.append(m['name'] + ' (锚点不唯一)')
            continue
        try:
            path.write_text(original.replace(m['old'], m['new'], 1), encoding='utf-8')
            r = subprocess.run(
                [str(PY), '-m', 'pytest', '-q', '-p', 'no:cacheprovider', *m['tests']],
                cwd=REPO, capture_output=True, text=True, encoding='utf-8', errors='replace')
            red = r.returncode != 0
            tail = [ln for ln in r.stdout.strip().splitlines() if ln.strip()][-1:] or ['']
            print(f'{m["name"]:40s} {"红(好)" if red else "绿(假绿!)":8s} {tail[0][:60]}')
            if not red:
                survivors.append(m['name'])
        finally:
            path.write_text(original, encoding='utf-8')

    print('-' * 90)
    if survivors:
        print(f'!!! {len(survivors)} 个变异存活（测试抓不住缺陷）：')
        for s in survivors:
            print('  -', s)
        return 1
    print(f'全部 {len(MUTATIONS)} 个变异被抓住，测试集有效。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
