"""DNP3 网关的协议层测试（帧长计算 / 帧解析 / 对象解析 / **接收侧帧重组**）。

⚠️ 先读 `gateway/dnp3_gateway.py` 模块头注的「现状与已知缺口」——
本模块是**教学/演示级简化实现**，发送侧 CRC 是假的、不校验任何 CRC，
**不能对真实从站工作**。本文件的作用是：

1. 把**已修的**接收侧帧重组钉住（这是本轮唯一的行为改动）；
2. 把**已知不符合标准之处**用断言固定下来 —— 将来补齐标准时，
   这些断言会红，从而逼着同步更新模块头注里的缺口清单；
3. 覆盖解析/构建的基本行为，避免静默漂移。

⚠️ 测试替身的使用纪律（上一轮踩过）：测 `_process_buffer` 的替身**覆盖了
`_handle_response`**，所以它**不能**用来测 `_handle_response` 本身；
后者用 `_SendingClient`（只记录，不覆盖被测方法）。
"""
import struct

import pytest

from gateway.dnp3_gateway import DNP3Client, DNP3FunctionCode, DNP3ObjectGroup, DNP3Parser

START = b'\x05\x64'


# ---------------------------------------------------------------------------
# 测试向量构造（按标准帧格式，不依赖被测代码）
# ---------------------------------------------------------------------------

def _frame(user_data: bytes, *, ctrl: int = 0xC0, dest: int = 2, src: int = 1,
           length: int | None = None, crc: bytes = b'\x00\x00') -> bytes:
    """构造一个**符合标准帧长**的 DNP3 链路层帧。

    LENGTH 从控制字节数到用户数据末尾（不含 CRC）；用户数据每满 16 字节
    追加一段 2 字节 CRC（测试里填 0，因为本实现不校验 CRC）。
    """
    ln = len(user_data) + 5 if length is None else length
    out = START + struct.pack('<BBHH', ln, ctrl, dest, src) + crc + user_data
    for _ in range(0, len(user_data), 16):
        out += b'\x00\x00'
    return out


def _response_frame(group: int, variation: int, values: bytes,
                    qualifier: int = 0x00) -> bytes:
    obj = bytes([group, variation, qualifier]) + values
    app = bytes([0xC0, DNP3FunctionCode.RESPONSE]) + obj
    return _frame(app)


def _ai_values(*pairs) -> bytes:
    """(值, 质量) 序列 → 16 位模拟输入的对象数据"""
    return b''.join(struct.pack('<hB', v, q) for v, q in pairs)


class _BufferedClient(DNP3Client):
    """只捕获被切出来的完整帧（**覆盖了 `_handle_response`**）"""

    def __init__(self):
        super().__init__('127.0.0.1')
        self.frames: list[bytes] = []

    def _handle_response(self, data: bytes):      # noqa: D102
        self.frames.append(data)


class _SendingClient(DNP3Client):
    """保留真实 `_handle_response`，只记录被处理的原始包（不覆盖被测方法）"""

    def __init__(self):
        super().__init__('127.0.0.1')
        self.raw: list[bytes] = []

    def _handle_response(self, data: bytes):      # noqa: D102
        self.raw.append(data)
        super()._handle_response(data)


# ---------------------------------------------------------------------------
# 1. 帧长计算（本轮新增；切帧全靠它）
# ---------------------------------------------------------------------------

class TestFrameSize:
    @pytest.mark.parametrize('length_byte,expected', [
        (5, 10),        # 无用户数据：2+1+5+2
        (14, 21),       # 9 字节用户数据：2+1+14+2 + 1 个数据 CRC
        (21, 28),       # 16 字节：正好 1 块
        (22, 31),       # 17 字节：跨到第 2 块 → 2 个数据 CRC
        (37, 46),       # 32 字节：2 块（2+1+37+2+4）
    ])
    def test_frame_size(self, length_byte, expected):
        assert DNP3Parser._frame_size(length_byte) == expected

    @pytest.mark.parametrize('bad', [0, 1, 4])
    def test_invalid_length_returns_none(self, bad):
        """LENGTH < 5 装不下「控制+目的+源」，无法定位帧尾"""
        assert DNP3Parser._frame_size(bad) is None

    def test_not_simply_length_plus_two(self):
        """元守卫：整帧长度**不是** LENGTH + 2（漏算数据 CRC 会切错帧）。

        一旦有人把实现改成 ``length + 2``，这条与上面的参数化用例会同时红。
        """
        assert DNP3Parser._frame_size(14) != 14 + 2
        assert DNP3Parser._frame_size(21) != 21 + 2

    def test_matches_standard_vector_construction(self):
        """与「按标准构造的帧」互相印证（构造器在测试里，不依赖被测代码）"""
        for user_len in (0, 1, 9, 15, 16, 17, 32, 33):
            f = _frame(b'\xAA' * user_len)
            assert len(f) == DNP3Parser._frame_size(user_len + 5), \
                f'user_len={user_len} 时帧长与计算不符'


