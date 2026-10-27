# -*- coding: utf-8 -*-
"""「已取消设备」登记表无界增长（round 289）。

缺陷形态（缺陷族：**状态只增不减 / 通道失败静默** 的存储侧变体）
--------------------------------------------------------------
`DataCollector._cancelled_devices` 的用途是**挡住复活**：设备删掉后，正在线程池里
跑的那一轮采集结束时仍会走 `_schedule_next()`，必须让它看见标记才不重新挂 Timer
（见 `__init__` 与 `tests/test_collector_task_cancel.py`）。

但这个集合此前**只在 `start()` 里 clear()**：
* 运行期每删一台设备 → `.add()` 一次；
* `sync_device_tasks()` 因配置热重载/禁用而停掉的设备也走 `remove_device_task()` → 再 +1；
* **没有任何路径会把单个 id 移出去**。

后果不是崩溃，而是**只增不减**：长时间运行 + 频繁增删（或配置反复热重载）时，
这个集合会累积成一张永远变大的表。它同时是推送型设备「幽灵数据」的拦截判据
（`on_data` 里 `device_id in self._cancelled_devices`），所以它涨到多大、
什么时候能回收，必须有一个明确判据，而不是"反正重启就清"。

修法
----
1. 登记时同步写 `_cancelled_at[device_id] = time.monotonic()`（**单调时钟**：
   墙钟回拨会让"年龄"变负 → 永不过期，与 round 264 断路器同一坑）；
2. `_prune_cancelled()` 裁掉「年龄 ≥ `CANCELLED_RETENTION_S`（300s）**且**
   该设备已无定时器」的登记；
3. 每次登记、每次 `get_stats()` 都顺手裁一次 → 规模上界 = 保留期内的删除次数，
   且在长期没有新删除时也会自行回收；
4. `get_stats()` 暴露 `cancelled_devices` 计数（只增不减的东西必须可观测）。

为什么保留期是 300s 而不是删完就扔
----------------------------------
`remove_device_task()` 取消不到**已在跑**的那一轮；那一轮的时长上界是单次采集
超时（Modbus 1s、REST 十几秒），几百倍余量足够，同时远小于运行尺度。
裁剪后仍安全的依据：这些登记唯一挡住的是"复活"与"幽灵回调"，而两者都只可能
发生在设备刚删的瞬间；重新添加设备的路径由 `start_device_task()` 的 discard 负责，
与是否过期无关（有用例钉住）。
"""

import time

import pytest
from unittest.mock import MagicMock

from 采集层.data_collector import DataCollector, CANCELLED_RETENTION_S


@pytest.fixture
def collector():
    dm = MagicMock()
    dm.get_client.return_value = MagicMock(connected=True)
    dm.get_device_status.return_value = {}
    c = DataCollector(dm, MagicMock())
    c.running = True
    yield c
    c.running = False
    with c._tasks_lock:
        for t in c.tasks.values():
            t.cancel()
        c.tasks.clear()
    c._pool.shutdown(wait=False)


def _age(collector, device_id, seconds=CANCELLED_RETENTION_S + 1):
    """把某条登记的时间戳往前拨（等价于"时间过去了 seconds 秒"）。"""
    collector._cancelled_at[device_id] -= seconds


def test_cancelled_entry_expires_after_retention(collector):
    """超过保留期且已无定时器的登记，必须能被裁掉。"""
    collector.remove_device_task('d1')
    assert 'd1' in collector._cancelled_devices, "前提不成立：登记没写进去"
    _age(collector, 'd1')

    # 任意一次新登记都会触发一次裁剪（生产里就是"又删了一台设备"）
    collector.remove_device_task('d2')

    assert 'd1' not in collector._cancelled_devices, (
        "过期的「已取消」登记没有被裁剪 —— 这个集合只增不减，"
        "长时间运行 + 频繁增删设备就是无界增长"
    )
    assert 'd1' not in collector._cancelled_at, "集合删了但时间戳还在 —— 两表不同步"
    assert 'd2' in collector._cancelled_devices, "刚登记的 d2 被误裁了"


