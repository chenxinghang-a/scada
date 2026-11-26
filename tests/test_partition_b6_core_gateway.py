"""Partition B6 tests: core utilities, path env overrides, MQTT broker, and protocol gateways."""

from __future__ import annotations

import logging
import sqlite3
import struct
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from flask import Flask, jsonify

import paths
from core import backup_drill, rate_limiter, request_dedup, structured_logging
from core.connection_pool import ConnectionPool, PooledConnection
from core.request_queue import RequestQueue
from core.scheduled_tasks import TaskManager
from core.write_safety import WriteSafetyValidator
from gateway.base_gateway import BaseGateway
from gateway.dnp3_gateway import DNP3Client, DNP3Gateway, DNP3ObjectGroup
from gateway.iec104_gateway import (
    IEC104ASDU,
    IEC104Client,
    IEC104Gateway,
    TypeID,
)
from gateway.s7_gateway import S7Gateway
from 模拟服务端.mqtt_broker import MqttSimBroker, _ClientSession


# ---------------------------------------------------------------------------
# core: rate_limiter, write_safety, request_dedup, request_queue, logging
# ---------------------------------------------------------------------------

def test_rate_limiter_exempts_estop_endpoints():
    prev_limiter = rate_limiter._limiter
    try:
        with rate_limiter._limiter_lock:
            rate_limiter._limiter = None
        app = Flask(__name__)

        @app.route('/api/control/write', methods=['POST'])
        def write_ep():
            return jsonify({'ok': True}), 200

        @app.route('/api/control/estop', methods=['POST'])
        def trigger_estop():
            return jsonify({'ok': True}), 200

        limiter = rate_limiter.create_limiter(app)
        app.view_functions['write_ep'] = limiter.limit('1 per minute')(app.view_functions['write_ep'])
        app.view_functions['trigger_estop'] = limiter.limit('1 per minute')(app.view_functions['trigger_estop'])

        client = app.test_client()
        assert client.post('/api/control/write').status_code == 200
        assert client.post('/api/control/write').status_code == 429

        # E-STOP remains exempted even after rapid repeated hits
        for _ in range(5):
            assert client.post('/api/control/estop').status_code == 200
    finally:
        with rate_limiter._limiter_lock:
            rate_limiter._limiter = prev_limiter


def test_write_safety_rejects_bool_and_handles_numeric_string():
    v = WriteSafetyValidator({
        'id': 'd1',
        'name': 'd1',
        'registers': [
            {'address': 1, 'name': 'temperature', 'unit': '°C', 'data_type': 'float32', 'access': 'rw'},
        ],
    })
    ok_bool, _ = v.validate_write(1, True)
    assert ok_bool is False

    ok_str, reason_str = v.validate_write(1, '50.5')
    assert ok_str is True, reason_str

    ok_oob_str, _ = v.validate_write(1, '9999')
    assert ok_oob_str is False


def test_request_dedup_uses_token_cookie():
    app = Flask(__name__)
    with app.test_request_context('/api/control/write', method='POST', headers={'Cookie': 'token=tokA'}):
        fp_a = request_dedup._make_fingerprint()
    with app.test_request_context('/api/control/write', method='POST', headers={'Cookie': 'token=tokB'}):
        fp_b = request_dedup._make_fingerprint()
    assert fp_a != fp_b


def test_request_queue_get_status_returns_copy():
    q = RequestQueue('b6_test', max_workers=1, max_queue_size=4)
    tid = q.submit(lambda: {'v': 1})
    snap = q.get_status(tid)
    assert snap is not None
    snap['status'] = 'tampered'
    assert q.get_status(tid)['status'] != 'tampered'


def test_structured_logging_installs_bridge_when_file_disabled():
    root = logging.getLogger()
    orig_handlers = list(root.handlers)
    try:
        root.handlers = [h for h in root.handlers if not isinstance(h, structured_logging.InterceptHandler)]
        lg = structured_logging.setup_logging(enable_console=False, enable_file=False)
        assert lg is not None
        assert any(isinstance(h, structured_logging.InterceptHandler) for h in root.handlers)
    finally:
        root.handlers = orig_handlers


# ---------------------------------------------------------------------------
# core: connection_pool, scheduled_tasks, env paths & backup_drill
# ---------------------------------------------------------------------------

def test_connection_pool_fast_shutdown_and_identity_eviction():
    created = []

    def _factory(key):
        c = MagicMock()
        created.append(c)
        return c

    pool = ConnectionPool(
        _factory,
        max_size=4,
        max_idle_time=0.05,
        health_check=lambda c: c is not created[0],
        name='b6_pool',
        auto_scale=False,
    )
    try:
        first = pool.acquire('k1')
        assert first is not None
        pool.release('k1', healthy=True)

        # Simulate key replacement during cleanup health_check window
        old_conn = pool._pool['k1']
        new_client = MagicMock()
        new_conn = PooledConnection(new_client)
        pool._health_check = lambda client: (
            pool._pool.__setitem__('k1', new_conn) or False
        )
        pool._cleanup_once()
        # Newer PooledConnection under 'k1' must NOT be destroyed by stale eviction of old_conn
        assert pool._pool['k1'] is new_conn
    finally:
        t0 = time.monotonic()
        pool.shutdown()
        assert (time.monotonic() - t0) < 2.0


