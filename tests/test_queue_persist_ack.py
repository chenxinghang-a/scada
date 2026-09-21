"""队列持久化「按批确认（ack）」的回归测试。

背景
----
`clear_persistence()` 原先每落库一批就把**整个**持久化文件清空。
这留下一个真实的数据丢失窗口：

    t0  采集线程 put(A)                → 文件: A
    t1  消费线程取走本批 [A]
    t2  采集线程 put(B)                → 文件: A B     ← B 在队列里，不在本批
    t3  本批写库成功 → 清空文件         → 文件: (空)    ← B 的磁盘副本被一起抹掉
    t4  进程崩溃                       → B 在内存与磁盘上**同时消失**

窗口 = 本批的写库耗时（批量插入 500 行时可达数十毫秒），而采集是持续进行的，
所以这不是理论问题：每次落库都有机会撞上。

修法：`clear_persistence(flushed_items)` 只移除**这批**对应的记录，
文件重写为剩余待恢复记录；`clear_persistence()` 不带参数才表示全部确认
（仅 `stop()` 收尾使用，此时生产者线程已停、队列已排空）。
"""

import json

import pytest

from 采集层.data_collector import DiskBackedQueue


def _item(reg='r1', value=1.0):
    return {'device_id': 'd1', 'register_name': reg, 'value': value}


def _lines(persist_file):
    if not persist_file.exists():
        return []
    return [ln for ln in persist_file.read_text(encoding='utf-8').splitlines() if ln.strip()]


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setenv('SCADA_QUEUE_PERSIST_DIR', str(tmp_path / 'q'))
    return DiskBackedQueue(maxsize=1000)


# ============================================================
# 核心：按批确认不会误删「已落盘但不在本批」的数据
# ============================================================

def test_ack_keeps_records_of_items_not_in_batch(queue):
    """本批写库成功、但期间新落盘的 B 不在本批 → B 的记录必须留下。"""
    a = _item('ra', 1.0)
    queue.put(a)

    batch = [queue.get_nowait()]          # 消费线程取走本批
    assert batch[0] is a

    b = _item('rb', 2.0)
    queue.put(b)                          # 本批写库期间采集线程又落了盘
    assert len(_lines(queue._persist_file)) == 2

    queue.clear_persistence(batch)        # 本批确认入库

    remaining = _lines(queue._persist_file)
    assert len(remaining) == 1, (
        f'本批之外的记录被一起清掉了（剩 {len(remaining)} 条，应为 1 条）—— '
        f'这条数据此刻只在内存队列里，崩溃即真丢'
    )
    assert json.loads(remaining[0])['register_name'] == 'rb'


def test_acked_data_is_gone_but_unacked_survives_restart(queue, tmp_path, monkeypatch):
    """端到端：确认过的批不再恢复；未确认的仍能恢复（崩溃安全）。"""
    a, b = _item('ra', 1.0), _item('rb', 2.0)
    queue.put(a)
    batch = [queue.get_nowait()]
    queue.put(b)
    queue.clear_persistence(batch)

    # 模拟重启
    restarted = DiskBackedQueue(maxsize=1000)
    recovered = []
    while not restarted.empty():
        recovered.append(restarted.get_nowait())

    names = sorted(r['register_name'] for r in recovered)
    assert names == ['rb'], (
        f'重启后恢复出 {names}，应为 [rb] —— 已确认的 ra 不该重复恢复，'
        f'未确认的 rb 不该丢失'
    )


def test_partial_ack_keeps_order_and_rest(queue):
    """只确认中间一条 → 其余两条按原顺序保留。"""
    items = [_item('r0', 0.0), _item('r1', 1.0), _item('r2', 2.0)]
    for it in items:
        queue.put(it)

    queue.clear_persistence([items[1]])

    kept = [json.loads(ln)['register_name'] for ln in _lines(queue._persist_file)]
    assert kept == ['r0', 'r2'], f'保留内容与顺序不对: {kept}'


def test_ack_with_empty_list_is_noop(queue):
    """空批确认不得把文件清空（消费线程拿到空批是可能的）。"""
    queue.put(_item('r0', 0.0))
    queue.clear_persistence([])
    assert len(_lines(queue._persist_file)) == 1


def test_clear_all_removes_everything(queue):
    """不带参数 = 全部确认（stop() 收尾用）。"""
    for i in range(3):
        queue.put(_item(f'r{i}', float(i)))
    queue.clear_persistence()
    assert _lines(queue._persist_file) == []


# ============================================================
# 持久化幂等：重复入队不得写出第二份
# ============================================================

def test_requeue_same_object_does_not_duplicate_line(queue):
    """写库失败后整批 `put_nowait()` 重新入队，同一条不得落盘两次。

    否则崩溃恢复时会把同一批数据恢复两遍 → history_data 出现重复行，
    能耗/统计等聚合随之偏大。
    """
    item = _item('r0', 0.0)
    queue.put(item)
    assert len(_lines(queue._persist_file)) == 1

    queue.put_nowait(item)   # 重试重新入队（同一个对象）

    assert len(_lines(queue._persist_file)) == 1, (
        f'同一条数据被落盘 {len(_lines(queue._persist_file))} 次 —— '
        f'崩溃恢复时会重复入库'
    )


# ============================================================
# 重写时不得把进程内重试标记带进磁盘
# ============================================================

def test_db_retry_flag_is_not_persisted(queue):
    """`_db_retry` 是进程内的重试标记，落盘后会让恢复出来的数据首次失败即被丢弃。"""
    item = _item('r0', 0.0)
    queue.put(item)
    other = _item('r1', 1.0)
    queue.put(other)

    item['_db_retry'] = True
    queue.clear_persistence([other])   # 触发一次重写

    lines = _lines(queue._persist_file)
    assert len(lines) == 1
    assert '_db_retry' not in lines[0], (
        '重试标记被写进了持久化文件 —— 崩溃恢复后这条数据第一次写库失败就会被丢弃'
    )
