"""IEC 60870-5-104 网关的**协议层**行为测试（APCI 帧 / ASDU 解析 / 帧重组）。

为什么测这些
------------
`gateway/iec104_gateway.py` 是**手写的字节级协议实现**（355 语句、覆盖仅 18%），
而 IEC 104 是电力系统远动规约，接的是真实子站/RTU。手写字节处理正是
「解析出来的值看起来正常、其实是错的」最典型的藏身处 ——
现场表现是「遥测/遥信数值不对，但通道一切正常」。

覆盖：
* APCI 帧构建/解析（I/S/U 三类）+ **构建→解析往返**
* ASDU 解析（单点遥信 / 短浮点遥测 / 归一化遥测 / 总召唤）
* ASDU 控制方向构建（遥控 / 遥调 / 总召唤）+ **构建→解析往返**
* 接收缓冲区帧分割（粘包 / 断包 / 垃圾前缀重同步）
* `_handle_asdu` 的数据快照与回调隔离

⚠️ 本文件**不**改动任何协议语义；发现的两处「实现与标准/命名不一致」只做
**钉住 + 标注**（见 TestAsduParse::test_normalized_value_divisor 与
TestApciFrames::test_length_field_is_raw_byte 的说明），不在无现场手册的情况下擅改。
"""
import struct

import pytest

from gateway.iec104_gateway import (
    COT, IEC104APCI, IEC104ASDU, IEC104Client, TypeID, UFrame,
)


def _i_frame_bytes(send_seq: int, recv_seq: int, asdu: bytes) -> bytes:
    return IEC104APCI.build_i_frame(send_seq, recv_seq, asdu)


def _sp_asdu(ioa: int, value: int, *, num: int = 1, cot: int = COT.SPONTANEOUS,
             common_addr: int = 1) -> bytes:
    """构造一个 M_SP_NA_1（单点遥信）ASDU 字节串"""
    head = struct.pack('<BBBBH', TypeID.M_SP_NA_1, num, cot, 0, common_addr)
    body = struct.pack('<BBB', ioa & 0xFF, (ioa >> 8) & 0xFF, (ioa >> 16) & 0xFF)
    body += struct.pack('<B', value & 0x01)
    return head + body


def _nc_asdu(ioa: int, value: float, quality: int = 0, *, num: int = 1,
             common_addr: int = 1) -> bytes:
    """构造一个 M_ME_NC_1（短浮点遥测）ASDU 字节串"""
    head = struct.pack('<BBBBH', TypeID.M_ME_NC_1, num, COT.PERIODIC, 0, common_addr)
    body = struct.pack('<BBB', ioa & 0xFF, (ioa >> 8) & 0xFF, (ioa >> 16) & 0xFF)
    body += struct.pack('<fB', value, quality)
    return head + body


# ---------------------------------------------------------------------------
# 1. APCI 帧
# ---------------------------------------------------------------------------

