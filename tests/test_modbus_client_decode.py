"""`采集层/modbus_client.py` 的**纯函数**行为测试（解码 / 标度变换 / 地址校验 / 缓存语义）。

为什么先测这些
--------------
该模块是全仓最大的覆盖缺口（557 语句 / 318 未覆盖），而它的读写在测试里需要真实
模拟器（`test_modbus_protocol_e2e.py` 等），跑得慢、覆盖不到分支。
但模块里有一批**纯函数**：解码、标度变换、地址校验、缓存年龄 —— 它们正是
「值算错了但看起来正常」的藏身处（现场表现为"读数不对但界面一切正常"）。

本轮顺带修了一处**文档与实现矛盾**：`ByteOrder` 的原注释把 BADC 写成"字交换"、
CDAB 写成"Little-endian字序"，与实现**正好相反** —— 现场照注释配 `byte_order`
就会配错（详见 `ByteOrder` 的 docstring）。
"""
import struct
import time

import pytest

from 采集层.modbus_client import ByteOrder, ModbusClient


def _client(**cfg):
    base = {'id': 'dev1', 'name': '测试设备', 'protocol': 'modbus_tcp',
            'registers': [], **cfg}
    return ModbusClient(base)


def _swap16(w: int) -> int:
    """16 位字内字节交换（BADC 的基础操作）"""
    return ((w & 0xFF) << 8) | ((w >> 8) & 0xFF)


def _f32_registers(value: float, order: str) -> list[int]:
    """按**字节排列定义**构造 float32 的寄存器对（不依赖被测代码）。

    先取 IEEE754 大端四字节 ``b0 b1 b2 b3``（``ABCD``），再按命名变换：
      ABCD → 原样；BADC → 字内字节交换；CDAB → 两字交换；DCBA → 四字节全反转。
    """
    b = struct.pack('>f', value)
    w0 = int.from_bytes(b[0:2], 'big')
    w1 = int.from_bytes(b[2:4], 'big')
    if order == 'ABCD':
        return [w0, w1]
    if order == 'BADC':
        return [_swap16(w0), _swap16(w1)]
    if order == 'CDAB':
        return [w1, w0]
    if order == 'DCBA':
        return [_swap16(w1), _swap16(w0)]
    raise AssertionError(order)


# ---------------------------------------------------------------------------
# 1. 整数解码
# ---------------------------------------------------------------------------

