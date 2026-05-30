"""
Tests for FINS (Omron) protocol client - covers 采集层/fins_client.py (0% -> high)
"""
import struct
import socket
import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from 采集层.fins_client import FINSClient, MEMORY_AREA_CODES, BIT_AREAS, WORD_AREAS


@pytest.fixture
def fins_config():
    return {
        'id': 'nj501_01',
        'name': 'Omron NJ501',
        'protocol': 'fins',
        'host': '192.168.1.60',
        'port': 9600,
        'timeout': 5,
    }


@pytest.fixture
def fins_client(fins_config):
    return FINSClient(fins_config)


class TestFINSClientInit:
    def test_default_init(self, fins_client):
        assert fins_client.device_id == 'nj501_01'
        assert fins_client.device_name == 'Omron NJ501'
        assert fins_client.host == '192.168.1.60'
        assert fins_client.port == 9600
        assert fins_client.timeout == 5
        assert fins_client.connected is False
        assert fins_client._sock is None
        assert fins_client._sid == 0
        assert fins_client._local_node == 0
        assert fins_client._remote_node == 0

    def test_init_defaults(self):
        client = FINSClient({})
        assert client.device_id == 'unknown'
        assert client.host == '192.168.1.60'
        assert client.port == 9600
        assert client.timeout == 10

    def test_stats_initialized(self, fins_client):
        stats = fins_client.stats
        assert stats['total_reads'] == 0
        assert stats['successful_reads'] == 0
        assert stats['failed_reads'] == 0
        assert stats['total_writes'] == 0
        assert stats['successful_writes'] == 0
        assert stats['failed_writes'] == 0
        assert stats['last_error'] is None


class TestFINSClientConnect:
    @patch('采集层.fins_client.socket.socket')
    def test_connect_success(self, mock_socket_cls, fins_client):
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock
        # FINS handshake: send 16 bytes, recv 24 bytes
        handshake_resp = struct.pack('>IIIII', 8, 0, 0, 1, 2) + b'\x00' * 4
        mock_sock.recv.return_value = handshake_resp

        result = fins_client.connect()

        assert result is True
        assert fins_client.connected is True
        assert fins_client._local_node == 1
        assert fins_client._remote_node == 2
        mock_sock.connect.assert_called_once_with(('192.168.1.60', 9600))

    @patch('采集层.fins_client.socket.socket')
    def test_connect_handshake_error(self, mock_socket_cls, fins_client):
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock
        # Handshake with error code
        handshake_resp = struct.pack('>IIIII', 8, 0, 1, 0, 0) + b'\x00' * 4
        mock_sock.recv.return_value = handshake_resp

        result = fins_client.connect()

        assert result is False
        assert fins_client.connected is False

    @patch('采集层.fins_client.socket.socket')
    def test_connect_handshake_short_response(self, mock_socket_cls, fins_client):
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock
        mock_sock.recv.return_value = b'\x00' * 10  # Too short

        result = fins_client.connect()

        assert result is False
        assert fins_client.connected is False

    @patch('采集层.fins_client.socket.socket')
    def test_connect_exception(self, mock_socket_cls, fins_client):
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock
        mock_sock.connect.side_effect = ConnectionRefusedError("refused")

        result = fins_client.connect()

        assert result is False
        assert fins_client.connected is False
        assert fins_client.stats['last_error'] is not None


class TestFINSClientDisconnect:
    def test_disconnect_when_connected(self, fins_client):
        mock_sock = MagicMock()
        fins_client._sock = mock_sock
        fins_client.connected = True

        fins_client.disconnect()

        assert fins_client.connected is False
        assert fins_client._sock is None
        mock_sock.close.assert_called_once()

    def test_disconnect_when_not_connected(self, fins_client):
        fins_client.disconnect()
        assert fins_client.connected is False

    def test_disconnect_close_raises(self, fins_client):
        mock_sock = MagicMock()
        mock_sock.close.side_effect = OSError("fail")
        fins_client._sock = mock_sock
        fins_client.connected = True

        fins_client.disconnect()
        assert fins_client.connected is False


class TestFINSServiceID:
    def test_next_sid_increments(self, fins_client):
        assert fins_client._next_sid() == 1
        assert fins_client._next_sid() == 2

    def test_next_sid_wraps_around(self, fins_client):
        fins_client._sid = 255
        assert fins_client._next_sid() == 0


class TestFINSBuildFrame:
    def test_build_fins_frame(self, fins_client):
        fins_client._local_node = 1
        fins_client._remote_node = 2
        data = b'\x01\x02\x03'
        frame = fins_client._build_fins_frame(0x0101, data)

        # Should start with 4-byte length (big-endian)
        assert len(frame) > 12 + 10 + len(data)
        # Verify it's valid bytes
        assert isinstance(frame, bytes)


