# -*- coding: utf-8 -*-
"""`gateway/modbus_gateway.py` 的解码层测试（round 213 之后补 gateway 短板第一站）。

覆盖此前接近空白的三个纯函数面：
* `_parse_byte_order` —— 字节序字符串 → pymodbus `(byteorder, wordorder)`。
  ⚠️ 钉住的是**定义**（每档两个字母各自代表什么），与 docstring 那句
  "CDAB: same as BADC for pymodbus" 的**错误表述**对照（实现里两者不同，
  注释写反了 —— 照注释配 byte_order 会整体错位）；
* `_read_register` —— uint16/int16/uint32/int32/float32/float64 的解码、
  寄存器数不足/不支持类型/Modbus 异常/普通异常各分支；
* `read_device_data` —— 单寄存器失败时的容忍（返回其余值而非整设备 None）。

字节序判据用**定义构造**：把 `字母 → Endian` 的映射写死在测试里
（A/D → 字节序、B/C → 字序的奇偶位），不依赖被测代码的 order_map。
"""
from __future__ import annotations

import pathlib
import sys
from unittest.mock import MagicMock

import pytest

_BACKEND = pathlib.Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pymodbus = pytest.importorskip("pymodbus", reason="网关独立进程依赖，未装则跳过")
from pymodbus.constants import Endian  # noqa: E402


def _gw():
    """不起连接的 ModbusGateway（__init__ 会建 pymodbus 客户端，用 __new__ 绕开）。"""
    from gateway.modbus_gateway import ModbusGateway
    import logging
    g = ModbusGateway.__new__(ModbusGateway)
    g.logger = logging.getLogger("test_modbus_gateway")
    return g


# ⚠️ 这里**不发明**「字母 → Endian」的推导规则 —— 那个推导我自己都会写错
# （第一版就写反了，被下面的元守卫抓住）。字节序的判据用**行为**说话：
# 四种映射各解同一个 float32，得到的值必须两两不同（映射一旦塌缩立刻红）。


class TestParseByteOrder:
    EXPECTED = {
        "ABCD": (Endian.BIG, Endian.BIG),
        "BADC": (Endian.LITTLE, Endian.BIG),
        "CDAB": (Endian.BIG, Endian.LITTLE),
        "DCBA": (Endian.LITTLE, Endian.LITTLE),
    }

    @pytest.mark.parametrize("order", list(EXPECTED))
    def test_definition_vectors(self, order):
        """四种字节序按定义映射（与 docstring 的错误表述脱钩）。"""
        from gateway.modbus_gateway import ModbusGateway
        assert ModbusGateway._parse_byte_order(order) == self.EXPECTED[order]

    def test_four_orders_decode_distinct_values(self):
        """行为元守卫：四种映射解同一寄存器，四个值必须**两两不同**。

        这比字母推导硬：映射一旦塌缩（比如有人按 docstring 把 BADC/CDAB 合并），
        两个值会相等 → 立刻红。寄存器值取一个「对字序/字节序都敏感」的：
        0x3F800000 是 float32 的 1.0，其寄存器形 0x3F80/0x0000 在换序后
        会变成 0x3F80/0x0000 的不同排列 —— 除恒等换序外四值必不同。
        """
        from gateway.modbus_gateway import ModbusGateway
        values = {}
        for order in self.EXPECTED:
            bo = ModbusGateway._parse_byte_order(order)
            v = _gw()._read_register(_client([0x3F80, 0x0000]), 1, 0, 2,
                                     "float32", byte_order=bo)
            values[order] = v
        uniq = set(values.values())
        assert len(uniq) == len(values), (
            f"有字节序映射塌缩成同值了: {values}")

    def test_abcd_equals_default(self):
        """ABCD 必须等于「不传 byte_order」的默认行为（pymodbus 默认 BE/BE）。"""
        from gateway.modbus_gateway import ModbusGateway
        abcd = ModbusGateway._parse_byte_order("ABCD")
        default = _gw()._read_register(_client([0x4049, 0x0FDB]), 1, 0, 2, "float32")
        via_abcd = _gw()._read_register(_client([0x4049, 0x0FDB]), 1, 0, 2,
                                        "float32", byte_order=abcd)
        assert abs(default - via_abcd) < 1e-12

    def test_badc_and_cdab_are_different(self):
        """钉住「BADC ≠ CDAB」—— docstring 曾写成 same as BADC（错的）。"""
        from gateway.modbus_gateway import ModbusGateway
        assert (ModbusGateway._parse_byte_order("BADC")
                != ModbusGateway._parse_byte_order("CDAB")), (
            "BADC 与 CDAB 在 pymodbus 里必须是两个不同组合 —— "
            "若哪天有人按 docstring 把它们合并，这条会红")

    def test_case_insensitive(self):
        from gateway.modbus_gateway import ModbusGateway
        assert ModbusGateway._parse_byte_order("abcd") == self.EXPECTED["ABCD"]

    def test_unknown_falls_back_to_big_big(self):
        from gateway.modbus_gateway import ModbusGateway
        assert ModbusGateway._parse_byte_order("XXXX") == (Endian.BIG, Endian.BIG)
        assert ModbusGateway._parse_byte_order("") == (Endian.BIG, Endian.BIG)


