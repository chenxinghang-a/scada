# -*- coding: utf-8 -*-
"""Regression tests for Partition B2a: collector, base/adapters, and simulators."""

from __future__ import annotations

import math
import queue as _queue
import threading
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from 智能层.edge_decision import EdgeDecisionEngine
from 智能层.energy_manager import EnergyManager
from 智能层.oee_calculator import OEECalculator
from 采集层 import custom_adapter, plugin_adapter as pa
from 采集层.adapter_spec import AdapterDataError, PointSpec, convert_value
from 采集层.base_client import (
    ByteOrder,
    ByteOrderCapableDecoder,
    encode_float32_registers,
    encode_float64_registers,
    encode_int32_registers,
)
from 采集层.data_collector import DataCollector, DataQualityAssessor, DiskBackedQueue
from 采集层.device_behavior_simulator import (
    DeviceBehaviorSimulator,
    DeviceState,
    FaultType,
)
from 采集层.enhanced_simulated_client import EnhancedSimulatedModbusClient
from 采集层.recipe_simulator import PhaseConfig, Recipe, RecipeSimulator
from 采集层.rest_client import RESTDeviceClient
from 采集层.simulated_client import SimulatedModbusClient, set_estop_state


# ---------------------------------------------------------------------------
# 1. base_client & adapter_spec
# ---------------------------------------------------------------------------

class _StandaloneDecoder(ByteOrderCapableDecoder):
    def __init__(self, bo: str):
        self.byte_order = ByteOrder(bo)


def test_base_decoder_respects_byte_order_attr_and_guards_short_inputs():
    dec = _StandaloneDecoder('CDAB')
    r_i = encode_int32_registers(0x12345678, 'CDAB', signed=False)
    r_f = encode_float32_registers(-42.5, 'CDAB')
    r_d = encode_float64_registers(3.1415926535, 'CDAB')

    assert dec.decode_uint32(r_i) == 0x12345678
    assert dec.decode_int32(r_i) == 0x12345678
    assert dec.decode_float32(r_f) == pytest.approx(-42.5, rel=1e-6)
    assert dec.decode_float64(r_d) == pytest.approx(3.1415926535, rel=1e-12)

    with pytest.raises(ValueError):
        dec.decode_uint32([0x1234])
    with pytest.raises(ValueError):
        dec.decode_int32([0x1234])


@pytest.mark.parametrize('bad_val', [True, False, float('nan'), float('inf'), float('-inf')])
def test_encoders_reject_bool_and_non_finite_values(bad_val):
    with pytest.raises(ValueError):
        encode_float32_registers(bad_val, 'ABCD')
    with pytest.raises(ValueError):
        encode_float64_registers(bad_val, 'ABCD')
    with pytest.raises(ValueError):
        encode_int32_registers(bad_val, 'ABCD')


def test_encoders_reject_invalid_byte_order():
    with pytest.raises(ValueError):
        encode_float32_registers(1.0, 'XYZW')
    with pytest.raises(ValueError):
        encode_int32_registers(1, 'XYZW')


def test_adapter_spec_bool_numeric_and_overflow_guard():
    p_bool = PointSpec(name='run', path='run', type='bool')
    assert convert_value(1, p_bool) == 1.0
    assert convert_value(0.0, p_bool) == 0.0
    with pytest.raises(AdapterDataError):
        convert_value(float('nan'), p_bool)

    p_scale = PointSpec(name='v', path='v', type='float', scale=1e308)
    with pytest.raises(AdapterDataError, match='溢出'):
        convert_value(1e10, p_scale)


# ---------------------------------------------------------------------------
# 2. custom_adapter, plugin_adapter & rest_client
# ---------------------------------------------------------------------------

