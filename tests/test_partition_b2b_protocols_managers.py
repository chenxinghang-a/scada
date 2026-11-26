"""Partition B2b tests: protocol clients and device managers."""

from __future__ import annotations

import json
import struct
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

import 采集层.simulated_client as simulated_client_mod
from 采集层.base_client import encode_float64_registers
from 采集层.device_manager import DeviceManager
from 采集层.fins_client import FINSClient
from 采集层.mc_client import MCClient
from 采集层.modbus_client import ModbusClient
from 采集层.mqtt_client import MQTTClient
from 采集层.opcua_client import OPCUAClient
from 采集层.real_device_manager import RealDeviceManager
from 采集层.simulated_device_manager import SimulatedDeviceManager


# ---------------------------------------------------------------------------
# MCClient
# ---------------------------------------------------------------------------

def test_mc_write_bits_uses_high_nibble_first():
    client = MCClient({'id': 'mc1', 'host': '127.0.0.1', 'port': 5000})
    client.connected = True
    captured: list[bytes] = []

    def _fake_send(frame: bytes) -> bytes:
        captured.append(frame)
        return b'\xd0\x00\x00\xff\xff\x03\x00\x02\x00\x00\x00'

    client._send_recv = _fake_send
    ok = client.write_bits('M', 0, [True, False, False, True])
    assert ok is True
    assert len(captured) == 1
    # 3E subheader(2) + net(1)+pc(1)+io(2)+station(1)+len(2)+timer(2)+cmd(2)+subcmd(2)+addr(3)+code(1)+cnt(2) = 21 bytes before data
    payload = captured[0][21:]
    assert payload == bytes([0x10, 0x01])


def test_mc_connect_closes_prior_and_failed_sockets(monkeypatch):
    client = MCClient({'id': 'mc2', 'host': '127.0.0.1', 'port': 5000})
    old_sock = MagicMock()
    client._sock = old_sock
    client.connected = True

    new_sock = MagicMock()
    new_sock.connect.side_effect = OSError('refused')
    monkeypatch.setattr('socket.socket', lambda *a, **kw: new_sock)

    assert client.connect() is False
    old_sock.close.assert_called_once()
    new_sock.close.assert_called_once()
    assert client._sock is None
    assert client.connected is False


def test_mc_area_routing_and_numeric_guards():
    client = MCClient({
        'id': 'mc3',
        'host': '127.0.0.1',
        'port': 5000,
        'word_area': 'D',
        'input_word_area': 'ZR',
        'bit_area': 'M',
        'di_area': 'X',
    })
    client.connected = True
    seen_words: list[tuple[str, int, int]] = []
    seen_bits: list[tuple[str, int, int]] = []
    client.read_words = lambda area, addr, cnt: seen_words.append((area, addr, cnt)) or [11]
    client.read_bits = lambda area, addr, cnt: seen_bits.append((area, addr, cnt)) or [True]

    assert client.read_input_registers(10, 1) == [11]
    assert client.read_discrete_inputs(20, 1) == [True]
    assert seen_words == [('ZR', 10, 1)]
    assert seen_bits == [('X', 20, 1)]

    assert client.write_single_register(0, True) is False
    assert client.write_single_register(0, float('nan')) is False
    assert client.write_multiple_registers(0, [1, float('inf')]) is False
    assert client.write_multiple_registers(0, [False, 2]) is False


# ---------------------------------------------------------------------------
# FINSClient
# ---------------------------------------------------------------------------

def test_fins_send_recv_reads_16_byte_header_and_rejects_tcp_error():
    client = FINSClient({'id': 'fins1', 'host': '127.0.0.1', 'port': 9600})
    client.connected = True

    payload = b'\xc0\x00\x02\x00\x01\x00\x00\x01\x00\x00\x01\x01\x00\x00\x12\x34'
    hdr = struct.pack('>4sIII', b'FINS', 8 + len(payload), 2, 0)
    full = hdr + payload

    class _ChunkedSock:
        def __init__(self, stream: bytes):
            self._buf = bytearray(stream)

        def sendall(self, data: bytes) -> None:
            return None

        def recv(self, size: int) -> bytes:
            take = min(3, size, len(self._buf))
            out = bytes(self._buf[:take])
            del self._buf[:take]
            return out

    client._sock = _ChunkedSock(full)
    resp = client._send_recv(b'\x00' * 16)
    assert resp == payload[14:]

    err_hdr = struct.pack('>4sIII', b'FINS', 8, 2, 1)
    client._sock = _ChunkedSock(err_hdr)
    assert client._send_recv(b'\x00' * 16) is None