class TestIntegerDecode:
    @pytest.mark.parametrize('raw,expected', [
        (0x0000, 0), (0x0001, 1), (0x7FFF, 32767), (0x8000, 32768), (0xFFFF, 65535),
    ])
    def test_uint16(self, raw, expected):
        assert _client().decode_uint16(raw) == expected

    @pytest.mark.parametrize('raw,expected', [
        (0x0000, 0), (0x7FFF, 32767), (0x8000, -32768), (0xFFFF, -1),
    ])
    def test_int16(self, raw, expected):
        assert _client().decode_int16(raw) == expected

    @pytest.mark.parametrize('regs,expected', [
        ([0x0000, 0x0001], 1),
        ([0x0000, 0xFFFF], 65535),
        ([0x7FFF, 0xFFFF], 2147483647),
        ([0x8000, 0x0000], -2147483648),
        ([0xFFFF, 0xFFFF], -1),
    ])
    def test_int32(self, regs, expected):
        assert _client().decode_int32(regs) == expected

    @pytest.mark.parametrize('regs,expected', [
        ([0x0000, 0x0001], 1),
        ([0xFFFF, 0xFFFF], 4294967295),
        ([0x8000, 0x0000], 2147483648),
    ])
    def test_uint32(self, regs, expected):
        assert _client().decode_uint32(regs) == expected

    # ---- round 327：32 位整数必须按 byte_order 组合 ----
    #
    # 原来 decode_int32 / decode_uint32 固定写死 (r0 << 16) | r1、完全忽略
    # byte_order —— 同一台设备配 CDAB 时 decode_float32 解对、32 位整数解错，
    # 而且不会有任何提示。下面用同一个目标值 0x12345678 的四种寄存器排列钉住。
    @pytest.mark.parametrize('bo,regs', [
        ('ABCD', [0x1234, 0x5678]),   # A B C D
        ('BADC', [0x3412, 0x7856]),   # B A D C（字内字节交换）
        ('CDAB', [0x5678, 0x1234]),   # C D A B（两字交换）
        ('DCBA', [0x7856, 0x3412]),   # D C B A（四字节反转）
    ])
    def test_uint32_respects_byte_order(self, bo, regs):
        assert _client(byte_order=bo).decode_uint32(regs) == 0x12345678

    @pytest.mark.parametrize('bo,regs', [
        ('ABCD', [0x1234, 0x5678]),
        ('BADC', [0x3412, 0x7856]),
        ('CDAB', [0x5678, 0x1234]),
        ('DCBA', [0x7856, 0x3412]),
    ])
    def test_int32_respects_byte_order(self, bo, regs):
        assert _client(byte_order=bo).decode_int32(regs) == 0x12345678

    def test_int32_negative_respects_byte_order(self):
        """负数（符号位在最高位）在非 ABCD 字节序下也要正确。"""
        # 0xFFFFFFFF = -1，四种排列下寄存器都是全 F，结果必须一致
        for bo in ('ABCD', 'BADC', 'CDAB', 'DCBA'):
            assert _client(byte_order=bo).decode_int32([0xFFFF, 0xFFFF]) == -1

    def test_int32_short_list_raises(self):
        """不足 2 个寄存器要明确报错，不能 IndexError。"""
        with pytest.raises(ValueError):
            _client().decode_int32([0x1234])

    def test_float32_and_uint32_agree_on_register_layout(self):
        """正向对照：同一组寄存器，float32 与 uint32 必须用**同一套**排列。

        防的是「两条解码路径各写一套字节序逻辑、日后只改其中一条」。
        """
        import struct as _struct
        regs = [0x1234, 0x5678]
        for bo in ('ABCD', 'BADC', 'CDAB', 'DCBA'):
            c = _client(byte_order=bo)
            expected = _struct.unpack('!f', _struct.pack('!I', c.decode_uint32(regs)))[0]
            assert c.decode_float32(regs) == pytest.approx(expected)

    def test_int16_uint16_agree_on_positive(self):
        """正向对照：正数区间两者必须一致（防其中一侧写反）"""
        c = _client()
        for raw in (0, 1, 100, 0x7FFF):
            assert c.decode_int16(raw) == c.decode_uint16(raw)


# ---------------------------------------------------------------------------
# 2. float32 解码（四种字节序，用定义构造的向量）
# ---------------------------------------------------------------------------

class TestFloat32Decode:
    VALUES = [0.0, 1.0, -1.0, 3.14159, 1234.5, -273.15, 1e-6, 1e6]

    @pytest.mark.parametrize('order', ['ABCD', 'BADC', 'CDAB', 'DCBA'])
    def test_round_trip_all_orders(self, order):
        c = _client(byte_order=order)
        for v in self.VALUES:
            regs = _f32_registers(v, order)
            got = c.decode_float32(regs)
            assert got == pytest.approx(v, rel=1e-6), f'{order} 解 {v} 得到 {got}'

    def test_orders_are_actually_different(self):
        """元守卫：四种字节序不能都解出同一个结果（否则上面那条会假绿）"""
        regs = _f32_registers(1.0, 'ABCD')
        results = {}
        for order in ('ABCD', 'BADC', 'CDAB', 'DCBA'):
            results[order] = _client(byte_order=order).decode_float32(regs)
        assert results['ABCD'] == pytest.approx(1.0)
        # 其余三种把 ABCD 的寄存器当自己的排列来解 → 必须得到**别的**值
        others = {k: v for k, v in results.items() if k != 'ABCD'}
        assert all(v is None or abs(v - 1.0) > 1e-9 for v in others.values()), \
            f'不同字节序解出相同结果，判据没有判别力: {results}'

    def test_nan_inf_return_none(self):
        """传感器断线时 PLC 常返回 0xFFFF → NaN/Inf，必须过滤成 None"""
        c = _client(byte_order='ABCD')
        assert c.decode_float32([0x7F80, 0x0000]) is None      # +Inf
        assert c.decode_float32([0xFF80, 0x0000]) is None      # -Inf
        assert c.decode_float32([0x7FC0, 0x0000]) is None      # NaN
        assert c.decode_float32([0xFFFF, 0xFFFF]) is None      # NaN

    def test_too_few_registers_raises(self):
        with pytest.raises(ValueError):
            _client().decode_float32([0x3F80])

    def test_default_byte_order_is_abcd(self):
        assert _client().byte_order is ByteOrder.ABCD

    def test_invalid_byte_order_raises_at_construction(self):
        """配置里写错 byte_order 必须**立刻**报错，不能静默用默认值"""
        with pytest.raises(ValueError):
            _client(byte_order='XYZW')


