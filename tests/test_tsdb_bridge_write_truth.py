# -*- coding: utf-8 -*-
"""TDengine 写入必须"真的写进去了"才算成功（round 291）。

缺陷形态（缺陷族：**通道失败静默**）
----------------------------------
`RealtimeDataBridge._flush_buffer()` 原先这样记账：

    try:
        self.tdengine.write_telemetry_batch(records)      # 失败时只打日志、返回 None
        self.stats['records_written'] += len(records)     # ← 无条件 +N
    except Exception as e:
        ... 回填重试 ...

而 `TDengineClient.write_telemetry_batch()` 在 SQL 失败时**不抛异常**，只
`logger.error("批量写入失败: ...")` 然后 `return`（None）。于是：

* 计数 `records_written` 照涨 —— 看板显示"正在写"，实际一条都没进库；
* 记录**不回填**、不进离线缓存、不触发内存回退缓冲的溢出告警 ——
  整条兜底链路永远走不到；
* 表现为「时序库静默丢数据」，而且越"健康"（写入计数越涨）越像正常。

判据（承接 283-290 同一族）
--------------------------
1. **通道返回值 = 真的发出去了吗**，不是"我尝试过了"：显式 False/None 都算失败；
2. **积压必须有上限**：这条已有（内存缓冲上限 + 溢出计数）；
3. **连续失败要熔断**：TDengine REST 超时 30s、flush 周期 5s —— 库挂掉时
   每轮都阻塞 30s，flush 线程被拖死、缓冲被填满、开始丢最旧。
   连续失败 N 次后冷却 T 秒（**单调时钟**，初值 -inf），期间不发起写；
4. **健康度可观测**：`get_stats()` 暴露 `fail_streak` / `circuit_open` /
   `circuit_remaining_seconds` / `records_requeued` / `last_write_error`。
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from 智能层.tsdb_adapter import RealtimeDataBridge


class _Clock:
    """可控单调时钟（替换 `bridge._clock`，不动全局 time.monotonic）。"""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def _bridge(return_value=True, offline_buffer=None, max_buffer_size=None):
    td = MagicMock()
    td.write_telemetry_batch.return_value = return_value
    b = RealtimeDataBridge(td, offline_buffer=offline_buffer,
                           max_buffer_size=max_buffer_size)
    clock = _Clock()
    b._clock = clock
    return b, td, clock


def _feed_one(b):
    b.feed('dev1', 'temp', 25.0)


# ---------------------------------------------------------------------------
# 1. 写入成功与否，必须按返回值记账
# ---------------------------------------------------------------------------

def test_failed_write_is_not_counted_as_written():
    """客户端返回 False（没写进去）→ 不计入 written，且记录必须回填重试。"""
    b, td, _ = _bridge(return_value=False)
    _feed_one(b)

    b._flush_buffer()

    assert b.stats['records_written'] == 0, (
        '失败的写入被计成了成功 —— 看板会显示"正在写"，而库里一条都没有')
    assert b.stats['errors'] == 1
    assert b.stats['records_requeued'] == 1
    assert len(b._buffer) == 1, '失败的记录没有回填，直接丢了'


def test_none_return_is_treated_as_failure():
    """返回避 None（本轮修复前的客户端行为）也必须当失败 —— 不能静默成功。"""
    b, td, _ = _bridge(return_value=None)
    _feed_one(b)

    b._flush_buffer()

    assert b.stats['records_written'] == 0, (
        '客户端返回 None（= 没写）却被当成成功 —— 正是本轮要修的静默丢数据')
    assert len(b._buffer) == 1


def test_success_counts_and_clears_buffer():
    """反向对照：真写成功时计数 +1、缓冲清空、失败链归零。"""
    b, td, _ = _bridge(return_value=True)
    _feed_one(b)

    b._flush_buffer()

    assert b.stats['records_written'] == 1
    assert b.stats['errors'] == 0
    assert len(b._buffer) == 0


# ---------------------------------------------------------------------------
# 2. 熔断：连续失败后不再每轮硬打 30s 超时
# ---------------------------------------------------------------------------

def test_circuit_opens_after_threshold():
    b, td, _ = _bridge(return_value=False)

    for i in range(RealtimeDataBridge.CIRCUIT_FAIL_THRESHOLD):
        _feed_one(b)
        b._flush_buffer()

    stats = b.get_stats()
    assert stats['circuit_open'] is True, (
        f'连续失败 {RealtimeDataBridge.CIRCUIT_FAIL_THRESHOLD} 次仍未熔断 —— '
        f'TDengine 挂掉时每 5 秒硬打一次 30 秒超时，flush 线程会被拖死')
    assert stats['circuit_remaining_seconds'] > 0
    assert stats['fail_streak'] == RealtimeDataBridge.CIRCUIT_FAIL_THRESHOLD
    assert stats.get('last_write_error')

    calls_before = td.write_telemetry_batch.call_count
    _feed_one(b)
    b._flush_buffer()

    assert td.write_telemetry_batch.call_count == calls_before, (
        '熔断期内仍然发起了写调用')
    assert len(b._buffer) >= 1, '熔断期内的记录必须留在缓冲里，不能丢'


def test_circuit_closes_after_cooldown_and_writes_again():
    """反向对照：冷却期过去后必须重新尝试写（否则就是"永久停写"）。"""
    b, td, clock = _bridge(return_value=False)
    for _ in range(RealtimeDataBridge.CIRCUIT_FAIL_THRESHOLD):
        _feed_one(b)
        b._flush_buffer()
    assert b.get_stats()['circuit_open'] is True

    clock.now += RealtimeDataBridge.CIRCUIT_COOLDOWN_S + 1.0
    assert b.get_stats()['circuit_open'] is False, '冷却期已过却仍判为熔断'

    td.write_telemetry_batch.return_value = True
    b._flush_buffer()

    assert td.write_telemetry_batch.call_count == RealtimeDataBridge.CIRCUIT_FAIL_THRESHOLD + 1, (
        '冷却期过后没有重新尝试写 —— 数据会永久滞留在缓冲里')
    assert b.stats['records_written'] >= 1
    assert b.get_stats()['fail_streak'] == 0, '写成功后失败链没有归零'


# ---------------------------------------------------------------------------
# 3. 客户端层：失败必须反映到返回值
# ---------------------------------------------------------------------------

def _client():
    from timeseries.tdengine_client import TDengineClient

    c = TDengineClient.__new__(TDengineClient)      # 不连库，只测判定
    c.logger = MagicMock()
    c.stats = {'writes': 0, 'errors': 0, 'last_error': None}
    c._sanitize_identifier = lambda name: f'"{name}"'          # noqa: SLF001
    c._escape_value = lambda v: str(v).replace("'", "''")      # noqa: SLF001
    c._ensure_table = MagicMock()                              # noqa: SLF001
    return c


def _records(n=2):
    from timeseries.data_models import TelemetryRecord
    from datetime import datetime

    return [TelemetryRecord(device_id='d1', register_name='temp',
                            timestamp=datetime(2026, 10, 27, 12, 0, i), value=float(i))
            for i in range(n)]


def test_client_batch_returns_false_when_sql_fails():
    c = _client()
    c._execute_sql = MagicMock(return_value=None)      # noqa: SLF001

    ok = c.write_telemetry_batch(_records(2))

    assert ok is False, 'SQL 失败却返回了"成功" —— 上层会把整批当已写入'
    assert c.stats['writes'] == 0, '失败时写入计数仍增长'


def test_client_batch_returns_true_when_sql_succeeds():
    """反向对照：成功时必须返回 True 并计数。"""
    c = _client()
    c._execute_sql = MagicMock(return_value={'code': 0})   # noqa: SLF001

    ok = c.write_telemetry_batch(_records(2))

    assert ok is True
    assert c.stats['writes'] == 2