class TestFINSReadWriteWords:
    def test_read_words_not_connected(self, fins_client):
        result = fins_client.read_words('D', 0, 10)
        assert result is None

    def test_read_words_invalid_area(self, fins_client):
        fins_client.connected = True
        result = fins_client.read_words('INVALID', 0, 10)
        assert result is None

    @patch.object(FINSClient, '_send_recv')
    def test_read_words_success(self, mock_send_recv, fins_client):
        fins_client.connected = True
        # Response: 3 words (6 bytes)
        mock_send_recv.return_value = struct.pack('>HHH', 100, 200, 300)

        result = fins_client.read_words('D', 0, 3)

        assert result == [100, 200, 300]
        assert fins_client.stats['total_reads'] == 1
        assert fins_client.stats['successful_reads'] == 1

    @patch.object(FINSClient, '_send_recv')
    def test_read_words_send_recv_fails(self, mock_send_recv, fins_client):
        fins_client.connected = True
        mock_send_recv.return_value = None

        result = fins_client.read_words('D', 0, 1)

        assert result is None
        assert fins_client.stats['failed_reads'] == 1

    @patch.object(FINSClient, '_send_recv')
    def test_read_words_partial_data(self, mock_send_recv, fins_client):
        fins_client.connected = True
        # Response shorter than expected
        mock_send_recv.return_value = struct.pack('>H', 100)

        result = fins_client.read_words('D', 0, 3)

        assert len(result) == 3
        assert result[0] == 100
        assert result[1] == 0  # padding
        assert result[2] == 0

    @patch.object(FINSClient, 'read_words')
    def test_read_single_word(self, mock_read, fins_client):
        mock_read.return_value = [42]
        result = fins_client.read_single_word('D', 0)
        assert result == 42

    @patch.object(FINSClient, 'read_words')
    def test_read_single_word_none(self, mock_read, fins_client):
        mock_read.return_value = None
        result = fins_client.read_single_word('D', 0)
        assert result is None

    def test_write_words_not_connected(self, fins_client):
        result = fins_client.write_words('D', 0, [1, 2])
        assert result is False

    def test_write_words_invalid_area(self, fins_client):
        fins_client.connected = True
        result = fins_client.write_words('INVALID', 0, [1])
        assert result is False

    @patch.object(FINSClient, '_send_recv')
    def test_write_words_success(self, mock_send_recv, fins_client):
        fins_client.connected = True
        mock_send_recv.return_value = b'\x00'

        result = fins_client.write_words('D', 0, [100, 200])

        assert result is True
        assert fins_client.stats['total_writes'] == 1
        assert fins_client.stats['successful_writes'] == 1

    @patch.object(FINSClient, '_send_recv')
    def test_write_words_failure(self, mock_send_recv, fins_client):
        fins_client.connected = True
        mock_send_recv.return_value = None

        result = fins_client.write_words('D', 0, [100])

        assert result is False
        assert fins_client.stats['failed_writes'] == 1

    @patch.object(FINSClient, 'write_words')
    def test_write_single_word(self, mock_write, fins_client):
        mock_write.return_value = True
        result = fins_client.write_single_word('D', 0, 42)
        assert result is True
        mock_write.assert_called_once_with('D', 0, [42 & 0xFFFF])


class TestFINSReadWriteBits:
    def test_read_bits_not_connected(self, fins_client):
        result = fins_client.read_bits('CIO', 0, 0, 8)
        assert result is None

    def test_read_bits_invalid_area(self, fins_client):
        fins_client.connected = True
        result = fins_client.read_bits('INVALID', 0, 0, 8)
        assert result is None

    @patch.object(FINSClient, '_send_recv')
    def test_read_bits_success(self, mock_send_recv, fins_client):
        fins_client.connected = True
        mock_send_recv.return_value = bytes([0x01, 0x00, 0x01])

        result = fins_client.read_bits('CIO', 0, 0, 3)

        assert result == [True, False, True]
        assert fins_client.stats['successful_reads'] == 1

    @patch.object(FINSClient, '_send_recv')
    def test_read_bits_failure(self, mock_send_recv, fins_client):
        fins_client.connected = True
        mock_send_recv.return_value = None

        result = fins_client.read_bits('CIO', 0, 0, 1)

        assert result is None
        assert fins_client.stats['failed_reads'] == 1

    @patch.object(FINSClient, '_send_recv')
    def test_read_bits_partial(self, mock_send_recv, fins_client):
        fins_client.connected = True
        mock_send_recv.return_value = bytes([0x01])

        result = fins_client.read_bits('W', 0, 0, 3)

        assert len(result) == 3
        assert result[0] is True
        assert result[1] is False
        assert result[2] is False

    def test_write_bits_not_connected(self, fins_client):
        result = fins_client.write_bits('CIO', 0, 0, [True])
        assert result is False

    def test_write_bits_invalid_area(self, fins_client):
        fins_client.connected = True
        result = fins_client.write_bits('INVALID', 0, 0, [True])
        assert result is False

    @patch.object(FINSClient, '_send_recv')
    def test_write_bits_success(self, mock_send_recv, fins_client):
        fins_client.connected = True
        mock_send_recv.return_value = b'\x00'

        result = fins_client.write_bits('CIO', 0, 0, [True, False, True])

        assert result is True
        assert fins_client.stats['successful_writes'] == 1

    @patch.object(FINSClient, '_send_recv')
    def test_write_bits_failure(self, mock_send_recv, fins_client):
        fins_client.connected = True
        mock_send_recv.return_value = None

        result = fins_client.write_bits('W', 0, 0, [True])

        assert result is False
        assert fins_client.stats['failed_writes'] == 1


