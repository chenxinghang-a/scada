"""
MC / FINS 客户端接入采集层 _collect_modbus 的适配测试。

覆盖：
1. MCClient / FINSClient 补齐 Modbus 兼容面（read_holding_registers/read_coils/decode_*）
2. 四种字节序（ABCD/BADC/CDAB/DCBA）解码正确
3. MC/FINS 设备进入 _collect_modbus 时真实解码、不再伪造数据；
   断线/解析失败时返回 False（非 GOOD），且默认不生成兜底假数据。
"""

import struct
import math
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from 采集层.base_client import ByteOrder
from 采集层.mc_client import MCClient
from 采集层.fins_client import FINSClient
from 采集层.data_collector import DataCollector, FALLBACK_SIMULATION_ENABLED


# ============================================================
# 字节序编码辅助（将值按给定字节序编码为寄存器序列）
# ============================================================
def _encode_float32(value: float, bo: ByteOrder) -> list[int]:
    b0, b1, b2, b3 = struct.pack('!f', value)
    if bo == ByteOrder.ABCD:
        return [(b0 << 8) | b1, (b2 << 8) | b3]
    if bo == ByteOrder.BADC:
        return [(b1 << 8) | b0, (b3 << 8) | b2]
    if bo == ByteOrder.CDAB:
        return [(b2 << 8) | b3, (b0 << 8) | b1]
    return [(b3 << 8) | b2, (b1 << 8) | b0]  # DCBA


def _encode_float64(value: float, bo: ByteOrder) -> list[int]:
    raw = struct.pack('!d', value)
    words = [(raw[i] << 8) | raw[i + 1] for i in range(0, 8, 2)]
    if bo == ByteOrder.ABCD:
        return words
    if bo == ByteOrder.BADC:
        return [words[1], words[0], words[3], words[2]]
    if bo == ByteOrder.CDAB:
        return [words[2], words[3], words[0], words[1]]
    return [((w & 0xFF) << 8) | ((w >> 8) & 0xFF) for w in reversed(words)]  # DCBA


def _encode_uint32(value: int, bo: ByteOrder) -> list[int]:
    raw = value & 0xFFFFFFFF
    w1 = (raw >> 16) & 0xFFFF
    w2 = raw & 0xFFFF
    if bo == ByteOrder.ABCD:
        return [w1, w2]
    if bo == ByteOrder.BADC:
        return [((w1 & 0xFF) << 8) | ((w1 >> 8) & 0xFF), ((w2 & 0xFF) << 8) | ((w2 >> 8) & 0xFF)]
    if bo == ByteOrder.CDAB:
        return [w2, w1]
    return [((w2 & 0xFF) << 8) | ((w2 >> 8) & 0xFF), ((w1 & 0xFF) << 8) | ((w1 >> 8) & 0xFF)]


# ============================================================
# 字节序解码测试（验收 #2）
# ============================================================
@pytest.mark.parametrize("client_cls", [MCClient, FINSClient])
class TestByteOrderDecode:
    def _client(self, bo: str, cls):
        return cls({'id': 'dev', 'name': 'dev', 'protocol': cls.__name__.lower(),
                    'byte_order': bo})

    def test_float32_all_orders(self, client_cls):
        for bo in ByteOrder:
            c = self._client(bo.value, client_cls)
            regs = _encode_float32(123.456, bo)
            assert abs(c.decode_float32(regs) - 123.456) < 0.001, bo

    def test_float32_negative_all_orders(self, client_cls):
        for bo in ByteOrder:
            c = self._client(bo.value, client_cls)
            regs = _encode_float32(-42.5, bo)
            assert abs(c.decode_float32(regs) - (-42.5)) < 0.001, bo

    def test_float64_all_orders(self, client_cls):
        for bo in ByteOrder:
            c = self._client(bo.value, client_cls)
            regs = _encode_float64(98765.4321, bo)
            assert abs(c.decode_float64(regs) - 98765.4321) < 0.01, bo

    def test_uint32_all_orders(self, client_cls):
        for bo in ByteOrder:
            c = self._client(bo.value, client_cls)
            regs = _encode_uint32(0x12345678, bo)
            assert c.decode_uint32(regs) == 0x12345678, bo

    def test_int32_all_orders(self, client_cls):
        for bo in ByteOrder:
            c = self._client(bo.value, client_cls)
            regs = _encode_uint32(0xFEDCBA98, bo)  # 有符号负数
            assert c.decode_int32(regs) == 0xFEDCBA98 - 0x100000000, bo

    def test_uint16_ignores_byte_order(self, client_cls):
        c = self._client('ABCD', client_cls)
        assert c.decode_uint16(0x1234) == 0x1234
        c2 = self._client('DCBA', client_cls)
        assert c2.decode_uint16(0x1234) == 0x1234

    def test_default_is_abcd(self, client_cls):
        c = client_cls({'id': 'dev', 'name': 'dev', 'protocol': 'mc'})
        regs = _encode_float32(99.9, ByteOrder.ABCD)
        assert abs(c.decode_float32(regs) - 99.9) < 0.01

    def test_wrong_order_gives_wrong_value(self, client_cls):
        """ABCD 编码的数据用 DCBA 解，应不等于原值（证明字节序真生效）"""
        c = self._client('DCBA', client_cls)
        regs = _encode_float32(42.0, ByteOrder.ABCD)
        r = c.decode_float32(regs)
        assert r is None or abs(r - 42.0) > 1.0


