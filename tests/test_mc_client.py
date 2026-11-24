"""
Tests for MC (Mitsubishi) protocol client - covers 采集层/mc_client.py (0% -> high)
"""
import struct
import socket
import pytest
from unittest.mock import MagicMock, patch
from 采集层.mc_client import MCClient, DEVICE_CODES, SUBCMD_WORD_READ, SUBCMD_BIT_READ


@pytest.fixture
def mc_config():
    return {
        'id': 'fx5u_01',
        'name': 'Mitsubishi FX5U',
        'protocol': 'mc',
        'host': '192.168.1.56',
        'port': 5000,
        'network': 0,
        'pc': 0xFF,
        'timer': 10,
    }


@pytest.fixture
def mc_client(mc_config):
    return MCClient(mc_config)


class TestMCClientInit:
    def test_default_init(self, mc_client):
        assert mc_client.device_id == 'fx5u_01'
        assert mc_client.device_name == 'Mitsubishi FX5U'
        assert mc_client.host == '192.168.1.56'
        assert mc_client.port == 5000
        assert mc_client.network == 0
        assert mc_client.pc == 0xFF
        assert mc_client.timer == 10
        assert mc_client.connected is False

    def test_init_defaults(self):
        client = MCClient({})
        assert client.device_id == 'unknown'
        assert client.host == '192.168.1.56'
        assert client.port == 5000
        assert client.pc == 0xFF


class TestMCClientConnect:
    @patch('采集层.mc_client.socket.socket')
    def test_connect_success(self, mock_socket_cls, mc_client):
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock

        result = mc_client.connect()

        assert result is True
        assert mc_client.connected is True
        mock_sock.connect.assert_called_once_with(('192.168.1.56', 5000))

    @patch('采集层.mc_client.socket.socket')
    def test_connect_failure(self, mock_socket_cls, mc_client):
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock
        mock_sock.connect.side_effect = ConnectionRefusedError("refused")

        result = mc_client.connect()

        assert result is False
        assert mc_client.connected is False
        assert mc_client.stats['last_error'] is not None


class TestMCClientDisconnect:
    def test_disconnect_when_connected(self, mc_client):
        mock_sock = MagicMock()
        mc_client._sock = mock_sock
        mc_client.connected = True

        mc_client.disconnect()

        assert mc_client.connected is False
        assert mc_client._sock is None

    def test_disconnect_when_not_connected(self, mc_client):
        mc_client.disconnect()
        assert mc_client.connected is False

    def test_disconnect_close_raises(self, mc_client):
        mock_sock = MagicMock()
        mock_sock.close.side_effect = OSError("fail")
        mc_client._sock = mock_sock
        mc_client.connected = True

        mc_client.disconnect()
        assert mc_client.connected is False


class TestMCBuildFrame:
    def test_build_3e_frame(self, mc_client):
        data = b'\xA8\x00\x00\x00\x0A\x00'
        frame = mc_client._build_3e_frame(0x0401, SUBCMD_WORD_READ, data)

        # Check sub-header (0x5000 big-endian)
        assert frame[0:2] == struct.pack('>H', 0x5000)
        # Check network
        assert frame[2] == 0
        # Check PC
        assert frame[3] == 0xFF
        assert isinstance(frame, bytes)
        assert len(frame) > 11


class TestMCParseDeviceAddress:
    def test_parse_valid_device(self, mc_client):
        code, addr = mc_client._parse_device_address('D', 100)
        assert code == DEVICE_CODES['D']
        assert addr == 100

    def test_parse_invalid_device(self, mc_client):
        with pytest.raises(ValueError, match="不支持的设备类型"):
            mc_client._parse_device_address('INVALID', 0)

    def test_parse_case_insensitive(self, mc_client):
        code, _ = mc_client._parse_device_address('d', 0)
        assert code == DEVICE_CODES['D']