class TestApciFrames:
    def test_i_frame_layout(self):
        asdu = b'\x01\x02\x03'
        f = IEC104APCI.build_i_frame(send_seq=5, recv_seq=7, asdu=asdu)
        assert f[0] == 0x68
        assert f[1] == len(asdu) + 4, '长度字段 = ASDU 长度 + 4（控制域 4 字节）'
        assert len(f) == len(asdu) + 6
        # 控制域：发送序号在低 2 字节、接收序号在高 2 字节，均左移 1 位
        assert struct.unpack('<H', f[2:4])[0] == 5 << 1
        assert struct.unpack('<H', f[4:6])[0] == 7 << 1
        assert f[6:] == asdu

    def test_s_frame_is_exactly_six_bytes(self):
        f = IEC104APCI.build_s_frame(recv_seq=9)
        assert len(f) == 6
        assert f[0] == 0x68 and f[1] == 4
        assert f[2] == 0x01, 'S 帧标识：控制域最低字节 = 0x01'
        assert struct.unpack('<H', f[4:6])[0] == 9 << 1

    def test_u_frame_is_exactly_six_bytes(self):
        f = IEC104APCI.build_u_frame(0x0004)   # STARTDT act
        assert len(f) == 6
        assert f[0] == 0x68 and f[1] == 4
        assert struct.unpack('<H', f[2:4])[0] == 0x0004

    def test_parse_classifies_u_frame(self):
        for control in (UFrame.STARTDT_ACT, UFrame.STARTDT_CON,
                        UFrame.STOPDT_ACT, UFrame.STOPDT_CON,
                        UFrame.TESTFR_ACT, UFrame.TESTFR_CON):
            p = IEC104APCI.parse_frame(IEC104APCI.build_u_frame(control))
            assert p['type'] == 'U' and p['control'] == control

    def test_all_u_frame_codes_have_u_flag_bits(self):
        """元守卫：U 帧标志是 bit0=1、bit1=1。六个标准控制码都必须满足。

        ⚠️ 回归：历史实现把 STARTDT act 写成 0x04、STOPDT act 写成 0x08 ——
        它们 bit0/bit1 都是 0，**会被解析成 I 帧**，握手永远建不起来。
        """
        for name, control in vars(UFrame).items():
            if name.startswith('_'):
                continue
            assert control & 0x03 == 0x03, f'{name}=0x{control:02X} 不是合法 U 帧控制码'

    def test_legacy_wrong_codes_are_not_u_frames(self):
        """回归对照：把历史那两个错值钉住 —— 它们必须**不被**当成 U 帧。

        这条是给「有人又把 0x04/0x08 写回去」准备的：一旦写回，
        `test_all_u_frame_codes_have_u_flag_bits` 与本条会同时红。
        """
        for bad in (0x04, 0x08):
            p = IEC104APCI.parse_frame(IEC104APCI.build_u_frame(bad))
            assert p['type'] != 'U', f'0x{bad:02X} 竟被当成 U 帧 —— U 帧标志判据坏了'
            assert p['type'] == 'I', '0x04/0x08 的 bit0/bit1 为 0，按当前判据会落进 I 帧'

    def test_u_frame_constants_match_standard(self):
        """钉住标准值（IEC 60870-5-104 §5.3），防止再被顺手改掉"""
        assert UFrame.STARTDT_ACT == 0x07 and UFrame.STARTDT_CON == 0x0B
        assert UFrame.STOPDT_ACT == 0x13 and UFrame.STOPDT_CON == 0x23
        assert UFrame.TESTFR_ACT == 0x43 and UFrame.TESTFR_CON == 0x83

    def test_parse_classifies_s_frame(self):
        p = IEC104APCI.parse_frame(IEC104APCI.build_s_frame(recv_seq=123))
        assert p['type'] == 'S' and p['recv_seq'] == 123

    def test_parse_classifies_i_frame(self):
        asdu = _sp_asdu(100, 1)
        p = IEC104APCI.parse_frame(_i_frame_bytes(3, 4, asdu))
        assert p['type'] == 'I'
        assert p['send_seq'] == 3 and p['recv_seq'] == 4
        assert p['asdu'] == asdu

    def test_i_frame_round_trip_at_sequence_boundaries(self):
        """往返：序号边界必须不串位（32767 是 15 位序号上限）"""
        asdu = b'\xAA' * 10
        for s, r in ((0, 0), (1, 0), (0, 32767), (32767, 32767), (12345, 6789)):
            p = IEC104APCI.parse_frame(_i_frame_bytes(s, r, asdu))
            assert (p['send_seq'], p['recv_seq']) == (s, r), f'序号串位: {s},{r}'
            assert p['asdu'] == asdu

    def test_invalid_start_byte(self):
        assert IEC104APCI.parse_frame(b'\x69\x04\x01\x00\x00\x00')['type'] == 'invalid'

    def test_too_short_is_invalid(self):
        for n in range(0, 6):
            assert IEC104APCI.parse_frame(b'\x68' * n)['type'] == 'invalid'

    def test_length_field_is_raw_byte(self):
        """钉住现状：长度字段直接取第二个字节，**不做合法性校验**。

        帧分割依赖它（见 TestBufferSplitting），所以这里把它固定下来。
        若将来加校验（IEC 104 上限 253），改这里即可，不用猜。
        """
        f = bytearray(_i_frame_bytes(1, 1, b'\x00' * 3))
        assert f[1] == 7
        f[1] = 200                       # 伪造一个超长 length
        p = IEC104APCI.parse_frame(bytes(f))
        assert p['type'] == 'I', '当前实现不看 length，只看控制域'