# ============================================================
# _collect_modbus 集成：真实解码，不伪造数据（验收 #4）
# ============================================================
def _make_collector():
    dc = DataCollector(MagicMock(), MagicMock())
    captured = []
    dc._enqueue_drop_oldest = lambda item: (captured.append(item), True)[1]
    return dc, captured


def _make_mc_device(byte_order='ABCD'):
    client = MCClient({'id': 'mc1', 'name': '三菱MC', 'protocol': 'mc',
                       'byte_order': byte_order, 'word_area': 'D', 'bit_area': 'M'})
    client.connected = True
    return client


def _make_fins_device(byte_order='ABCD'):
    client = FINSClient({'id': 'fins1', 'name': '欧姆龙FINS', 'protocol': 'fins',
                         'byte_order': byte_order, 'word_area': 'D', 'bit_area': 'CIO'})
    client.connected = True
    return client


DEVICE_CONFIG = {
    'protocol': 'mc',
    'registers': [
        {'name': 'temperature', 'address': 0, 'data_type': 'float32', 'unit': '°C'},
        {'name': 'speed', 'address': 2, 'data_type': 'uint16', 'unit': 'rpm'},
    ],
}


def test_mc_collect_real_data_no_fallback():
    client = _make_mc_device()
    # 0,1 编码 float 25.5(ABCD)；2 为 uint16 1234
    regs = _encode_float32(25.5, ByteOrder.ABCD) + [1234]
    client.read_words = MagicMock(return_value=regs)

    dc, captured = _make_collector()
    ok = dc._collect_modbus(client, 'mc1', DEVICE_CONFIG, datetime.now())

    assert ok is True, "真实读取应返回 True"
    values = {it['register_name']: it['value'] for it in captured}
    assert abs(values['temperature'] - 25.5) < 0.001
    assert values['speed'] == 1234
    assert all(it.get('quality') != 'BAD' for it in captured), "不应出现假数据的质量码"
    assert all('simulated' not in str(it.get('quality')) for it in captured)


def test_fins_collect_real_data_no_fallback():
    client = _make_fins_device()
    regs = _encode_float32(36.6, ByteOrder.ABCD) + [4321]
    client.read_words = MagicMock(return_value=regs)

    dc, captured = _make_collector()
    ok = dc._collect_modbus(client, 'fins1',
                            {**DEVICE_CONFIG, 'protocol': 'fins'}, datetime.now())

    assert ok is True
    values = {it['register_name']: it['value'] for it in captured}
    assert abs(values['temperature'] - 36.6) < 0.001
    assert values['speed'] == 4321


def test_mc_byte_order_applied_in_collect():
    # 用 DCBA 编码，但告诉客户端这是 DCBA，应正确解回
    client = _make_mc_device(byte_order='DCBA')
    regs = _encode_float32(7.25, ByteOrder.DCBA) + [777]
    client.read_words = MagicMock(return_value=regs)

    dc, captured = _make_collector()
    ok = dc._collect_modbus(client, 'mc1', DEVICE_CONFIG, datetime.now())
    assert ok is True
    values = {it['register_name']: it['value'] for it in captured}
    assert abs(values['temperature'] - 7.25) < 0.001
    assert values['speed'] == 777


def test_mc_disconnected_no_fake_data():
    """断线：read 返回 None → _collect_modbus 返回 False，且默认不生成兜底假数据"""
    assert FALLBACK_SIMULATION_ENABLED is False, "默认应关闭兜底，避免假成功"
    client = _make_mc_device()
    client.connected = False  # read_holding_registers 直接返回 None
    client.read_words = MagicMock(return_value=[1, 2, 3])  # 不应被调用

    dc, captured = _make_collector()
    ok = dc._collect_modbus(client, 'mc1', DEVICE_CONFIG, datetime.now())

    assert ok is False
    assert captured == [], "断线时不应伪造/生成任何数据"
    client.read_words.assert_not_called()


def test_mc_read_returns_none_no_fake_data():
    """已连接但读取失败（返回 None）→ 返回 False，无兜底数据"""
    client = _make_mc_device()
    client.read_words = MagicMock(return_value=None)

    dc, captured = _make_collector()
    ok = dc._collect_modbus(client, 'mc1', DEVICE_CONFIG, datetime.now())

    assert ok is False
    assert captured == []
