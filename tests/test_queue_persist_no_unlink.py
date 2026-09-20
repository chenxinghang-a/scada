"""队列持久化文件「不得删除、只能截断」的防回归守卫。

背景（2026-09-20 实机故障，排查耗时数小时）
==========================================
``clear_persistence()`` 原本每落库一批就 ``unlink()`` 一次队列持久化文件
（每批 0.5s，即每秒数次）。当运行环境存在「同一轮内批量删除保护」时
（本机工作台阈值 50），删除调用累积到阈值会被拦截，并且**不是抛异常，
而是挂起调用线程**：

  - 消费线程永久卡在 unlink 这一行 → **落库完全停滞**
  - 采集线程照常运行（另一个线程）→ 外部看起来"系统在跑"
  - 队列文件持续膨胀到 160MB
  - **日志完全静默**（线程被挂起，既无异常也无输出）
  - 原有健康检查全绿（database 只做 SELECT 1、collector 只看内存队列长度）

``_recover_from_disk()`` 的 unlink 更直接：启动阶段若残留文件行数超过阈值，
会被拦下并**终止进程**（实测 count=51, threshold=50）。

因此这两个方法必须使用 **truncate**，绝不可回退成 unlink。
本文件同时提供静态守卫（AST）与行为守卫（真实调用），任何回退都会被拦下。
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / '采集层' / 'data_collector.py'

# 被守卫的方法：这些方法里的文件清理动作必须用截断
GUARDED_METHODS = ('clear_persistence', '_recover_from_disk')


def _method_nodes():
    """取出被守卫方法的 AST 节点。"""
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in GUARDED_METHODS:
            found[node.name] = node
    return found


def test_guarded_methods_exist():
    """守卫自身有效性：被守卫的方法必须存在（防止改名后守卫静默失效）。"""
    nodes = _method_nodes()
    missing = [n for n in GUARDED_METHODS if n not in nodes]
    assert not missing, (
        f'被守卫的方法不存在: {missing} —— 若是重命名，请同步更新本测试，'
        f'否则守卫会静默失效'
    )


def test_no_unlink_in_guarded_methods():
    """静态守卫：clear_persistence / _recover_from_disk 不得出现 .unlink(...)。"""
    offenders = []
    for name, node in _method_nodes().items():
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == 'unlink'
            ):
                offenders.append(f'{name} (line {sub.lineno}): {ast.unparse(sub)}')

    assert not offenders, (
        '队列持久化文件被删除（unlink）—— 这会触发运行环境的批量删除保护，'
        '导致消费线程被挂起、落库静默停滞（见 2026-09-20 故障复盘）。\n'
        '请改用截断：\n'
        "    with open(self._persist_file, 'w', encoding='utf-8'):\n"
        '        pass\n'
        '涉及位置：\n  ' + '\n  '.join(offenders)
    )


def test_clear_persistence_keeps_file_but_empties_it(tmp_path, monkeypatch):
    """行为守卫：清空后文件必须**仍然存在**且内容为空。"""
    monkeypatch.setenv('SCADA_QUEUE_PERSIST_DIR', str(tmp_path / 'q'))
    from 采集层.data_collector import DiskBackedQueue

    q = DiskBackedQueue(maxsize=100)
    q.put({'device_id': 'd1', 'register_name': 'r1', 'value': 1.0})
    assert q._persist_file.exists()
    assert q._persist_file.stat().st_size > 0, '前置条件：写入后文件应有内容'

    q.clear_persistence()

    assert q._persist_file.exists(), (
        'clear_persistence 把文件删掉了 —— 删除会触发批量删除保护，'
        '消费线程会被挂起。必须改为截断内容、保留文件。'
    )
    assert q._persist_file.stat().st_size == 0, 'clear_persistence 应清空文件内容'


def test_recover_from_disk_keeps_file(tmp_path, monkeypatch):
    """行为守卫：启动恢复后文件同样不得被删除。"""
    monkeypatch.setenv('SCADA_QUEUE_PERSIST_DIR', str(tmp_path / 'q'))
    from 采集层.data_collector import DiskBackedQueue

    first = DiskBackedQueue(maxsize=100)
    for i in range(5):
        first.put({'device_id': 'd1', 'register_name': f'r{i}', 'value': float(i)})
    persist_file = first._persist_file
    assert persist_file.exists() and persist_file.stat().st_size > 0

    # 模拟重启：新实例会从磁盘恢复
    second = DiskBackedQueue(maxsize=100)
    assert second.qsize() == 5, '应从磁盘恢复 5 条'

    assert persist_file.exists(), (
        '_recover_from_disk 把文件删掉了 —— 残留行数超阈值时启动阶段会被'
        '直接拦下并终止进程。必须改为截断。'
    )
    assert persist_file.stat().st_size == 0, '恢复后应清空文件内容'