class _FakeResponse:
    def __init__(self, registers=None, error=False):
        self._registers = registers
        self._error = error

    def isError(self):
        return self._error

    @property
    def registers(self):
        if self._registers is None:
            raise AttributeError("no registers")
        return self._registers


def _client(registers=None, error=False, raises=None):
    c = MagicMock()
    if raises is not None:
        c.read_holding_registers.side_effect = raises
    else:
        c.read_holding_registers.return_value = _FakeResponse(registers, error)
    return c


class TestReadRegister:
    def test_uint16(self):
        assert _gw()._read_register(_client([12345]), 1, 0, 1, "uint16") == 12345.0

    def test_int16_negative(self):
        """0x8000 → -32768（符号扩展，不是 32768）。"""
        assert _gw()._read_register(_client([0x8000]), 1, 0, 1, "int16") == -32768.0
        assert _gw()._read_register(_client([0x7FFF]), 1, 0, 1, "int16") == 32767.0
        assert _gw()._read_register(_client([0xFFFF]), 1, 0, 1, "int16") == -1.0

    def test_uint32_word_order_high_first(self):
        """(r0 << 16) | r1 —— 高字在前（pymodbus 惯例）。"""
        v = _gw()._read_register(_client([0x0001, 0x0002]), 1, 0, 2, "uint32")
        assert v == 0x00010002

    def test_int32_negative(self):
        v = _gw()._read_register(_client([0x8000, 0x0000]), 1, 0, 2, "int32")
        assert v == -2147483648.0
        v = _gw()._read_register(_client([0xFFFF, 0xFFFF]), 1, 0, 2, "int32")
        assert v == -1.0

    def test_float32_default_big_endian(self):
        """未传 byte_order 时默认 (BIG, BIG)：0x40490FDB ≈ π。"""
        v = _gw()._read_register(_client([0x4049, 0x0FDB]), 1, 0, 2, "float32")
        assert abs(v - 3.1415927) < 1e-5

    def test_float32_needs_two_registers(self):
        assert _gw()._read_register(_client([0x4049]), 1, 0, 1, "float32") is None

    def test_float64_needs_four_registers(self):
        assert _gw()._read_register(_client([1, 2, 3]), 1, 0, 3, "float64") is None

    def test_float64_roundtrip_big_endian(self):
        """float64 ABCD：π 的双精度寄存器分解。"""
        import struct
        raw = struct.pack(">d", 3.141592653589793)
        regs = list(struct.unpack(">HHHH", raw))
        v = _gw()._read_register(_client(regs), 1, 0, 4, "float64")
        assert abs(v - 3.141592653589793) < 1e-12

    def test_error_response_returns_none(self):
        assert _gw()._read_register(_client(error=True), 1, 0, 1, "uint16") is None

    def test_unsupported_type_returns_none(self):
        assert _gw()._read_register(_client([1]), 1, 0, 1, "string") is None

    def test_modbus_exception_returns_none(self):
        from pymodbus.exceptions import ModbusException
        assert _gw()._read_register(_client(raises=ModbusException("boom")),
                                    1, 0, 1, "uint16") is None

    def test_generic_exception_returns_none(self):
        """普通异常（如 client None）也不能炸出调用方。"""
        c = MagicMock()
        c.read_holding_registers.side_effect = RuntimeError("socket dead")
        assert _gw()._read_register(c, 1, 0, 1, "uint16") is None


class TestReadDeviceData:
    """单寄存器失败时的容忍策略（整设备不报 None）。"""

    def _gateway(self):
        g = _gw()
        g._clients = {"d1": _client([1])}
        g._register_configs = {"d1": [
            {"name": "ok", "address": 0, "count": 1, "type": "uint16"},
            {"name": "bad", "address": 5, "count": 1, "type": "string"},
        ]}
        g._byte_orders = {}
        g.devices_config = [{"device_id": "d1", "slave_id": 3}]
        return g

    def test_partial_failure_keeps_other_values(self):
        out = self._gateway().read_device_data("d1")
        assert out == {"ok": 1.0}, f"坏寄存器不应拖垮好寄存器: {out}"

    def test_no_client_returns_none(self):
        g = self._gateway()
        g._clients = {}
        assert g.read_device_data("d1") is None

    def test_no_register_config_returns_none(self):
        g = self._gateway()
        g._register_configs = {}
        assert g.read_device_data("d1") is None

    def test_all_failed_returns_none(self):
        g = self._gateway()
        g._register_configs = {"d1": [{"name": "bad", "address": 0,
                                        "count": 1, "type": "string"}]}
        assert g.read_device_data("d1") is None