# ---------------------------------------------------------------------------
# 2. 帧解析
# ---------------------------------------------------------------------------

class TestParseFrame:
    def test_too_short(self):
        assert DNP3Parser.parse_frame(b'\x05\x64\x05')['valid'] is False

    def test_wrong_start_bytes(self):
        assert DNP3Parser.parse_frame(b'\x05\x65' + b'\x00' * 8)['valid'] is False

    def test_field_extraction(self):
        f = _frame(b'\xC0\x81\x01\x02\x00', ctrl=0xC4, dest=0x1234, src=0x5678)
        p = DNP3Parser.parse_frame(f)
        assert p['valid'] is True
        assert p['length'] == 10
        assert p['control'] == 0xC4
        assert p['destination'] == 0x1234 and p['source'] == 0x5678

    def test_application_layer_extraction(self):
        f = _response_frame(DNP3ObjectGroup.AI, 1, _ai_values((42, 0)))
        p = DNP3Parser.parse_frame(f)
        assert p['app_control'] == 0xC0
        assert p['function_code'] == DNP3FunctionCode.RESPONSE
        assert p['data'][:3] == bytes([DNP3ObjectGroup.AI, 1, 0x00])

    def test_header_crc_is_exposed_but_not_verified(self):
        """钉住**已知缺口**：帧头 CRC 解析出来但从不校验。

        补齐校验时这条会红 —— 那时请同步更新模块头注的缺口清单第 3 条。
        """
        p = DNP3Parser.parse_frame(_frame(b'\xC0\x81', crc=b'\xAB\xCD'))
        assert p['crc'] == 0xCDAB
        assert p['crc_verified'] is False, '本实现不校验 CRC（已知缺口，见模块头注）'

    def test_length_field_is_not_validated(self):
        """钉住现状：LENGTH 与实际长度不符也照样判 valid（校验在切帧侧做）"""
        f = _frame(b'\xC0\x81', length=99)      # LENGTH 谎报
        assert DNP3Parser.parse_frame(f)['valid'] is True


# ---------------------------------------------------------------------------
# 3. 对象解析
# ---------------------------------------------------------------------------

class TestParseResponse:
    def test_analog_input_16bit_signed(self):
        data = bytes([DNP3ObjectGroup.AI, 1, 0x00]) + _ai_values((100, 0), (-50, 1), (7, 2))
        objs = DNP3Parser.parse_response(data)
        assert [o['value'] for o in objs] == [100, -50, 7]
        assert [o['quality'] for o in objs] == [0, 1, 2]
        assert [o['index'] for o in objs] == [0, 1, 2], '无索引前缀时索引按出现顺序编号'

    def test_analog_input_32bit(self):
        vals = struct.pack('<iB', -123456, 3)
        objs = DNP3Parser.parse_response(bytes([DNP3ObjectGroup.AI32, 1, 0x00]) + vals)
        assert objs[0]['value'] == -123456 and objs[0]['quality'] == 3

    def test_binary_input_takes_low_bit(self):
        """钉住现状：二进制输入取**最低位**（见模块头注缺口第 5 条 —— 与 variation 无关）"""
        objs = DNP3Parser.parse_response(
            bytes([DNP3ObjectGroup.BI, 1, 0x00, 0x01, 0x00, 0x81]))
        assert [o['value'] for o in objs] == [1, 0, 1]
        assert all(o['quality'] == 0 for o in objs)

    def test_truncated_value_does_not_invent_data(self):
        """值被截断时只返回已完整解析的对象，不得凭残缺字节编值"""
        data = bytes([DNP3ObjectGroup.AI, 1, 0x00]) + _ai_values((1, 0)) + b'\x05'
        assert len(DNP3Parser.parse_response(data)) == 1

    def test_unknown_group_returns_empty(self):
        assert DNP3Parser.parse_response(bytes([0x77, 1, 0x00, 1, 2, 3])) == []

    def test_only_qualifier_zero_supported(self):
        """钉住现状：只处理 qualifier 0x00（见模块头注缺口第 6 条）"""
        data = bytes([DNP3ObjectGroup.AI, 1, 0x01]) + _ai_values((1, 0))
        assert DNP3Parser.parse_response(data) == []

    def test_too_short_returns_empty(self):
        assert DNP3Parser.parse_response(b'\x1e\x01') == []


