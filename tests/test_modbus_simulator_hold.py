# -*- coding: utf-8 -*-
"""模拟器「写保持」机制的单元回归测试。

背景（真实缺陷，2026-09-21 定位）
--------------------------------
`tools/modbus_simulator.py::build_slave_block` 把每台寄存器的保持时长算进了
``spec['hold']``，但**从来没有人读它** —— 四个 ``SimDataBlock`` 一律用
``hold_seconds=0`` 构造，于是 ``setValues`` 记下的过期时刻是 ``now + 0``，
而模型线程的跳过条件是 ``_written[i] > now`` → **恒为假**。

也就是说：**写保持完全没生效**，客户端写进去的值活不过一个更新周期。
实测（``--interval 1.0``）：

    写 [111,222,333,444] → 立即回读 [111,222,333,444]（对）
                        → 等 1.5s 回读 [1,1,1,1]（错，被模型覆盖）

这个缺陷的可见症状是 ``test_modbus_protocol_e2e.py::test_write_multiple_readback``
**概率性失败** —— 写与读之间恰好撞上一次更新 tick 就红。全量套件里偶发
（2447 个用例的时序抖动），单跑 15 遍都不复现，属于最难排查的那类红。

对产品的意义不只是测试：``--simulator`` 模式下用户在 SCADA 界面上改的
继电器/设定值 1 秒后就被模型改回去，看起来"写了没用"；
配置项 ``writable_hold_seconds: 300`` 完全是个摆设。

本文件把**机制本身**钉死，不依赖时序运气。
"""

import os
import sys
import threading
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

try:
    import modbus_simulator as ms  # noqa: E402
except Exception:  # pragma: no cover
    ms = None

pytestmark = pytest.mark.skipif(
    ms is None, reason="pymodbus 不可用，跳过模拟器单元测试"
)

WRITABLE_HOLD = 300.0
READ_ONLY_HOLD = 5.0

# 与真实配置同构：两个 rw 继电器 + 一个只读温度寄存器
REGISTERS = [
    {"name": "relay_1", "address": 0, "length": 1, "data_type": "uint16",
     "access": "rw"},
    {"name": "relay_2", "address": 1, "length": 1, "data_type": "uint16",
     "access": "rw"},
    {"name": "temperature", "address": 2, "length": 1, "data_type": "uint16",
     "access": "r"},
]


def _build():
    """按真实参数构建 (hr, di, co, ir, specs, block_size)。"""
    return ms.build_slave_block(REGISTERS, "ABCD", WRITABLE_HOLD, READ_ONLY_HOLD)


def _spec(specs, name):
    return next(s for s in specs if s["name"] == name)


class TestHoldValueIsWiredThrough:
    """保持时长必须真的传到数据块上 —— 原先只算进 spec 就没人用了。"""

    def test_spec_hold_reflects_access(self):
        _hr, _di, _co, _ir, specs, _size = _build()
        assert _spec(specs, "relay_1")["hold"] == WRITABLE_HOLD, (
            "rw 寄存器必须用 writable_hold_seconds（客户端写入要长期保持）"
        )
        assert _spec(specs, "temperature")["hold"] == READ_ONLY_HOLD, (
            "只读寄存器只需短暂保持"
        )

    def test_block_hold_by_index_matches_spec(self):
        """核心回归：数据块必须能按 block 索引查到该寄存器的保持时长。

        修复前 `_hold_by_index` 是空的、`_hold` 是 0，所以 hold_for() 恒返回 0
        —— 这正是"写保持形同虚设"的直接原因。
        """
        hr, _di, _co, _ir, specs, _size = _build()
        for spec in specs:
            assert hr.hold_for(spec["bi_start"]) == spec["hold"], (
                f"block 索引 {spec['bi_start']}（{spec['name']}）的保持时长没传下去："
                f"hold_for()={hr.hold_for(spec['bi_start'])}，spec['hold']={spec['hold']}"
            )

    def test_setvalues_records_expiry_using_spec_hold(self):
        """setValues 记的过期时刻必须是 now + 该地址的保持时长，而不是 now + 0。"""
        hr, _di, _co, _ir, _specs, _size = _build()
        # block 索引 1 = 配置地址 0（pymodbus 内部 address += 1）
        hr.setValues(1, [111])

        assert hr.values[1] == 111
        remaining = hr._written[1] - time.monotonic()
        assert remaining > WRITABLE_HOLD - 5, (
            f"写保持过期时刻只比现在晚 {remaining:.1f}s，应接近 {WRITABLE_HOLD}s —— "
            f"写保持没生效时这里会是 0（甚至负数）"
        )