def test_scheduled_tasks_run_now_preserves_pending_status():
    mgr = TaskManager()
    entered = threading.Event()
    release = threading.Event()

    def _job():
        entered.set()
        release.wait(2.0)

    mgr.add('pending_job', _job, interval=60.0)
    assert mgr.run_now('pending_job') is True
    assert entered.wait(2.0)
    snap = mgr.get_status('pending_job')
    assert snap['executing'] is True
    assert snap['status'] == 'pending'
    release.set()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and mgr.get_status('pending_job')['executing']:
        time.sleep(0.02)
    assert mgr.get_status('pending_job')['run_count'] == 1
    assert mgr.get_status('pending_job')['status'] == 'pending'


def test_paths_env_overrides_and_backup_drill_cwd(tmp_path, monkeypatch):
    cfg_dir = tmp_path / 'iso_cfg'
    data_dir = tmp_path / 'iso_data'
    backups_dir = data_dir / 'backups'
    log_dir = tmp_path / 'iso_logs'
    elsewhere = tmp_path / 'other_cwd'
    cfg_dir.mkdir()
    backups_dir.mkdir(parents=True)
    log_dir.mkdir()
    elsewhere.mkdir()

    monkeypatch.setenv('SCADA_CONFIG_DIR', str(cfg_dir))
    monkeypatch.setenv('SCADA_DATA_DIR', str(data_dir))
    monkeypatch.setenv('SCADA_LOG_DIR', str(log_dir))
    monkeypatch.chdir(elsewhere)

    assert paths.resolve('配置/system.yaml') == cfg_dir / 'system.yaml'
    assert paths.resolve('data/backups') == backups_dir
    assert paths.resolve('logs/app.log') == log_dir / 'app.log'
    assert paths.get_config_path('alarms.yaml') == str(cfg_dir / 'alarms.yaml')
    assert paths.get_db_path('simulated') == str(data_dir / 'scada_simulated.db')

    # Create a valid backup file in SCADA_DATA_DIR/backups and verify run_drill works from elsewhere CWD
    db_file = backups_dir / 'scada_backup_test.db'
    conn = sqlite3.connect(str(db_file))
    try:
        for tbl in backup_drill.REQUIRED_TABLES:
            conn.execute(f'CREATE TABLE {tbl} (id INTEGER PRIMARY KEY)')
        conn.commit()
    finally:
        conn.close()

    res = backup_drill.run_drill()
    assert res['ok'] is True, res['problems']
    assert str(db_file) == res['backup']


# ---------------------------------------------------------------------------
# 模拟服务端/mqtt_broker: _sub_lock protects subscriptions
# ---------------------------------------------------------------------------