# ---------------------------------------------------------------------------
# 4. 帧构建（含已知缺口）
# ---------------------------------------------------------------------------

class TestBuildFrames:
    def test_read_request_header(self):
        f = DNP3Parser.build_read_request(source=1, destination=2,
                                          object_group=DNP3ObjectGroup.AI)
        assert f[:2] == START, '起始字节必须是标准的 0x05 0x64'
        assert f[3] == 0xC0, '控制字节 DIR+PRM'
        assert struct.unpack('<H', f[4:6])[0] == 2 and struct.unpack('<H', f[6:8])[0] == 1
        assert f[10] == 0xC0 and f[11] == DNP3FunctionCode.READ

    def test_read_request_length_field(self):
        """LENGTH = 5 + 用户数据长度（**不含 CRC**）。

        历史实现写的是 +8 —— 帧长字段偏大 3，真实从站会按错误长度取数据。
        """
        f = DNP3Parser.build_read_request(1, 2, DNP3ObjectGroup.AI)
        app_len = len(f) - 10
        assert f[2] == app_len + 5

    def test_direct_operate_header(self):
        f = DNP3Parser.build_direct_operate(1, 2, index=7, value=1)
        assert f[:2] == START
        assert f[11] == DNP3FunctionCode.DIRECT_OPERATE
        assert f[-1] == 0x01, '单点输出值'

    def test_direct_operate_masks_value_to_one_bit(self):
        f = DNP3Parser.build_direct_operate(1, 2, index=7, value=0xFF)
        assert f[-1] == 0x01

    def test_builders_do_not_raise(self):
        """回归：历史实现多传一个参数，struct.pack 必抛、异常被 except 吞掉
        → **DNP3 从未真正发出过报文**"""
        assert DNP3Parser.build_read_request(1, 2, DNP3ObjectGroup.AI)
        assert DNP3Parser.build_direct_operate(1, 2, 1, 1)

    def test_crc_is_a_byte_sum_not_crc16(self):
        """钉住**已知缺口 1**：发送侧 CRC 是字节和，不是 CRC-16/DNP。

        补齐标准 CRC 时这条会红 —— 请同步更新模块头注缺口清单第 1 条。
        """
        f = DNP3Parser.build_read_request(1, 2, DNP3ObjectGroup.AI)
        header = f[:8]
        assert struct.unpack('<H', f[8:10])[0] == sum(header) & 0xFFFF

    def test_built_frame_is_shorter_than_standard(self):
        """钉住**已知缺口 2**：帧尾没有用户数据 CRC → 比标准短 2 字节。

        （补齐后这条会红，请同步更新缺口清单第 2 条。）
        """
        f = DNP3Parser.build_read_request(1, 2, DNP3ObjectGroup.AI)
        assert len(f) == DNP3Parser._frame_size(f[2]) - 2

    def test_read_request_qualifier_range_mismatch(self):
        """钉住**已知缺口 4**：限定符 0x06（all objects，不带范围）后面却跟了 4 字节范围"""
        f = DNP3Parser.build_read_request(1, 2, DNP3ObjectGroup.AI, start=0, stop=9)
        obj_header = f[12:]
        assert obj_header[2] == 0x06, '当前用 0x06'
        assert len(obj_header) == 7, '0x06 本应无范围字段，却带了 4 字节 start/stop'


# ---------------------------------------------------------------------------
# 5. 接收侧帧重组（本轮的行为改动）
# ---------------------------------------------------------------------------

