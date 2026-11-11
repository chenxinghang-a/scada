"""属性/蜕变测试（不引入额外依赖，用固定种子的随机生成）。

依据：Chen et al., "Metamorphic Testing: A Review of Challenges and
Opportunities", ACM CSUR 2018 —— 没有测试预言时，用**必须成立的关系**判定：
  * R1: `decode_float64(encode_float64(v, order), order)` 的**位模式**必须等于 v
  * R2: MC `write_float32` 写出的两个字，经 `read_float32` 的取数口径必须还原 v
  * R3: 响应帧被任意切分到达时，`_send_recv` 解析结果不变（不得半帧成功）

相比手写用例，随机位型能覆盖 NaN/次正规数/极值之外的"奇怪但合法"的 double。
"""
import random
import struct

import pytest

from 采集层.base_client import decode_float64_registers, encode_float64_registers
from 采集层.mc_client import MCClient

BYTE_ORDERS = ('ABCD', 'BADC', 'CDAB', 'DCBA')


def _finite_doubles(n=120, seed=20261111):
    rng = random.Random(seed)
    out = []
    while len(out) < n:
        raw = rng.getrandbits(64).to_bytes(8, 'big')
        value = struct.unpack('>d', raw)[0]
        if value != value or value in (float('inf'), float('-inf')):
            continue
        out.append(value)
    return out


@pytest.mark.parametrize('order', BYTE_ORDERS)
def test_float64_roundtrip_bit_exact(order):
    for value in _finite_doubles():
        regs = encode_float64_registers(value, order)
        got = decode_float64_registers(regs, order)
        # 用位模式比较：-0.0 与 0.0 数值相等但位不同，也必须区分
        assert struct.pack('>d', got) == struct.pack('>d', value)


@pytest.mark.parametrize('order', BYTE_ORDERS)
def test_mc_float32_write_then_read(order):
    client = MCClient({'id': 'mc1', 'byte_order': order})
    rng = random.Random(405)
    for _ in range(60):
        value = rng.uniform(-1e6, 1e6)
        captured = {}

        def fake_write_words(device, address, words, _cap=captured):
            _cap['words'] = list(words)
            return True

        client.write_words = fake_write_words
        assert client.write_float32('D', 0, value) is True
        words = captured['words']
        decoded = client.decode_float32([words[1], words[0]])
        # float32 只有 24 位尾数：用相对误差约束
        assert decoded == pytest.approx(value, rel=1e-6)


class _ChunkedSock:
    """按随机块大小返回数据的 socket 替身（模拟 TCP 分包）。"""

    def __init__(self, payload, seed):
        self._buf = payload
        self._rng = random.Random(seed)

    def sendall(self, _frame):
        return None

    def recv(self, n):
        if not self._buf:
            return b''
        take = min(n, self._rng.randint(1, max(1, n)))
        chunk, self._buf = self._buf[:take], self._buf[take:]
        return chunk


def _mc_response(values):
    data = struct.pack('<H', 0)                      # 完成码
    for v in values:
        data += struct.pack('<H', v)
    header = b'\xd0\x00' + b'\x00\xff' + b'\xff\x03' + b'\x00'
    header += struct.pack('<H', len(data))
    return header + data


@pytest.mark.parametrize('seed', [1, 7, 42, 99, 2026])
def test_mc_frame_split_does_not_change_result(seed):
    values = [0x1234, 0xBEEF, 0x0001, 0xFFFF]
    client = MCClient({'id': 'mc1'})
    client._sock = _ChunkedSock(_mc_response(values), seed)
    client.connected = True

    assert client.read_words('D', 0, len(values)) == values
    assert client.connected is True


def test_mc_truncated_frame_never_returns_partial_values():
    values = [0x1111, 0x2222, 0x3333, 0x4444]
    full = _mc_response(values)
    # 砍掉一半数据段：声明长度不变，实际字节不足
    client = MCClient({'id': 'mc1'})
    client._sock = _ChunkedSock(full[:len(full) - 4], seed=3)
    client.connected = True

    assert client.read_words('D', 0, len(values)) is None
    assert client.connected is False
