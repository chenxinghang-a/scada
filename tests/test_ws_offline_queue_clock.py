# -*- coding: utf-8 -*-
"""WebSocket 离线队列：过期判断用单调时钟 + 丢弃要计数（round 275）。

两个缺陷
--------
**1) 过期判断用墙钟**

    now = time.time()
    valid = [m for m in queue if now - m.timestamp < max_age]

`max_age`（默认 1 小时）表达的是**时长**。墙钟被回拨一次（NTP 校时 /
现场机开机后校时 / 手工改时间），`now - timestamp` 变负 → 消息**永不过期**：
一直占内存，客户端重连时被投递一堆几小时前的旧状态（界面上显示的是历史值，
操作员会以为现场就是那样）。向前跳则全部立即过期 —— 离线期间的状态全丢。

**2) 队列满时静默丢消息**

    if len(queue) >= max_messages:
        queue.pop(0)          # ← 没有计数

`total_enqueued` 涨、`total_delivered` 不涨，差额被无声吞掉。运维无法知道
"某个客户端漏了一段状态"。同 round 267（通知通道）是一类问题：
**丢数据必须可观测**。
"""

from __future__ import annotations

import time

import pytest


@pytest.fixture
def queue():
    from core.ws_offline_queue import OfflineMessageQueue
    return OfflineMessageQueue(max_messages_per_client=5, max_age_seconds=60)


# ---------------------------------------------------------------------------
# 1. 丢弃计数
# ---------------------------------------------------------------------------

def test_overflow_is_counted(queue):
    for i in range(8):          # 上限 5，塞 8 条
        queue.enqueue('c1', 'tick', i)

    stats = queue.get_stats()
    assert stats['total_queued'] == 5
    assert stats['total_dropped'] == 3, (
        '队列满时丢的消息没有计数 —— 客户端漏了一段状态而没人知道')


def test_no_drop_when_under_cap(queue):
    for i in range(5):
        queue.enqueue('c1', 'tick', i)
    assert queue.get_stats()['total_dropped'] == 0


def test_drop_prefers_low_priority_first():
    """满了优先丢**低优先级**的，而不是无脑丢最旧。"""
    from core.ws_offline_queue import OfflineMessageQueue

    q = OfflineMessageQueue(max_messages_per_client=3, max_age_seconds=600)
    q.enqueue('c1', 'low', 'old-low', priority=0)
    q.enqueue('c1', 'high', 'high-1', priority=9)
    q.enqueue('c1', 'low', 'low-2', priority=0)

    q.enqueue('c1', 'high', 'high-2', priority=9)      # 触发丢弃

    events = [m['data'] for m in q.peek('c1')]
    assert 'high-1' in events and 'high-2' in events, '高优先级消息被丢了'
    assert 'old-low' not in events, '该丢的是最旧的低优先级消息'


# ---------------------------------------------------------------------------
# 2. 过期判断用单调时钟
# ---------------------------------------------------------------------------

def test_messages_expire_in_real_time(queue, monkeypatch):
    """墙钟回拨 8 小时后，消息仍按真实经过的时间过期。"""
    real_time = time.time          # 先存原函数，否则 lambda 里再调会无限递归
    queue.enqueue('c1', 'tick', 1)

    monkeypatch.setattr(time, 'time', lambda: real_time() - 8 * 3600)

    # 真实时间只过了几毫秒 → 消息仍在（没有因为墙钟回拨而"永不过期"）
    assert queue.get_stats()['total_queued'] == 1
    # 且 `to_dict()` 的 timestamp 仍是**墙钟**（展示用），不是单调值
    msg = queue.peek('c1')[0]
    assert abs(msg['timestamp'] - real_time()) < 60, (
        f"timestamp={msg['timestamp']} 不是墙钟 —— 前端会显示 1970 年")


def test_expiry_uses_monotonic_clock(queue, monkeypatch):
    """把单调时钟推进到 max_age 之外 → 消息必须过期（墙钟不动也一样）。"""
    import core.ws_offline_queue as mod

    queue.enqueue('c1', 'tick', 1)
    msg = queue.peek('c1')[0]

    # 把该消息的单调时刻改成"很久以前"
    queue._queues['c1'][0].monotonic = time.monotonic() - 61

    assert queue.dequeue('c1') == [], '过期消息仍被投递 —— 客户端会收到旧状态'
    assert queue.get_stats()['total_expired'] >= 1


def test_cleanup_expired_uses_monotonic(queue):
    queue.enqueue('c1', 'tick', 1)
    queue._queues['c1'][0].monotonic = time.monotonic() - 61

    assert queue.cleanup_expired() == 1
    assert queue.get_stats()['active_clients'] == 0, '过期后客户端条目应被清掉'


def test_timestamp_field_is_wall_clock(queue):
    """`to_dict()` 里的 timestamp 必须仍是墙钟（前端要显示时间）。"""
    before = time.time()
    queue.enqueue('c1', 'tick', 1)
    after = time.time()

    ts = queue.peek('c1')[0]['timestamp']
    assert before - 1 <= ts <= after + 1, f'timestamp={ts} 不是墙钟'


# ---------------------------------------------------------------------------
# 3. 不误伤既有行为
# ---------------------------------------------------------------------------

def test_dequeue_returns_in_priority_order(queue):
    queue.enqueue('c1', 'e', 'low', priority=0)
    queue.enqueue('c1', 'e', 'high', priority=9)
    queue.enqueue('c1', 'e', 'mid', priority=5)

    got = [m['data'] for m in queue.dequeue('c1')]
    assert got == ['high', 'mid', 'low']


def test_empty_client_is_removed(queue):
    queue.enqueue('c1', 'tick', 1)
    queue.dequeue('c1')
    assert queue.get_stats()['active_clients'] == 0, '空队列的客户端条目泄漏'