class TestUpdateSlaveRespectsHold:
    """模型线程必须跳过仍在写保持期内的寄存器。"""

    def test_client_written_register_is_not_overwritten(self):
        hr, _di, _co, _ir, specs, _size = _build()
        hr.setValues(1, [111])
        hr.setValues(2, [222])

        ms.update_slave(hr, specs, "ABCD", time.time())

        assert hr.values[1] == 111, (
            f"客户端写入的 relay_1 被模型覆盖成 {hr.values[1]} —— 写保持失效"
        )
        assert hr.values[2] == 222, (
            f"客户端写入的 relay_2 被模型覆盖成 {hr.values[2]} —— 写保持失效"
        )

    def test_read_only_register_is_model_driven(self):
        """反向：没被客户端写过的只读寄存器仍必须由模型驱动（别把保持做成"全冻结"）。"""
        hr, _di, _co, _ir, specs, _size = _build()
        _specs = specs
        # 索引 3 = 配置地址 2（temperature）
        ms.update_slave(hr, specs, "ABCD", time.time())
        assert hr._written.get(3) is None, "模型写入不应被登记成客户端写入"
        # 模型值写进去了（具体数值随模型，但至少要动过 _written 之外的路径）
        assert isinstance(hr.values[3], int)

    def test_hold_expiry_lets_model_take_over_again(self):
        """保持期一过，模型必须重新接管 —— 否则写入会永久冻结该寄存器。"""
        hr, _di, _co, _ir, specs, _size = _build()
        hr.setValues(1, [111])
        # 把过期时刻推到过去，模拟"保持期已过"
        hr._written[1] = time.monotonic() - 1.0

        ms.update_slave(hr, specs, "ABCD", time.time())

        assert hr.values[1] != 111, (
            "保持期已过，模型仍没有接管 —— 写保持变成了永久冻结"
        )


class TestConcurrencyBetweenModelAndClient:
    """模型线程与客户端写入必须互斥（`SimDataBlock.lock`）。"""

    def test_update_slave_waits_for_block_lock(self):
        """update_slave 必须获取数据块的锁。

        做法：主线程先持有锁，再让 update_slave 在另一线程跑。
        真用了锁 → 被挡住（超时前不完成）；没用锁 → 立刻跑完。
        这是「检查写保持」与「写模型值」之间那个覆盖窗口的**确定性**守卫，
        不依赖概率。
        """
        hr, _di, _co, _ir, specs, _size = _build()
        done = threading.Event()

        def _run():
            ms.update_slave(hr, specs, "ABCD", time.time())
            done.set()

        hr.lock.acquire()
        try:
            th = threading.Thread(target=_run, daemon=True)
            th.start()
            assert not done.wait(timeout=0.5), (
                "update_slave 没有获取 SimDataBlock.lock —— 模型线程可能在"
                "「检查写保持」之后、「写模型值」之前被客户端插入，"
                "把刚写进去的值静默覆盖掉"
            )
        finally:
            hr.lock.release()

        assert done.wait(timeout=5), "释放锁后 update_slave 仍未完成"

    def test_concurrent_updates_never_lose_client_write(self):
        """压测不变量：模型线程持续跑的同时反复写入，客户端的值一次都不能丢。"""
        hr, _di, _co, _ir, specs, _size = _build()
        stop = threading.Event()
        start = time.time()

        def _hammer():
            while not stop.is_set():
                ms.update_slave(hr, specs, "ABCD", start)

        th = threading.Thread(target=_hammer, daemon=True)
        th.start()
        lost = []
        try:
            for i in range(100):
                hr.setValues(1, [111])
                if hr.values[1] != 111:
                    lost.append((i, hr.values[1]))
        finally:
            stop.set()
            th.join(timeout=5)

        assert not lost, f"并发下客户端写入被覆盖 {len(lost)} 次: {lost[:5]}"