# ---------------------------------------------------------------------------
# 2. ASDU 解析
# ---------------------------------------------------------------------------

class TestAsduParse:
    def test_short_asdu_returns_empty(self):
        for n in range(0, 6):
            assert IEC104ASDU.parse_asdu(b'\x00' * n) == {}

    def test_single_point_information(self):
        p = IEC104ASDU.parse_asdu(_sp_asdu(100, 1, cot=COT.SPONTANEOUS, common_addr=3))
        assert p['type_id'] == TypeID.M_SP_NA_1
        assert p['num'] == 1 and p['cot'] == COT.SPONTANEOUS and p['common_addr'] == 3
        assert len(p['info_objects']) == 1
        o = p['info_objects'][0]
        assert o['ioa'] == 100 and o['value'] == 1.0

    def test_single_point_value_is_masked_to_one_bit(self):
        """单点遥信只取最低位（其余位是保留位，不得当成数值）"""
        raw = bytearray(_sp_asdu(1, 1))
        raw[-1] = 0b1111_1111
        assert IEC104ASDU.parse_asdu(bytes(raw))['info_objects'][0]['value'] == 1.0
        raw[-1] = 0b1111_1110
        assert IEC104ASDU.parse_asdu(bytes(raw))['info_objects'][0]['value'] == 0.0

    def test_short_float_telemetry(self):
        p = IEC104ASDU.parse_asdu(_nc_asdu(2000, -12.5, quality=0xA0))
        o = p['info_objects'][0]
        assert o['ioa'] == 2000
        assert o['value'] == pytest.approx(-12.5)
        assert o['quality'] == 0xA0

    def test_short_float_uses_little_endian(self):
        """IEC 104 的短浮点数按 IEEE754 低字节在前传输"""
        asdu = _nc_asdu(1, 1.0)
        assert asdu[-5:-1] == struct.pack('<f', 1.0)

    def test_normalized_value_divisor(self):
        """钉住现状：归一化值除以 **32767**。

        ⚠️ 待确认项：IEC 60870-5-101 §8.3 的归一化定义通常按 **32768** 换算
        （raw=32767 → 0.99997，raw=-32768 → -1.0）。
        本实现用 32767（raw=32767 → 恰好 1.0）。
        两者相差约 0.003%，工程上可忽略，但**语义不同** ——
        在拿到现场设备手册前不擅改（改错会让全站遥测整体偏移）。
        """
        head = struct.pack('<BBBBH', TypeID.M_ME_NA_1, 1, COT.PERIODIC, 0, 1)
        body = struct.pack('<BBBhB', 1, 0, 0, 32767, 0)
        p = IEC104ASDU.parse_asdu(head + body)
        assert p['info_objects'][0]['value'] == pytest.approx(1.0)
        body_min = struct.pack('<BBBhB', 1, 0, 0, -32768, 0)
        assert IEC104ASDU.parse_asdu(head + body_min)['info_objects'][0]['value'] \
            == pytest.approx(-32768 / 32767.0)

    def test_multiple_information_objects(self):
        asdu = _sp_asdu(10, 1, num=1) + struct.pack('<BBB', 11, 0, 0) + b'\x00'
        asdu = bytearray(asdu)
        asdu[1] = 2                       # num = 2
        p = IEC104ASDU.parse_asdu(bytes(asdu))
        assert [o['ioa'] for o in p['info_objects']] == [10, 11]
        assert [o['value'] for o in p['info_objects']] == [1.0, 0.0]

    def test_truncated_object_does_not_invent_data(self):
        """数据被截断时必须**少报**，不能凭残缺字节编出一个值"""
        full = _sp_asdu(100, 1)
        p = IEC104ASDU.parse_asdu(full[:-1])     # 少最后 1 字节（数值）
        assert p['info_objects'] == [], '截断的数据不得产出信息对象'

    def test_unsupported_type_yields_no_objects(self):
        head = struct.pack('<BBBBH', 99, 1, COT.SPONTANEOUS, 0, 1)
        p = IEC104ASDU.parse_asdu(head + b'\x01\x00\x00\x00')
        assert p['type_id'] == 99 and p['info_objects'] == []

    def test_num_zero_yields_no_objects(self):
        p = IEC104ASDU.parse_asdu(_sp_asdu(1, 1, num=0))
        assert p['info_objects'] == []


