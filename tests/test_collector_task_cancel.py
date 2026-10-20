# -*- coding: utf-8 -*-
"""采集任务「删了又活」竞态的回归测试（round 219）。

缺陷
----
`DataCollector` 用 `threading.Timer` 自调度：每次采集结束后 `_schedule_next()`
新建下一个 Timer 并写回 `self.tasks[device_id]`。
而 `remove_device_task()` 只能 `cancel()` **还没触发**的 Timer。

于是这个顺序会出问题：

1. 设备 d1 的 Timer 触发，`collect_task` 在**线程池**里跑（最长等 30s）；
2. 这期间用户删除设备 → `remove_device_task('d1')` 把 `tasks` 里的 Timer 弹掉，
   但那个正在跑的采集**取消不到**；
3. 采集结束 → `_schedule_next()` 无条件新建 Timer → **d1 又回到 `self.tasks`**。

两个后果：

* **僵尸采集**：设备已从 `device_manager` 删除，采集器仍按间隔轮询它，
  `failed_collections` 每轮 +1，健康指标被一个不存在的设备污染；
* **删了再加不采集**（更隐蔽、用户直接可见）：`start_device_task()` 看到
  `device_id in self.tasks` 就「已有任务，跳过」→ 同一 id 重新添加设备后
  **采集永不启动**，必须重启进程才恢复。

修法：加 `_cancelled_devices` 登记集合，`_schedule_next()` 除 `running` 外
还要检查它；`start_device_task()` 重新添加时解除登记；`start()` 新一轮清空。
"""

import threading
import time
from unittest.mock import MagicMock

import pytest

from 采集层.data_collector import DataCollector


@pytest.fixture
def collector():
    dm = MagicMock()
    dm.get_client.return_value = MagicMock(connected=True)
    dm.get_device_status.return_value = {}
    c = DataCollector(dm, MagicMock())
    c.running = True
    yield c
    # 收尾：停掉所有定时器与线程池，避免污染后续测试
    c.running = False
    with c._tasks_lock:
        for t in c.tasks.values():
            t.cancel()
        c.tasks.clear()
    c._pool.shutdown(wait=False)


def _cfg(device_id='d1', interval=60):
    return {'id': device_id, 'protocol': 'modbus_tcp',
            'collection_interval': interval, 'enabled': True}


def test_remove_device_task_marks_cancelled(collector):
    collector._start_device_collection('d1', _cfg())
    assert 'd1' in collector.tasks

    collector.remove_device_task('d1')

    assert 'd1' not in collector.tasks, "定时器没被摘掉"
    assert 'd1' in collector._cancelled_devices, (
        "删除设备后没有登记「已取消」—— 正在跑的那一轮结束后会把任务复活"
    )


def test_running_collection_does_not_resurrect_after_removal(collector):
    """核心用例：采集正在跑时删设备，跑完后**不许**重新挂上定时器。"""
    started = threading.Event()
    release = threading.Event()

    def slow_collect(*_a, **_kw):
        started.set()
        release.wait(10)
        return True

    collector._collect_device_data = slow_collect
    collector._start_device_collection('d1', _cfg())

    assert started.wait(5), "采集任务没启动（Timer 0.1s 后应触发）"
    collector.remove_device_task('d1')   # 采集还在线程池里跑
    release.set()                        # 让它跑完

    # 给 _schedule_next 一点时间执行
    deadline = time.time() + 5
    while time.time() < deadline:
        if 'd1' in collector.tasks:
            break
        time.sleep(0.05)

    assert 'd1' not in collector.tasks, (
        "设备已删除，采集结束后又新建了定时器 —— 僵尸任务复活（会继续轮询、"
        "并让同一 id 重新添加时被判为「已有任务」而永不采集）"
    )


def test_readd_after_removal_clears_cancelled_flag(collector):
    """删掉再重新添加同一台设备，必须能重新开始采集。"""
    collector._start_device_collection('d1', _cfg())
    collector.remove_device_task('d1')

    collector.start_device_task('d1', _cfg())

    assert 'd1' not in collector._cancelled_devices, (
        "重新添加设备时没有解除「已取消」标记 → _schedule_next 会一直拒绝调度，"
        "设备加回来了却永远不采集"
    )
    assert 'd1' in collector.tasks, "重新添加设备后没有创建采集任务"


def test_start_clears_cancelled_set(collector):
    """进程内重启采集器（stop → start）不应被上一轮的取消记录挡住。"""
    collector.remove_device_task('d1')
    assert 'd1' in collector._cancelled_devices

    collector.running = False
    collector.start()   # device_manager 是 MagicMock，get_all_devices 返回空

    assert not collector._cancelled_devices, (
        "start() 没有清空 _cancelled_devices —— 上一轮删过的设备 id 会被永久挡住"
    )


def test_push_callback_drops_data_after_removal(collector):
    """推送型设备（MQTT/OPC UA）删除后，残留回调不许再往队列里塞数据。

    轮询型靠 Timer 取消；推送型没有 Timer，靠的是客户端回调。
    设备删除后客户端可能还没来得及断开，回调仍会被调用 —— 若不拦截，
    已删除设备的数据照样入库，而且绕过了 `remove_device_task` 的追踪清理。
    """
    client = MagicMock(connected=True)
    client.connect.return_value = True
    collector.device_manager.get_client.return_value = client

    collector._setup_push_device('d1', {'id': 'd1', 'protocol': 'mqtt'})
    callback = client.add_data_callback.call_args[0][0]

    callback('d1', 'temp', 25.0, 'C')
    before = collector.data_queue.qsize()
    assert before >= 1, "回调没有把数据放进队列，用例前提不成立"

    collector.remove_device_task('d1')
    callback('d1', 'temp', 26.0, 'C')

    assert collector.data_queue.qsize() == before, (
        "设备已删除，推送回调仍把数据写进了队列 —— 已删设备的数据会继续入库"
    )
