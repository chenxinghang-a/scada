# -*- coding: utf-8 -*-
"""`gateway/s7_gateway.py` 的解码层测试（gateway 短板第二站）。

覆盖此前空白的 `_read_register` 六种数据类型 + `read_device_data` 的容忍策略。
逻辑组不依赖 snap7（db_read 长度/类型分发/异常路径/容忍策略）；
解码组用 struct 手工构造已知值 + 与 snap7 官方解码器对拍（依赖缺失时
按条件 skip，不空转）。
"""
from __future__ import annotations

import pathlib
import struct
import sys
from unittest.mock import MagicMock

import pytest

_BACKEND = pathlib.Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

try:
    import snap7  # noqa: F401
    from snap7.util import get_dint, get_real  # noqa: E402
    SNAP7_OK = True
except ImportError:
    SNAP7_OK = False

#: 解码组（值与 snap7 官方解码器对拍）需要 snap7；逻辑组（db_read 参数/分发/异常）
#: 不依赖它。**不能整文件 skip** —— 那会让逻辑组在没装 snap7 的环境里空转
#: （本仓 venv 就没装，而 requirements 也没声明它，属可选依赖）。
needs_snap7 = pytest.mark.skipif(not SNAP7_OK, reason="python-snap7 未安装（可选依赖）")


def _gw():
    from gateway.s7_gateway import S7Gateway
    import logging
    g = S7Gateway.__new__(S7Gateway)
    g.logger = logging.getLogger("test_s7_gateway")
    return g


def _client(payload: bytes = b""):
    c = MagicMock()
    c.db_read.return_value = payload
    return c


class TestReadRegister:
    @needs_snap7
    def test_real_uses_big_endian(self):
        """S7 REAL 是大端 float32：struct.pack('>f', π) 的字节。"""
        raw = struct.pack(">f", 3.1415927)
        v = _gw()._read_register(_client(raw), 1, 0, "real")
        assert abs(v - 3.1415927) < 1e-5

    @needs_snap7
    def test_real_negative(self):
        raw = struct.pack(">f", -12.5)
        assert _gw()._read_register(_client(raw), 1, 0, "real") == -12.5

    @needs_snap7
    def test_int_boundaries(self):
        assert _gw()._read_register(_client(struct.pack(">h", 0x7FFF)), 1, 0, "int") == 32767.0
        assert _gw()._read_register(_client(struct.pack(">h", -32768)), 1, 0, "int") == -32768.0
        assert _gw()._read_register(_client(struct.pack(">h", -1)), 1, 0, "int") == -1.0

    @needs_snap7
    def test_dint_boundaries(self):
        assert _gw()._read_register(_client(struct.pack(">i", 2147483647)), 1, 0, "dint") == 2147483647.0
        assert _gw()._read_register(_client(struct.pack(">i", -2147483648)), 1, 0, "dint") == -2147483648.0

    @needs_snap7
    def test_word_is_unsigned(self):
        """word 必须是**无符号** 0xFFFF → 65535（不是 -1）。"""
        assert _gw()._read_register(_client(b"\xff\xff"), 1, 0, "word") == 65535.0

    @needs_snap7
    def test_byte_is_uint8(self):
        assert _gw()._read_register(_client(b"\xff"), 1, 0, "byte") == 255.0
        assert _gw()._read_register(_client(b"\x00"), 1, 0, "byte") == 0.0

    @needs_snap7
    def test_bool_uses_first_byte(self):
        assert _gw()._read_register(_client(b"\x01"), 1, 0, "bool") == 1.0
        assert _gw()._read_register(_client(b"\x00"), 1, 0, "bool") == 0.0

    def test_db_read_length_matches_type(self):
        """关键不变量：db_read 的**长度**必须按类型取（real=4/int=2/byte=1）。

        S7 是 DB 块寻址，取短了会读到半个值、取长了可能越界报错。
        """
        expected = {"real": 4, "int": 2, "dint": 4, "word": 2, "byte": 1, "bool": 1}
        for dtype, size in expected.items():
            c = _client(b"\x00" * size)
            _gw()._read_register(c, 7, 42, dtype)
            assert c.db_read.call_args[0] == (7, 42, size), (
                f"{dtype} 的 db_read 参数错了: {c.db_read.call_args} "
                f"(应为 db_number=7, offset=42, size={size})")

    def test_unsupported_type_returns_none(self):
        assert _gw()._read_register(_client(b"\x00" * 4), 1, 0, "string") is None

    def test_db_read_failure_returns_none(self):
        c = MagicMock()
        c.db_read.side_effect = RuntimeError("PLC offline")
        assert _gw()._read_register(c, 1, 0, "real") is None

    def test_short_payload_does_not_crash(self):
        """真实设备偶发短包：不能炸出调用方（异常被吞 + 记日志 + None）。"""
        c = MagicMock()
        c.db_read.return_value = b"\x01"      # real 要 4 字节
        v = _gw()._read_register(c, 1, 0, "real")
        # snap7 的 get_real 对短包抛异常 → 走 except 分支返回 None
        assert v is None


class TestReadDeviceData:  # 逻辑组：不依赖 snap7
    def _gateway(self):
        g = _gw()
        g._clients = {"d1": _client()}
        g._register_configs = {"d1": [
            {"name": "temp", "db": 1, "offset": 0, "type": "real"},
            {"name": "bad", "db": 1, "offset": 99, "type": "string"},
        ]}
        g.devices_config = [{"device_id": "d1"}]
        return g

    @needs_snap7
    def test_partial_failure_keeps_other_values(self):
        """⚠️ 这条其实依赖 snap7：要点是「解出一个值 + 另一个点位失败」，
        没有 snap7 时好点位也是 None（get_real 名字不存在），容忍策略测不到。"""
        g = self._gateway()
        g._clients["d1"] = MagicMock()
        g._clients["d1"].db_read.return_value = struct.pack(">f", 21.5)
        out = g.read_device_data("d1")
        assert out == {"temp": 21.5}, f"坏点位不应拖垮好点位: {out}"

    def test_no_client_returns_none(self):
        g = self._gateway()
        g._clients = {}
        assert g.read_device_data("d1") is None

    def test_no_config_returns_none(self):
        g = self._gateway()
        g._register_configs = {}
        assert g.read_device_data("d1") is None


@needs_snap7
class TestSnap7DecoderAgreement:
    """与 snap7 官方解码器的口径对拍（防实现悄悄换字节序解释）。"""

    def test_real_matches_snap7(self):
        raw = struct.pack(">f", -0.125)
        assert _gw()._read_register(_client(raw), 1, 0, "real") == get_real(raw, 0)

    def test_dint_matches_snap7(self):
        raw = struct.pack(">i", -123456)
        assert _gw()._read_register(_client(raw), 1, 0, "dint") == float(get_dint(raw, 0))
