# -*- coding: utf-8 -*-
"""Partition B3b 回归测试：OEE / Energy / Vibration / Predictive / SPC / TSDBAdapter。"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from 智能层.energy_manager import EnergyManager
from 智能层.oee_calculator import OEECalculator
from 智能层.predictive_maintenance import PredictiveMaintenance
from 智能层.spc_analyzer import SPCAnalyzer
from 智能层.tsdb_adapter import RealtimeDataBridge, TSDBAdapter
from 智能层.vibration_analyzer import VibrationAnalyzer


def test_oee_calculator_guards_bool_nan_reject_and_ntp_clamp():
    oee = OEECalculator(MagicMock())
    oee.start_shift('d1', planned_hours=8)
    oee.record_production('d1', count=True)
    oee.record_production('d1', count=float('nan'))
    oee.record_production('d1', count=-5)
    assert oee.shift_data['d1']['total_count'] == 0

    oee.record_production('d1', count=100, register_name='prod_count')
    oee.record_production('d1', reject_count=5, register_name='reject_count')
    assert oee.shift_data['d1']['total_count'] == 100
    res = oee.calculate_oee('d1')
    assert res['good_count'] == 95
    assert res['defect_count'] == 5
    assert 0.0 <= res['quality'] <= 1.0

    # NTP 时钟回拨不应导致 actual_run < 0
    oee.device_states['d1'] = {'status': 'running', 'since': datetime.now() + timedelta(minutes=10)}
    res2 = oee.calculate_oee('d1')
    assert res2['actual_run_time'] >= 0.0


def test_energy_manager_guards_nonfinite_negative_order_and_channel_key(tmp_path):
    em = EnergyManager(database=MagicMock(), config_path=tmp_path / 'energy.yaml')
    em.feed_power_data('d1', True)
    em.feed_power_data('d1', float('nan'))
    em.feed_power_data('d1', -10.0)
    assert em.get_realtime_power('d1') == {}

    now = datetime.now().replace(microsecond=0)
    em.feed_power_data('d1', 50.0, timestamp=now)
    # 乱序历史帧不得覆盖较新的 realtime_power
    em.feed_power_data('d1', 999.0, timestamp=now - timedelta(minutes=5))
    assert em.get_realtime_power('d1')['power_kw'] == 50.0

    # 多电量寄存器基线相互隔离 + forget_device 清干净
    em.feed_power_data('d1', 0.0, energy_kwh=100.0, timestamp=now, register_name='kwh_main')
    em.feed_power_data('d1', 0.0, energy_kwh=20.0, timestamp=now, register_name='kwh_aux')
    em.feed_power_data('d1', 0.0, energy_kwh=110.0, timestamp=now + timedelta(seconds=10), register_name='kwh_main')
    em.feed_power_data('d1', 0.0, energy_kwh=25.0, timestamp=now + timedelta(seconds=10), register_name='kwh_aux')
    assert em.get_energy_summary('d1')['total_energy_kwh'] == pytest.approx(15.0)
    assert em.forget_device('d1') is True
    assert not any(k.startswith('d1') for k in em._last_energy_reading)


def test_vibration_analyzer_rpm_zero_and_bad_ts():
    va = VibrationAnalyzer(database=MagicMock(), config={'sample_rate': 1000.0})
    va._running = True
    va.feed_data('d1', 'vibration_x', 2.0, timestamp=True)
    for i in range(64):
        va.feed_data('d1', 'vibration_x', 1.0 + 0.5 * math.sin(i * 0.3), timestamp=datetime.now())
    assert va.check_bearing_fault('d1', 0) is None
    assert va.check_bearing_fault('d1', True) is None
    assert va.check_bearing_fault('d1', float('nan')) is None


def test_predictive_maintenance_no_resurrect_and_threshold_validation():
    pm = PredictiveMaintenance(database=MagicMock())
    pm.feed_data('d1', 'temp', True)
    assert 'd1:temp' not in pm.data_windows

    with pytest.raises(ValueError):
        pm.set_threshold('d1:temp', upper=True)
    with pytest.raises(ValueError):
        pm.set_threshold('d1:temp', upper=10.0, lower=20.0)

    for i in range(15):
        pm.feed_data('d1', 'temp', 20.0 + i)
    # 模拟并发：分析中途 forget_device 删掉该设备，写回前应检查 key 仍存在
    pm._analyze_trend = lambda w: pm.forget_device('d1') or {'slope': 0, 'direction': 'stable', 'r_squared': 0}
    pm._run_analysis()
    assert 'd1:temp' not in pm.health_scores


def test_spc_rejects_bool_in_spec_limits():
    spc = SPCAnalyzer(database=MagicMock())
    with pytest.raises(ValueError):
        spc.set_spec_limits('d1:temp', usl=True, lsl=0.0)


def test_tsdb_adapter_and_bridge_guards_power_factor_tz_and_bool(tmp_path):
    td = MagicMock()
    td.write_telemetry_batch.return_value = True
    em = EnergyManager(database=MagicMock(), config_path=tmp_path / 'energy.yaml')
    oee = OEECalculator(MagicMock())
    adapter = TSDBAdapter(tdengine=td, energy_manager=em, oee_calculator=oee)

    # power_factor / reactive_power 不得混入有功功率；Z 时区时间戳与本地 now 相减不报错
    adapter._feed_to_modules('d1', 'power_factor', [{'value': 0.98, 'timestamp': '2026-10-01T10:00:00Z'}])
    adapter._feed_to_modules('d1', 'reactive_power', [{'value': 12.0, 'timestamp': '2026-10-01T10:00:00Z'}])
    assert em.get_realtime_power('d1') == {}

    adapter._feed_to_modules('d1', 'active_power', [
        {'value': True, 'timestamp': '2026-10-01T10:00:00Z'},
        {'value': 45.0, 'timestamp': '2026-10-01T10:00:00Z'},
    ])
    assert em.get_realtime_power('d1')['power_kw'] == 45.0

    bridge = RealtimeDataBridge(td)
    bridge.feed('d1', 'temp', True)
    bridge.feed('d1', 'temp', float('nan'))
    assert bridge.get_stats()['records_received'] == 0
