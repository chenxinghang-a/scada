"""
Tests for Modbus byte order configuration (ABCD/BADC/CDAB/DCBA)
"""

import struct
import math
import pytest

from 采集层.modbus_client import ModbusClient, ByteOrder


# ============================================================
# ByteOrder Enum Tests
# ============================================================

class TestByteOrderEnum:

    def test_abcd_value(self):
        assert ByteOrder.ABCD.value == 'ABCD'

    def test_badc_value(self):
        assert ByteOrder.BADC.value == 'BADC'

    def test_cdab_value(self):
        assert ByteOrder.CDAB.value == 'CDAB'

    def test_dcba_value(self):
        assert ByteOrder.DCBA.value == 'DCBA'

    def test_from_string_abcd(self):
        assert ByteOrder('ABCD') == ByteOrder.ABCD

    def test_from_string_lowercase_raises(self):
        """Enum values are case-sensitive; lowercase is not valid"""
        with pytest.raises(ValueError):
            ByteOrder('abcd')

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            ByteOrder('INVALID')


# ============================================================
# Helper: encode float32 to registers in given byte order
# ============================================================

def _encode_float32(value: float, byte_order: ByteOrder) -> list[int]:
    """Encode a float32 into 2 Modbus registers according to byte order."""
    raw_bytes = struct.pack('!f', value)
    b0, b1, b2, b3 = raw_bytes[0], raw_bytes[1], raw_bytes[2], raw_bytes[3]

    if byte_order == ByteOrder.ABCD:
        w1 = (b0 << 8) | b1
        w2 = (b2 << 8) | b3
    elif byte_order == ByteOrder.BADC:
        w1 = (b2 << 8) | b3
        w2 = (b0 << 8) | b1
    elif byte_order == ByteOrder.CDAB:
        w1 = (b2 << 8) | b3
        w2 = (b0 << 8) | b1
    elif byte_order == ByteOrder.DCBA:
        w1 = (b3 << 8) | b2
        w2 = (b1 << 8) | b0
    else:
        w1 = (b0 << 8) | b1
        w2 = (b2 << 8) | b3

    return [w1, w2]


def _encode_float64(value: float, byte_order: ByteOrder) -> list[int]:
    """Encode a float64 into 4 Modbus registers according to byte order."""
    raw_bytes = struct.pack('!d', value)
    words = []
    for i in range(0, 8, 2):
        words.append((raw_bytes[i] << 8) | raw_bytes[i + 1])

    if byte_order == ByteOrder.ABCD:
        return words
    elif byte_order == ByteOrder.BADC:
        return [words[1], words[0], words[3], words[2]]
    elif byte_order == ByteOrder.CDAB:
        return [words[2], words[3], words[0], words[1]]
    elif byte_order == ByteOrder.DCBA:
        def _swap16(w):
            return ((w & 0xFF) << 8) | ((w >> 8) & 0xFF)
        return [_swap16(words[3]), _swap16(words[2]), _swap16(words[1]), _swap16(words[0])]
    else:
        return words


# ============================================================
# decode_float32 Tests
# ============================================================

class TestDecodeFloat32:

    def _make_client(self, byte_order: str) -> ModbusClient:
        return ModbusClient({'id': 'test', 'name': 'test', 'protocol': 'modbus_tcp',
                             'byte_order': byte_order})

    def test_abcd_positive(self):
        client = self._make_client('ABCD')
        regs = _encode_float32(123.456, ByteOrder.ABCD)
        result = client.decode_float32(regs)
        assert abs(result - 123.456) < 0.001

    def test_badc_positive(self):
        client = self._make_client('BADC')
        regs = _encode_float32(123.456, ByteOrder.BADC)
        result = client.decode_float32(regs)
        assert abs(result - 123.456) < 0.001

    def test_cdab_positive(self):
        client = self._make_client('CDAB')
        regs = _encode_float32(123.456, ByteOrder.CDAB)
        result = client.decode_float32(regs)
        assert abs(result - 123.456) < 0.001

    def test_dcba_positive(self):
        client = self._make_client('DCBA')
        regs = _encode_float32(123.456, ByteOrder.DCBA)
        result = client.decode_float32(regs)
        assert abs(result - 123.456) < 0.001

    def test_abcd_negative(self):
        client = self._make_client('ABCD')
        regs = _encode_float32(-42.5, ByteOrder.ABCD)
        result = client.decode_float32(regs)
        assert abs(result - (-42.5)) < 0.001

    def test_badc_negative(self):
        client = self._make_client('BADC')
        regs = _encode_float32(-42.5, ByteOrder.BADC)
        result = client.decode_float32(regs)
        assert abs(result - (-42.5)) < 0.001

    def test_cdab_negative(self):
        client = self._make_client('CDAB')
        regs = _encode_float32(-42.5, ByteOrder.CDAB)
        result = client.decode_float32(regs)
        assert abs(result - (-42.5)) < 0.001

    def test_dcba_negative(self):
        client = self._make_client('DCBA')
        regs = _encode_float32(-42.5, ByteOrder.DCBA)
        result = client.decode_float32(regs)
        assert abs(result - (-42.5)) < 0.001

    def test_abcd_zero(self):
        client = self._make_client('ABCD')
        regs = [0x0000, 0x0000]
        result = client.decode_float32(regs)
        assert result == 0.0

    def test_abcd_one(self):
        client = self._make_client('ABCD')
        regs = _encode_float32(1.0, ByteOrder.ABCD)
        result = client.decode_float32(regs)
        assert abs(result - 1.0) < 0.0001

    def test_dcba_one(self):
        client = self._make_client('DCBA')
        regs = _encode_float32(1.0, ByteOrder.DCBA)
        result = client.decode_float32(regs)
        assert abs(result - 1.0) < 0.0001

    def test_nan_returns_none(self):
        client = self._make_client('ABCD')
        regs = [0xFFFF, 0xFFFF]
        result = client.decode_float32(regs)
        assert result is None

    def test_inf_returns_none(self):
        client = self._make_client('ABCD')
        # +Inf: 0x7F800000
        regs = [0x7F80, 0x0000]
        result = client.decode_float32(regs)
        assert result is None

    def test_default_is_abcd(self):
        """Without byte_order specified, default is ABCD"""
        client = ModbusClient({'id': 'test', 'name': 'test', 'protocol': 'modbus_tcp'})
        regs = _encode_float32(99.9, ByteOrder.ABCD)
        result = client.decode_float32(regs)
        assert abs(result - 99.9) < 0.01

    def test_too_few_registers_raises(self):
        client = self._make_client('ABCD')
        with pytest.raises(ValueError):
            client.decode_float32([1])

    def test_all_orders_same_value(self):
        """All 4 byte orders decode to the same value when encoded correctly"""
        value = 3.14159
        for order in ByteOrder:
            client = self._make_client(order.value)
            regs = _encode_float32(value, order)
            result = client.decode_float32(regs)
            assert abs(result - value) < 0.001, f"Failed for {order.value}"


