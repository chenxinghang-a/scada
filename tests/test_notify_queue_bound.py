# -*- coding: utf-8 -*-
"""通知投递队列必须有界 + SMTP 必须有超时（round 288）。

缺陷形态
--------
`Notification.send_email()` 建连时**不传 timeout**：

    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls()
        server.login(username, password)
        server.send_message(msg)

不传 timeout 时 smtplib 用的是 `socket._GLOBAL_DEFAULT_TIMEOUT`（= 阻塞到永远）。
SMTP 服务器被防火墙丢包 / 断电 / 端口不通时，`starttls()` 或 `login()`
会卡住**几分钟甚至永远**。

而报警管理器的投递线程池是 `max_workers=1`：

    ThreadPoolExecutor(max_workers=1, thread_name_prefix='alarm-email')

于是那条唯一的 worker 一卡死：

1. **所有邮件都发不出去**（后面的任务全在排队）；
2. `ThreadPoolExecutor` 的队列是**无界**的 —— 每条报警都塞一个任务，
   内存一路涨；
3. `_record_output_failure()` **一次都不会触发**（第一个任务还没返回），
   所以 round 267 的「通知通道连续失败要报警」永远不生效 ——
   **通道坏了完全不可见**；
4. `stop()` 里的 `executor.shutdown(wait=True)` 会跟着**永久阻塞**，进程退不掉。

判据
----
与 round 286/287 同一条：**I/O 必须快速失败，积压必须有上限**。
丢一封邮件是可控的，无界积压把进程内存吃光不是。

本文件钉住：
1. 建连显式传超时（默认 10s，可配）；
2. 投递队列按通道计数，超过上限**丢弃并记失败**（可见，不是静默）；
3. 无论成败都释放槽位（否则"只进不出"，几次失败后所有邮件都被误丢）；
4. 槽位计数不会变负。
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def am():
    from 报警层.alarm_manager import AlarmManager

    db = MagicMock()
    mgr = AlarmManager(db)
    yield mgr
    try:
        mgr.stop()
    except Exception:       # noqa: BLE001
        pass


def _notifier(block: threading.Event | None = None, ok: bool = True):
    """一个可阻塞/可失败的假通知实现。"""
    n = MagicMock()
    n.email_enabled = True
    n.sms_enabled = True
    n.sms_config = {'phone_numbers': ['13800000000']}

    def _send(_payload):
        if block is not None:
            block.wait(10)
        return ok

    n.send_alarm_notification.side_effect = _send
    n.send_sms.side_effect = lambda nums, text: ok
    return n


PAYLOAD = {
    'alarm_level': 'critical', 'alarm_message': '温度过高',
    'device_id': 'dev1', 'register_name': 'temp',
    'actual_value': 99.0, 'threshold': 80.0, 'timestamp': '2026-10-27T01:00:00',
}


# ---------------------------------------------------------------------------
# SMTP 超时
# ---------------------------------------------------------------------------

def test_smtp_is_created_with_timeout(monkeypatch):
    import smtplib

    import 报警层.notification as mod

    seen = {}

    class _FakeSMTP:
        def __init__(self, host, port, timeout=None):
            seen['host'] = host
            seen['port'] = port
            seen['timeout'] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, u, p):
            pass

        def send_message(self, msg):
            pass

    monkeypatch.setattr(smtplib, 'SMTP', _FakeSMTP)

    n = mod.Notification({
        'email': {
            'enabled': True,
            'smtp_server': 'smtp.example.com', 'smtp_port': 587,
            'username': 'u@example.com', 'password': 'p',
            'recipients': ['a@example.com'],
        }
    })
    assert n.send_email('主题', '正文') is True

    assert seen['timeout'] is not None, (
        'SMTP 建连没传超时 —— 服务器不通时投递线程会永久阻塞，'
        '所有邮件都发不出去且失败计数不涨')
    assert seen['timeout'] <= 30, f"超时太长: {seen['timeout']}"


def test_smtp_timeout_is_configurable(monkeypatch):
    import smtplib

    import 报警层.notification as mod

    seen = {}

    class _FakeSMTP:
        def __init__(self, host, port, timeout=None):
            seen['timeout'] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, u, p):
            pass

        def send_message(self, msg):
            pass

    monkeypatch.setattr(smtplib, 'SMTP', _FakeSMTP)

    n = mod.Notification({
        'email': {
            'enabled': True, 'smtp_server': 'smtp.example.com', 'smtp_port': 587,
            'username': 'u@example.com', 'password': 'p',
            'recipients': ['a@example.com'], 'timeout': 3,
        }
    })
    assert n.send_email('主题', '正文') is True
    assert seen['timeout'] == 3.0


# ---------------------------------------------------------------------------
# 队列上限
# ---------------------------------------------------------------------------

def test_queue_is_bounded_when_worker_stuck(am, monkeypatch):
    """worker 卡死时，积压不能无界增长 —— 超过上限就丢弃并记失败。"""
    import 报警层.alarm_manager as mod

    monkeypatch.setattr(mod, 'NOTIFY_QUEUE_MAX', 3)
    release = threading.Event()
    am.notification = _notifier(block=release)

    try:
        for _ in range(10):
            am._notify_by_email(dict(PAYLOAD))
            # 给回调一点机会跑（前几条会很快完成的话 pending 会降下来，
            # 所以这里用阻塞的 notifier 保证它们都卡在队列里）
        pending = am.get_notify_pending()['email']

        assert pending <= 3, (
            f'投递队列积压 {pending} 条，超过上限 3 —— '
            f'worker 卡死时内存会无界增长')
        assert am._output_errors.get('email', 0) >= 1, (
            '积压丢弃没有记失败 —— 通道卡死完全不可见')
    finally:
        release.set()
        # 等回调把槽位释放掉，避免影响后续用例
        for _ in range(50):
            if am.get_notify_pending()['email'] == 0:
                break
            threading.Event().wait(0.02)


def test_queue_slots_are_released_after_delivery(am):
    am.notification = _notifier(ok=True)

    am._notify_by_email(dict(PAYLOAD))
    for _ in range(100):
        if am.get_notify_pending()['email'] == 0:
            break
        threading.Event().wait(0.02)

    assert am.get_notify_pending()['email'] == 0, (
        '投递完成后槽位没释放 —— 队列"只进不出"，几次之后所有邮件都会被误丢')


def test_queue_slots_released_on_failure(am):
    am.notification = _notifier(ok=False)

    am._notify_by_email(dict(PAYLOAD))
    for _ in range(100):
        if am.get_notify_pending()['email'] == 0:
            break
        threading.Event().wait(0.02)

    assert am.get_notify_pending()['email'] == 0, (
        '投递**失败**后槽位没释放 —— 失败几次后邮件通道会被自己堵死')


def test_sms_queue_also_bounded(am, monkeypatch):
    import 报警层.alarm_manager as mod

    monkeypatch.setattr(mod, 'NOTIFY_QUEUE_MAX', 2)
    release = threading.Event()
    am.notification = _notifier(block=release)

    try:
        for _ in range(8):
            am._notify_by_sms(dict(PAYLOAD))
        assert am.get_notify_pending()['sms'] <= 2
        assert am._output_errors.get('sms', 0) >= 1
    finally:
        release.set()
        for _ in range(50):
            if am.get_notify_pending()['sms'] == 0:
                break
            threading.Event().wait(0.02)


def test_slot_count_never_goes_negative(am):
    am._release_notify_slot('email')
    am._release_notify_slot('email')
    assert am.get_notify_pending()['email'] == 0


def test_submit_failure_releases_slot(am):
    """`executor.submit` 自己抛异常时也要把槽位还回去。"""
    am.notification = _notifier(ok=True)
    bad = MagicMock()
    bad.submit.side_effect = RuntimeError('线程池已关闭')
    am._notify_executor = bad

    am._notify_by_email(dict(PAYLOAD))

    assert am.get_notify_pending()['email'] == 0, '提交失败后槽位泄漏了'
    assert am._output_errors.get('email', 0) == 1


def test_pending_is_observable(am):
    state = am.get_notify_pending()
    assert set(state) == {'email', 'sms'}
    assert all(v == 0 for v in state.values())