def test_fins_sid_lock_thread_safe_and_socket_cleanup(monkeypatch):
    client = FINSClient({'id': 'fins2', 'host': '127.0.0.1', 'port': 9600})

    def _worker():
        for _ in range(100):
            client._next_sid()

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert client._sid == (800 % 256)

    old_sock = MagicMock()
    client._sock = old_sock
    new_sock = MagicMock()
    new_sock.connect.side_effect = OSError('down')
    monkeypatch.setattr('socket.socket', lambda *a, **kw: new_sock)
    assert client.connect() is False
    old_sock.close.assert_called_once()
    new_sock.close.assert_called_once()
    assert client._sock is None

    client.connected = True
    assert client.write_single_register(0, True) is False
    assert client.write_multiple_registers(0, [float('nan')]) is False


# ---------------------------------------------------------------------------
# ModbusClient
# ---------------------------------------------------------------------------

def test_modbus_cache_fc_slave_isolation_and_guards():
    client = ModbusClient({'id': 'mb1', 'name': 'mb1', 'protocol': 'modbus_tcp', 'host': '127.0.0.1', 'port': 502})
    client.connected = True

    def _fc3_res(addr, cnt, slave=1):
        return SimpleNamespace(isError=lambda: False, registers=[100 + slave, 200 + slave])

    def _fc4_res(addr, cnt, slave=1):
        return SimpleNamespace(isError=lambda: False, registers=[300 + slave, 400 + slave])

    fake_py = MagicMock()
    fake_py.read_holding_registers.side_effect = lambda address, count, slave=1: _fc3_res(address, count, slave)
    fake_py.read_input_registers.side_effect = lambda address, count, slave=1: _fc4_res(address, count, slave)
    client.client = fake_py

    assert client.read_holding_registers(0, 2, slave_id=1) == [101, 201]
    assert client.read_holding_registers(0, 2, slave_id=2) == [102, 202]
    assert client.read_input_registers(0, 2, slave_id=1) == [301, 401]
    assert client.get_cache_age(0, 2, function_code=3, slave_id=1) is not None
    assert client.get_cache_age(0, 2, function_code=4, slave_id=1) is not None

    fake_py.read_holding_registers.side_effect = TimeoutError('timeout')
    fake_py.read_input_registers.side_effect = TimeoutError('timeout')
    assert client.read_holding_registers(0, 2, slave_id=1) == [101, 201]
    assert client.read_holding_registers(0, 2, slave_id=2) == [102, 202]
    assert client.read_input_registers(0, 2, slave_id=1) == [301, 401]

    # Short coil / discrete input responses return None instead of short list
    fake_py.read_coils.return_value = SimpleNamespace(isError=lambda: False, bits=[True])
    fake_py.read_discrete_inputs.return_value = SimpleNamespace(isError=lambda: False, bits=[False])
    assert client.read_coils(0, 3) is None
    assert client.read_discrete_inputs(0, 3) is None

    # Non-finite / bool write rejection
    assert client.write_multiple_registers(0, [True]) is False
    assert client.write_multiple_registers(0, [float('nan')]) is False
    assert client.write_multiple_registers(0, [float('inf')]) is False

    client.disconnect()
    assert client.client is None
    assert client.connected is False


# ---------------------------------------------------------------------------
# OPCUAClient & MQTTClient
# ---------------------------------------------------------------------------

def test_opcua_stats_quality_and_callback_signature():
    client = OPCUAClient({
        'id': 'ua1',
        'name': 'ua1',
        'endpoint': 'opc.tcp://127.0.0.1:4840',
        'nodes': [{'node_id': 'ns=2;s=Temp', 'name': 'temp', 'unit': 'C'}],
    })
    stats = client.get_stats()
    assert stats['device_id'] == 'ua1'
    assert stats['connected'] is False

    seen_plain = []
    seen_quality = []

    def cb_plain(dev_id, reg_name, val, unit):
        seen_plain.append((dev_id, reg_name, val, unit))

    def cb_quality(dev_id, reg_name, val, unit, quality=None):
        seen_quality.append((dev_id, reg_name, val, unit, quality))

    client.add_data_callback(cb_plain)
    client.add_data_callback(cb_quality)

    mock_node = MagicMock()
    mock_node.nodeid.to_string.return_value = 'ns=2;s=Temp'
    client.datachange_notification(mock_node, float('nan'), None)

    assert len(seen_plain) == 1
    assert len(seen_quality) == 1
    assert seen_quality[0][4] == 'bad'
    assert client.latest_data['temp']['quality'] == 'bad'


