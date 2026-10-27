# -*- coding: utf-8 -*-
"""一次修六处「假成功 / 失败静默」（round 298）。

本轮是批量清理：用三个只读审计代理分别扫了「智能层 + 分发」「存储层 + 时序」
「API + 推送」，共 24 项候选，挑了 6 项**后果最重且可纯单测验证**的。

| # | 位置 | 缺陷 | 后果 |
|---|---|---|---|
| 1 | `智能层/device_control.py` | 联锁状态被**任意**其它寄存器的采样清成"未触发" | 超温状态下仍可启动设备（`_interlock_states` 是唯一准入依据） |
| 2 | `采集层/data_collector.py` | `*_kwh` 被当成瞬时功率 kW 做积分 | 电费/碳排约 3.5 倍；「总功率 320 kW」与「每小时 1120 kWh」同页矛盾 |
| 3 | `采集层/data_collector.py` | 总产量寄存器凭空推算"98% 合格率" | 真实良率 80% 的产线看板显示 98%，假值进 TDengine + Grafana |
| 4 | `timeseries/tdengine_client.py` | 四个单条写入恒返回 None | 调用方「调用完就计数」→ 写失败时 `results_written` 照涨、`errors=0` |
| 5 | `存储层/database.py` + `core/backup_drill.py` | 行数查不到被填成 0 | 备份完整性对比被整段跳过，空备份 + 库被锁 = 演练报通过 |
| 6 | `展示层/api/api_health.py` | `unhealthy` 仍回 200 | 负载均衡/K8s readiness 永远认为健康，探活对自动化消费方等于不存在 |

判据（同一族）：**失败必须与"正常但为空"可区分；不可用的量不许编一个数。**
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# 1. 联锁状态不许被无关点位清掉
# ---------------------------------------------------------------------------

def _safety():
    from 智能层.device_control import DeviceControlSafety

    return DeviceControlSafety(MagicMock(), config={
        'interlocks': [{
            'id': 'IL_TEMP',
            'name': '超温禁启',
            'enabled': True,
            'condition': {'type': 'threshold', 'device_id': 'D1',
                          'register': 'temperature', 'operator': '>',
                          'value': 95},
            'action': {'type': 'block_start'},
        }]
    })


def test_interlock_survives_unrelated_register():
    """触发之后，**别的寄存器**的采样不许把联锁清掉。"""
    il = _safety()

    il.check_interlocks('D1', 'temperature', 120.0)
    assert il._interlock_states.get('IL_TEMP') is True, '前提不成立：联锁没触发'

    # 现场每 5s 就会来一条别的寄存器的数据
    il.check_interlocks('D1', 'pressure', 0.5)
    il.check_interlocks('D2', 'level', 10.0)

    assert il._interlock_states.get('IL_TEMP') is True, (
        '无关点位的采样把联锁清成了"未触发" —— 超温状态下仍可启动设备')


def test_interlock_clears_on_its_own_register():
    """反向对照：**本点位**恢复正常时，联锁必须解除。"""
    il = _safety()

    il.check_interlocks('D1', 'temperature', 120.0)
    assert il._interlock_states.get('IL_TEMP') is True

    il.check_interlocks('D1', 'temperature', 60.0)
    assert il._interlock_states.get('IL_TEMP') is False, (
        '本点位恢复后联锁没解除 —— 会永久锁死设备')


# ---------------------------------------------------------------------------
# 2. kWh 不许走功率通道
# ---------------------------------------------------------------------------

def _collector(**overrides):
    """只装配 `_dispatch_intelligence` 需要的字段（不起线程）。"""
    from 采集层.data_collector import DataCollector

    dc = DataCollector.__new__(DataCollector)
    dc.stats = {}
    dc._stats_lock = threading.Lock()
    for attr in ('predictive_maintenance', 'edge_decision', 'energy_manager',
                 'spc_analyzer', 'vibration_analyzer', 'oee_calculator',
                 'quality_predictor', 'anomaly_detector', 'device_control'):
        setattr(dc, attr, None)
    dc._intel_enabled = lambda name: True
    for key, value in overrides.items():
        setattr(dc, key, value)
    return dc


def test_kwh_goes_to_energy_path_not_power():
    """`power_kwh` 是**电量**，必须走 `energy_kwh` 差分，不能当功率积分。"""
    energy = MagicMock()
    dc = _collector(energy_manager=energy)

    dc._dispatch_intelligence({
        'device_id': 'd1', 'register_name': 'power_kwh',
        'value': 450.0, 'timestamp': None,
    })

    assert energy.feed_power_data.called
    kwargs = energy.feed_power_data.call_args.kwargs
    assert kwargs.get('energy_kwh') == 450.0, (
        f'kWh 电量被当成瞬时功率积分了 —— 电费/碳排会被放大数倍: {kwargs}')


def test_kw_still_goes_to_power_path():
    """反向对照：`power_kw` 仍然是功率，不许被误判成电量。"""
    energy = MagicMock()
    dc = _collector(energy_manager=energy)

    dc._dispatch_intelligence({
        'device_id': 'd1', 'register_name': 'total_power_kw',
        'value': 320.0, 'timestamp': None,
    })

    assert energy.feed_power_data.called
    args = energy.feed_power_data.call_args
    assert args.args[1] == 320.0, f'功率值丢了: {args}'
    assert 'energy_kwh' not in args.kwargs


# ---------------------------------------------------------------------------
# 3. OEE 不许编造合格数
# ---------------------------------------------------------------------------

def test_total_count_does_not_fabricate_good_count():
    oee = MagicMock()
    dc = _collector(oee_calculator=oee)

    dc._dispatch_oee('d1', 'product_count', 100)

    assert oee.record_production.called
    kwargs = oee.record_production.call_args.kwargs
    assert 'good_count' not in kwargs, (
        f'合格数被凭空推算成 {kwargs.get("good_count")} —— '
        f'真实良率 80% 的产线会显示 98%')


def test_good_count_still_passed_when_present():
    """反向对照：寄存器本身是合格品计数时，必须照常传。"""
    oee = MagicMock()
    dc = _collector(oee_calculator=oee)

    dc._dispatch_oee('d1', 'good_count', 80)

    kwargs = oee.record_production.call_args.kwargs
    assert kwargs.get('good_count') == 80


# ---------------------------------------------------------------------------
# 4. TDengine 单条写入必须返回 bool
# ---------------------------------------------------------------------------

def _tdengine(ok: bool):
    from timeseries.tdengine_client import TDengineClient

    client = TDengineClient.__new__(TDengineClient)
    client.stats = {'writes': 0, 'errors': 0}
    client.logger = MagicMock()
    client._execute_sql = lambda sql: object() if ok else None
    client._ensure_table = lambda *a, **kw: None
    client._sanitize_identifier = lambda name: name
    return client


@pytest.mark.parametrize('method,record_attr', [
    ('write_alarm', 'alarm'),
    ('write_oee', 'oee'),
    ('write_energy', 'energy'),
    ('write_predictive', 'predictive'),
])
def test_single_write_returns_bool(method, record_attr):
    """写失败必须能被调用方判定（原来恒返回 None）。"""
    client = _tdengine(ok=False)
    record = MagicMock()
    record.device_id = 'd1'
    record.to_sql_values.return_value = '(1)'
    if record_attr == 'alarm':
        record.alarm_id = 'a1'

    got = getattr(client, method)(record)
    assert got is False, f'{method} 失败时返回 {got!r}，调用方无法判定'

    ok_client = _tdengine(ok=True)
    assert getattr(ok_client, method)(record) is True


def test_tsdb_adapter_counts_only_successful_writes():
    """桥接器只在**真的写进去**时才 `results_written += 1`。"""
    from 智能层.tsdb_adapter import TSDBAdapter

    bridge = TSDBAdapter.__new__(TSDBAdapter)
    bridge.stats = {'results_written': 0, 'errors': 0}
    bridge.logger = MagicMock()
    bridge.oee_calculator = MagicMock()
    bridge.oee_calculator.get_all_oee.return_value = {
        'd1': {'availability': 1, 'performance': 1, 'quality': 1, 'oee': 1,
               'total_count': 10, 'good_count': 10, 'actual_run_time': 1,
               'downtime': 0},
    }
    bridge.tdengine = MagicMock()
    bridge.tdengine.write_oee.return_value = False

    bridge._write_oee_results()

    assert bridge.stats['results_written'] == 0, '写失败了却还在计数'
    assert bridge.stats['errors'] == 1


# ---------------------------------------------------------------------------
# 5. 行数查不到 ≠ 0 行
# ---------------------------------------------------------------------------

def test_table_size_failure_is_not_zero(tmp_path):
    from 存储层.database import Database

    db = Database(str(tmp_path / 't.db'))
    try:
        db._TABLE_SIZE_QUERIES = {'no_such': 'SELECT * FROM no_such_table'}
        sizes = db.get_table_sizes()

        assert 'no_such' not in sizes, (
            f'查询失败被填成了 0 行 —— 下游会把"查不到"当成"空表"跳过校验: {sizes}')
    finally:
        db.close()


def test_backup_drill_reports_uncheckable_table():
    """生产库行数查不到时，演练必须报问题，不能跳过。

    `run_once()` 需要真实备份文件，太重；这里用**源码断言**钉住那条分支 ——
    只要有人把 `live_rows is None` 又改回"跳过"，这条就红。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / 'core' / 'backup_drill.py').read_text(
        encoding='utf-8')

    assert 'if live_rows is None:' in src, (
        'backup_drill 不再区分"查不到"与"空表" —— 空备份 + 库被锁会演练通过')
    seg = src.split('if live_rows is None:', 1)[1][:400]
    assert "problems'].append" in seg, (
        '"查不到"分支没有记进 problems —— 演练仍然会报 ok=True')