def _make_custom_cfg(**overrides):
    cfg = {
        'id': 'c1',
        'protocol': 'custom',
        'adapter': {
            'transport': 'http',
            'base_url': 'https://mes.local/api',
            'request': {'method': 'GET', 'path': '/points'},
            'response': {},
            'points': [{'name': 'temp', 'path': 'temp', 'type': 'float'}],
            'write': {'method': 'POST', 'path': '/write/{name}', 'json': {'value': '{value}'}},
        },
    }
    cfg['adapter'].update(overrides)
    return cfg


def test_custom_adapter_write_204_and_failure_payload(monkeypatch):
    class _Resp:
        def __init__(self, status=200, payload=None, content=b''):
            self.status_code = status
            self._payload = payload
            self.content = content
            self.text = ''
            self.headers = {'Content-Type': 'application/json'}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f'HTTP {self.status_code}')

        def json(self):
            if self._payload is None:
                raise ValueError('no json')
            return self._payload

    responses = [
        _Resp(204, None, b''),
        _Resp(200, {'success': False}, b'{"success": false}'),
    ]
    calls = []

    class _Sess:
        def __init__(self):
            self.headers = {}
            self.auth = None

        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            if method == 'GET':
                return _Resp(200, {'temp': 25.0}, b'{"temp": 25.0}')
            return responses.pop(0)

        def close(self):
            pass

    monkeypatch.setattr(custom_adapter.requests, 'Session', _Sess)
    cfg = _make_custom_cfg()
    cfg['poll_interval'] = 60.0
    client = custom_adapter.CustomAdapterClient(cfg)
    q_events = []
    client.add_data_callback(lambda dev, n, v, u, quality=None: q_events.append((dev, n, v, quality)))
    try:
        assert client.connect() is True
        # 204 write succeeds without JSON error and uses allow_redirects=False
        assert client.write_point('temp', 30.0) is True
        assert calls[-1][2].get('allow_redirects') is False
        # business failure payload rejected cleanly
        assert client.write_point('temp', 31.0) is False
        assert '失败' in (client.stats['last_error'] or '')
        client._notify({'temp': {'value': 25.0, 'unit': 'C', 'quality': 192}})
        assert q_events and q_events[0][3] == 192
    finally:
        client.disconnect()


def test_rest_client_write_204_and_json_failure():
    client = RESTDeviceClient({'id': 'r1', 'base_url': 'http://127.0.0.1:9999'})
    client.connected = True
    sess = MagicMock()
    client._session = sess

    resp_204 = MagicMock()
    resp_204.status_code = 204
    resp_204.raise_for_status = MagicMock()
    sess.request.return_value = resp_204
    assert client.write_endpoint({'path': '/sp'}, 42.5) is True

    resp_fail = MagicMock()
    resp_fail.status_code = 200
    resp_fail.content = b'{"ok": false}'
    resp_fail.text = '{"ok": false}'
    resp_fail.json.return_value = {'ok': False}
    resp_fail.raise_for_status = MagicMock()
    sess.request.return_value = resp_fail
    assert client.write_endpoint({'path': '/sp'}, 42.5) is False

    # Non-finite write refused without network call
    sess.request.reset_mock()
    assert client.write_endpoint({'path': '/sp'}, float('nan')) is False
    sess.request.assert_not_called()


def test_plugin_adapter_notify_passes_optional_quality(tmp_path, monkeypatch):
    src = (
        'class Adapter:\n'
        '    def __init__(self, s): pass\n'
        '    def read(self): return {"pressure": 5.5}\n'
    )
    (tmp_path / 'qplug.py').write_text(src, encoding='utf-8')
    monkeypatch.setenv(pa.PLUGIN_DIR_ENV, str(tmp_path))
    pa.clear_plugin_cache()
    client = pa.PluginAdapterClient({
        'id': 'p1', 'protocol': 'custom', 'poll_interval': 60.0,
        'adapter': {'transport': 'plugin', 'plugin': 'qplug'},
    })
    events = []
    client.add_data_callback(lambda dev, name, val, unit, quality=None: events.append(quality))
    try:
        assert client.connect() is True
        events.clear()
        client._notify({'pressure': {'value': 5.5, 'unit': '', 'quality': 64}})
        assert events[-1] == 64
    finally:
        client.disconnect()
        pa.clear_plugin_cache()


