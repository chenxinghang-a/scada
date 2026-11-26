# -*- coding: utf-8 -*-
"""Partition B4 回归测试：存储层 / timeseries / config_validator / config_schema / 配置。"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from core.config_schema import validate_alarms_document, validate_devices_document
from core.config_validator import (
    validate_all_configs,
    validate_config,
)
from timeseries.data_models import AlarmRecord, OEERecord, TelemetryRecord
from timeseries.migration import SQLiteToTDengineMigrator
from timeseries.mqtt_to_tsdb import MQTTToTSDBService, PredictiveDataWriter
from timeseries.query_builder import QueryBuilder
from timeseries.tdengine_client import TDengineClient
from 存储层.data_archive import DataArchive
from 存储层.data_export import DataExport
from 存储层.database import ARCHIVE_TABLE, Database


def test_tdengine_rest_connect_does_not_mutate_rest_url(monkeypatch):
    c = TDengineClient('127.0.0.1', 6041, database='scada', use_rest=True)
    observed = []

    def fake_exec(sql, url=None):
        observed.append((sql, url, c.rest_url))
        return {'code': 0}

    monkeypatch.setattr(c, '_execute_sql', fake_exec)
    assert c.connect() is True
    assert observed[0][1] == 'http://127.0.0.1:6041/rest/sql'
    assert observed[0][2] == 'http://127.0.0.1:6041/rest/sql/scada'
    assert c.rest_url == 'http://127.0.0.1:6041/rest/sql/scada'


def test_tdengine_latest_telemetry_uses_order_by_limit(monkeypatch):
    c = TDengineClient('127.0.0.1', 6041)
    seen = []

    def fake_exec(sql):
        seen.append(sql)
        return {'code': 0, 'data': [['2026-10-01 00:00:00.000', 42.5, 192]]}

    monkeypatch.setattr(c, '_execute_sql', fake_exec)
    res = c.query_telemetry_latest('plc_1', 'temp')
    assert 'ORDER BY ts DESC LIMIT 1' in seen[0]
    assert 'LAST(' not in seen[0]
    assert res['value'] == 42.5


def test_tdengine_batch_quarantines_poison_pill_and_keeps_valid_rows(monkeypatch):
    c = TDengineClient('127.0.0.1', 6041)
    monkeypatch.setattr(c, '_execute_sql', lambda sql: {'code': 0})

    ok_rec = TelemetryRecord('d1', 'temp', datetime(2026, 10, 1, 12, 0, 0), 25.0, 192)
    poison_str = TelemetryRecord('d1', 'temp', datetime(2026, 10, 1, 12, 0, 1), 'bad-val', 192)
    poison_id = TelemetryRecord('', 'temp', datetime(2026, 10, 1, 12, 0, 2), 10.0, 192)
    poison_bool_q = TelemetryRecord('d1', 'temp', datetime(2026, 10, 1, 12, 0, 3), 11.0, True)

    assert c.write_telemetry_batch([poison_str, poison_id, poison_bool_q, ok_rec]) is True
    stats = c.get_stats()
    assert stats['writes'] == 1
    assert stats['quarantined'] == 3


def test_data_models_reject_bool_int_cols_and_bad_timestamp():
    rec = TelemetryRecord('d1', 'r1', datetime(2026, 10, 1), 1.0, True)
    with pytest.raises(ValueError):
        rec.to_sql_values()
    oee = OEERecord('d1', datetime(2026, 10, 1), 0.9, 0.9, 0.9, 0.729, total_count=True)
    with pytest.raises(ValueError):
        oee.to_sql_values()
    bad_ts = AlarmRecord('a1', 'd1', 'not-dt', 'high', 't', 'msg', 1.0, 0.5)
    with pytest.raises(ValueError):
        bad_ts.to_sql_values()


def test_mqtt_to_tsdb_skips_bad_metric_and_preserves_order():
    sub = SimpleNamespace(on_telemetry=None, on_alarm=None, subscribe_telemetry=lambda: None,
                          subscribe_alarms=lambda: None, start=lambda: None, stop=lambda: None)
    written_batches = []
    td = SimpleNamespace(
        connect=lambda: True,
        init_tables=lambda: None,
        disconnect=lambda: None,
        write_telemetry_batch=lambda recs: written_batches.append(list(recs)) or False,
        write_alarm=lambda rec: True,
        write_predictive=lambda rec: False,
    )
    svc = MQTTToTSDBService(sub, td, batch_size=100, flush_interval=3600)
    svc._on_telemetry(SimpleNamespace(
        DeviceID='d1',
        Timestamp=datetime(2026, 10, 1, 10, 0, 0).timestamp(),
        Metrics={
            'bad_nan': {'value': float('nan')},
            'bad_bool': {'value': True},
            'good': {'value': 12.5, 'quality': 192},
        },
        Protocol='modbus_tcp',
        GatewayID='gw1',
    ))
    assert len(svc._telemetry_buffer) == 1
    assert svc._telemetry_buffer[0].register_name == 'good'
    assert svc.stats['errors'] == 2

    svc._flush_telemetry()
    svc._on_telemetry(SimpleNamespace(
        DeviceID='d1',
        Timestamp=datetime(2026, 10, 1, 10, 0, 5).timestamp(),
        Metrics={'newer': {'value': 13.5}},
        Protocol='modbus_tcp',
        GatewayID='gw1',
    ))
    assert [r.register_name for r in svc._telemetry_buffer] == ['good', 'newer']
    assert PredictiveDataWriter(td).write_predictive('d1', 90.0, 0.1, 100.0) is False


def test_query_builder_and_migration_fixes(tmp_path):
    qb = QueryBuilder('device_telemetry')
    with pytest.raises(ValueError):
        qb.select_agg('PERCENTILE', 'value', params=[float('nan')])
    with pytest.raises(ValueError):
        qb.where_value_gt(True)
    with pytest.raises(ValueError):
        qb.offset(True)
    sql = (
        QueryBuilder('device_telemetry')
        .where_time(datetime(2026, 10, 1, 12, 0, 0, 123000), datetime(2026, 10, 1, 12, 5, 0, 456000))
        .build()
    )
    assert '2026-10-01 12:00:00.123' in sql and '2026-10-01 12:05:00.456' in sql

    db_path = tmp_path / 'mig.db'
    conn = sqlite3.connect(str(db_path))
    conn.execute('''CREATE TABLE history_data (device_id TEXT, register_name TEXT, timestamp TEXT, value REAL, unit TEXT)''')
    conn.execute('''CREATE TABLE alarm_records (alarm_id TEXT, device_id TEXT, timestamp TEXT, alarm_level TEXT, alarm_message TEXT, threshold REAL, actual_value REAL)''')
    conn.execute('''INSERT INTO alarm_records VALUES ('a1', 'd1', '2026-10-01 10:00:00', 'high', 'msg', 0.0, 0.0)''')
    conn.commit()
    conn.close()

    alarms = []
    td = MagicMock()
    td.write_alarm.side_effect = lambda rec: alarms.append(rec) or True
    td._sanitize_identifier = TDengineClient._sanitize_identifier
    migrator = SQLiteToTDengineMigrator(str(db_path), td)
    migrator.connect_sqlite()
    migrator._migrate_alarms(10)
    migrator.disconnect_sqlite()
    assert len(alarms) == 1 and alarms[0].value == 0.0 and alarms[0].threshold == 0.0


def test_data_export_tracks_last_error_and_returns_none(tmp_path, monkeypatch):
    exp = DataExport(export_dir=tmp_path)
    assert exp.export_csv([]) is None
    assert exp.last_error is None

    monkeypatch.setattr('builtins.open', lambda *a, **k: (_ for _ in ()).throw(PermissionError('disk full')))
    assert exp.export_json([{'a': 1}], 'fail.json') is None
    assert exp.last_error and 'disk full' in exp.last_error

    monkeypatch.undo()
    ok = exp.export_json([{'a': 1}], 'ok.json')
    assert ok and exp.last_error is None


def test_database_late_backfill_folds_into_archive_and_cleans_wal(tmp_path):
    db_path = tmp_path / 'db.sqlite'
    db = Database(str(db_path))
    try:
        old_day = (datetime.now() - timedelta(days=45)).replace(hour=10, minute=0, second=0, microsecond=0)
        db.insert_data('d1', 'temp', 10.0, old_day, 'C')
        db.insert_data('d1', 'temp', 20.0, old_day + timedelta(minutes=1), 'C')
        # 布尔与非有限数应被单条与批量通道共同拒绝
        db.insert_data('d1', 'temp', True, old_day, 'C')
        assert db.insert_data_batch([{'device_id': 'd1', 'register_name': 'temp', 'value': True, 'timestamp': old_day}]) == 0

        res1 = db.archive_old_data(archive_days=7, delete_days=30)
        assert res1['archived'] == 1
        assert res1['deleted_history'] == 2

        # 幂等复跑：无新数据时 archived 应为 0
        res2 = db.archive_old_data(archive_days=7, delete_days=30)
        assert res2['archived'] == 0

        # 迟到回填：同一已归档日期新补 2 条数据（value=40.0, 50.0）
        db.insert_data('d1', 'temp', 40.0, old_day + timedelta(minutes=2), 'C')
        db.insert_data('d1', 'temp', 50.0, old_day + timedelta(minutes=3), 'C')

        # 模拟归档窗口之后直接跑删库也不得提前删掉 archived=0 的回填行；再归档则应合并进已有日聚合
        res3 = db.archive_old_data(archive_days=7, delete_days=30)
        assert res3['archived'] == 1
        assert res3['deleted_history'] == 2

        rows = db.get_archive_data(
            'd1', 'temp',
            old_day.strftime('%Y-%m-%d'),
            old_day.strftime('%Y-%m-%d'),
        )
        assert len(rows) == 1
        assert rows[0]['sample_count'] == 4
        assert rows[0]['avg_value'] == pytest.approx(30.0)
        assert rows[0]['min_value'] == pytest.approx(10.0)
        assert rows[0]['max_value'] == pytest.approx(50.0)

        # cleanup_old_data 需推进 _dbstats_gen
        gen_before = db._dbstats_gen
        db.cleanup_old_data(retention_days=30)
        assert db._dbstats_gen > gen_before

        # 恢复备份：应清理掉目标库残留的无主 -wal/-shm
        backup_file = db.backup_database(str(tmp_path / 'backups'))
        assert backup_file
        stale_wal = tmp_path / 'db.sqlite-wal'
        stale_wal.write_bytes(b'corrupt wal bytes')
        ok_restore, msg = db.restore_database(backup_file, safety_copy=False)
        assert ok_restore, msg
        assert not stale_wal.exists()
    finally:
        db.close()


def test_data_archive_aligns_cutoff_and_paginates():
    mock_db = MagicMock()
    mock_db.archive_old_data.return_value = {'archived': 1, 'deleted_history': 1}
    arch = DataArchive(mock_db)
    res = arch.archive_data(retention_days=7)
    assert res['cutoff_date'].endswith('00:00:00')

    ts0 = datetime(2026, 10, 1, 0, 0, 0)
    page1 = [{'time_bucket': (ts0 + timedelta(minutes=i)).strftime('%Y-%m-%d %H:%M:00'),
              'avg_value': float(i)} for i in range(10000)]
    page2 = [{'time_bucket': (ts0 + timedelta(minutes=10000)).strftime('%Y-%m-%d %H:%M:00'),
              'avg_value': 99.0}]
    mock_db.get_history_data.side_effect = [page1, page2]
    comp = arch.compress_data('d1', 'temp', ts0, ts0 + timedelta(days=10), interval='1hour')
    assert comp['original_count'] == 10001
    assert mock_db.get_history_data.call_count == 2


def test_config_validator_and_schema_alignment(tmp_path, monkeypatch):
    # real system.yaml passes validate_config
    ok, errs = validate_config('配置/system.yaml', 'system')
    assert ok, errs

    # config_validator honors SCADA_CONFIG_DIR when set
    iso = tmp_path / 'iso_cfg'
    iso.mkdir()
    (iso / 'devices.yaml').write_text(yaml.safe_dump({'devices': [{'id': 'd1', 'name': 'n', 'protocol': 'mqtt'}]}), encoding='utf-8')
    (iso / 'alarms.yaml').write_text(yaml.safe_dump({'alarm_rules': [{'id': 'a1', 'device_id': 'd1', 'condition': 'gt', 'threshold': 10}]}), encoding='utf-8')
    (iso / 'system.yaml').write_text(yaml.safe_dump({'system': {'simulation_mode': True}}), encoding='utf-8')
    monkeypatch.setenv('SCADA_CONFIG_DIR', str(iso))
    res = validate_all_configs('配置')
    assert all(valid for valid, _ in res.values()), res

    # config_schema rejects NaN / Inf and accepts float string interval
    errs_dev = validate_devices_document({
        'devices': [{
            'id': 'd1', 'name': 'ok', 'protocol': 'mqtt', 'topics': ['t1'],
            'collection_interval': '0.5',
            'registers': [{'name': 'r1', 'address': 0, 'scale': float('nan')}],
        }]
    })
    assert any('scale' in e for e in errs_dev)
    assert not any('collection_interval' in e for e in errs_dev)

    errs_alm = validate_alarms_document({
        'alarm_rules': [{'id': 'a1', 'device_id': 'd1', 'condition': 'greater_than', 'threshold': math.inf}]
    })
    assert errs_alm