def test_mqtt_tls_guard_bad_utf8_and_publish_nan(monkeypatch):
    paho_client = MagicMock()
    paho_client.connect.return_value = 0
    paho_client.publish.return_value = SimpleNamespace(rc=0)
    monkeypatch.setattr('采集层.mqtt_client.mqtt.Client', lambda *a, **kw: paho_client)

    client = MQTTClient({
        'id': 'mq1',
        'name': 'mq1',
        'broker': '127.0.0.1',
        'port': 8883,
        'tls_enabled': True,
        'tls_insecure': False,
    })
    assert client.connect() is True
    assert client.connect() is True
    assert paho_client.tls_set_context.call_count == 1

    # Non-zero connect return code fails loudly
    paho_client.connect.return_value = 5
    assert client.connect() is False

    # Publish rejects NaN via allow_nan=False
    client.connected = True
    assert client.publish('t/1', {'v': float('nan')}) is False

    # Bad UTF-8 payload decoded without crashing, and retained JSON forwards flag only to retained-aware callback
    seen_retained = []
    seen_plain = []
    client.add_data_callback(lambda d, r, v, u, retained=False: seen_retained.append((d, r, v, retained)))
    client.add_data_callback(lambda d, r, v, u: seen_plain.append((d, r, v)))
    bad_msg = SimpleNamespace(topic='factory/temp', payload=b'\xff\xfe', retain=True)
    client._on_message(paho_client, None, bad_msg)
    good_msg = SimpleNamespace(
        topic='factory/temp',
        payload=json.dumps({'device_id': 'mq1', 'register': 'temp', 'value': 42.5, 'unit': 'C'}).encode('utf-8'),
        retain=True,
    )
    client._on_message(paho_client, None, good_msg)
    assert client.stats['retained_messages'] == 2
    assert len(seen_retained) == 1 and seen_retained[0] == ('mq1', 'temp', 42.5, True)
    assert len(seen_plain) == 1 and seen_plain[0] == ('mq1', 'temp', 42.5)


# ---------------------------------------------------------------------------
# DeviceManager, RealDeviceManager, SimulatedDeviceManager
# ---------------------------------------------------------------------------

def test_device_manager_status_lock_and_get_stats_copy():
    mgr = DeviceManager(simulation_mode=False)
    mgr.devices['d1'] = {'id': 'd1', 'name': 'D1', 'protocol': 'modbus_tcp'}
    fake_client = MagicMock()
    fake_client.connected = True
    fake_client.is_connected.return_value = True
    fake_client.get_stats.return_value = {'total_reads': 9}
    mgr.clients['d1'] = fake_client

    status = mgr.get_device_status('d1')
    assert status['stats'] == {'total_reads': 9}
    status['stats']['total_reads'] = 999
    status2 = mgr.get_device_status('d1')
    assert status2['stats'] == {'total_reads': 9}

    mgr.start_reconnect_loop(interval=1)
    mgr.start_reconnect_loop(interval=1)
    mgr._stop_reconnect_loop()
    assert mgr._reconnect_thread is None