# ---------------------------------------------------------------------------
# 3. 控制方向 ASDU 构建
# ---------------------------------------------------------------------------

class TestBuildCommand:
    def test_setpoint_float_layout(self):
        b = IEC104ASDU.build_command(TypeID.C_SE_NC_1, ioa=0x010203, value=25.5,
                                     qual=0, common_addr=1)
        assert b[0] == TypeID.C_SE_NC_1
        assert b[1] == 1, 'VSQ = 1（单个信息对象）'
        assert b[2] == COT.ACTIVATION
        assert struct.unpack('<H', b[4:6])[0] == 1
        # IOA 三字节小端
        assert list(b[6:9]) == [0x03, 0x02, 0x01]
        assert struct.unpack('<f', b[9:13])[0] == pytest.approx(25.5)
        assert b[13] == 0
        assert len(b) == 14

    def test_single_command_masks_to_one_bit(self):
        b = IEC104ASDU.build_command(TypeID.C_SC_NA_1, ioa=1, value=1)
        assert b[-1] == 0x01
        b = IEC104ASDU.build_command(TypeID.C_SC_NA_1, ioa=1, value=0)
        assert b[-1] == 0x00
        b = IEC104ASDU.build_command(TypeID.C_SC_NA_1, ioa=1, value=7)
        assert b[-1] == 0x01, '单命令只允许 1 位，多余位必须被屏蔽'

    def test_general_interrogation_qoi(self):
        b = IEC104ASDU.build_command(TypeID.C_IC_NA_1, ioa=0, value=0)
        assert b[-1] == 0x14, 'QOI = 20（全体）'
        assert b[2] == COT.ACTIVATION

    def test_unsupported_type_returns_empty(self):
        assert IEC104ASDU.build_command(999, ioa=1, value=1) == b''

    def test_general_interrogation_round_trip(self):
        """构建 → 解析 往返：总召唤是现场联调第一步，必须能被自己解析回来"""
        b = IEC104ASDU.build_command(TypeID.C_IC_NA_1, ioa=0, value=0, common_addr=5)
        p = IEC104ASDU.parse_asdu(b)
        assert p['type_id'] == TypeID.C_IC_NA_1
        assert p['cot'] == COT.ACTIVATION
        assert p['common_addr'] == 5
        assert len(p['info_objects']) == 1

    def test_common_addr_is_packed_little_endian(self):
        b = IEC104ASDU.build_command(TypeID.C_IC_NA_1, ioa=0, value=0, common_addr=0x1234)
        assert struct.unpack('<H', b[4:6])[0] == 0x1234


# ---------------------------------------------------------------------------
# 4. 接收缓冲区帧分割
# ---------------------------------------------------------------------------

