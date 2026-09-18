"""关机时「剩余数据不得静默丢失」的回归测试。

背景（2026-09 审计遗留项）
--------------------------
`DataCollector.stop()` 的收尾逻辑原先是：

    remaining = [...从队列取出的剩余数据...]
    if remaining:
        try:
            self.database.insert_data_batch(remaining)
        except Exception as e:
            logger.error(f"关闭前写入剩余数据失败: {e}")   # ← 只记日志
    self.data_queue.clear_persistence()                    # ← 无条件删持久化文件

一旦写库失败（DB 被锁 / 磁盘满 / 进程正在退出），会出现**双重丢失**：
  - 内存里的 remaining 已经从队列 `get_nowait()` 取走 → 丢了
  - 磁盘上的持久化副本又被 `clear_persistence()` 删掉 → 也丢了

而 `DiskBackedQueue` 存在的意义正是防这种丢数据 —— 它的 `put()` 是
「先落盘再入队」，所以只要**保留**持久化文件，下次启动
`_recover_from_disk()` 就能把这批数据捞回来。

修法：只有确认入库成功才清除持久化文件；失败则保留并明确告警。
"""
import json

import pytest
from unittest.mock import MagicMock

from 采集层.data_collector import DataCollector, DiskBackedQueue


def _item(dev='dev1', reg='temp', value=25.0):
    """构造一条能被 _recover_from_disk 接受的数据（必须有 value 字段）"""
    return {'device_id': dev, 'register_name': reg, 'value': value,
            'timestamp': '2026-09-17 10:00:00'}


@pytest.fixture
def persist_dir(tmp_path, monkeypatch):
    """把队列持久化目录指到临时目录（不改生产行为，只影响本次测试）"""
    d = tmp_path / 'queue_persist'
    d.mkdir()
    monkeypatch.setenv('SCADA_QUEUE_PERSIST_DIR', str(d))
    return d


def _make_collector(db):
    dm = MagicMock()
    dm.devices = {}
    return DataCollector(dm, db, MagicMock())


# ============================================================
# 核心回归
# ============================================================

def test_persist_file_kept_when_final_flush_fails(persist_dir):
    """写库失败时**必须保留**持久化文件 —— 这是数据不丢的唯一依靠。

    修复前这里无条件 clear_persistence()，该断言失败。
    """
    db = MagicMock()
    db.insert_data_batch.side_effect = RuntimeError('database is locked')
    c = _make_collector(db)

    c.data_queue.put(_item())
    persist_file = c.data_queue._persist_file
    assert persist_file.exists(), '前置条件：put() 应先把数据落盘'

    c.stop()

    assert persist_file.exists(), (
        '关机时写库失败，持久化文件却被删了 —— '
        '这批数据在内存和磁盘上同时消失，无法恢复'
    )


def test_persist_file_cleared_when_final_flush_succeeds(persist_dir):
    """写库成功时仍要清除持久化文件（否则下次启动会重复入库）"""
    db = MagicMock()
    c = _make_collector(db)

    c.data_queue.put(_item())
    persist_file = c.data_queue._persist_file
    assert persist_file.exists()

    c.stop()

    assert not persist_file.exists(), (
        '数据已成功入库，持久化文件应被清除，否则下次启动会重复恢复'
    )
    db.insert_data_batch.assert_called_once()


def test_data_is_recoverable_on_next_startup(persist_dir):
    """端到端：写库失败 → 关机 → **下次启动能把数据捞回来**"""
    db = MagicMock()
    db.insert_data_batch.side_effect = RuntimeError('database is locked')
    c = _make_collector(db)

    c.data_queue.put(_item(reg='press', value=101.3))
    c.stop()

    # 模拟进程重启：新建一个采集器，共用同一个持久化目录
    db2 = MagicMock()
    c2 = _make_collector(db2)

    recovered = []
    while not c2.data_queue.empty():
        recovered.append(c2.data_queue.get_nowait())

    assert recovered, '重启后没有恢复任何数据 —— 关机时那批数据真的丢了'
    assert recovered[0]['value'] == 101.3
    assert recovered[0]['register_name'] == 'press'


def test_no_data_lost_when_db_ok(persist_dir):
    """对照：一切正常时，剩余数据必须进库且不残留持久化文件"""
    written = []
    db = MagicMock()
    db.insert_data_batch.side_effect = lambda rows: written.extend(rows)
    c = _make_collector(db)

    for i in range(5):
        c.data_queue.put(_item(value=float(i)))
    c.stop()

    assert len(written) == 5, f'应写入 5 条，实际 {len(written)}'
    assert not c.data_queue._persist_file.exists()