class TestMCSendRecv:
    def test_send_recv_short_header(self, mc_client):
        mock_sock = MagicMock()
        mock_sock.recv.return_value = b'\x00' * 5
        mc_client._sock = mock_sock

        result = mc_client._send_recv(b'\x00' * 20)

        assert result is None

    # 3E 帧响应头是 **9 字节**：
    #   副帧头(2) + 网络号(1) + PC号(1) + I/O编号(2) + 站号(1) + 数据长(2)
    # 数据长字段在偏移 7-8（小端）。
    # 下面两个用例原先按 **11 字节**构造头（`b'\x00'*9 + 长度`）—— 那是 4E 帧
    # 的布局，等于把「读 11 字节、从 header[9:11] 取长度」这个缺陷固化住了。
    # 2026-09 审计修掉该缺陷（改为读 9 字节、从 header[7:9] 取长度），
    # 测试随之对齐到正确的 3E 布局。

    def test_send_recv_completion_error(self, mc_client):
        mock_sock = MagicMock()
        resp_data = struct.pack('<H', 1) + b'\x00\x00'      # 完成码非 0
        header = b'\x00' * 7 + struct.pack('<H', len(resp_data))
        mock_sock.recv.side_effect = [header, resp_data]
        mc_client._sock = mock_sock

        result = mc_client._send_recv(b'\x00' * 20)

        assert result is None

    def test_send_recv_success(self, mc_client):
        mock_sock = MagicMock()
        payload = b'\x01\x02\x03\x04'
        resp_data = struct.pack('<H', 0) + payload          # 完成码 0 + 数据
        header = b'\x00' * 7 + struct.pack('<H', len(resp_data))
        mock_sock.recv.side_effect = [header, resp_data]
        mc_client._sock = mock_sock

        result = mc_client._send_recv(b'\x00' * 20)

        assert result == payload, '应返回去掉 2 字节完成码后的数据'

    def test_send_recv_timeout(self, mc_client):
        mock_sock = MagicMock()
        mock_sock.recv.side_effect = socket.timeout()
        mc_client._sock = mock_sock
        mc_client.connected = True

        result = mc_client._send_recv(b'\x00' * 20)

        assert result is None
        assert mc_client.connected is False

    def test_send_recv_general_exception(self, mc_client):
        mock_sock = MagicMock()
        mock_sock.send.side_effect = OSError("broken")
        mc_client._sock = mock_sock

        result = mc_client._send_recv(b'\x00' * 20)

        assert result is None


class TestMCReadWriteWords:
    def test_read_words_not_connected(self, mc_client):
        result = mc_client.read_words('D', 0, 10)
        assert result is None

    @patch.object(MCClient, '_send_recv')
    def test_read_words_success(self, mock_send_recv, mc_client):
        mc_client.connected = True
        mock_send_recv.return_value = struct.pack('<HH', 100, 200)

        result = mc_client.read_words('D', 0, 2)

        assert result == [100, 200]
        assert mc_client.stats['total_reads'] == 1
        assert mc_client.stats['successful_reads'] == 1

    @patch.object(MCClient, '_send_recv')
    def test_read_words_failure(self, mock_send_recv, mc_client):
        mc_client.connected = True
        mock_send_recv.return_value = None

        result = mc_client.read_words('D', 0, 1)

        assert result is None
        assert mc_client.stats['failed_reads'] == 1

    @patch.object(MCClient, '_send_recv')
    def test_read_words_partial(self, mock_send_recv, mc_client):
        mc_client.connected = True
        mock_send_recv.return_value = struct.pack('<H', 50)

        result = mc_client.read_words('D', 0, 3)

        # 响应短于请求字数 = 通信失败：原来补 0 冒充有效值（还会被计成功、
        # 污染报警与统计），现在必须返回 None 并计失败。
        assert result is None
        assert mc_client.stats['failed_reads'] >= 1

    def test_write_words_not_connected(self, mc_client):
        result = mc_client.write_words('D', 0, [1])
        assert result is False

    @patch.object(MCClient, '_send_recv')
    def test_write_words_success(self, mock_send_recv, mc_client):
        mc_client.connected = True
        mock_send_recv.return_value = b'\x00'

        result = mc_client.write_words('D', 0, [100, 200])

        assert result is True
        assert mc_client.stats['successful_writes'] == 1

    @patch.object(MCClient, '_send_recv')
    def test_write_words_failure(self, mock_send_recv, mc_client):
        mc_client.connected = True
        mock_send_recv.return_value = None

        result = mc_client.write_words('D', 0, [100])

        assert result is False
        assert mc_client.stats['failed_writes'] == 1


