"""采集队列背压回归测试（审计 P1-3）。

背景（2026-09-16 代码质量审计）：
``采集层/data_collector.py`` 的采集链路是"采集 → 在回调里 ``_schedule_next()``
排下一次"的**串行**结构。其中断路器降级分支用的是阻塞式
``self.data_queue.put(item)`` —— 队列有上限（``DiskBackedQueue(maxsize=200000)``），
一旦消费端卡住，``put()`` 会**永久阻塞**：

- 该设备的采集链再也走不到 ``_schedule_next()`` → **从此静默停止采集**
- 不报错、不告警 → 操作员看到的是"设备正常但数据不再更新"

同一文件里另外两处入队用的是"非阻塞 + 丢最旧"，行为不一致。本文件把这个
不变量固定下来：**入队路径永远不得阻塞**。
"""
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _bare_collector(queue_maxsize: int):
    """不跑 __init__（会去连设备/建线程池），只装配入队相关的属性"""
    from 采集层.data_collector import DataCollector, DiskBackedQueue

    collector = DataCollector.__new__(DataCollector)
    collector.data_queue = DiskBackedQueue(maxsize=queue_maxsize)
    collector.stats = {'dropped_items': 0}
    collector._stats_lock = threading.Lock()
    return collector


def test_enqueue_drop_oldest_returns_true_while_not_full(tmp_path, monkeypatch):
    monkeypatch.setenv('SCADA_QUEUE_PERSIST_DIR', str(tmp_path / 'q'))
    collector = _bare_collector(queue_maxsize=3)

    for i in range(3):
        assert collector._enqueue_drop_oldest({'i': i}) is True
    assert collector.data_queue.full()


def test_enqueue_drop_oldest_does_not_block_when_full(tmp_path, monkeypatch):
    """队列满时必须立刻返回并丢最旧，绝不能阻塞。"""
    monkeypatch.setenv('SCADA_QUEUE_PERSIST_DIR', str(tmp_path / 'q'))
    collector = _bare_collector(queue_maxsize=3)
    for i in range(3):
        collector._enqueue_drop_oldest({'i': i})

    start = time.monotonic()
    accepted = collector._enqueue_drop_oldest({'i': 99})
    elapsed = time.monotonic() - start

    assert accepted is True, '丢最旧之后应当能入队成功'
    assert elapsed < 0.5, f'入队阻塞了 {elapsed:.2f}s —— 采集线程会被永久挂起'
    # 丢的是最旧的一条（i=0），新数据在队尾
    assert collector.data_queue.get_nowait()['i'] == 1


def test_dropped_items_is_counted(tmp_path, monkeypatch):
    """真被丢弃（连丢最旧都腾不出位置）时必须计数，不能静默。"""
    monkeypatch.setenv('SCADA_QUEUE_PERSIST_DIR', str(tmp_path / 'q'))
    collector = _bare_collector(queue_maxsize=2)

    # 手动制造"丢完最旧仍然满"的竞态：先塞满，再让 get_nowait 拿不到东西
    for i in range(2):
        collector._enqueue_drop_oldest({'i': i})

    original_get_nowait = collector.data_queue.get_nowait
    collector.data_queue.get_nowait = lambda: (_ for _ in ()).throw(__import__('queue').Empty())

    accepted = collector._enqueue_drop_oldest({'i': 99})
    collector.data_queue.get_nowait = original_get_nowait

    assert accepted is False, '腾不出位置时应当返回 False'
    assert collector.stats['dropped_items'] == 1, '被丢弃的数据必须计数'


def test_no_blocking_put_on_data_queue():
    """静态守卫：采集入队不得使用阻塞式 put()（必须 put_nowait）。

    用 AST 而不是正则 —— 否则模块自己的注释/docstring 提到 ``data_queue.put()``
    就会误报（第一次写这条守卫时正是这么翻车的）。
    """
    import ast

    source = (REPO_ROOT / '采集层' / 'data_collector.py').read_text(encoding='utf-8')
    tree = ast.parse(source)

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != 'put':
            continue
        owner = func.value
        if isinstance(owner, ast.Attribute) and owner.attr == 'data_queue':
            offenders.append(f'{node.lineno}: {ast.unparse(node)}')

    assert not offenders, (
        'data_queue 上出现阻塞式 put() —— 队列满时会让采集线程永久挂起，'
        '该设备会静默停止采集且不报错。请改用 _enqueue_drop_oldest()：\n  '
        + '\n  '.join(offenders)
    )
