"""采集层「写库失败」故障注入测试（审计 #6 / P1）。

审计报告 #6 的要求：验证「写库失败后重试/去重」与「队列满」等边界，
**并给出「数据没丢」的证明**。本文件把 `_process_data` 的失败分支拆到
`DataCollector._requeue_failed_batch()` 后逐条钉死。

核心不变式：``retried + dropped + requeue_full == len(batch)``
—— 每一条都必须被归类，不能有「既没入队也没计数」的隐形条目。

历史缺陷（本次修掉）：原实现在队列满时 `break`，本批**剩余条目**
既不入队、也不进任何计数 —— 日志只说「1 条未能重新入队」，实际可能几百条。
数据本身还在磁盘上（不是丢失），但**账目不准**比数据丢失更难发现。
"""
import ast
import logging
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COLLECTOR_SRC = REPO_ROOT / '采集层' / 'data_collector.py'


def _bare_collector(queue_maxsize: int, tmp_path, monkeypatch):
    """不跑 __init__（会连设备/建线程池），只装配入队与统计所需属性"""
    monkeypatch.setenv('SCADA_QUEUE_PERSIST_DIR', str(tmp_path / 'q'))
    from 采集层.data_collector import DataCollector, DiskBackedQueue

    collector = DataCollector.__new__(DataCollector)
    collector.data_queue = DiskBackedQueue(maxsize=queue_maxsize)
    collector.stats = {}
    collector._stats_lock = threading.Lock()
    collector.running = True
    return collector


def _item(i):
    return {'device_id': f'dev{i}', 'register_name': 'r', 'value': i}


# ---------------------------------------------------------------------------
# 1. 基本分类
# ---------------------------------------------------------------------------

def test_all_fresh_items_are_requeued(tmp_path, monkeypatch):
    c = _bare_collector(10, tmp_path, monkeypatch)
    batch = [_item(i) for i in range(3)]

    counts = c._requeue_failed_batch(batch)

    assert counts == {'retried': 3, 'dropped': 0, 'requeue_full': 0}
    assert c.data_queue.qsize() == 3
    assert all(it['_db_retry'] for it in batch), '重新入队的条目必须带重试标记'


def test_already_retried_items_are_dropped_and_counted(tmp_path, monkeypatch):
    """带 `_db_retry` 的条目 = 已经重试过一次仍失败 → 计入 dropped（真丢失点）"""
    c = _bare_collector(10, tmp_path, monkeypatch)
    batch = [_item(i) for i in range(3)]
    batch[1]['_db_retry'] = True

    counts = c._requeue_failed_batch(batch)

    assert counts == {'retried': 2, 'dropped': 1, 'requeue_full': 0}
    assert c.data_queue.qsize() == 2, '被丢弃的条目不得再进队列'


# ---------------------------------------------------------------------------
# 2. 队列满 —— 核心不变式
# ---------------------------------------------------------------------------

def test_queue_full_still_accounts_for_every_item(tmp_path, monkeypatch):
    """⚠️ 回归：队列满时**本批每一条**都必须被归类（原实现 break 后漏计）。

    构造：队列容量 2，先占 1 格 → 只剩 1 个位置；批 5 条（1 条已重试）。
    预期：retried=1 / dropped=1 / requeue_full=3，合计 5。
    旧实现：A 入队后 C 撞满即 break → 只记 1 条 requeue_full，D、E 隐形（合计 3）。
    """
    c = _bare_collector(2, tmp_path, monkeypatch)
    c.data_queue.put_nowait(_item(99))          # 占掉 1 格，剩 1 格

    batch = [_item(i) for i in range(5)]
    batch[1]['_db_retry'] = True

    counts = c._requeue_failed_batch(batch)

    assert counts['dropped'] == 1
    assert counts['retried'] == 1
    assert counts['requeue_full'] == 3
    assert sum(counts.values()) == len(batch), (
        f"有条目没被归类：counts={counts} 批大小={len(batch)}"
    )