class TestMCReadWriteBits:
    def test_read_bits_not_connected(self, mc_client):
        result = mc_client.read_bits('M', 0, 8)
        assert result is None

    @patch.object(MCClient, '_send_recv')
    def test_read_bits_success(self, mock_send_recv, mc_client):
        mc_client.connected = True
        # MC 3E 二进制位读取：1 字节装 2 点，**先头点在 bit4-7（0x10）**、
        # 后一点在 bit0-3（0x01）。两半解反会让每对相邻线圈状态互换。
        mock_send_recv.return_value = bytes([0x10, 0x10])

        result = mc_client.read_bits('M', 0, 4)

        assert result == [True, False, True, False]
        assert mc_client.stats['successful_reads'] == 1

    @patch.object(MCClient, '_send_recv')
    def test_read_bits_nibble_order(self, mock_send_recv, mc_client):
        """独立判据：0x01 = 前点 0/后点 1，0x11 = 前点 1/后点 1。"""
        mc_client.connected = True
        mock_send_recv.return_value = bytes([0x01, 0x11])

        result = mc_client.read_bits('M', 0, 4)

        assert result == [False, True, True, True]
        assert mc_client.stats['successful_reads'] == 1

    @patch.object(MCClient, '_send_recv')
    def test_read_bits_failure(self, mock_send_recv, mc_client):
        mc_client.connected = True
        mock_send_recv.return_value = None

        result = mc_client.read_bits('M', 0, 1)

        assert result is None
        assert mc_client.stats['failed_reads'] == 1

    @patch.object(MCClient, '_send_recv')
    def test_read_bits_partial(self, mock_send_recv, mc_client):
        mc_client.connected = True
        mock_send_recv.return_value = bytes([0x05])

        result = mc_client.read_bits('M', 0, 4)

        # 4 个位需要 2 字节，只回 1 字节 = 通信失败：原来补 False 会把失败
        # 记成成功（还会让上位机看到"全 0"的正常假象），现在必须返回 None。
        assert result is None
        assert mc_client.stats['failed_reads'] >= 1

    def test_write_bits_not_connected(self, mc_client):
        result = mc_client.write_bits('M', 0, [True])
        assert result is False

    @patch.object(MCClient, '_send_recv')
    def test_write_bits_success(self, mock_send_recv, mc_client):
        mc_client.connected = True
        mock_send_recv.return_value = b'\x00'

        result = mc_client.write_bits('M', 0, [True, False, True, False])

        assert result is True
        assert mc_client.stats['successful_writes'] == 1

    @patch.object(MCClient, '_send_recv')
    def test_write_bits_failure(self, mock_send_recv, mc_client):
        mc_client.connected = True
        mock_send_recv.return_value = None

        result = mc_client.write_bits('M', 0, [True])

        assert result is False
        assert mc_client.stats['failed_writes'] == 1

    @patch.object(MCClient, '_send_recv')
    def test_write_bits_odd_count(self, mock_send_recv, mc_client):
        mc_client.connected = True
        mock_send_recv.return_value = b'\x00'

        result = mc_client.write_bits('M', 0, [True, False, True])

        assert result is True


class TestMCSingleAndFloat:
    @patch.object(MCClient, 'read_words')
    def test_read_single_word(self, mock_read, mc_client):
        mock_read.return_value = [42]
        result = mc_client.read_single_word('D', 0)
        assert result == 42

    @patch.object(MCClient, 'read_words')
    def test_read_single_word_none(self, mock_read, mc_client):
        mock_read.return_value = None
        result = mc_client.read_single_word('D', 0)
        assert result is None

    @patch.object(MCClient, 'write_words')
    def test_write_single_word(self, mock_write, mc_client):
        mock_write.return_value = True
        result = mc_client.write_single_word('D', 0, 42)
        assert result is True

    @patch.object(MCClient, 'read_bits')
    def test_read_single_bit(self, mock_read, mc_client):
        mock_read.return_value = [True]
        result = mc_client.read_single_bit('M', 0)
        assert result is True

    @patch.object(MCClient, 'read_bits')
    def test_read_single_bit_none(self, mock_read, mc_client):
        mock_read.return_value = None
        result = mc_client.read_single_bit('M', 0)
        assert result is None

    @patch.object(MCClient, 'write_bits')
    def test_write_single_bit(self, mock_write, mc_client):
        mock_write.return_value = True
        result = mc_client.write_single_bit('M', 0, True)
        assert result is True

    @patch.object(MCClient, 'read_words')
    def test_read_float32(self, mock_read, mc_client):
        raw = struct.pack('<f', 3.14)
        words = struct.unpack('<HH', raw)
        mock_read.return_value = list(words)

        result = mc_client.read_float32('D', 0)
        assert abs(result - 3.14) < 0.01

    @patch.object(MCClient, 'read_words')
    def test_read_float32_none(self, mock_read, mc_client):
        mock_read.return_value = None
        result = mc_client.read_float32('D', 0)
        assert result is None

    @patch.object(MCClient, 'read_words')
    def test_read_float32_short(self, mock_read, mc_client):
        mock_read.return_value = [1]
        result = mc_client.read_float32('D', 0)
        assert result is None

    @patch.object(MCClient, 'read_words')
    def test_read_int32(self, mock_read, mc_client):
        mock_read.return_value = [0x0000, 0x0001]
        result = mc_client.read_int32('D', 0)
        assert result == 0x10000

    @patch.object(MCClient, 'read_words')
    def test_read_int32_none(self, mock_read, mc_client):
        mock_read.return_value = None
        result = mc_client.read_int32('D', 0)
        assert result is None

    @patch.object(MCClient, 'write_words')
    def test_write_float32(self, mock_write, mc_client):
        mock_write.return_value = True
        result = mc_client.write_float32('D', 0, 3.14)
        assert result is True


class TestMCGetStats:
    def test_get_stats(self, mc_client):
        stats = mc_client.get_stats()
        assert stats['device_id'] == 'fx5u_01'
        assert stats['device_name'] == 'Mitsubishi FX5U'
        assert stats['connected'] is False