# ---------------------------------------------------------------------------
# 3. float64 解码
# ---------------------------------------------------------------------------

class TestFloat64Decode:
    @staticmethod
    def _regs_abcd(v):
        b = struct.pack('>d', v)
        return [int.from_bytes(b[i:i + 2], 'big') for i in range(0, 8, 2)]

    def test_abcd_round_trip(self):
        c = _client(byte_order='ABCD')
        for v in (0.0, 1.0, -2.5, 3.141592653589793, 1e300):
            assert c.decode_float64(self._regs_abcd(v)) == pytest.approx(v, rel=1e-12)

    def test_cdab_swaps_32bit_halves(self):
        """CDAB：交换两个 32 位半字（[w2,w3,w0,w1]）"""
        r = self._regs_abcd(1.0)
        assert _client(byte_order='CDAB').decode_float64([r[2], r[3], r[0], r[1]]) \
            == pytest.approx(1.0)

    def test_dcba_reverses_all_bytes(self):
        """DCBA：四字节全反转 = 字内交换 + 字序反转"""
        r = self._regs_abcd(1.0)
        assert _client(byte_order='DCBA').decode_float64(
            [_swap16(r[3]), _swap16(r[2]), _swap16(r[1]), _swap16(r[0])]
        ) == pytest.approx(1.0)

    def test_badc_pins_current_semantics(self):
        """⚠️ 钉住**当前实现**：float64 的 BADC = 相邻 16 位字在**每个 32 位半字内**交换
        （即 ``[w1, w0, w3, w2]``）。

        ⚠️ 注意这与 float32 的 BADC（**字内字节交换**）语义**不同**。
        64 位下这四个名字没有统一行业定义，本实现的具体映射在此固定下来；
        「是否应与 float32 统一」已列为待现场确认项（见报告），
        在确认前**不要**凭猜测改动 —— 改错会让现场读数整体错位。
        """
        r = self._regs_abcd(1.0)
        assert _client(byte_order='BADC').decode_float64([r[1], r[0], r[3], r[2]]) \
            == pytest.approx(1.0)

    def test_nan_inf_return_none(self):
        c = _client(byte_order='ABCD')
        assert c.decode_float64([0x7FF0, 0, 0, 0]) is None     # +Inf
        assert c.decode_float64([0xFFF0, 0, 0, 0]) is None     # -Inf
        assert c.decode_float64([0x7FF8, 0, 0, 0]) is None     # NaN

    def test_too_few_registers_raises(self):
        with pytest.raises(ValueError):
            _client().decode_float64([0, 0, 0])


# ---------------------------------------------------------------------------
# 4. 标度变换
# ---------------------------------------------------------------------------

class TestRawToEngineering:
    def _c(self, registers):
        return _client(registers=registers)

    def test_scale_and_offset_applied(self):
        c = self._c([{'address': 10, 'name': 'temp', 'scale': 0.1, 'offset': -40}])
        assert c._raw_to_engineering(10, 250) == pytest.approx(-15.0)

    def test_default_scale_offset(self):
        """未声明 scale/offset 时必须是恒等变换（默认 1.0 / 0）"""
        c = self._c([{'address': 10, 'name': 'raw'}])
        assert c._raw_to_engineering(10, 1234) == pytest.approx(1234.0)

    def test_address_not_in_config_returns_raw(self):
        c = self._c([{'address': 10, 'name': 'a', 'scale': 0.1}])
        assert c._raw_to_engineering(99, 1234) == pytest.approx(1234.0)

    def test_direction_matches_reverse_transform(self):
        """标度方向必须与「工程值 → 原始值」的反变换一致（device_control 写路径用它）。

        正变换：raw * scale + offset；反变换：(eng - offset) / scale。
        两者互逆 —— 这条钉住方向，防止有人把 scale 改成除法而没人发现。
        """
        c = self._c([{'address': 5, 'name': 'sp', 'scale': 0.5, 'offset': 10}])
        for raw in (0, 100, 1000):
            eng = c._raw_to_engineering(5, raw)
            assert (eng - 10) / 0.5 == pytest.approx(raw)