class _CapturingClient(IEC104Client):
    """只捕获被处理掉的完整帧，不碰 socket"""

    def __init__(self):
        super().__init__('127.0.0.1')
        self.handled: list[bytes] = []
        self.s_frames: list[int] = []

    def _handle_frame(self, frame_data: bytes):      # noqa: D102
        self.handled.append(frame_data)

    def _send_s_frame(self):                          # noqa: D102
        self.s_frames.append(self.recv_seq)


class TestBufferSplitting:
    def test_two_frames_in_one_read(self):
        c = _CapturingClient()
        f1 = _i_frame_bytes(0, 0, _sp_asdu(1, 1))
        f2 = _i_frame_bytes(1, 0, _sp_asdu(2, 0))
        c._recv_buffer = f1 + f2
        c._process_buffer()
        assert c.handled == [f1, f2]
        assert c._recv_buffer == b''

    def test_partial_frame_is_kept(self):
        """断包：不完整帧必须留在缓冲区等下一批数据，不能被当成坏帧丢掉"""
        c = _CapturingClient()
        f = _i_frame_bytes(0, 0, _sp_asdu(1, 1))
        c._recv_buffer = f[:-3]
        c._process_buffer()
        assert c.handled == []
        assert c._recv_buffer == f[:-3], '不完整帧被丢掉了 —— 会整段丢数据'

    def test_partial_then_complete(self):
        """先来半帧再来剩下的一半 → 必须拼成完整帧处理一次"""
        c = _CapturingClient()
        f = _i_frame_bytes(0, 0, _sp_asdu(1, 1))
        c._recv_buffer = f[:4]
        c._process_buffer()
        assert c.handled == []
        c._recv_buffer += f[4:]
        c._process_buffer()
        assert c.handled == [f]

    def test_garbage_prefix_is_resynced(self):
        """垃圾前缀：必须找到下一个 0x68 重新同步，而不是整段丢弃"""
        c = _CapturingClient()
        f = _i_frame_bytes(0, 0, _sp_asdu(1, 1))
        c._recv_buffer = b'\x00\x11\x22' + f
        c._process_buffer()
        assert c.handled == [f]

    def test_all_garbage_clears_buffer(self):
        c = _CapturingClient()
        c._recv_buffer = b'\x01\x02\x03\x04\x05\x06\x07'
        c._process_buffer()
        assert c.handled == []
        assert c._recv_buffer == b'', '无起始字节时应清空，否则缓冲区会无界增长'

    def test_short_buffer_untouched(self):
        c = _CapturingClient()
        c._recv_buffer = b'\x68\x04\x01'
        c._process_buffer()
        assert c._recv_buffer == b'\x68\x04\x01'


# ---------------------------------------------------------------------------
# 5. ASDU 落库与回调
# ---------------------------------------------------------------------------

class TestHandleAsdu:
    def test_snapshot_updated_per_ioa(self):
        c = _CapturingClient()
        c._handle_asdu(_sp_asdu(100, 1))
        c._handle_asdu(_sp_asdu(200, 0))
        snap = c.read_data()
        assert set(snap) == {100, 200}
        assert snap[100]['value'] == 1.0
        assert snap[200]['value'] == 0.0
        assert snap[100]['type_id'] == TypeID.M_SP_NA_1

    def test_same_ioa_is_overwritten(self):
        """同一 IOA 的新值必须覆盖旧值（否则界面会停在第一个值上）"""
        c = _CapturingClient()
        c._handle_asdu(_sp_asdu(100, 1))
        c._handle_asdu(_sp_asdu(100, 0))
        assert c.read_data()[100]['value'] == 0.0

    def test_callbacks_receive_ioa_and_value(self):
        c = _CapturingClient()
        seen = []
        c.add_callback(lambda ioa, value, asdu: seen.append((ioa, value)))
        c._handle_asdu(_nc_asdu(7, 3.5))
        assert seen == [(7, 3.5)]

    def test_callback_exception_is_isolated(self):
        """回调抛异常不得影响其他回调，也不得中断解析"""
        c = _CapturingClient()
        seen = []

        def boom(ioa, value, asdu):
            raise RuntimeError('回调炸了')

        c.add_callback(boom)
        c.add_callback(lambda ioa, value, asdu: seen.append(ioa))
        c._handle_asdu(_sp_asdu(1, 1))
        assert seen == [1], '前一个回调抛异常后，后续回调没有执行'

    def test_empty_asdu_is_ignored(self):
        c = _CapturingClient()
        c._handle_asdu(b'')
        assert c.read_data() == {}

    def test_read_data_returns_copy(self):
        """快照必须是副本，否则调用方改动会污染客户端内部状态"""
        c = _CapturingClient()
        c._handle_asdu(_sp_asdu(1, 1))
        snap = c.read_data()
        snap[999] = {'value': 1}
        assert 999 not in c.read_data()