def test_full_queue_counts_every_unrequeued_item(tmp_path, monkeypatch):
    """全满队列：4 条全新条目必须全部计入 requeue_full，而不是只记 1"""
    c = _bare_collector(1, tmp_path, monkeypatch)
    c.data_queue.put_nowait(_item(99))

    batch = [_item(i) for i in range(4)]
    counts = c._requeue_failed_batch(batch)

    assert counts == {'retried': 0, 'dropped': 0, 'requeue_full': 4}
    assert sum(counts.values()) == len(batch)


def test_requeue_preserves_order(tmp_path, monkeypatch):
    """重新入队必须保持原顺序（乱序会让时序数据看起来像抖动的设备）"""
    c = _bare_collector(10, tmp_path, monkeypatch)
    batch = [_item(i) for i in range(5)]

    c._requeue_failed_batch(batch)

    got = [c.data_queue.get_nowait()['value'] for _ in range(5)]
    assert got == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# 3. 「数据没丢」的证明：磁盘副本必须留下
# ---------------------------------------------------------------------------

def test_items_that_cannot_be_requeued_stay_on_disk(tmp_path, monkeypatch):
    """队列满而未能重新入队的数据，其磁盘副本必须仍在 —— 下次启动可恢复。

    这是「不是静默丢失」的实证：内存队列进不去，但持久化文件里一行不少。
    """
    c = _bare_collector(1, tmp_path, monkeypatch)
    batch = [_item(i) for i in range(3)]
    for it in batch:
        c.data_queue._persist_item(it)          # 模拟「采集线程已先落盘」
    c.data_queue.put_nowait(_item(99))          # 队列占满

    counts = c._requeue_failed_batch(batch)
    assert counts['requeue_full'] == 3

    lines = [ln for ln in (tmp_path / 'q' / 'pending_data.jsonl')
             .read_text(encoding='utf-8').splitlines() if ln.strip()]
    assert len(lines) == 4, f'磁盘副本丢失：只剩 {len(lines)} 行（应为 4：3 批 + 1 占位）'


def test_requeue_does_not_duplicate_disk_lines(tmp_path, monkeypatch):
    """重新入队不得让同一条数据在文件里出现第二遍（否则重启会重复入库）"""
    c = _bare_collector(10, tmp_path, monkeypatch)
    batch = [_item(i) for i in range(3)]
    for it in batch:
        c.data_queue._persist_item(it)

    c._requeue_failed_batch(batch)

    lines = [ln for ln in (tmp_path / 'q' / 'pending_data.jsonl')
             .read_text(encoding='utf-8').splitlines() if ln.strip()]
    assert len(lines) == 3, f'出现重复落盘：{len(lines)} 行（应为 3）'


# ---------------------------------------------------------------------------
# 5. 落盘失败（磁盘满 / 权限 / 只读挂载）—— 崩溃保护失效必须可见
# ---------------------------------------------------------------------------

def _break_persistence(queue, tmp_path):
    """把持久化文件指到一个**父目录不存在**的路径 —— 等价于磁盘满/目录不可用"""
    queue._persist_file = tmp_path / 'no_such_dir' / 'pending_data.jsonl'


def test_persist_failure_returns_false_and_counts(tmp_path, monkeypatch):
    """落盘失败必须返回 False、计数 +1，且**不得**把条目当已落盘记进 _pending"""
    c = _bare_collector(10, tmp_path, monkeypatch)
    _break_persistence(c.data_queue, tmp_path)
    item = _item(1)

    ok = c.data_queue._persist_item(item)

    assert ok is False, '落盘失败却返回成功 —— 调用方无法得知崩溃保护已失效'
    assert c.data_queue.persist_failures == 1
    assert id(item) not in c.data_queue._pending_ids, '失败的条目被错误地标记为已落盘'


def test_persist_failure_is_logged_at_error_level(tmp_path, monkeypatch, caplog):
    """⚠️ 回归：落盘失败曾只打 logger.debug —— 默认 INFO 级别下完全隐形。

    磁盘满时这是**静默的数据丢失路径**：数据只在内存里，一崩就没，日志上什么也看不到。
    """
    c = _bare_collector(10, tmp_path, monkeypatch)
    _break_persistence(c.data_queue, tmp_path)

    with caplog.at_level(logging.DEBUG):
        c.data_queue._persist_item(_item(1))

    records = [r for r in caplog.records if '持久化失败' in r.getMessage()]
    assert records, '落盘失败没有任何日志'
    assert any(r.levelno >= logging.ERROR for r in records), \
        f'落盘失败只打了低级别日志（{records[0].levelname}）—— 生产环境看不见'


