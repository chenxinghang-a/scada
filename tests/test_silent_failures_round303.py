# -*- coding: utf-8 -*-
"""round 303：诚实清单里的前三项（校验时效 / 两份真值 / 降级边沿）。

三条共同点：**状态迁移必须只在边沿发生，而且判据只能有一份真值。**
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock


def _dc():
    from 智能层.device_control import DeviceControlSafety
    dc = DeviceControlSafety(MagicMock(), MagicMock(), MagicMock())
    dc._resolve_register_address = lambda device_id, register: 100
    return dc


# ---------------------------------------------------------------------------
# 1. 写操作：拿锁之后必须重新复核（校验通过是会过期的前提）
# ---------------------------------------------------------------------------

def test_write_rechecked_after_acquiring_lock():
    """等锁期间现场按下急停 → 这笔写必须被拦下。

    原来 `validate_write` 只在拿锁**之前**跑一次，之后还要等锁（最长 5s）
    + connect + 最多 3 次重试；这段时间 `_estop_active` 完全可能已经置起来，
    而写入前不再复核 —— 急停之后仍有一笔写会下发，审计里还是 write_success。
    """
    dc = _dc()
    client = MagicMock()
    dc.device_manager.get_client.return_value = client

    real_acquire = dc.acquire_device_lock

    def acquire_then_estop(device_id, operator):
        ok = real_acquire(device_id, operator)
        dc._estop_active = True          # 等锁期间现场按下急停
        return ok

    dc.acquire_device_lock = acquire_then_estop

    result = dc.write_with_verification('d1', 100, 1, 'op')

    assert result['success'] is False, '急停之后仍然写下去了'
    assert result.get('rechecked') is True, '拿锁后没有做第二次复核'
    client.write_single_register.assert_not_called()


def test_write_proceeds_when_state_unchanged():
    """反向对照：状态没变时必须照常写（别把复核写成"永远拒绝"）。"""
    dc = _dc()
    client = MagicMock()
    client.write_single_register.return_value = True
    client.read_holding_registers.return_value = [1]
    dc.device_manager.get_client.return_value = client

    result = dc.write_with_verification('d1', 100, 1, 'op')

    assert result['success'] is True, f'正常写入被误拦：{result}'
    client.write_single_register.assert_called_once()


# ---------------------------------------------------------------------------
# 2. 通信失败计数：一份真值 + 只在边沿降级
# ---------------------------------------------------------------------------

def test_refresh_health_syncs_failure_counter():
    """`refresh_device_health` 只清界面那份、不清判据那份 —— 两份真值分了叉。

    后果：界面显示 0 次失败，再失败一次就 `4 >= 阈值` 立刻判断线并再次降级，
    而实际只失败了一次。
    """
    dc = _dc()
    dc.device_manager.get_all_devices.return_value = {'d1': {'name': 'D1'}}
    client = MagicMock()
    client.connected = True
    dc.device_manager.get_client.return_value = client
    dc._comm_failures['d1'] = 3
    dc._device_health['d1'] = {'status': 'disconnected', 'consecutive_failures': 3}

    dc.refresh_device_health()

    assert dc._comm_failures['d1'] == 0, '刷新只清了界面那份，判据那份还留着 3'
    assert dc._device_health['d1']['consecutive_failures'] == 0


def test_degradation_only_on_threshold_crossing():
    """掉线 6 次只许降级 1 次（阈值 3）—— 否则安全状态 handler 被重复下发。"""
    dc = _dc()
    dc._comm_threshold = 3
    fires = []
    dc._trigger_fault_degradation = lambda device_id, n: fires.append(n)

    for _ in range(6):
        dc.update_device_health('d1', False)

    assert fires == [3], f'降级被执行了 {len(fires)} 次（应为 1 次）：{fires}'

    # 恢复后再掉线 → 允许重新触发一次
    dc.update_device_health('d1', True)
    for _ in range(3):
        dc.update_device_health('d1', False)

    assert len(fires) == 2, '恢复后再次掉线没有重新降级'


def test_init_health_uses_single_source():
    """初始健康数据里的失败次数从 `_comm_failures` 派生，不写死 3。"""
    dc = _dc()
    dc.device_manager.get_all_devices.return_value = {
        'd1': {'name': 'D1'}, 'd2': {'name': 'D2'}}
    dc.device_manager.get_client.return_value = None

    dc._init_device_health()

    assert dc._device_health['d1']['consecutive_failures'] == 0, (
        '未连接时写死了 3 次失败 —— 阈值是 3 的话界面一上来就显示"已达降级线"')


# ---------------------------------------------------------------------------
# 3. trigger_count 与 sample_count 分开
# ---------------------------------------------------------------------------

def _alarm_mgr():
    from 报警层.alarm_manager import AlarmManager
    return AlarmManager(MagicMock())


def test_active_alarm_trigger_count_is_not_sample_count():
    """持续 10 个采样点，trigger_count 必须还是 1（与库里一致）。"""
    mgr = _alarm_mgr()
    try:
        mgr.add_rule({'id': 'r1', 'name': '超温', 'device_id': 'd1',
                      'register_name': 't', 'condition': 'gt', 'threshold': 100,
                      'level': 'warning'})
        now = datetime.now()
        for _ in range(10):
            mgr.check_alarm('d1', 't', 120.0, now)

        active = [a for a in mgr.get_active_alarms() if a['alarm_id'] == 'r1']
        assert active, '前提不成立：没有产生活动报警'
        row = active[0]
        assert row['trigger_count'] == 1, (
            f"trigger_count 被采样点刷成了 {row['trigger_count']} —— "
            '看板读内存、报表读库，两边永远对不上')
        assert row['sample_count'] >= 9, (
            f"sample_count 没在涨（{row['sample_count']}）—— 活跃期采样点数丢了另一个消费方")
    finally:
        mgr.stop()
