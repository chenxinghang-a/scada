# -*- coding: utf-8 -*-
"""连接池的空闲/存活窗口必须按**单调时钟**算（round 259）。

缺陷形态
--------
`PooledConnection.created_at` / `last_used` 与池里的 `now` 原来都用
`time.time()`，而它们唯一的用途是**做差**：

    now - conn.last_used  >= max_idle_time     # 空闲回收
    now - conn.created_at >= max_lifetime      # 超龄重建

墙钟被回拨一次（NTP 校时、现场机无电池开机后校时、手工改时间），
差值变负 → 空闲连接**永不回收**、超龄连接**永不过期**：
一条已经死掉的 socket 会一直躺在池里被反复取出，该设备的采集从此一直失败，
只能重启进程。反过来（向前跳）会让所有连接同时超龄 → 全部重建（重连风暴）。

判据：**时长用 monotonic，时刻用 wall clock**。
"""

from __future__ import annotations

import re
import time

#: 真实墙钟函数（被 monkeypatch 之前的原版）—— 用来模拟"回拨 8 小时"
_REAL_TIME = time.time
from pathlib import Path

POOL_SRC = Path(__file__).resolve().parents[1] / "core" / "connection_pool.py"


def _make_pool(**kw):
    from core.connection_pool import ConnectionPool

    created = []

    def factory(key):
        obj = object()
        created.append(obj)
        return obj

    params = dict(factory=factory, max_size=5, max_idle_time=0.05,
                  max_lifetime=100.0, name='t-pool', auto_scale=False)
    params.update(kw)
    return ConnectionPool(**params), created


def test_idle_connection_recycled_after_backward_clock_jump(monkeypatch):
    """墙钟回拨 8 小时后，空闲超时的连接仍必须被回收重建。"""
    pool, created = _make_pool()
    first = pool.acquire('dev1')
    pool.release('dev1')
    assert first is created[0]

    monkeypatch.setattr(time, 'time', lambda: _REAL_TIME() - 8 * 3600)   # 墙钟回拨
    time.sleep(0.08)                                          # 真实空闲 80ms > 50ms

    second = pool.acquire('dev1')
    assert second is not first, (
        '空闲超时后仍复用了旧连接 —— 说明窗口用的是墙钟，'
        '一次校时之后死连接会永远躺在池里被反复取出')
    pool.shutdown()


def test_max_lifetime_rebuilds_after_backward_clock_jump(monkeypatch):
    """超龄重建同理（max_lifetime 也是时长）。"""
    pool, created = _make_pool(max_idle_time=1000.0, max_lifetime=0.05)
    first = pool.acquire('dev2')
    pool.release('dev2')

    monkeypatch.setattr(time, 'time', lambda: _REAL_TIME() - 8 * 3600)
    time.sleep(0.08)

    second = pool.acquire('dev2')
    assert second is not first, '连接已超龄却仍被复用（max_lifetime 用墙钟算的）'
    pool.shutdown()


def test_pool_timestamps_are_monotonic():
    """`created_at` / `last_used` 必须是单调时钟的值（与墙钟差 > 1 天即用错）。"""
    pool, _ = _make_pool()
    client = pool.acquire('dev3')
    conn = pool._pool['dev3']
    assert conn.client is client

    for name in ('created_at', 'last_used'):
        value = getattr(conn, name)
        assert abs(time.monotonic() - value) < 60, (
            f'{name}={value!r} 看起来是墙钟（与 monotonic 差 '
            f'{abs(time.monotonic() - value):.0f}s）—— 校时后池会算错')
    pool.shutdown()


def test_pool_has_no_wall_clock():
    """静态守卫：连接池里不许再出现 `time.time()`。"""
    code = re.sub(r"#.*$", "", POOL_SRC.read_text(encoding='utf-8'), flags=re.M)
    assert 'time.time()' not in code, (
        'connection_pool 里又出现了 time.time() —— 墙钟回拨会让死连接永不回收')
