# -*- coding: utf-8 -*-
"""round 299：又一批「假成功 / 状态机走错」（7 项）。

这一轮把审计代理给的候选按"后果最重"继续往下清。判据仍是同一族：
**内存与库必须是同一套真值；不可观测不许报成正常；成功返回必须等于状态收敛。**

| # | 位置 | 缺陷 | 后果 |
|---|---|---|---|
| 1 | `core/maintenance.py` | `register_maintenance_tasks` 调 `build_maintenance_tasks` 时**漏传** `data_collector` | round 285 的自动热重载只做了一半：新增设备永远不采、删掉的继续重试刷报警 |
| 2 | `core/metrics.py` | `scada_alarms_active` 无 `level` 维度 | MONITORING.md 的 `{level="critical"} > 10` 序列不存在，规则永不触发 |
| 3 | `core/metrics.py` | `scada_devices_total` 的 `status` 恒为字面量 `'total'` | `{status="offline"} > 0` 永不触发，全厂掉线一声不响 |
| 4 | `用户层/auth.py` | `change_password` 不清 `must_change_password` | 首次登录改密 → 仍被要求改密 → 除白名单外全 403：**改密死循环** |
| 5 | `用户层/auth.py` | `force_change_password` 不写 `password_changed_at`、不撤旧令牌 | 强制改密后攻击者 token 仍有效、refresh 可持续续期 |
| 6 | `报警层/alarm_manager.py` | 确认时**先置内存**再写库 | 写库失败 → 界面显示"已确认"、升级照打，值班员以为处理完了 |
| 7 | `报警层/alarm_manager.py` | 旁路只写 `_shelved_alarms`，不撤升级跟踪；`duration_minutes=0` 被当 falsy | 旁路后 300/900/1800 秒照样打电话；"旁路 0 分钟"= **永久静默** |
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. maintenance 必须把 data_collector 传下去
# ---------------------------------------------------------------------------

def test_register_passes_data_collector(monkeypatch):
    """`register_maintenance_tasks` 漏传 → 热重载永不同步采集任务。"""
    import core.maintenance as mod

    seen = {}

    def _fake_build(*args, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(mod, 'build_maintenance_tasks', _fake_build)
    dc = MagicMock()
    mod.register_maintenance_tasks(device_manager=MagicMock(), data_collector=dc)

    assert seen.get('data_collector') is dc, (
        '注册维护任务时没把 data_collector 传下去 —— '
        'devices.yaml 热重载只会换配置表，采集任务永远是孤儿')


# ---------------------------------------------------------------------------
# 2 / 3. 指标必须有文档里那些 label 维度
# ---------------------------------------------------------------------------

def _collector_metrics():
    from core.metrics import MetricsCollector
    return MetricsCollector()


def test_alarms_active_has_level_label():
    from core.metrics import ALARMS_ACTIVE

    mgr = MagicMock()
    mgr.get_active_alarms.return_value = [
        {'alarm_level': 'critical'}, {'alarm_level': 'critical'},
        {'alarm_level': 'warning'},
    ]
    _collector_metrics().update_alarm_metrics(mgr)

    assert ALARMS_ACTIVE.labels(level='critical')._value.get() == 2, (
        'scada_alarms_active 没有按 level 分开 —— '
        'MONITORING.md 的 {level="critical"} 规则永不触发')
    assert ALARMS_ACTIVE.labels(level='warning')._value.get() == 1
    # 本轮没出现的级别要显式归零，不能留着上一轮的旧值
    assert ALARMS_ACTIVE.labels(level='info')._value.get() == 0


def test_devices_total_has_offline_status():
    from core.metrics import DEVICES_TOTAL

    dm = MagicMock()
    dm.get_all_status.return_value = {
        'd1': {'protocol': 'modbus', 'connected': True},
        'd2': {'protocol': 'modbus', 'connected': False},
    }
    _collector_metrics().update_device_metrics(dm)

    assert DEVICES_TOTAL.labels(protocol='modbus', status='offline')._value.get() == 1, (
        'scada_devices_total 的 status 恒为 "total" —— '
        'MONITORING.md 的 {status="offline"} 规则永不触发')
    assert DEVICES_TOTAL.labels(protocol='modbus', status='online')._value.get() == 1
    assert DEVICES_TOTAL.labels(protocol='modbus', status='total')._value.get() == 2


def test_devices_total_list_contract():
    """真实契约：`device_manager.get_all_status()` 返回 **list**（device_manager.py:599）。

    round 299 第一版只把 dict 分支改对，list 分支反而把所有协议塞进
    `protocol="unknown"` 且三个值全是 0 —— 生产路径上比修复前更糟
    （原 `total` 至少是对的），而测试因为用 dict 做 mock 依然全绿。
    判据：mock 的返回值必须等于被替身对象的真实契约。
    """
    from core.metrics import DEVICES_TOTAL

    dm = MagicMock()
    dm.get_all_status.return_value = [
        {'protocol': 'sim_modbus', 'connected': True},
        {'protocol': 'sim_modbus', 'connected': False},
        {'protocol': 'sim_opcua', 'connected': True},
    ]
    _collector_metrics().update_device_metrics(dm)

    assert DEVICES_TOTAL.labels(protocol='sim_modbus', status='total')._value.get() == 2
    assert DEVICES_TOTAL.labels(protocol='sim_modbus', status='online')._value.get() == 1
    assert DEVICES_TOTAL.labels(protocol='sim_modbus', status='offline')._value.get() == 1
    assert DEVICES_TOTAL.labels(protocol='sim_opcua', status='total')._value.get() == 1
    assert DEVICES_TOTAL.labels(protocol='sim_opcua', status='offline')._value.get() == 0, (
        'list 路径没有按协议聚合 —— offline 规则同样永不触发')


# ---------------------------------------------------------------------------
# 4 / 5. 两条改密路径必须对称（源码级钉住 SQL）
# ---------------------------------------------------------------------------

def _auth_source() -> str:
    return (REPO / '用户层' / 'auth.py').read_text(encoding='utf-8')


def test_change_password_clears_must_change_flag():
    src = _auth_source()
    seg = src.split('def change_password', 1)[1][:3000]
    assert 'must_change_password = 0' in seg, (
        'change_password 不清 must_change_password —— '
        '首次登录改密后会陷入「改完仍要求改密」的死循环')


def test_force_change_password_writes_timestamp_and_revokes():
    src = _auth_source()
    seg = src.split('def force_change_password', 1)[1][:3000]
    assert 'password_changed_at' in seg, (
        'force_change_password 不写 password_changed_at —— '
        'refresh token 无法判定"早于最后一次改密"，可持续续期')
    assert '_blacklist_user_tokens' in seg, (
        'force_change_password 不撤旧令牌 —— 强制改密后攻击者会话仍然有效')


# ---------------------------------------------------------------------------
# 6. 确认：库没成功就不许改内存
# ---------------------------------------------------------------------------

def _mgr(db_result):
    from 报警层.alarm_manager import AlarmManager

    db = MagicMock()
    db.acknowledge_alarm.return_value = db_result
    mgr = AlarmManager(db)
    return mgr


KEY = ('r1', 'dev1', 'temp')


def _arm_state(mgr):
    ts = datetime(2026, 10, 28, 2, 0, 0)
    mgr.alarm_states[KEY] = {
        'alarm_id': KEY[0], 'device_id': KEY[1], 'register_name': KEY[2],
        'first_trigger_time': ts, 'last_trigger_time': ts,
        'trigger_count': 1, 'acknowledged': False,
        'alarm_level': 'critical', 'alarm_message': 'x',
    }


def test_acknowledge_failure_does_not_mark_memory():
    mgr = _mgr(db_result=False)
    try:
        _arm_state(mgr)
        ok = mgr.acknowledge_alarm(KEY[0], 'dev1', 'temp', 'operator')

        assert ok is False
        assert mgr.alarm_states[KEY]['acknowledged'] is False, (
            '写库失败了却把内存标成"已确认" —— '
            '界面显示已确认、升级电话照打，值班员以为处理完了')
    finally:
        mgr.stop()


def test_acknowledge_success_marks_memory():
    """反向对照：写库成功时必须置位（否则确认等于没生效）。"""
    mgr = _mgr(db_result=True)
    try:
        _arm_state(mgr)
        assert mgr.acknowledge_alarm(KEY[0], 'dev1', 'temp', 'operator') is True
        assert mgr.alarm_states[KEY]['acknowledged'] is True
    finally:
        mgr.stop()


# ---------------------------------------------------------------------------
# 7. 旁路：撤升级跟踪 + 拒绝 0 分钟
# ---------------------------------------------------------------------------

def test_shelve_removes_escalation_tracking():
    mgr = _mgr(db_result=True)
    try:
        _arm_state(mgr)
        esc = mgr._escalation_manager
        if esc is None:
            pytest.skip('未装配升级管理器')
        esc.record_alarm(KEY[0], KEY[1], KEY[2])
        key = f'{KEY[0]}:{KEY[1]}:{KEY[2]}'
        assert key in esc._states, '前提不成立：升级跟踪没登记'

        mgr.shelve_alarm(KEY[0], KEY[1], KEY[2], '维护', 'operator',
                         duration_minutes=30)

        assert key not in esc._states, (
            '旁路只写了 _shelved_alarms，升级跟踪还在 —— '
            '维护窗口里照样给三级主管打电话')
    finally:
        mgr.stop()


def test_shelve_rejects_zero_duration():
    mgr = _mgr(db_result=True)
    try:
        _arm_state(mgr)
        ok = mgr.shelve_alarm(KEY[0], KEY[1], KEY[2], '维护', 'operator',
                              duration_minutes=0)
        assert ok is False, (
            'duration_minutes=0 被当成 falsy → shelved_until=None → '
            'is_expired() 恒 False → **永久静默**')
    finally:
        mgr.stop()


def test_shelve_accepts_positive_duration():
    """反向对照：正常时长必须成功，并且带过期时间。"""
    mgr = _mgr(db_result=True)
    try:
        _arm_state(mgr)
        assert mgr.shelve_alarm(KEY[0], KEY[1], KEY[2], '维护', 'operator',
                                duration_minutes=15) is True
        state = mgr._shelved_alarms[(KEY[0], KEY[1], KEY[2])]
        assert state.shelved_until is not None
    finally:
        mgr.stop()


# ---------------------------------------------------------------------------
# 8. 旁路状态位必须有人消费、且解除时对称清除
# ---------------------------------------------------------------------------

def _active_flag(mgr, field):
    for a in mgr.get_active_alarms():
        if (a['alarm_id'], a['device_id'], a['register_name']) == KEY:
            return a.get(field)
    raise AssertionError(f'活动报警里找不到 {KEY}')


def test_shelved_flag_reaches_dashboard():
    """`state['shelved']` 此前全仓无人读取 —— 前端分不清「在响」和「被屏蔽」。"""
    mgr = _mgr(db_result=True)
    try:
        _arm_state(mgr)
        assert _active_flag(mgr, 'shelved') is False
        mgr.shelve_alarm(KEY[0], KEY[1], KEY[2], '维护', 'operator',
                         duration_minutes=30)
        assert _active_flag(mgr, 'shelved') is True, (
            '旁路后看板仍显示「在响」—— state["shelved"] 是个没人消费的死键')
    finally:
        mgr.stop()


def test_unshelve_clears_shelved_flag():
    """解除旁路必须对称清位，否则看板永远显示「已旁路」。"""
    mgr = _mgr(db_result=True)
    try:
        _arm_state(mgr)
        mgr.shelve_alarm(KEY[0], KEY[1], KEY[2], '维护', 'operator',
                         duration_minutes=30)
        assert mgr.unshelve_alarm(KEY[0], KEY[1], KEY[2]) is True
        assert _active_flag(mgr, 'shelved') is False, (
            '解除旁路后状态位没清 —— 看板会一直显示已旁路')
    finally:
        mgr.stop()


def test_expired_shelve_clears_flag():
    """到期自动解除同样要清位。"""
    mgr = _mgr(db_result=True)
    try:
        _arm_state(mgr)
        mgr.shelve_alarm(KEY[0], KEY[1], KEY[2], '维护', 'operator',
                         duration_minutes=1)
        # 把过期时间推到过去，模拟自然到期
        mgr._shelved_alarms[(KEY[0], KEY[1], KEY[2])].shelved_until = \
            datetime(2026, 10, 28, 2, 0, 0)
        mgr._cleanup_expired_shelves()
        assert _active_flag(mgr, 'shelved') is False, (
            '旁路到期自动解除后状态位残留 —— 看板永远显示已旁路')
    finally:
        mgr.stop()


# ---------------------------------------------------------------------------
# 9. 历史记录必须合并运行时旁路状态
# ---------------------------------------------------------------------------

def test_alarm_records_merge_runtime_shelved(client, auth_headers, app):
    """`/api/alarms` 读的是库里的 `alarm_records`，旁路只存在于 `alarm_states`。

    不合并的话，前端那个「已旁路」筛选永远筛不出东西 —— 界面上看着有这个功能，
    实际一条都不生效。同样的形状已经在 round 299 里出现过两次（指标 dict/list 分支、
    `state['shelved']` 无人读），所以这条单独钉住。
    """
    app.database.get_alarm_records.return_value = [
        {'alarm_id': 'a1', 'device_id': 'dev1', 'register_name': 'temp',
         'acknowledged': 0},
        {'alarm_id': 'a2', 'device_id': 'dev1', 'register_name': 'temp',
         'acknowledged': 0},
    ]
    app.alarm_manager.get_active_alarms.return_value = [
        {'alarm_id': 'a1', 'device_id': 'dev1', 'register_name': 'temp',
         'shelved': True},
    ]

    resp = client.get('/api/alarms', headers=auth_headers)
    assert resp.status_code == 200
    by_id = {r['alarm_id']: r for r in resp.get_json()['alarms']}

    assert by_id['a1']['shelved'] is True, (
        '历史记录没合并运行时旁路 —— 前端「已旁路」筛选恒为空')
    assert by_id['a2']['shelved'] is False, (
        '非活动记录也要显式给 False，否则 filter(a => a.shelved) 依赖 undefined 的偶然真值')