# ---------------------------------------------------------------------------
# 6. health/status 不健康必须 503
# ---------------------------------------------------------------------------

def test_health_returns_503_when_unhealthy(client, monkeypatch):
    import 展示层.api.api_health as mod
    from core.health_checker import HealthStatus

    monkeypatch.setattr(mod.ModuleRegistry, 'get_status', classmethod(
        lambda cls: {'m': {'status': 'ok'}}))
    monkeypatch.setattr(mod.HealthChecker, 'get_status', classmethod(
        lambda cls: {'global_status': HealthStatus.UNHEALTHY, 'checks': {}}))

    resp = client.get('/api/health/status')

    assert resp.status_code == 503, (
        '不健康却回 200 —— 负载均衡/K8s readiness 永远认为健康')


def test_health_returns_200_when_healthy(client, monkeypatch):
    """反向对照：健康时不许回 5xx。"""
    import 展示层.api.api_health as mod

    monkeypatch.setattr(mod.ModuleRegistry, 'get_status', classmethod(
        lambda cls: {'m': {'status': 'ok'}}))
    monkeypatch.setattr(mod.HealthChecker, 'get_status', classmethod(
        lambda cls: {'global_status': 'healthy', 'checks': {}}))

    resp = client.get('/api/health/status')

    assert resp.status_code == 200