class TestFINSFloatInt:
    @patch.object(FINSClient, 'read_words')
    def test_read_float32_success(self, mock_read, fins_client):
        raw = struct.pack('>f', 3.14)
        words = struct.unpack('>HH', raw)
        mock_read.return_value = list(words)

        result = fins_client.read_float32('D', 0)
        assert abs(result - 3.14) < 0.01

    @patch.object(FINSClient, 'read_words')
    def test_read_float32_none(self, mock_read, fins_client):
        mock_read.return_value = None
        result = fins_client.read_float32('D', 0)
        assert result is None

    @patch.object(FINSClient, 'read_words')
    def test_read_float32_short(self, mock_read, fins_client):
        mock_read.return_value = [1]
        result = fins_client.read_float32('D', 0)
        assert result is None

    @patch.object(FINSClient, 'read_words')
    def test_read_int32_success(self, mock_read, fins_client):
        mock_read.return_value = [0x0001, 0x0000]
        result = fins_client.read_int32('D', 0)
        assert result == 0x10000

    @patch.object(FINSClient, 'read_words')
    def test_read_int32_none(self, mock_read, fins_client):
        mock_read.return_value = None
        result = fins_client.read_int32('D', 0)
        assert result is None

    @patch.object(FINSClient, 'write_words')
    def test_write_float32(self, mock_write, fins_client):
        mock_write.return_value = True
        result = fins_client.write_float32('D', 0, 3.14)
        assert result is True


class TestFINSSendRecv:
    def test_send_recv_timeout(self, fins_client):
        mock_sock = MagicMock()
        mock_sock.send.return_value = None
        mock_sock.recv.side_effect = socket.timeout("timed out")
        fins_client._sock = mock_sock
        fins_client.connected = True

        result = fins_client._send_recv(b'\x00' * 20)

        assert result is None
        assert fins_client.connected is False

    def test_send_recv_short_header(self, fins_client):
        mock_sock = MagicMock()
        mock_sock.recv.return_value = b'\x00' * 5  # Too short
        fins_client._sock = mock_sock

        result = fins_client._send_recv(b'\x00' * 20)

        assert result is None

    def test_send_recv_short_data(self, fins_client):
        mock_sock = MagicMock()
        # Header: length=100
        header = struct.pack('>III', 100, 0, 0)
        mock_sock.recv.side_effect = [header, b'\x00' * 5]  # Not enough data
        fins_client._sock = mock_sock

        result = fins_client._send_recv(b'\x00' * 20)

        assert result is None

    def test_send_recv_completion_code_error(self, fins_client):
        mock_sock = MagicMock()
        # Header + data with error completion code
        fins_data = b'\x00' * 12 + struct.pack('>H', 1) + b'\x00\x00'
        header = struct.pack('>III', len(fins_data), 0, 0)
        mock_sock.recv.side_effect = [header, fins_data]
        fins_client._sock = mock_sock

        result = fins_client._send_recv(b'\x00' * 20)

        assert result is None

    def test_send_recv_success(self, fins_client):
        mock_sock = MagicMock()
        payload = b'\x01\x02\x03\x04'
        fins_data = b'\x00' * 12 + struct.pack('>H', 0) + payload
        header = struct.pack('>III', len(fins_data), 0, 0)
        mock_sock.recv.side_effect = [header, fins_data]
        fins_client._sock = mock_sock

        result = fins_client._send_recv(b'\x00' * 20)

        assert result == payload

    def test_send_recv_general_exception(self, fins_client):
        mock_sock = MagicMock()
        mock_sock.send.side_effect = OSError("broken pipe")
        fins_client._sock = mock_sock

        result = fins_client._send_recv(b'\x00' * 20)

        assert result is None


class TestFINSGetStats:
    def test_get_stats(self, fins_client):
        stats = fins_client.get_stats()
        assert stats['device_id'] == 'nj501_01'
        assert stats['device_name'] == 'Omron NJ501'
        assert stats['connected'] is False
        assert 'total_reads' in stats


class TestFINSConstants:
    def test_memory_area_codes(self):
        assert 'D' in MEMORY_AREA_CODES
        assert 'CIO' in MEMORY_AREA_CODES
        assert 'W' in MEMORY_AREA_CODES
        assert 'H' in MEMORY_AREA_CODES
        assert MEMORY_AREA_CODES['D'] == 0x02

    def test_bit_and_word_areas(self):
        assert 'CIO' in BIT_AREAS
        assert 'D' in WORD_AREAS