# ---------------------------------------------------------------------------
# 3. DataCollector ingestion, chunking, DB partial write, and routing
# ---------------------------------------------------------------------------

def test_disk_backed_queue_skips_non_dict_json_lines(tmp_path):
    dbq = DiskBackedQueue(maxsize=10, persist_dir=str(tmp_path))
    p = tmp_path / 'pending_data.jsonl'
    p.write_text('123\n["list"]\n"str"\n{"device_id":"d1","register_name":"t","value":12.5}\n', encoding='utf-8')
    dbq2 = DiskBackedQueue(maxsize=10, persist_dir=str(tmp_path))
    assert dbq2.qsize() == 1
    assert dbq2.get_nowait()['value'] == 12.5


def test_collect_fc_group_chunked_no_duplicate_on_overlap():
    dm = MagicMock()
    dm.get_all_devices.return_value = {}
    collector = DataCollector(dm, MagicMock())
    client = MagicMock()
    client.last_read_source = 'fresh'
    client.decode_float32.return_value = 77.5
    client.decode_uint16.side_effect = lambda r: r

    # 130 registers: chunk 1 [0..124] (ends at 125), chunk 2 overlaps around 124
    # float32 at addr 124 spans 124..125 -> chunk 1 has len 125 so offset 124 only has 1 word!
    # Chunk 1 must NOT treat 124 as short error; Chunk 2 contains [124, 125] and emits it once!
    regs = [
        {'name': 'r0', 'address': 0, 'data_type': 'uint16'},
        {'name': 'f_boundary', 'address': 124, 'data_type': 'float32'},
        {'name': 'r_end', 'address': 130, 'data_type': 'uint16'},
    ]
    client.read_holding_registers.side_effect = [
        [10] + [0] * 124,   # chunk 1: addr 0..124
        [0x429B, 0x0000] + [0] * 5 + [99],  # chunk 2 starting 124 -> full float32 + r_end
    ]
    enqueued = []
    ok, fresh = collector._collect_function_code_group(
        client, 'd1', regs, 3, None, datetime.now(),
        enqueued.append, lambda *a: None,
    )
    assert ok is True and fresh is True
    names = [item['register_name'] for item in enqueued]
    assert names.count('r0') == 1
    assert names.count('f_boundary') == 1
    assert names.count('r_end') == 1
    assert collector.stats.get('dropped_register_short', 0) == 0


def test_read_register_fc_and_none_decode_safety():
    dm = MagicMock()
    dm.get_all_devices.return_value = {}
    collector = DataCollector(dm, MagicMock())
    client = MagicMock(spec=['read_coils', 'read_discrete_inputs', 'read_input_registers',
                            'read_holding_registers', 'decode_float32'])
    client.read_coils.return_value = [True]
    client.read_discrete_inputs.return_value = [False]
    client.read_input_registers.return_value = [0x7FC0, 0x0000]
    client.decode_float32.return_value = None  # NaN filtered to None

    assert collector._read_register(client, {'name': 'c0', 'address': 0, 'function_code': 1}) == 1.0
    assert collector._read_register(client, {'name': 'di0', 'address': 0, 'function_code': 'di'}) == 0.0
    assert collector._read_register(client, {'name': 'ir0', 'address': 0, 'data_type': 'float32', 'function_code': 4}) is None