def test_fresh_cancelled_entry_is_kept(collector):
    """反向对照：**没**过期的登记必须原样保留。

    否则「把裁剪写成清空」这种改法也能让上一条用例变绿 —— 而它会让
    `_schedule_next` 立刻复活刚删的设备（僵尸采集）。
    """
    collector.remove_device_task('d1')
    collector.remove_device_task('d2')

    assert 'd1' in collector._cancelled_devices, (
        "刚登记的设备被裁掉了 —— 在途采集结束时会把已删除的设备重新挂上定时器"
    )
    assert 'd2' in collector._cancelled_devices


def test_prune_keeps_devices_that_still_have_a_timer(collector):
    """防御性：登记过期了但还有活的定时器 → 不许裁（否则那个定时器会继续跑）。"""
    collector.remove_device_task('d1')
    _age(collector, 'd1')
    collector.tasks['d1'] = MagicMock()      # 模拟"意外还留着一个定时器"

    collector.remove_device_task('d2')

    assert 'd1' in collector._cancelled_devices, (
        "设备还有活定时器却被裁掉了登记 —— 该定时器链会继续轮询一台已删除的设备"
    )


def test_cancelled_set_stays_bounded_under_churn(collector):
    """200 台设备删完之后，集合规模只能是「保留期内」的那些，不是 200。"""
    for i in range(200):
        collector.remove_device_task(f'dev{i:03d}')

    # 前面 199 台是"很久以前"删的，最后一台是刚删的
    for i in range(199):
        _age(collector, f'dev{i:03d}')

    collector.remove_device_task('trigger')     # 一次新登记 → 触发裁剪

    assert len(collector._cancelled_devices) == 2, (
        f"裁剪后应只剩 dev199 + trigger 两条，实际 {len(collector._cancelled_devices)} 条 —— "
        "说明登记表随删除次数线性增长"
    )
    assert 'dev199' in collector._cancelled_devices


def test_readd_after_removal_clears_timestamp(collector):
    """删掉再重新添加：集合与时间戳必须一起清，否则下次裁剪会误判。"""
    collector.remove_device_task('d1')
    collector.start_device_task('d1', {'id': 'd1', 'protocol': 'modbus_tcp',
                                       'collection_interval': 60, 'enabled': True})

    assert 'd1' not in collector._cancelled_devices
    assert 'd1' not in collector._cancelled_at, (
        "重新添加设备后时间戳残留 —— 下次裁剪会拿旧时刻算年龄"
    )


def test_start_clears_timestamps(collector):
    """进程内重启采集器：两表一起清（否则清完集合、时间戳还占着内存）。"""
    collector.remove_device_task('d1')
    collector.running = False
    collector.start()

    assert not collector._cancelled_devices
    assert not collector._cancelled_at, "start() 清了集合但没清时间戳"


def test_get_stats_reports_cancelled_count(collector):
    """只增不减的东西必须可观测：统计里要能看见它的规模。"""
    assert 'cancelled_devices' not in collector.get_stats(), (
        "没有登记时不该凭空出现这个字段（0 值不报，与其它统计一致）"
    )

    collector.remove_device_task('d1')

    assert collector.get_stats().get('cancelled_devices') == 1, (
        "get_stats() 没有暴露「已取消」登记的规模 —— "
        "运维无法发现它在长期增长（这正是本缺陷活了这么久的原因）"
    )


def test_get_stats_prunes_expired_entries(collector):
    """长期没有新的删除动作时，读统计也应回收过期登记。"""
    collector.remove_device_task('d1')
    _age(collector, 'd1')

    stats = collector.get_stats()

    assert 'cancelled_devices' not in stats, "过期登记没有被统计路径回收"
    assert 'd1' not in collector._cancelled_devices


def test_cancelled_timestamp_uses_monotonic_clock():
    """登记时刻必须用单调时钟：墙钟回拨会让年龄变负 → 永不过期。

    与 round 264（断路器冷却用 `time.monotonic()`）同一判据。
    这里用源码扫描而不是运行时断言 —— 运行时看不出用的是哪个时钟。
    """
    import inspect
    src = inspect.getsource(DataCollector._mark_cancelled)

    assert 'time.monotonic()' in src, (
        "_mark_cancelled 没用 time.monotonic —— 墙钟回拨会让登记永不过期"
    )
    assert 'time.time()' not in src, (
        "_mark_cancelled 用了墙钟 time.time() —— 校时回拨会让年龄变负，登记永不过期"
    )