def test_persist_failure_log_is_rate_limited(tmp_path, monkeypatch, caplog):
    """限流：不能每失败一条就刷一行 ERROR（磁盘满时会把别的线索淹掉）"""
    c = _bare_collector(10, tmp_path, monkeypatch)
    _break_persistence(c.data_queue, tmp_path)

    with caplog.at_level(logging.ERROR):
        for i in range(50):
            c.data_queue._persist_item(_item(i))

    logged = [r for r in caplog.records if '持久化失败' in r.getMessage()]
    assert c.data_queue.persist_failures == 50, '计数不能被限流影响'
    assert len(logged) == 1, f'50 条失败打了 {len(logged)} 行日志 —— 未限流'


def test_persist_failure_is_visible_in_get_stats(tmp_path, monkeypatch):
    """计数必须从 get_stats() 出去（/api/system/status 的 collector 段读它）"""
    c = _bare_collector(10, tmp_path, monkeypatch)
    assert 'queue_persist_failures' not in c.get_stats(), '无失败时不应出现该字段'

    _break_persistence(c.data_queue, tmp_path)
    c.data_queue._persist_item(_item(1))

    assert c.get_stats()['queue_persist_failures'] == 1


def test_health_check_degrades_on_persist_failure():
    """健康检查必须因落盘失败而降级 —— 只算不报等于没算"""
    from core.health_checker import _check_collector

    healthy = _check_collector(_FakeCollector({'running': True, 'queue_size': 5}))
    assert healthy['status'] == 'healthy'

    degraded = _check_collector(
        _FakeCollector({'running': True, 'queue_size': 5, 'queue_persist_failures': 7}))
    assert degraded['status'] == 'degraded', '落盘失败却仍报健康'
    assert '落盘失败' in degraded['message']


class _FakeCollector:
    def __init__(self, stats):
        self._stats = stats

    def get_stats(self):
        return dict(self._stats)


# ---------------------------------------------------------------------------
# 6. 接线守卫（AST）
# ---------------------------------------------------------------------------

def _process_data_fn():
    tree = ast.parse(COLLECTOR_SRC.read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == '_process_data':
            return node
    raise AssertionError('data_collector.py 里找不到 _process_data')


def test_failure_branch_calls_requeue_helper():
    """失败分支必须走 _requeue_failed_batch（分类逻辑只有一份实现）"""
    fn = _process_data_fn()
    called = {n.func.attr for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert '_requeue_failed_batch' in called, \
        '写库失败分支没有调用 _requeue_failed_batch —— 分类逻辑被内联/拆散了'


def test_persistence_only_cleared_after_successful_write():
    """⚠️ 关键不变量：`clear_persistence` 只能在**写库成功**时调用。

    写库失败时数据刚被重新入队，此刻它们只存在于内存里，磁盘副本是唯一的
    崩溃保护。无条件清除 = 崩溃就真丢。
    判据走 AST：**每一处** clear_persistence 调用都必须位于
    「以 db_write_ok 为条件」的 if 体内。
    """
    fn = _process_data_fn()

    # 建立「子节点 → 父节点」映射，才能判断某个调用是否被 if 包住
    parents = {}
    for parent in ast.walk(fn):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    offenders = []
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'clear_persistence'):
            continue
        # 向上找最近的 ast.If；找到且条件含 db_write_ok 才算受保护
        cur, guard_ok = node, False
        while id(cur) in parents:
            cur = parents[id(cur)]
            if isinstance(cur, ast.If):
                guard_ok = 'db_write_ok' in ast.unparse(cur.test)
                break
            if isinstance(cur, (ast.FunctionDef, ast.Module)):
                break
        if not guard_ok:
            offenders.append(f'line {node.lineno}: {ast.unparse(node)}')

    assert not offenders, (
        "存在未被「写库成功」守卫的 clear_persistence 调用 —— 写库失败时清除"
        "持久化文件会造成真实数据丢失：\n  " + '\n  '.join(offenders)
    )