def test_real_device_manager_estop_coil_float64_and_reconnect_lock(tmp_path):
    cfg = tmp_path / 'devices.yaml'
    cfg.write_text(
        yaml.safe_dump({
            'devices': [
                {
                    'id': 'mech1',
                    'name': 'Pump',
                    'protocol': 'modbus_tcp',
                    'device_category': 'mechanical',
                    'byte_order': 'DCBA',
                    'registers': [
                        {'name': 'speed', 'address': 0, 'data_type': 'uint16'},
                        {'name': 'run_coil', 'address': 5, 'data_type': 'bool'},
                        {'name': 'energy_f64', 'address': 10, 'data_type': 'float64'},
                    ],
                },
                {
                    'id': 'env1',
                    'name': 'HeaterCtrl',
                    'protocol': 'modbus_tcp',
                    'device_category': 'sensor',
                    'byte_order': 'ABCD',
                    'registers': [
                        {'name': 'run_coil', 'address': 1, 'data_type': 'coil'},
                        {'name': 'target_f64', 'address': 20, 'data_type': 'float64'},
                    ],
                },
            ]
        }, allow_unicode=True),
        encoding='utf-8',
    )
    mgr = RealDeviceManager(config_path=str(cfg))

    class _FakeRWClient:
        def __init__(self):
            self.connected = True
            self.last_read_source = 'fresh'
            self.regs: dict[int, int] = {}
            self.coils: dict[int, bool] = {}

        def write_single_coil(self, address, value):
            self.coils[address] = bool(value)
            return True

        def read_coils(self, address, count=1):
            return [self.coils.get(address + i, False) for i in range(count)]

        def write_multiple_registers(self, address, values):
            for idx, word in enumerate(values):
                self.regs[address + idx] = int(word) & 0xFFFF
            return True

        def write_single_register(self, address, value):
            self.regs[address] = int(value) & 0xFFFF
            return True

        def read_holding_registers(self, address, count=1):
            return [self.regs.get(address + i, 0) for i in range(count)]

        def disconnect(self):
            self.connected = False

    mech_client = _FakeRWClient()
    env_client = _FakeRWClient()
    mgr.clients['mech1'] = mech_client
    mgr.clients['env1'] = env_client

    # E-STOP rejects mechanical adjustment
    mgr._estop_active = True
    res_mech = mgr.adjust_device('mech1', 'speed', 100)
    assert res_mech['success'] is False and '紧急停机' in res_mech['message']

    # E-STOP still allows non-mechanical adjustment; test coil and float64
    res_coil = mgr.adjust_device('env1', 'run_coil', 'on')
    assert res_coil['success'] is True and env_client.coils[1] is True

    res_bad_coil = mgr.adjust_device('env1', 'run_coil', float('nan'))
    assert res_bad_coil['success'] is False

    res_f64 = mgr.adjust_device('env1', 'target_f64', 123.456)
    assert res_f64['success'] is True
    expected = encode_float64_registers(123.456, 'ABCD')
    assert [env_client.regs[20 + i] for i in range(4)] == expected

    # Failure counter cleanup & reconnect loop lifecycle
    mgr._estop_active = False
    mgr._record_connection_failure('env1')
    assert 'env1' in mgr._connection_failures
    mgr.disconnect_device('env1')
    assert 'env1' not in mgr._connection_failures
    mgr.start_reconnect_loop(interval=1)
    mgr.stop_reconnect_loop()
    assert mgr._reconnect_thread is None


def test_simulated_device_manager_custom_estop_and_remove_cleanup(tmp_path):
    cfg = tmp_path / 'sim_devices.yaml'
    cfg.write_text(
        yaml.safe_dump({
            'devices': [
                {
                    'id': 'sim_mech',
                    'name': 'SimPump',
                    'protocol': 'modbus_tcp',
                    'device_category': 'mechanical',
                    'registers': [{'name': 'speed', 'address': 0, 'data_type': 'uint16'}],
                }
            ]
        }, allow_unicode=True),
        encoding='utf-8',
    )
    mgr = SimulatedDeviceManager(config_path=str(cfg))
    assert 'custom' in mgr.SUPPORTED_PROTOCOLS

    # Custom HTTP & Plugin adapter creation
    assert mgr.add_device({
        'id': 'cust_http',
        'name': 'CustomHTTP',
        'protocol': 'custom',
        'transport': 'http',
        'endpoint': 'http://127.0.0.1:9000/points',
    }) is True
    assert mgr.add_device({
        'id': 'cust_plug',
        'name': 'CustomPlugin',
        'protocol': 'custom',
        'transport': 'plugin',
        'plugin': 'example_plugin',
    }) is True

    # E-STOP rejects adjust_device on mechanical simulated device
    prev_estop = simulated_client_mod._ESTOP_ACTIVE
    try:
        simulated_client_mod.set_estop_state(True)
        res = mgr.adjust_device('sim_mech', 'speed', 100)
        assert res['success'] is False and '紧急停机' in res['message']
    finally:
        simulated_client_mod.set_estop_state(prev_estop)

    # remove_device clears stopped latch
    simulated_client_mod.set_device_stopped('sim_mech', True)
    assert simulated_client_mod.is_device_stopped('sim_mech') is True
    assert mgr.remove_device('sim_mech') is True
    assert simulated_client_mod.is_device_stopped('sim_mech') is False
