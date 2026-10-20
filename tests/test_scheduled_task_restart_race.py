# -*- coding: utf-8 -*-
"""定时任务「stop → start」竞态：同一任务被两条循环同时执行（round 221）。

缺陷
----
`TaskManager.stop()` 只 `set()` 停止事件、**不 join**；`start()` 紧接着就
`clear()` 同一个事件。若停的那一刻任务正卡在 `_execute_task()`（备份、清理
动辄几十秒），事件被清掉之后**旧循环被救活**，同时 `start()` 又起了一条新线程
—— 于是同一个任务被**两条循环同时执行**：备份写两份、清理跑两遍、
指标重复上报，而且 `task._thread` 只指向新的那条，旧的从此无人可停。

修法：
- `stop()` 在**锁外** join 旧线程（最多 `STOP_JOIN_TIMEOUT_S`）；
- `start()` 发现旧线程仍存活时**拒绝启动**并留痕，而不是 clear 后硬起新线程；
- `stop_all()` / `start_all()` 改成「锁内取名单 → 锁外逐个操作」，
  避免持锁 join 把正在执行的任务线程挡在 `self._lock` 上。

⚠️ join 必须在锁外：`_execute_task()` 会取 `self._lock` 更新状态，
持锁 join 等于让被 join 的线程等自己放锁 —— 只能等到超时。
"""

import threading
import time

import pytest

from core.scheduled_tasks import TaskManager


@pytest.fixture
def manager():
    m = TaskManager()
    # 测试里把 join 上限压到 0.2s，否则「任务卡住」的用例要真等 5 秒
    m.STOP_JOIN_TIMEOUT_S = 0.2
    yield m
    m.STOP_JOIN_TIMEOUT_S = 5.0
    m.stop_all()


def test_stop_start_while_task_running_is_refused(manager):
    """任务正在执行时 stop→start 必须被拒绝（否则两条循环并存）。"""
    entered = threading.Event()
    release = threading.Event()

    def slow_task():
        entered.set()
        release.wait(5)

    manager.add('t', slow_task, 0.05)
    assert manager.start('t') is True
    assert entered.wait(3), '任务没开始执行，用例前提不成立'

    manager.stop('t')                 # join 0.2s 超时，线程仍在跑
    assert manager.start('t') is False, (
        '旧循环还活着就允许了重新启动 —— 会让同一个任务被两条循环同时执行'
    )

    release.set()                     # 放行旧任务
    deadline = time.time() + 3
    while time.time() < deadline and manager.get_status('t')['status'] == 'running':
        time.sleep(0.02)
    time.sleep(0.2)                   # 给旧循环一点时间真正退出

    assert manager.start('t') is True, '旧循环结束后应当允许重新启动'


def test_stop_joins_when_task_is_idle(manager):
    """任务空闲时 stop 必须真的把线程收掉（不是只改状态）。"""
    manager.add('idle', lambda: None, 60)
    assert manager.start('idle') is True
    thread = manager._tasks['idle']._thread
    assert thread.is_alive()

    manager.stop('idle')

    assert not thread.is_alive(), (
        'stop 之后线程仍存活 —— 状态显示已停止、实际还在等下一个周期'
    )
    assert manager.get_status('idle')['status'] == 'stopped'


def test_stop_all_does_not_deadlock_with_running_task(manager):
    """stop_all 不许因为持锁 join 而卡住（任务正在执行时最容易暴露）。"""
    entered = threading.Event()
    release = threading.Event()

    def slow_task():
        entered.set()
        release.wait(5)

    manager.add('a', slow_task, 0.05)
    manager.add('b', lambda: None, 60)
    manager.start_all()
    assert entered.wait(3)

    started_at = time.time()
    release.set()                     # 先放行，确保 stop_all 能真正停下
    manager.stop_all()
    elapsed = time.time() - started_at

    assert elapsed < 3, f'stop_all 花了 {elapsed:.1f}s —— 疑似持锁 join 互相等待'
    for name in ('a', 'b'):
        assert manager.get_status(name)['status'] == 'stopped'
