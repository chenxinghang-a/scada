# -*- coding: utf-8 -*-
"""周期健康检查线程的「迟绑定」回归（round 221）。

缺陷
----
`HealthChecker.start_periodic_checks()` 每次都 `cls._stop_event = threading.Event()`
装一个**新**事件，而线程体写的是 `while not cls._stop_event.wait(interval)` ——
读的是**类属性**（迟绑定），不是启动时那一份。

正常 stop 时 join 会成功，所以看不出问题。但 `check()` 会摸数据库、设备、
磁盘 —— 一旦卡住超过 `join(timeout=30)`，`stop_periodic_checks` 会把
`_periodic_thread` 置 None 后返回，线程其实还在跑。此后任何一次
`start_periodic_checks()` 都会装上新事件，旧线程下一轮读到的就是**新事件**
（未 set）→ 永远收不到自己的停止信号。

后果：健康检查线程越积越多，每多一条就多一轮全量检查（DB/磁盘压力成倍），
且 `_emit_health_alert` 的告警会被重复触发。

修法：事件在 start 时捕获成**局部变量** `stop_event`，循环只认这一份。
"""

import threading

import pytest

from core.health_checker import HealthChecker


@pytest.fixture(autouse=True)
def clean_thread():
    """每个用例前后都清场，避免类属性把线程漏给下一个用例。"""
    HealthChecker.stop_periodic_checks()
    yield
    HealthChecker.stop_periodic_checks()


@pytest.fixture
def fast_check(monkeypatch):
    """把 check() 换成不碰 IO 的桩（否则每 50ms 真查一次库）。"""
    monkeypatch.setattr(
        HealthChecker, 'check',
        classmethod(lambda cls, name=None: {'checks': {}}),
    )


def test_loop_uses_event_captured_at_start(monkeypatch):
    """核心用例：类属性被换成新事件后，旧线程仍须认自己那一份。

    ⚠️ 必须让线程**正卡在 `check()` 里**再换事件 —— 这是缺陷成立的唯一时机。
    若线程当时正阻塞在旧事件的 `wait()` 上，`set()` 会直接唤醒它并退出循环，
    无论循环体读的是局部变量还是类属性都看不出差别（第一版用例就是这么假绿的）。
    """
    entered = threading.Event()
    release = threading.Event()
    calls = {'n': 0}

    def fake_check(cls, name=None):
        calls['n'] += 1
        if calls['n'] == 1:          # 只有第一次卡住，模拟 check() 慢
            entered.set()
            release.wait(5)
        return {'checks': {}}

    monkeypatch.setattr(HealthChecker, 'check', classmethod(fake_check))

    HealthChecker.start_periodic_checks(interval=0.05)
    thread = HealthChecker._periodic_thread
    own_event = HealthChecker._stop_event
    assert thread is not None and thread.is_alive(), '周期线程没启动'
    assert entered.wait(3), '线程没进入 check()，用例前提不成立'

    # 精确模拟「stop 的 join 超时 → 又 start 装了新事件」之后的状态：
    # 类属性指向新对象，而旧线程仍卡在 check() 里。
    HealthChecker._stop_event = threading.Event()

    own_event.set()      # 只通知**旧线程自己**那一份
    release.set()        # 放行 check()，让线程回到循环条件
    thread.join(2)

    assert not thread.is_alive(), (
        '旧健康检查线程没被自己那份事件停掉 —— 它读的是类属性（迟绑定），'
        'restart 之后会永久残留，每多一条就多一轮全量检查'
    )


def test_normal_start_stop(fast_check):
    """常规路径：start → stop 能干净退出（防止修迟绑定修坏了正常停止）。"""
    HealthChecker.start_periodic_checks(interval=0.05)
    thread = HealthChecker._periodic_thread
    assert thread.is_alive()

    HealthChecker.stop_periodic_checks()

    assert not thread.is_alive(), 'stop 之后线程仍存活'
    assert HealthChecker._periodic_thread is None


def test_double_start_is_ignored(fast_check):
    """重复 start 不许起第二条线程（既有守卫，别被本次改动破坏）。"""
    HealthChecker.start_periodic_checks(interval=0.05)
    first = HealthChecker._periodic_thread

    HealthChecker.start_periodic_checks(interval=0.05)

    assert HealthChecker._periodic_thread is first, '重复 start 换掉了线程对象'
