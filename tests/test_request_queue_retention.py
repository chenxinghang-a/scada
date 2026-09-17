"""请求队列任务记录的回收回归测试。

背景（2026-09 审计遗留项）
--------------------------
`RequestQueue.submit()` 每次都往 `self._tasks` 写一条记录（含 `result` 引用），
而全文件**没有任何 pop/del/clear**。报表生成、批量导出、数据迁移都走这个队列，
长期运行的服务会因此无界增长，最终 OOM。

本测试锁死「`_tasks` 有上限、且只回收已结束的任务」。
"""
import threading
import time

import pytest

from core.request_queue import RequestQueue, TaskStatus, MAX_RETAINED_TASKS


@pytest.fixture
def q():
    # max_workers=0 → 不启动工作线程，测试完全同步、不受并发时序影响
    return RequestQueue('test-retention', max_workers=0, max_queue_size=10)


def _seed_finished(q, n, status=TaskStatus.COMPLETED):
    """直接塞 n 条「已结束」的历史记录"""
    now = time.time()
    with q._tasks_lock:
        for i in range(n):
            q._tasks[f'old{i}'] = {
                'id': f'old{i}',
                'status': status,
                'created_at': now - 1000 + i,
                'started_at': now - 1000 + i,
                'completed_at': now - 1000 + i,
                'result': {'payload': 'x' * 100},
                'error': None,
                'queue_position': 1,
            }


def _seed_active(q, n, status=TaskStatus.PENDING):
    """塞 n 条「未结束」的记录（排队中/执行中）"""
    now = time.time()
    with q._tasks_lock:
        for i in range(n):
            q._tasks[f'live{i}'] = {
                'id': f'live{i}',
                'status': status,
                'created_at': now,
                'started_at': None,
                'completed_at': None,
                'result': None,
                'error': None,
                'queue_position': i + 1,
            }


def test_retention_cap_is_enforced(q):
    """核心回归：历史记录不能无界增长。

    修复前 submit() 只增不减，塞多少就留多少，该断言失败。
    """
    _seed_finished(q, MAX_RETAINED_TASKS + 200)
    assert len(q._tasks) == MAX_RETAINED_TASKS + 200

    q.submit(lambda: None)          # 触发一次回收

    assert len(q._tasks) <= MAX_RETAINED_TASKS + 1, (
        f'_tasks 无界增长：{len(q._tasks)} 条（上限 {MAX_RETAINED_TASKS}）'
    )


def test_eviction_keeps_the_newest(q):
    """保留的应该是**最新**的那批，而不是最老的"""
    _seed_finished(q, MAX_RETAINED_TASKS + 50)   # old0..old549，编号越大越新

    q.submit(lambda: None)

    remaining_old = sorted(
        (k for k in q._tasks if k.startswith('old')),
        key=lambda k: int(k[3:]),
    )
    assert remaining_old, '不该把所有历史都清掉'
    # 最老的几条应已被回收，最新的应还在
    assert 'old0' not in q._tasks, '最老的记录应当先被回收'
    assert f'old{MAX_RETAINED_TASKS + 49}' in q._tasks, '最新的记录不应被回收'


def test_pending_and_running_tasks_are_never_evicted(q):
    """排队中/执行中的任务**绝不能**被回收，否则 get_status() 会突然查不到"""
    _seed_finished(q, MAX_RETAINED_TASKS + 100)
    _seed_active(q, 5, TaskStatus.PENDING)
    _seed_active(q, 3, TaskStatus.RUNNING)

    q.submit(lambda: None)

    for i in range(5):
        assert f'live{i}' in q._tasks, '排队中的任务被误回收了'
    # RUNNING 的 id 前缀也是 live，检查其状态仍在
    running = [t for t in q._tasks.values() if t['status'] == TaskStatus.RUNNING]
    assert len(running) == 3, '执行中的任务被误回收了'


def test_recent_result_still_queryable(q):
    """回收只针对旧记录：刚提交的任务必须仍能查到状态"""
    _seed_finished(q, MAX_RETAINED_TASKS + 50)

    tid = q.submit(lambda: None)

    assert q.get_status(tid) is not None, '刚提交的任务查不到状态'
    assert q.get_status(tid)['status'] == TaskStatus.PENDING


def test_failed_tasks_are_also_recycled(q):
    """失败记录同样会累积，必须一起回收"""
    _seed_finished(q, MAX_RETAINED_TASKS + 100, status=TaskStatus.FAILED)

    q.submit(lambda: None)

    assert len(q._tasks) <= MAX_RETAINED_TASKS + 1


def test_no_eviction_below_cap(q):
    """未超上限时不应误删任何记录"""
    _seed_finished(q, 10)
    before = set(q._tasks)

    q.submit(lambda: None)

    assert before.issubset(set(q._tasks)), '未超上限却删了记录'


def test_eviction_is_thread_safe(q):
    """并发 submit 时回收不能破坏内部结构"""
    _seed_finished(q, MAX_RETAINED_TASKS + 200)
    errors = []

    def worker():
        try:
            for _ in range(20):
                q.submit(lambda: None)
        except Exception as e:      # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f'并发 submit 抛异常: {errors}'
    assert len(q._tasks) <= MAX_RETAINED_TASKS + 200