# ---------------------------------------------------------------------------
# 6. I 帧处理（序号推进 + S 帧确认）
# ---------------------------------------------------------------------------

class _SendingClient(IEC104Client):
    """保留真实的 `_handle_frame` / `_handle_asdu`，只把 socket 发送打桩。

    ⚠️ 与 `_CapturingClient` 的区别很重要：那个类**覆盖了 `_handle_frame`**，
    所以不能用它测 `_handle_frame` 本身（否则测的是自己的桩 —— 我第一次就栽在这）。
    """

    def __init__(self):
        super().__init__('127.0.0.1')
        self.sent: list[bytes] = []

    def _send_u_frame(self, control: int):            # noqa: D102
        self.sent.append(IEC104APCI.build_u_frame(control))

    def _send_s_frame(self):                          # noqa: D102
        self.sent.append(IEC104APCI.build_s_frame(self.recv_seq))

    def _send_i_frame(self, asdu: bytes):             # noqa: D102
        self.sent.append(IEC104APCI.build_i_frame(self.send_seq, self.recv_seq, asdu))


class TestIFrameHandling:
    def test_i_frame_advances_recv_seq_and_acks(self):
        c = _SendingClient()
        c._handle_frame(_i_frame_bytes(0, 0, _sp_asdu(1, 1)))
        assert c.recv_seq == 1, '收到 I 帧后接收序号必须 +1'
        assert len(c.sent) == 1, '必须回一帧 S 帧确认（否则对端会停发）'
        ack = IEC104APCI.parse_frame(c.sent[0])
        assert ack['type'] == 'S' and ack['recv_seq'] == 1
        assert c.read_data()[1]['value'] == 1.0

    def test_recv_seq_wraps_at_32768(self):
        """序号是 15 位，32767 之后必须回绕到 0"""
        c = _SendingClient()
        c.recv_seq = 32767
        c._handle_frame(_i_frame_bytes(0, 0, _sp_asdu(1, 1)))
        assert c.recv_seq == 0

    def test_u_and_s_frames_do_not_touch_data(self):
        c = _SendingClient()
        c._handle_frame(IEC104APCI.build_u_frame(UFrame.STARTDT_CON))
        c._handle_frame(IEC104APCI.build_s_frame(5))
        assert c.read_data() == {} and c.sent == []

    def test_testfr_act_is_answered_with_con(self):
        """⚠️ 标准要求：收到 TESTFR act 必须回 TESTFR con。

        不回的话，对端在测试超时后判定链路失效并断开 ——
        表现为「连接周期性掉线」，而本地日志毫无异常。
        """
        c = _SendingClient()
        c._handle_frame(IEC104APCI.build_u_frame(UFrame.TESTFR_ACT))
        assert len(c.sent) == 1, 'TESTFR act 没有被应答'
        assert IEC104APCI.parse_frame(c.sent[0])['control'] == UFrame.TESTFR_CON

    def test_testfr_con_does_not_trigger_a_reply(self):
        """反向对照：收到 con 不得再回一帧（否则两端会无限对答）"""
        c = _SendingClient()
        c._handle_frame(IEC104APCI.build_u_frame(UFrame.TESTFR_CON))
        assert c.sent == []
