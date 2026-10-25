# -*- coding: utf-8 -*-
"""通知通道坏了，本身必须是一条报警（round 267）。

缺陷形态
--------
邮件/短信/声光/广播投递失败时，代码会 `_record_output_failure()`：打 ERROR 日志
+ 累加 `output_errors[channel]`。看着挺周全，但——

* 计数只出现在 `/api/alarms/statistics` 的 `output.errors` 里，**没人主动看**；
* 没有任何报警说"通知发不出去了"。

于是 SMTP 密码过期、短信网关欠费、声光 DO 写失败，都可能连续几天无人发现，
而这段时间里**所有报警都到不了人**。对 SCADA 来说这比某一条报警漏掉严重得多：
它把整个报警系统变成了"看起来很正常的哑巴"。

判据：**监控报警系统自身**（ISA-18.2 明确要求）。连续失败达阈值 →
把"通道坏了"报成一条 critical 报警；恢复后自动清除。

⚠️ 自指风险：这条报警不能走邮件/短信（否则 发邮件失败 → 报警 → 再发邮件 → …），
所以 `raise_external_alarm(..., notify=False)` 跳过这两个通道，
声光/广播/WebSocket 仍然触发。
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def am():
    from 报警层.alarm_manager import AlarmManager

    db = MagicMock()
    db.get_alarm_records.return_value = []
    db.get_active_alarms.return_value = []
    mgr = AlarmManager(db)
    yield mgr
    try:
        mgr.stop()
    except Exception:       # noqa: BLE001
        pass


def _active_ids(mgr):
    return {a['alarm_id'] for a in mgr.get_active_alarms()}


# ---------------------------------------------------------------------------
# 1. 连续失败 → 报警
# ---------------------------------------------------------------------------

def test_single_failure_does_not_alarm(am):
    """偶发一次失败不该惊动现场（网络抖动很常见）。"""
    am._record_output_failure('email', 'SMTP timeout')
    assert 'notify_broken_email' not in _active_ids(am)


def test_consecutive_failures_raise_alarm(am):
    from 报警层.alarm_manager import NOTIFY_FAILURE_ALARM_THRESHOLD

    for i in range(NOTIFY_FAILURE_ALARM_THRESHOLD):
        am._record_output_failure('email', f'SMTP timeout #{i}')

    assert 'notify_broken_email' in _active_ids(am), (
        '邮件通道连续失败达阈值，却没有产生任何报警 —— 报警到不了人也没人知道')


def test_alarm_message_explains_impact(am):
    from 报警层.alarm_manager import NOTIFY_FAILURE_ALARM_THRESHOLD

    for i in range(NOTIFY_FAILURE_ALARM_THRESHOLD):
        am._record_output_failure('sms', '网关 502')

    entry = next(a for a in am.get_active_alarms()
                 if a['alarm_id'] == 'notify_broken_sms')
    assert entry['alarm_level'] == 'critical'
    assert '连续失败' in entry['alarm_message']
    assert '到不了人' in entry['alarm_message'], '报警文案没说清后果'


def test_alarm_is_raised_only_once(am):
    """持续失败不该每次失败都新开一条（重复登记会淹没其它报警）。"""
    from 报警层.alarm_manager import NOTIFY_FAILURE_ALARM_THRESHOLD

    for i in range(NOTIFY_FAILURE_ALARM_THRESHOLD + 5):
        am._record_output_failure('email', f'fail #{i}')

    broken = [a for a in am.get_active_alarms() if a['alarm_id'] == 'notify_broken_email']
    assert len(broken) == 1


def test_success_clears_alarm(am):
    from 报警层.alarm_manager import NOTIFY_FAILURE_ALARM_THRESHOLD

    for i in range(NOTIFY_FAILURE_ALARM_THRESHOLD):
        am._record_output_failure('email', 'fail')
    assert 'notify_broken_email' in _active_ids(am)

    am._record_output_success('email')
    assert 'notify_broken_email' not in _active_ids(am), '通道恢复了但报警没清掉'


def test_success_resets_counter(am):
    """失败-成功-失败 不该累加成"连续失败"。"""
    from 报警层.alarm_manager import NOTIFY_FAILURE_ALARM_THRESHOLD

    for _ in range(NOTIFY_FAILURE_ALARM_THRESHOLD - 1):
        am._record_output_failure('email', 'fail')
    am._record_output_success('email')
    for _ in range(NOTIFY_FAILURE_ALARM_THRESHOLD - 1):
        am._record_output_failure('email', 'fail')

    assert 'notify_broken_email' not in _active_ids(am), (
        '中间成功过一次，不该算"连续失败"')


# ---------------------------------------------------------------------------
# 2. 不能自指递归
# ---------------------------------------------------------------------------

def test_channel_alarm_skips_email_and_sms(am):
    """通道故障报警必须 notify=False，否则 发邮件失败→报警→再发邮件→…"""
    called = {'email': 0, 'sms': 0}
    am._notify_by_email = lambda payload: called.__setitem__('email', called['email'] + 1)
    am._notify_by_sms = lambda payload: called.__setitem__('sms', called['sms'] + 1)

    from 报警层.alarm_manager import NOTIFY_FAILURE_ALARM_THRESHOLD
    for i in range(NOTIFY_FAILURE_ALARM_THRESHOLD):
        am._record_output_failure('email', 'fail')

    assert 'notify_broken_email' in _active_ids(am)
    assert called['email'] == 0, '通道故障报警又去发邮件了 —— 会自指递归'
    assert called['sms'] == 0


def test_normal_alarm_still_notifies(am):
    """反向对照：普通报警照常走邮件/短信。"""
    called = {'email': 0}
    am._notify_by_email = lambda payload: called.__setitem__('email', called['email'] + 1)
    am._notify_by_sms = lambda payload: None

    am.raise_external_alarm('edge_x', '普通外部报警', level='warning',
                            device_id='edge', register_name='auto')
    assert called['email'] == 1


# ---------------------------------------------------------------------------
# 3. 统计接口暴露
# ---------------------------------------------------------------------------

def test_stats_expose_consecutive_and_broken(am):
    from 报警层.alarm_manager import NOTIFY_FAILURE_ALARM_THRESHOLD

    am._record_output_failure('broadcast', 'MQTT 连接失败')
    stats = am.get_alarm_statistics()
    out = stats['output']
    assert out['consecutive_failures']['broadcast'] == 1
    assert out['failure_alarm_threshold'] == NOTIFY_FAILURE_ALARM_THRESHOLD
    assert out['broken_channels'] == []

    for _ in range(NOTIFY_FAILURE_ALARM_THRESHOLD - 1):
        am._record_output_failure('broadcast', 'MQTT 连接失败')
    assert am.get_alarm_statistics()['output']['broken_channels'] == ['broadcast']


# ---------------------------------------------------------------------------
# 4. 声光/广播成功路径也要清零
# ---------------------------------------------------------------------------

def test_output_method_success_resets_counter(am):
    from 报警层.alarm_manager import NOTIFY_FAILURE_ALARM_THRESHOLD

    for _ in range(NOTIFY_FAILURE_ALARM_THRESHOLD):
        am._record_output_failure('alarm_output', 'DO 写失败')
    assert 'notify_broken_alarm_output' in _active_ids(am)

    target = MagicMock()
    target.trigger_alarm.return_value = True
    am._call_output_method('alarm_output', target, 'trigger_alarm',
                           level='critical', message='x', device_id='d')

    assert 'notify_broken_alarm_output' not in _active_ids(am), (
        '声光恢复后没有清除通道故障报警')