def test_processing_loop_keeps_persist_file_when_batch_write_fails(persist_dir):
    """处理循环里写库失败时也不得清持久化文件。

    写失败的数据刚被 `put_nowait()` 重新入队 —— 此刻它们**只在内存里**，
    磁盘副本是唯一的崩溃保护。无条件清除等于把保护也抹掉。
    """
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(DataCollector._process_data))
    tree = ast.parse(src)

    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == 'clear_persistence'
    ]
    assert calls, '处理循环里找不到 clear_persistence()，实现可能变了'

    # 注意：条件写的是 `if db_write_ok and hasattr(...)`，
    # 那是 ast.BoolOp 而不是 ast.Name —— 所以要递归找 test 里有没有引用 db_write_ok，
    # 不能直接判 `isinstance(node.test, ast.Name)`（第一版就是这么写错的）。
    guarding = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(isinstance(n, ast.Name) and n.id == 'db_write_ok'
                for n in ast.walk(node.test))
    ]
    assert guarding, (
        'clear_persistence() 没有被 `db_write_ok` 保护 —— '
        '批量写库失败时会把重试数据的崩溃保护一起清掉'
    )
    assert any(c in ast.walk(g) for g in guarding for c in calls), (
        'clear_persistence() 不在 `if db_write_ok` 分支内'
    )


def test_dropped_retry_items_are_counted_in_stats(persist_dir):
    """重试后仍失败而被丢弃的数据必须计入 stats（不能静默丢）。

    原实现对 `_db_retry` 已置位的数据直接 `continue`，**既没日志也没计数** ——
    运维完全不知道丢了什么、丢了多少。

    这里断言 **stats 计数**而不是日志：日志断言依赖 caplog 与工作线程的时序，
    会出现「单独跑通过、和其他测试一起跑就失败」的假红（本测试第一版就是如此）。
    计数是确定性的，且顺带让「丢了多少」在 /api/metrics 一类的地方可查。
    """
    import threading
    import time

    db = MagicMock()
    db.insert_data_batch.side_effect = RuntimeError('db down')
    c = _make_collector(db)

    item = _item()
    item['_db_retry'] = True          # 已重试过一次 → 再失败就该被丢弃
    c.data_queue.put(item)

    c.running = True
    t = threading.Thread(target=c._process_data, daemon=True)
    t.start()

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if c.stats.get('dropped_db_retry', 0) >= 1:
            break
        time.sleep(0.05)
    c.running = False
    t.join(timeout=10)

    assert c.stats.get('dropped_db_retry', 0) == 1, (
        f'重试后丢弃的数据没有被计数 —— 数据丢失对运维不可见。'
        f'当前 stats: {c.stats}'
    )


def test_requeued_items_are_counted(persist_dir):
    """写库失败后成功重新入队的数据也要计数（第一次失败、还能重试的那批）"""
    import threading
    import time

    db = MagicMock()
    db.insert_data_batch.side_effect = RuntimeError('db down')
    c = _make_collector(db)

    # 没有 _db_retry 标记 → 第一次失败，应被重新入队而不是丢弃
    c.data_queue.put(_item())

    c.running = True
    t = threading.Thread(target=c._process_data, daemon=True)
    t.start()

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if c.stats.get('requeued_db_retry', 0) >= 1:
            break
        time.sleep(0.05)
    c.running = False
    t.join(timeout=10)

    assert c.stats.get('requeued_db_retry', 0) == 1, (
        f'重新入队的数据没有被计数。当前 stats: {c.stats}'
    )


def test_clear_persistence_only_inside_flushed_branch(persist_dir):
    """静态守卫（AST）：`clear_persistence()` 必须且只能出现在 `if flushed:` 分支内。

    这条挡的是「把修复改回去」—— 只要有人再把它写成无条件调用，这里就红。

    **用 AST 而不是文本匹配**：本文件第一版写的是
    `src.count('clear_persistence()') == 1`，结果被**注释里出现的同一串字面量**
    误判为「有多处调用」而假红。文本匹配分不清「代码」和「提到这段代码的注释」，
    这类守卫必须走语法树。（同一坑在 test_collector_concurrency.py 里也踩过一次。）
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(DataCollector.stop)))

    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == 'clear_persistence'
    ]
    assert len(calls) == 1, (
        f'stop() 里 clear_persistence() 被调用 {len(calls)} 次，期望恰好 1 次'
    )

    target = calls[0]
    guarding = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == 'flushed'
    ]
    assert guarding, 'stop() 里没有基于 `flushed` 的分支 —— 持久化文件可能被无条件清除'

    assert any(target in ast.walk(node) for node in guarding), (
        'clear_persistence() 不在 `if flushed:` 分支内 —— 写库失败时数据仍会被删'
    )