# ---------------------------------------------------------------------------
# 5. 地址范围校验
# ---------------------------------------------------------------------------

class TestValidateAddressRange:
    @pytest.mark.parametrize('address,count', [
        (0, 1), (0, 125), (65535, 1), (65411, 125), (100, 10),
    ])
    def test_valid(self, address, count):
        assert _client().validate_address_range(address, count) is True

    @pytest.mark.parametrize('address,count', [
        (-1, 1), (65536, 1), (0, 0), (0, 126), (65535, 2),
    ])
    def test_invalid_raises(self, address, count):
        with pytest.raises(ValueError):
            _client().validate_address_range(address, count)

    def test_boundary_exactly_fits(self):
        """边界：address + count == 65536 合法；再大 1 就非法"""
        c = _client()
        assert c.validate_address_range(65535, 1) is True
        with pytest.raises(ValueError):
            c.validate_address_range(65535, 2)


# ---------------------------------------------------------------------------
# 6. 陈旧缓存语义（"静默假成功"的修复点）
# ---------------------------------------------------------------------------

class TestCacheSemantics:
    def test_age_none_before_first_read(self):
        assert _client().get_cache_age(0, 1) is None

    def test_mark_fresh_sets_source_and_age(self):
        c = _client()
        c._mark_read_fresh((0, 2), [1, 2])
        assert c.last_read_source == 'fresh'
        assert c.last_read_ok is True
        age = c.get_cache_age(0, 2)
        assert age is not None and age >= 0.0

    def test_fallback_returns_cache_but_marks_stale(self):
        """⚠️ 核心：兜底返回缓存值时必须**显式暴露陈旧性**。

        历史缺陷：直接 return 缓存值，调用方无法区分新值与几天前的旧值，
        上游熔断器永远不跳闸（"静默假成功"）。
        """
        c = _client()
        c._mark_read_fresh((0, 2), [7, 8])
        got = c._fallback_to_cache((0, 2), 0, 2, '超时')
        assert got == [7, 8]
        assert c.stats['stale_reads'] == 1
        assert c.last_read_source == 'cache'
        assert c.last_read_ok is False

    def test_fallback_without_cache_returns_none(self):
        c = _client()
        assert c._fallback_to_cache((0, 2), 0, 2, '超时') is None
        assert c.last_read_source == 'none'
        assert c.last_read_ok is False
        assert c.stats['stale_reads'] == 0, '无缓存可用时不该计 stale'

    def test_stale_reads_accumulates(self):
        c = _client()
        c._mark_read_fresh((0, 1), [1])
        for _ in range(3):
            c._fallback_to_cache((0, 1), 0, 1, '超时')
        assert c.stats['stale_reads'] == 3

    def test_cache_key_is_address_and_count(self):
        """缓存按 (address, count) 分键 —— 不同窗口不得互相污染"""
        c = _client()
        c._mark_read_fresh((0, 2), [1, 2])
        assert c.get_cache_age(0, 4) is None
        assert c._fallback_to_cache((0, 4), 0, 4, '超时') is None


# ---------------------------------------------------------------------------
# 7. 通信日志
# ---------------------------------------------------------------------------

class TestCommLog:
    def test_log_entry_fields(self):
        c = _client()
        c._log_operation('read_holding_registers', 10, 2, True, 'ok')
        log = c.get_communication_log()
        assert len(log) == 1
        e = log[0]
        assert e['operation'] == 'read_holding_registers'
        assert e['address'] == 10 and e['count'] == 2
        assert e['success'] is True and e['detail'] == 'ok'
        assert 'timestamp' in e

    def test_limit_and_order(self):
        c = _client()
        for i in range(10):
            c._log_operation('op', i, 1, True)
        log = c.get_communication_log(limit=3)
        assert len(log) == 3
        assert [e['address'] for e in log] == [7, 8, 9], '应返回最近 3 条且按时间顺序'

    def test_deque_is_bounded(self):
        """日志必须有上限（deque maxlen），否则长跑会吃光内存"""
        c = _client()
        for i in range(1100):
            c._log_operation('op', i, 1, True)
        assert len(c.get_communication_log(limit=10_000)) == 1000