class TestBufferReassembly:
    def test_two_frames_in_one_read(self):
        """粘包：一次 recv 里有两帧时，**两帧都要处理**。

        修复前 `_recv_loop` 把整包直接丢给 parse_frame ——
        第 2 帧被当成第 1 帧的数据丢掉。
        """
        c = _BufferedClient()
        f1 = _response_frame(DNP3ObjectGroup.AI, 1, _ai_values((1, 0)))
        f2 = _response_frame(DNP3ObjectGroup.AI, 1, _ai_values((2, 0)))
        c._recv_buffer = f1 + f2
        c._process_buffer()
        assert c.frames == [f1, f2]
        assert c._recv_buffer == b''

    def test_partial_frame_is_buffered(self):
        """半包：不完整帧必须留在缓冲区，不能被当成坏帧丢掉"""
        c = _BufferedClient()
        f = _response_frame(DNP3ObjectGroup.AI, 1, _ai_values((1, 0)))
        c._recv_buffer = f[:-4]
        c._process_buffer()
        assert c.frames == []
        assert c._recv_buffer == f[:-4]

    def test_partial_then_rest(self):
        c = _BufferedClient()
        f = _response_frame(DNP3ObjectGroup.AI, 1, _ai_values((1, 0)))
        c._recv_buffer = f[:6]
        c._process_buffer()
        assert c.frames == []
        c._recv_buffer += f[6:]
        c._process_buffer()
        assert c.frames == [f]

    def test_garbage_prefix_is_resynced(self):
        c = _BufferedClient()
        f = _response_frame(DNP3ObjectGroup.AI, 1, _ai_values((1, 0)))
        c._recv_buffer = b'\x11\x22\x33' + f
        c._process_buffer()
        assert c.frames == [f]

    def test_trailing_start_byte_is_kept(self):
        """只收到起始序列的前一半（0x05）时必须保留，等下一包补齐。

        （输入要 ≥10 字节才会进切帧循环；短于 10 字节时缓冲区原样保留即可。）
        """
        c = _BufferedClient()
        c._recv_buffer = b'\x11' * 11 + b'\x05'
        c._process_buffer()
        assert c._recv_buffer == b'\x05', '结尾那个 0x05 被丢掉了 —— 下一包补上后会对不齐'

    def test_invalid_length_is_resynced(self):
        """LENGTH 非法（< 5）→ 丢掉这对起始字节继续找，而不是卡死"""
        c = _BufferedClient()
        bad = START + bytes([0x01, 0xC0, 0x02, 0x00, 0x01, 0x00, 0x00, 0x00])
        good = _response_frame(DNP3ObjectGroup.AI, 1, _ai_values((9, 0)))
        c._recv_buffer = bad + good
        c._process_buffer()
        assert c.frames == [good]

    def test_all_garbage_does_not_grow_buffer(self):
        c = _BufferedClient()
        c._recv_buffer = b'\x01' * 40
        c._process_buffer()
        assert c.frames == []
        assert len(c._recv_buffer) <= 1, '无起始字节时缓冲区不得无界增长'

    def test_three_frames_with_mixed_sizes(self):
        c = _BufferedClient()
        frames = [
            _response_frame(DNP3ObjectGroup.AI, 1, _ai_values(*[(i, 0) for i in range(3)])),
            _response_frame(DNP3ObjectGroup.BI, 1, bytes([1, 0, 1])),
            _response_frame(DNP3ObjectGroup.AI32, 1, struct.pack('<iB', 5, 0)),
        ]
        c._recv_buffer = b''.join(frames)
        c._process_buffer()
        assert c.frames == frames


# ---------------------------------------------------------------------------
# 6. 响应落库（用不覆盖被测方法的替身）
# ---------------------------------------------------------------------------

class TestHandleResponse:
    def test_response_updates_snapshot(self):
        c = _SendingClient()
        c._handle_response(_response_frame(DNP3ObjectGroup.AI, 1, _ai_values((11, 0), (22, 0))))
        snap = c.read_data()
        assert [snap[i]['value'] for i in sorted(snap)] == [11, 22]
        assert snap[0]['group'] == DNP3ObjectGroup.AI

    def test_non_response_function_is_ignored(self):
        """outstation 只发 RESPONSE(0x81)；其他功能码不得污染数据"""
        c = _SendingClient()
        app = bytes([0xC0, DNP3FunctionCode.READ]) + bytes([DNP3ObjectGroup.AI, 1, 0x00])
        c._handle_response(_frame(app))
        assert c.read_data() == {}

    def test_invalid_frame_is_ignored(self):
        c = _SendingClient()
        c._handle_response(b'\x05\x65' + b'\x00' * 8)
        assert c.read_data() == {}

    def test_same_index_is_overwritten(self):
        c = _SendingClient()
        c._handle_response(_response_frame(DNP3ObjectGroup.AI, 1, _ai_values((1, 0))))
        c._handle_response(_response_frame(DNP3ObjectGroup.AI, 1, _ai_values((2, 0))))
        assert c.read_data()[0]['value'] == 2

    def test_read_data_returns_copy(self):
        c = _SendingClient()
        c._handle_response(_response_frame(DNP3ObjectGroup.AI, 1, _ai_values((1, 0))))
        snap = c.read_data()
        snap[999] = {'value': 0}
        assert 999 not in c.read_data()
