# -*- coding: utf-8 -*-
"""设备数据导出：必须是**原始明细**、带寄存器归属、且不静默截断（round 228）。

三个缺陷都在「导出设备数据」这一个动作里，而且**文件看起来是好的**：

1. **导出的是 1 分钟均值，不是原始数据**
   `export_device_data` 调用 `get_history_data` 时**没传 `interval`**，
   于是走默认 `'1min'`。用户以为导的是原始采样，实际每个 1 分钟只剩一条均值
   （10Hz 数据 → 1/600 的信息量），而 CSV 里连「这是聚合值」都没有提示。

2. **多寄存器导出没有 `register_name` 列**
   `get_history_data` 只 SELECT `time_bucket/avg_value/...`，不含寄存器名；
   逐寄存器查完直接 `data.extend(...)`。设备有 5 个寄存器时，导出文件里
   5 路数据**混成一锅**，无法判断哪一行属于哪个寄存器。

3. **每寄存器静默截断在 10000 行**
   `get_history_data` 的默认 `limit=10000`。24 小时原始数据动辄几万行，
   导出文件少一半，**没有任何提示** —— 而导出文件通常被当成「完整数据」存档。

修法：默认 `interval='raw'`；逐行补 `register_name`；分页拉全量，
超过 `MAX_EXPORT_ROWS` **抛 ValueError 明确拒绝**（宁可不导，也不给残缺文件）。
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from 存储层 import data_export as de_module
from 存储层.data_export import DataExport, MAX_EXPORT_ROWS
from 存储层.database import Database


def _db(tmp_path) -> Database:
    return Database(str(tmp_path / 'export.db'))


def _seed(database: Database, register: str, base: datetime, n: int, step_ms: int = 100):
    rows = [
        {
            'device_id': 'd1', 'register_name': register, 'value': float(i),
            'unit': 'C', 'timestamp': base + timedelta(milliseconds=step_ms * i),
        }
        for i in range(n)
    ]
    database.insert_data_batch(rows)


def _read_csv(path) -> tuple[list[str], list[dict]]:
    with open(path, encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def _export(tmp_path, database, **kwargs):
    exporter = DataExport(tmp_path / 'exports')
    return exporter.export_device_data(
        database=database,
        device_id='d1',
        start_time=datetime(2026, 10, 21, 0, 0, 0),
        end_time=datetime(2026, 10, 21, 23, 59, 59),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. 默认导出原始明细（不是 1 分钟均值）
# ---------------------------------------------------------------------------

def test_export_is_raw_by_default(tmp_path):
    """同一秒内的 5 条采样必须全部出现在文件里 —— 默认不能是 1min 聚合。"""
    db = _db(tmp_path)
    base = datetime(2026, 10, 21, 12, 0, 0)
    try:
        _seed(db, 'temp', base, n=5)          # 5 条都在同一秒内（100ms 间隔）
        path = _export(tmp_path, db)
        assert path, '导出返回空'
        cols, rows = _read_csv(path)

        assert len(rows) == 5, (
            f'只导出 {len(rows)} 行 —— 默认走了 1min 聚合，'
            '同秒的 5 条被压成 1 条均值（用户以为导的是原始数据）'
        )
        assert 'timestamp' in cols and 'value' in cols, f'列不对（像是聚合结果）: {cols}'
        assert 'avg_value' not in cols, f'导出里出现 avg_value → 是聚合值不是原始数据: {cols}'
    finally:
        db.close()


def test_export_interval_can_be_aggregated_explicitly(tmp_path):
    """显式要聚合时仍然可用（回归：别把聚合能力删掉）。"""
    db = _db(tmp_path)
    base = datetime(2026, 10, 21, 12, 0, 0)
    try:
        _seed(db, 'temp', base, n=5)
        path = _export(tmp_path, db, interval='1min')
        cols, rows = _read_csv(path)
        assert len(rows) == 1, f'1min 应合成 1 行，实际 {len(rows)}'
        assert 'avg_value' in cols
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 2. 多寄存器必须能区分
# ---------------------------------------------------------------------------

def test_export_includes_register_name_for_each_row(tmp_path):
    """两个寄存器的导出必须能区分归属。"""
    db = _db(tmp_path)
    base = datetime(2026, 10, 21, 12, 0, 0)
    try:
        _seed(db, 'temp', base, n=3)
        _seed(db, 'pressure', base, n=2)

        path = _export(tmp_path, db)
        cols, rows = _read_csv(path)

        assert 'register_name' in cols, (
            f'导出没有 register_name 列 —— 多寄存器数据混成一锅，无法判断归属: {cols}'
        )
        assert {r['register_name'] for r in rows} == {'temp', 'pressure'}
        assert len(rows) == 5
    finally:
        db.close()


def test_export_register_name_survives_aggregation(tmp_path):
    db = _db(tmp_path)
    base = datetime(2026, 10, 21, 12, 0, 0)
    try:
        _seed(db, 'temp', base, n=3)
        _seed(db, 'pressure', base, n=3)
        path = _export(tmp_path, db, interval='1min')
        _cols, rows = _read_csv(path)
        assert {r['register_name'] for r in rows} == {'temp', 'pressure'}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 3. 不静默截断
# ---------------------------------------------------------------------------

def test_export_pages_past_single_query_limit(tmp_path):
    """行数超过单次查询上限（10000）时，必须**全部**导出（分页拉取）。"""
    db = _db(tmp_path)
    base = datetime(2026, 10, 21, 0, 0, 0)
    total = de_module.EXPORT_PAGE_SIZE + 500      # 10500 行，跨一页
    try:
        _seed(db, 'temp', base, n=total, step_ms=1)
        path = _export(tmp_path, db)
        _cols, rows = _read_csv(path)
        assert len(rows) == total, (
            f'导出 {len(rows)} 行 / 实际 {total} 行 —— 仍在单次查询上限处静默截断'
        )
    finally:
        db.close()


def test_export_refuses_when_over_hard_cap(tmp_path, monkeypatch):
    """超过总上限时必须**明确报错**，不能给一份看起来完整的残缺文件。"""
    monkeypatch.setattr(de_module, 'MAX_EXPORT_ROWS', 5)

    db = _db(tmp_path)
    base = datetime(2026, 10, 21, 12, 0, 0)
    try:
        _seed(db, 'temp', base, n=10)
        with pytest.raises(ValueError) as ei:
            _export(tmp_path, db)
        msg = str(ei.value)
        assert '导出数据量过大' in msg
        assert str(MAX_EXPORT_ROWS) in msg or '行' in msg
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 4. API 层：超上限要回 400 + 具体原因
# ---------------------------------------------------------------------------

def test_api_export_maps_value_error_to_400(client, auth_headers, monkeypatch):
    """数据量超限不能落进「请求参数验证失败」的通用 400，要说明怎么改。"""
    from 存储层 import data_export

    def boom(self, **kwargs):
        raise ValueError('导出数据量过大：已超过 200,000 行，请缩小时间范围')

    monkeypatch.setattr(data_export.DataExport, 'export_device_data', boom)

    resp = client.post(
        '/api/export/device/d1',
        json={'start_time': '2026-10-21T00:00:00', 'end_time': '2026-10-21T01:00:00'},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    msg = resp.get_json().get('message', '')
    assert '缩小时间范围' in msg, f'提示不可操作: {msg!r}'


def test_api_export_defaults_to_raw(client, auth_headers, monkeypatch):
    """端点默认按 raw 导出（不能默默退回 1min 聚合）。"""
    from 存储层 import data_export

    captured = {}

    def fake(self, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(data_export.DataExport, 'export_device_data', fake)

    client.post(
        '/api/export/device/d1',
        json={'start_time': '2026-10-21T00:00:00', 'end_time': '2026-10-21T01:00:00'},
        headers=auth_headers,
    )
    assert captured.get('interval') == 'raw', (
        f'导出端点没有默认 raw（实际 {captured.get("interval")!r}）—— '
        '用户会拿到 1 分钟均值却以为在导原始数据'
    )


# ---------------------------------------------------------------------------
# 5. 报警导出同样不能静默截断（原来写死 limit=10000）
# ---------------------------------------------------------------------------

def _seed_alarms(database: Database, n: int, base: datetime):
    for i in range(n):
        database.insert_alarm(
            alarm_id=f'a{i}', device_id='d1', register_name='temp',
            alarm_level='warning', alarm_message=f'报警{i}',
            threshold=50.0, actual_value=60.0,
            timestamp=base + timedelta(seconds=i),
        )


def test_alarm_export_pages_past_single_query_limit(tmp_path):
    """报警条数超过单次查询上限时也必须全部导出。"""
    db = _db(tmp_path)
    base = datetime(2026, 10, 21, 0, 0, 0)
    total = de_module.EXPORT_PAGE_SIZE + 200
    try:
        _seed_alarms(db, total, base)
        exporter = DataExport(tmp_path / 'exports')
        path = exporter.export_alarm_records(
            database=db,
            start_time=base - timedelta(minutes=1),
            end_time=base + timedelta(seconds=total + 60),
        )
        assert path, '报警导出返回空'
        _cols, rows = _read_csv(path)
        assert len(rows) == total, (
            f'只导出 {len(rows)} 条 / 实际 {total} 条 —— 仍在单次查询上限处静默截断'
        )
    finally:
        db.close()


def test_alarm_export_refuses_when_over_hard_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(de_module, 'MAX_EXPORT_ROWS', 5)

    db = _db(tmp_path)
    base = datetime(2026, 10, 21, 0, 0, 0)
    try:
        _seed_alarms(db, 10, base)
        exporter = DataExport(tmp_path / 'exports')
        with pytest.raises(ValueError) as ei:
            exporter.export_alarm_records(
                database=db,
                start_time=base - timedelta(minutes=1),
                end_time=base + timedelta(minutes=5),
            )
        assert '导出报警记录过多' in str(ei.value)
    finally:
        db.close()
