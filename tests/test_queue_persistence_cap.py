# -*- coding: utf-8 -*-
"""崩溃恢复清单必须有硬上限（round 265）。

缺陷形态
--------
`DiskBackedQueue` 的内存队列有 `maxsize`，但**磁盘副本没有上限**：

    put(item) → _persist_item(item)   # 先写 pending_data.jsonl
              → queue.put(item)       # 队列满就丢弃最旧的一条

消费端（写库）一旦停摆，采集线程仍按周期落盘：文件只增不减。
2026-09-20 实机故障就是这个形状 —— 落库停滞数小时，`pending_data.jsonl`
涨到 160MB；当时只加了健康检查阈值（能看见），没有治根。
再往下就是**磁盘写满**：连数据库都写不进去，从"数据积压"升级成"整机不可用"。

修法（两条，缺一不可）
----------------------
1. **硬上限**：清单超过 `pending_cap` 就淘汰最旧的 10%，计 `dropped_persisted`
   并打 ERROR —— 这是**显式的、可观测的**丢弃策略，不是无声膨胀。
2. **不动"内存丢弃"的磁盘副本**：内存队列扔掉只代表"这一轮送不进库"，
   磁盘副本还在文件里，下次启动能恢复（见
   `tests/test_collector_db_failure.py::test_items_that_cannot_be_requeued_stay_on_disk`）。

⚠️ 第 2 条是被既有守卫纠正过的：最初实现"丢弃即删磁盘副本"来省空间，
等于把"积压"变成"永久丢失"，被那条用例当场拦下。
"""

from __future__ import annotations

import threading

import pytest


class _StubCollector:
    """只具备 `_enqueue_drop_oldest` 需要的字段（不构造完整采集器）。

    `_inc_stat` 借用生产类的方法（它只用到 `stats` 与 `_stats_lock`），
    这样计数逻辑也是生产实现，不是替身。
    """

    def __init__(self, queue):
        self.data_queue = queue
        self.stats = {}
        self._stats_lock = threading.Lock()

    def _inc_stat(self, key, amount=1):
        from 采集层.data_collector import DataCollector
        return DataCollector._inc_stat(self, key, amount)


@pytest.fixture
def dq(tmp_path):
    """小容量队列 + 显式小上限，便于在几条数据内触发淘汰。"""
    from 采集层.data_collector import DiskBackedQueue
    return DiskBackedQueue(maxsize=50, persist_dir=str(tmp_path / 'queue'),
                           pending_cap=50)


def _enqueue(dq, item):
    """走**生产代码** `DataCollector._enqueue_drop_oldest` 入队。

    ⚠️ 不要在这里"复刻"一遍入队逻辑：本文件最初就是这么写的，
    结果生产实现改坏了用例依然全绿 —— 平行实现测不出生产实现的回归。
    """
    from 采集层.data_collector import DataCollector
    return DataCollector._enqueue_drop_oldest(_StubCollector(dq), item)


def _item(i):
    return {'device_id': 'd1', 'register_name': 'temp', 'value': float(i),
            'timestamp': '2026-10-25 14:00:00'}


def _lines(dq):
    f = dq._persist_file
    if not f.exists():
        return 0
    return sum(1 for _ in f.open(encoding='utf-8'))


# ---------------------------------------------------------------------------
# 1. 硬上限（核心）
# ---------------------------------------------------------------------------

def test_pending_list_is_capped(dq):
    """消费停摆、只入队不出队时，清单不许无界增长。"""
    from 采集层.data_collector import PENDING_COMPACT_EVERY

    for i in range(500):        # 上限 50，灌 10 倍
        _enqueue(dq, _item(i))

    assert dq.pending_count <= 50, (
        f'清单涨到 {dq.pending_count} 条，超过上限 50 —— 消费停摆时会写满磁盘')
    # 磁盘文件允许比清单"滞后"一个压缩周期（重写是 O(n)，不能每条都做），
    # 但必须是**有界**的 —— 旧实现这里是 500（无界增长）。
    assert _lines(dq) <= 50 + PENDING_COMPACT_EVERY, (
        f'pending_data.jsonl 有 {_lines(dq)} 行，超出「上限 + 压缩周期」—— 仍在无界增长')


def test_overflow_is_counted(dq):
    """淘汰必须计数 —— 静默丢数据比丢数据更糟。"""
    for i in range(500):
        dq._persist_item(_item(i))

    assert dq.pending_count <= 50, '清单没被上限兜住'
    assert dq.dropped_persisted > 0, '淘汰了却不计数'


def test_under_cap_keeps_everything(dq):
    """反向对照：没超上限时一条都不许丢。"""
    for i in range(10):
        _enqueue(dq, _item(i))
    assert dq.pending_count == 10
    assert dq.dropped_persisted == 0
    assert _lines(dq) == 10


# ---------------------------------------------------------------------------
# 2. 内存丢弃 ≠ 数据丢失（既有不变式，不许被"省空间"改坏）
# ---------------------------------------------------------------------------

def test_dropped_from_memory_keeps_disk_copy(dq):
    """队列满时从**内存**丢弃的那条，磁盘副本必须保留（下次启动可恢复）。

    内存队列扔掉只代表"这一轮送不进库"，不代表数据该消失。
    总量由硬上限兜住，不需要靠删副本省空间。
    """
    for i in range(60):          # 上限 50 → 会触发内存丢弃
        _enqueue(dq, _item(i))

    assert _lines(dq) >= dq.pending_count, '磁盘副本被误删了（那等于永久丢数据）'
    assert _lines(dq) <= 50 + 50, f'文件涨到 {_lines(dq)} 行，超出上限兜底范围'


def test_cap_does_not_shrink_below_queue_size(tmp_path):
    """默认上限必须 ≥ 内存队列容量：否则"刚落盘就被自己的上限淘汰"。"""
    from 采集层.data_collector import DiskBackedQueue

    q = DiskBackedQueue(maxsize=1, persist_dir=str(tmp_path / 'q1'))
    assert q._pending_cap >= 1
    for i in range(3):
        q._persist_item(_item(i))
    assert q.pending_count == 3, '容量很小时落盘数据被立刻淘汰了'


# ---------------------------------------------------------------------------
# 3. 崩溃恢复仍然有效（别把好功能改坏）
# ---------------------------------------------------------------------------

def test_recover_from_disk_still_works(tmp_path):
    from 采集层.data_collector import DiskBackedQueue

    q1 = DiskBackedQueue(maxsize=100, persist_dir=str(tmp_path / 'q2'))
    for i in range(3):
        q1._persist_item(_item(i))

    # 新实例读同一个目录 = 模拟进程重启
    q2 = DiskBackedQueue(maxsize=100, persist_dir=str(tmp_path / 'q2'))
    assert q2.pending_count == 3, '重启后没有从磁盘恢复'


def test_cap_is_configurable(tmp_path):
    from 采集层.data_collector import DiskBackedQueue

    q = DiskBackedQueue(maxsize=100, persist_dir=str(tmp_path / 'q3'),
                        pending_cap=5)
    assert q._pending_cap == 5
    for i in range(20):
        q._persist_item(_item(i))
    assert q.pending_count <= 5