# ============================================================
# decode_float64 Tests
# ============================================================

class TestDecodeFloat64:

    def _make_client(self, byte_order: str) -> ModbusClient:
        return ModbusClient({'id': 'test', 'name': 'test', 'protocol': 'modbus_tcp',
                             'byte_order': byte_order})

    def test_abcd_positive(self):
        client = self._make_client('ABCD')
        regs = _encode_float64(123456.789, ByteOrder.ABCD)
        result = client.decode_float64(regs)
        assert abs(result - 123456.789) < 0.01

    def test_badc_positive(self):
        client = self._make_client('BADC')
        regs = _encode_float64(123456.789, ByteOrder.BADC)
        result = client.decode_float64(regs)
        assert abs(result - 123456.789) < 0.01

    def test_cdab_positive(self):
        client = self._make_client('CDAB')
        regs = _encode_float64(123456.789, ByteOrder.CDAB)
        result = client.decode_float64(regs)
        assert abs(result - 123456.789) < 0.01

    def test_dcba_positive(self):
        client = self._make_client('DCBA')
        regs = _encode_float64(123456.789, ByteOrder.DCBA)
        result = client.decode_float64(regs)
        assert abs(result - 123456.789) < 0.01

    def test_abcd_negative(self):
        client = self._make_client('ABCD')
        regs = _encode_float64(-9876.543, ByteOrder.ABCD)
        result = client.decode_float64(regs)
        assert abs(result - (-9876.543)) < 0.01

    def test_dcba_negative(self):
        client = self._make_client('DCBA')
        regs = _encode_float64(-9876.543, ByteOrder.DCBA)
        result = client.decode_float64(regs)
        assert abs(result - (-9876.543)) < 0.01

    def test_default_is_abcd(self):
        """Without byte_order specified, default is ABCD"""
        client = ModbusClient({'id': 'test', 'name': 'test', 'protocol': 'modbus_tcp'})
        regs = _encode_float64(42.0, ByteOrder.ABCD)
        result = client.decode_float64(regs)
        assert abs(result - 42.0) < 0.001

    def test_too_few_registers_raises(self):
        client = self._make_client('ABCD')
        with pytest.raises(ValueError):
            client.decode_float64([1, 2, 3])

    def test_all_orders_same_value(self):
        """All 4 byte orders decode to the same value when encoded correctly"""
        value = 2.718281828
        for order in ByteOrder:
            client = self._make_client(order.value)
            regs = _encode_float64(value, order)
            result = client.decode_float64(regs)
            assert abs(result - value) < 0.0001, f"Failed for {order.value}"


# ============================================================
# Cross-order sanity: ABCD-encoded data must NOT decode correctly
# with DCBA, confirming byte order actually matters
# ============================================================

class TestByteOrderMatters:

    def test_wrong_order_gives_wrong_value(self):
        """ABCD-encoded data decoded as DCBA should NOT give the original value"""
        value = 42.0
        abcd_regs = _encode_float32(value, ByteOrder.ABCD)
        client = ModbusClient({'id': 'test', 'name': 'test', 'protocol': 'modbus_tcp',
                               'byte_order': 'DCBA'})
        result = client.decode_float32(abcd_regs)
        # Should not be 42.0 (unless by cosmic coincidence)
        assert result is None or abs(result - value) > 1.0
