"""短帧 / 截断应答不得被当成有效值（追溯表 SF-09）。

## 为什么写这一份

SF-09 原来标 partial，note 是「只模拟 socket；缺少真实 PLC/网关的异常帧样本库」。

但「短帧」这件事**不需要真 PLC**：协议层只要返回的寄存器数量少于请求数量，
就构成了短帧。这一层用 mock 客户端就能造，而且这才是最容易出错的地方 ——
真实异常帧样本库解决的是「CRC 错/超长帧」这类传输层问题（那份确实要现场抓），
但「少给了几个寄存器就补 0」是本系统自己的逻辑缺陷，跟硬件无关。

## 不变量（用户口径）

> 半帧/短应答必须判失败，禁止补 0 或补 False 冒充有效值。

所以断言的重点不是「没崩」，而是：
  · 该寄存器**没有产生记录**（不是产生一条 value=0 的记录）；
  · 计入 `dropped_register_short` 统计（可观测，不是静默丢）；
  · 同一批里长度够的寄存器照常入队（不能因为一个坏寄存器丢整批）。

⚠️ 这条路径原来**零测试覆盖**：`dropped_register_short` 在测试里 grep 不到。
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from 采集层.data_collector import DataCollector


def _reg(addr: int, name: str, data_type: str = 'uint16') -> dict:
    return {'address': addr, 'name': name, 'data_type': data_type,
            'scale': 1, 'unit': '', 'access': 'r'}


def _make(registers: list[dict], returned: list[int]):
    """构造一个只跑 `_collect_device_data_inner` 的采集器。"""
    dc = object.__new__(DataCollector)
    dc.stats = {}
    dc._stats_lock = threading.Lock()
    dc.device_manager = MagicMock()

    client = MagicMock()
    client.connected = True
    # 不支持 last_read_source 的客户端视为新鲜数据（见 _read_is_stale）
    del client.last_read_source
    client.read_holding_registers.return_value = returned
    dc.device_manager.get_client.return_value = client

    dc.data_queue = MagicMock()
    dc.data_queue.full.return_value = False
    collected: list[dict] = []
    dc._enqueue_drop_oldest = lambda item: (collected.append(item), True)[1]

    cfg = {'protocol': 'modbus_tcp', 'registers': registers}
    return dc, cfg, collected


class TestShortFrame:
    def test_short_frame_register_is_dropped_not_zero_filled(self):
        """两个 uint16 只要回 1 个：第二个必须**不存在**，不是 value=0。"""
        regs = [_reg(0, 'r0'), _reg(1, 'r1')]
        dc, cfg, got = _make(regs, [1234])          # 少一个

        dc._collect_device_data_inner('D1', cfg, 'modbus_tcp')

        names = [it['register_name'] for it in got]
        assert 'r0' in names, '长度够的寄存器应照常入队'
        assert 'r1' not in names, (
            '短帧的寄存器不能入队 —— 补一条 value=0 的记录就是「补 0 冒充有效值」'
        )
        assert dc.stats.get('dropped_register_short', 0) == 1, (
            f'短帧必须计入 dropped_register_short，实际 stats={dc.stats}'
        )

    def test_empty_response_drops_everything(self):
        """一个都没回（空数组）：全部丢弃，且不产生任何记录。"""
        regs = [_reg(0, 'a'), _reg(1, 'b')]
        dc, cfg, got = _make(regs, [])

        dc._collect_device_data_inner('D1', cfg, 'modbus_tcp')

        assert got == [], f'空应答不该产生任何记录，实际 {got}'
        assert dc.stats.get('dropped_register_short', 0) == 2

    def test_float32_truncated_in_the_middle(self):
        """float32 占 2 个寄存器，只回 1 个 → 必须丢，不能拿半个字解码。"""
        regs = [_reg(0, 'speed', 'float32')]
        dc, cfg, got = _make(regs, [0x4248])        # 只有高字

        dc._collect_device_data_inner('D1', cfg, 'modbus_tcp')

        assert got == [], '半个 float32 不能解码成值（那会是一个完全错误的小数）'
        assert dc.stats.get('dropped_register_short', 0) == 1

    def test_good_registers_survive_a_bad_neighbour(self):
        """同一批里好坏混排：坏的丢、好的留，不能整批报废。"""
        regs = [_reg(0, 'ok1'), _reg(1, 'bad', 'float32'), _reg(3, 'ok2')]
        # 请求 4 个寄存器（0..3），只回 2 个 → ok1 够、bad 不够、ok2 完全没有
        dc, cfg, got = _make(regs, [11, 22])

        dc._collect_device_data_inner('D1', cfg, 'modbus_tcp')

        names = [it['register_name'] for it in got]
        assert names == ['ok1'], f'只应留下 ok1，实际 {names}'
        assert dc.stats.get('dropped_register_short', 0) == 2


class TestShortFrameChunkedPath:
    """分块读取路径（寄存器数 > 125）—— 与单次读取是**两条独立实现**。"""

    def _chunked(self, n: int, returned_per_call: list[list[int]]):
        regs = [_reg(i, f'r{i}') for i in range(n)]
        dc, cfg, got = _make(regs, [])
        client = dc.device_manager.get_client.return_value
        calls = {'n': 0}

        def _read(addr, count):
            idx = calls['n']
            calls['n'] += 1
            return returned_per_call[idx] if idx < len(returned_per_call) else []

        client.read_holding_registers.side_effect = _read
        return dc, cfg, got, calls

    def test_chunked_short_frame_dropped(self):
        """130 个寄存器走分块；某块短了，那块里的寄存器必须丢。"""
        # uint16 宽 1，step = 125 - (1-1) = 125，所以 130 个分两块：125 + 5
        dc, cfg, got, calls = self._chunked(130, [[1] * 125, [1] * 5])
        dc._collect_device_data_inner('D1', cfg, 'modbus_tcp')

        assert len(got) == 130, f'完整应答应全部入队，实际 {len(got)}'
        assert dc.stats.get('dropped_register_short', 0) == 0

    def test_chunked_short_frame_is_not_zero_filled(self):
        """分块路径同样不许补 0：短了的块里那些寄存器直接没有记录。"""
        # 第一块只要 125 却只回 60 → 前 60 个能解，后 65 个丢；
        # 第二块（125..129）返回空 → 那 5 个也丢
        dc, cfg, got, calls = self._chunked(130, [[1] * 60, []])
        dc._collect_device_data_inner('D1', cfg, 'modbus_tcp')

        assert len(got) == 60, f'只应解出前 60 个，实际 {len(got)}'
        assert all(it['value'] == 1 for it in got), (
            '解出来的值必须是应答里真实存在的，不能是 0 填充'
        )
        assert dc.stats.get('dropped_register_short', 0) == 70, (
            f'65 + 5 = 70 个寄存器应计为短帧丢弃，实际 '
            f'{dc.stats.get("dropped_register_short", 0)}'
        )
