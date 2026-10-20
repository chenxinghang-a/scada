# -*- coding: utf-8 -*-
"""`_check_data_freshness` 的交叉信号「读不到就跳过」必须留痕（round 221）。

背景
----
`core/health_checker.py` 里原本有一处 `except Exception: pass`：
`data_collector.data_queue.qsize()` 读不出来时，**静默**跳过
「内存队列为空但磁盘队列有积压 → 消费链路停摆」这条交叉信号。

而这条信号本身正是用来发现「采集在跑但数据进不了库」的（2026-09-20 实机故障：
5 项检查全绿、实际落库停滞数小时、磁盘队列涨到 160MB）。检查不出来 = 可能漏报，
却没有任何痕迹 —— 属于本项目最危险的「静默失效」。

`tests/test_silent_exception_guard.py` 里曾为此登记了一条按**行号**索引的豁免。
本轮（给 `start_periodic_checks` 加注释）行号漂移，豁免失效、守卫报红，
于是按豁免里写好的修法直接修掉：

  · 改绑 `as e`；
  · `logger.warning(...)`（不是 debug —— 这是「可能漏报」，运维要能看见）；
  · `queue_in_memory` 显式置 **None**，不能留 0（0 会被下游当成「内存队列确实是空的」）。

本文件钉住这三条，防止退回 `pass`。
"""

import logging
from unittest.mock import MagicMock

import pytest

from core.health_checker import _check_data_freshness


def _db_ok():
    """让「数据新鲜度」这一项正常通过（返回刚写入的时间戳）。"""
    from datetime import datetime
    db = MagicMock()
    db.get_latest_data_time.return_value = datetime.now()
    db.get_data_count.return_value = 100
    return db


def test_qsize_failure_is_logged_and_marked_unknown(caplog, tmp_path, monkeypatch):
    collector = MagicMock()
    collector.data_queue.qsize.side_effect = RuntimeError('queue 已关闭（模拟）')

    # 让磁盘队列体积这一项有值，确保走到交叉信号判定
    monkeypatch.setattr('core.health_checker.QUEUE_DEGRADED_BYTES', 1)
    monkeypatch.setattr(
        'core.health_checker._queue_persist_size', lambda *a, **k: 10 * 1024 * 1024,
        raising=False,
    )

    with caplog.at_level(logging.WARNING, logger='core.health_checker'):
        result = _check_data_freshness(_db_ok(), collector)

    messages = '\n'.join(r.getMessage() for r in caplog.records)
    assert '队列深度读取失败' in messages, (
        f'读不到队列深度时没有任何 warning —— 又变回静默吞异常了。实际日志: {messages!r}'
    )
    assert result['details'].get('queue_in_memory', 'MISSING') is None, (
        'queue_in_memory 没有被显式置 None —— 缺失或 0 都会被下游误读成「内存队列是空的」'
    )


def test_normal_path_still_works(caplog):
    """正常路径不受影响：能读到队列深度时照常给出数值。"""
    collector = MagicMock()
    collector.data_queue.qsize.return_value = 0

    result = _check_data_freshness(_db_ok(), collector)

    assert result['details'].get('queue_in_memory') == 0
    assert result['status'] is not None