def test_mqtt_broker_subscription_lock_concurrency():
    sess = _ClientSession(MagicMock(), ('127.0.0.1', 12345))
    errors = []

    def _mutator():
        try:
            for i in range(100):
                with sess._sub_lock:
                    sess.subscriptions[f'factory/t/{i % 10}'] = 1
                    sess.subscriptions.pop(f'factory/t/{(i + 5) % 10}', None)
        except Exception as exc:
            errors.append(exc)

    def _reader():
        try:
            for i in range(100):
                MqttSimBroker._match_qos(sess, f'factory/t/{i % 10}')
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_mutator), threading.Thread(target=_reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


# ---------------------------------------------------------------------------
# gateway: BaseGateway, DNP3, IEC104, S7
# ---------------------------------------------------------------------------

def test_base_gateway_poll_loop_triggers_reconnect_with_backoff():
    class _ReconnGW(BaseGateway):
        def __init__(self):
            self.reconn_calls = 0
            self.read_ok = False
            super().__init__({
                'gateway_id': 'gw_reconn',
                'devices': [{'device_id': 'd1'}],
                'poll_interval': 0.01,
                'reconnect_interval': 5.0,
                'max_reconnect_attempts': 2,
            })

        def connect(self) -> bool:
            return True

        def disconnect(self):
            pass

        def read_device_data(self, device_id: str):
            return {'temp': 22.0} if self.read_ok else None

        def convert_to_telemetry(self, device_id: str, raw_data: dict[str, float]):
            raise NotImplementedError

        def reconnect_device(self, device_id: str) -> bool:
            self.reconn_calls += 1
            return False

    gw = _ReconnGW()
    gw.publish_status = MagicMock(return_value=True)
    # First failure triggers immediate reconnect_device, second inside 5s interval backs off
    gw._maybe_reconnect_device('d1')
    gw._maybe_reconnect_device('d1')
    assert gw.reconn_calls == 1

    # After interval elapses, 2nd attempt runs; 3rd is capped by max_reconnect_attempts=2
    gw.reconnect_interval = 0.0
    gw._maybe_reconnect_device('d1')
    gw._maybe_reconnect_device('d1')
    assert gw.reconn_calls == 2


def test_dnp3_control_none_status_and_non_finite_read():
    client = DNP3Client('127.0.0.1')
    client.connected = True
    fake_sock = MagicMock()
    client.socket = fake_sock
    client._operate_event.set()
    # `_operate_status` stayed None -> must reject rather than treating None as success
    assert client.send_control(0, 1) is False

    # read_data returns defensive copies
    client._last_data[(DNP3ObjectGroup.AI, 0)] = {'value': 12.0, 'quality': 0x01, 'group': DNP3ObjectGroup.AI, 'index': 0, 'time': time.time()}
    snap = client.read_data()
    snap[(DNP3ObjectGroup.AI, 0)]['value'] = 999.0
    assert client.read_data()[(DNP3ObjectGroup.AI, 0)]['value'] == 12.0

    # Non-finite point rejected in read_device_data
    gw = DNP3Gateway({
        'gateway_id': 'dnp3_b6',
        'devices': [{'device_id': 'rtu1', 'registers': [{'name': 'v', 'index': 0, 'group': 'ai'}]}],
    })
    fake_client = MagicMock()
    fake_client.connected = True
    fake_client.data_max_age = 15.0
    fake_client.read_data.return_value = {
        (DNP3ObjectGroup.AI, 0): {'value': float('nan'), 'quality': 0x01, 'group': DNP3ObjectGroup.AI, 'index': 0, 'time': time.time()},
    }
    gw._clients['rtu1'] = fake_client
    assert gw.read_device_data('rtu1') is None
    assert gw.stats['read_failures'] >= 1


def test_iec104_command_bounds_quality_and_staleness():
    assert IEC104ASDU.build_command(TypeID.C_SE_NC_1, 100, float('nan')) == b''
    assert IEC104ASDU.build_command(TypeID.C_SE_NC_1, 100, float('inf')) == b''
    assert IEC104ASDU.build_command(TypeID.C_SE_NC_1, 100, 1e40) == b''
    assert IEC104ASDU.build_command(TypeID.C_SC_NA_1, 100, float('nan')) == b''
    assert IEC104ASDU.build_command(TypeID.C_SE_NC_1, 100, 12.5) != b''

    gw = IEC104Gateway({
        'gateway_id': 'iec_b6',
        'devices': [{
            'device_id': 'rtu1',
            'registers': [
                {'name': 'v_good', 'ioa': 1},
                {'name': 'v_bad_q', 'ioa': 2},
            ],
        }],
    })
    now = time.time()
    c = IEC104Client('127.0.0.1')
    c.connected = True
    c.data_max_age = 10.0
    c._last_data = {
        1: {'value': 220.0, 'quality': 0x00, 'type_id': TypeID.M_ME_NC_1, 'time': now},
        2: {'value': 999.0, 'quality': 0x80, 'type_id': TypeID.M_ME_NC_1, 'time': now},
    }
    gw._clients['rtu1'] = c
    out = gw.read_device_data('rtu1')
    assert out == {'v_good': 220.0}
    assert gw.stats['read_failures'] == 1

    # All points stale -> returns None
    c._last_data[1]['time'] = now - 60.0
    c._last_data[2]['time'] = now - 60.0
    assert gw.read_device_data('rtu1') is None


def test_s7_gateway_write_guards_verify_and_destroy():
    gw = S7Gateway.__new__(S7Gateway)
    gw.logger = logging.getLogger('test_s7_b6')
    gw._write_locks = {}
    gw.devices_config = [{'device_id': 'plc1', 'host': '127.0.0.1'}]
    gw.connected_devices = {'plc1': True}

    old_client = MagicMock()
    gw._clients = {'plc1': old_client}
    gw._create_client = lambda cfg: None
    assert gw.reconnect_device('plc1') is False
    old_client.disconnect.assert_called_once()
    old_client.destroy.assert_called_once()

    buf = bytearray(b'\x00')
    client = MagicMock()
    client.db_read.side_effect = lambda db, off, sz: bytes(buf[:sz])
    client.db_write.side_effect = lambda db, off, data: buf.__setitem__(slice(0, len(data)), data)
    gw._clients['plc1'] = client

    assert gw.write_register('plc1', 1, 0, 1, 'bool', bit_offset=8) is False
    assert gw.write_register('plc1', 1, 0, 1, 'bool', bit_offset=-1) is False
    assert gw.write_register('plc1', 1, 0, float('nan'), 'real') is False
    assert gw.write_register('plc1', 1, 0, True, 'real') is False
    assert gw.write_register('plc1', 1, 0, 1, 'bool', bit_offset=2, verify=True) is True
    assert buf[0] == 0b0000_0100