def test_dispatch_intelligence_and_oee_routing():
    dm = MagicMock()
    dm.get_all_devices.return_value = {}
    em = EnergyManager(database=None)
    oee = OEECalculator(database=None)
    ed = EdgeDecisionEngine(database=None)
    collector = DataCollector(dm, MagicMock(), energy_manager=em, oee_calculator=oee, edge_decision=ed)

    now = datetime.now()
    # power_factor must NOT feed power integration
    collector._dispatch_intelligence({'device_id': 'd1', 'register_name': 'power_factor', 'value': 0.95, 'timestamp': now})
    assert 'd1' not in em.realtime_power

    # water_flow routes to water consumption, gas_flow routes to gas consumption
    collector._dispatch_intelligence({'device_id': 'd1', 'register_name': 'water_flow', 'value': 12.0, 'timestamp': now})
    collector._dispatch_intelligence({'device_id': 'd1', 'register_name': 'gas_flow', 'value': 30.0, 'timestamp': now})
    assert em.realtime_power.get('d1', {}).get('power_kw', 0) == 0

    # edge_decision receives quality & monotonic_ts
    collector._dispatch_intelligence({
        'device_id': 'd1', 'register_name': 'temp', 'value': 44.0,
        'timestamp': now, 'quality': DataQualityAssessor.BAD, 'monotonic_ts': 10.5,
    })
    assert ed._data_snapshot['d1:temp'] == 44.0
    assert ed._data_quality['d1:temp'] == (DataQualityAssessor.BAD, 10.5)

    # OEE reject_count updates reject_count, not total_count
    collector._dispatch_oee('d1', 'production_count', 100)
    collector._dispatch_oee('d1', 'good_count', 95)
    collector._dispatch_oee('d1', 'reject_count', 5)
    assert oee.shift_data['d1']['reject_count'] == 5
    assert oee.shift_data['d1']['total_count'] == 100


def test_sync_device_tasks_restarts_changed_config():
    dm = MagicMock()
    dev_cfg_v1 = {'id': 'd1', 'enabled': True, 'protocol': 'modbus_tcp', 'collection_interval': 5, 'registers': []}
    dm.get_all_devices.return_value = {'d1': dict(dev_cfg_v1)}
    collector = DataCollector(dm, MagicMock())
    collector.running = True
    try:
        assert collector.start_device_task('d1', dev_cfg_v1) is True
        dev_cfg_v2 = {'id': 'd1', 'enabled': True, 'protocol': 'modbus_tcp', 'collection_interval': 2, 'registers': [{'name': 't', 'address': 0}]}
        dm.get_all_devices.return_value = {'d1': dev_cfg_v2}
        res = collector.sync_device_tasks()
        assert res['restarted'] == ['d1']
        assert res['unchanged'] == []
    finally:
        collector.running = False
        collector.remove_device_task('d1')


def test_partial_db_write_keeps_disk_persistence_and_full_intel_queue_runs_interlocks(tmp_path):
    dm = MagicMock()
    dm.get_all_devices.return_value = {}
    dm.get_device_status.return_value = {'connected': True, 'stopped': False, 'error': False}
    db = MagicMock()
    # Batch has 2 records, only 1 written -> partial failure!
    db.insert_data_batch.return_value = 1
    dc = MagicMock()
    am = MagicMock()
    collector = DataCollector(dm, db, alarm_manager=am, device_control=dc)
    collector.data_queue = DiskBackedQueue(maxsize=100, persist_dir=str(tmp_path))
    clear_spy = MagicMock()
    collector.data_queue.clear_persistence = clear_spy
    collector.running = True

    import 采集层.data_collector as dc_mod
    orig_queue = dc_mod.Queue
    small_intel_q = _queue.Queue(maxsize=1)
    small_intel_q.put({'device_id': 'pre', 'register_name': 'x', 'value': 1, 'timestamp': datetime.now()})
    dc_mod.Queue = lambda maxsize=10000: small_intel_q
    try:
        t = threading.Thread(target=collector._process_data, daemon=True)
        t.start()
        collector.data_queue.put({'device_id': 'd1', 'register_name': 'temp', 'value': 88.0, 'timestamp': datetime.now()})
        collector.data_queue.put({'device_id': 'd1', 'register_name': 'press', 'value': 4.5, 'timestamp': datetime.now()})
        import time as _time
        deadline = _time.monotonic() + 3.0
        while _time.monotonic() < deadline and dc.check_interlocks.call_count < 2:
            _time.sleep(0.05)
        collector.running = False
        t.join(timeout=3.0)
    finally:
        dc_mod.Queue = orig_queue

    # Partial write must NOT clear persistence
    clear_spy.assert_not_called()
    # Full intel_queue must still evaluate safety interlocks synchronously
    assert dc.check_interlocks.call_count >= 2


# ---------------------------------------------------------------------------
# 4. Simulators
# ---------------------------------------------------------------------------

def test_behavior_simulator_clears_faults_after_maintenance_and_guards_writes():
    sim = DeviceBehaviorSimulator('d_sim', {'id': 'd_sim', 'name': 'Pump', 'registers': [{'address': 0, 'name': 'temp', 'scale': 0.1}]})
    now = [1000.0]
    sim._clock = lambda: now[0]
    sim.inject_fault(FaultType.MOTOR_WEAR, 0.9)
    sim.force_state(DeviceState.MAINTENANCE)
    now[0] = 1700.0  # > 600s maintenance duration
    sim._update_state_machine(1.0)
    assert sim.state == DeviceState.IDLE
    assert sim.active_fault == FaultType.NONE
    assert sim.active_faults == {}
    assert sim.fault_severity == 0

    sim.handle_write_register(0, float('nan'))
    sim.handle_write_register(0, True)
    assert sim.get_written_register_value(0) is None
    assert sim.update(float('nan')) == {}


def test_recipe_simulator_callback_outside_lock_and_finite_guards():
    seen_status = []
    recipe = Recipe(name='r1', version='1.0', phases=[PhaseConfig(name='p1', duration=1.0, setpoints={'temp': 80.0, 'bad': float('nan')})])
    sim = RecipeSimulator(recipe)
    sim._completion_callback = lambda c: seen_status.append(sim.get_status())
    sim.start()
    sim.phase_start_time -= 2.0
    out = sim.update(1.0)
    assert len(seen_status) == 1
    assert 'bad' not in sim._interpolate_setpoints(recipe.phases[0], 0.5)
    assert sim.update(float('nan')) == {}


def test_enhanced_simulator_isolates_di_and_preserves_float_writes():
    set_estop_state(False)
    cfg = {
        'id': 'enh1',
        'name': 'Motor1',
        'device_category': 'mechanical',
        'registers': [{'address': 10, 'name': 'temperature_setpoint', 'data_type': 'float32'}],
        'endpoints': [{'name': 'temperature_setpoint', 'path': '/sp', 'method': 'GET'}],
    }
    client = EnhancedSimulatedModbusClient(cfg)
    assert client.connect() is True
    try:
        assert client.write_single_coil(0, True) is True
        assert client.read_coils(0, 1) == [True]
        assert client.read_discrete_inputs(0, 1) == [False]
        client.set_discrete_input(0, True)
        assert client.read_discrete_inputs(0, 1) == [True]

        # E-STOP fail-closed on mechanical coil & DI
        set_estop_state(True)
        assert client.read_coils(0, 1) == [False]
        assert client.read_discrete_inputs(0, 1) == [False]
    finally:
        set_estop_state(False)

    # Reject bool & non-finite register writes on both simulators
    assert client.write_single_register(10, True) is False
    assert client.write_single_register(10, float('nan')) is False
    sim_plain = SimulatedModbusClient({'id': 's1', 'registers': []})
    sim_plain.connect()
    assert sim_plain.write_single_register(1, True) is False
    assert sim_plain.write_single_register(1, float('inf')) is False

    # Preserve fractional setpoint when engineering=True write succeeds via _resolve_endpoint_address
    client.behavior_simulator.handle_write_register(10, 37.5, engineering=True)
    assert client.behavior_simulator.process_model.base_temperature == pytest.approx(37.5)
    assert client.read_holding_registers(10, 1) == [38]
