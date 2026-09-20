"""
Tests for data archive and export modules - covers 存储层/data_archive.py (52%->high) and data_export.py (61%->high)
"""
import os
import json
import csv
import tempfile
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from pathlib import Path
from 存储层.data_archive import DataArchive
from 存储层.data_export import DataExport


# ── DataArchive Tests ──

@pytest.fixture
def mock_database():
    db = MagicMock()
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    db.get_connection.return_value = conn
    db.cleanup_old_data.return_value = 10
    db.get_history_data.return_value = [
        {'timestamp': '2024-01-01T00:00:00', 'value': 10.0, 'unit': 'C'},
        {'timestamp': '2024-01-01T00:01:00', 'value': 20.0, 'unit': 'C'},
        {'timestamp': '2024-01-01T00:02:00', 'value': 30.0, 'unit': 'C'},
    ]
    return db


@pytest.fixture
def archive(mock_database):
    return DataArchive(mock_database)


class TestDataArchiveInit:
    def test_init(self, archive, mock_database):
        assert archive.database is mock_database
        assert 'moving_average' in archive.compress_algorithms
        assert 'max_keep' in archive.compress_algorithms
        assert 'min_keep' in archive.compress_algorithms
        assert 'lttb' in archive.compress_algorithms
        assert 'statistical' in archive.compress_algorithms


class TestArchiveData:
    def test_archive_data(self, tmp_path):
        """archive_data 现在是 ``Database.archive_old_data`` 的薄适配层。

        2026-09 审计前它自己建 ``history_data_archive`` 并整行复制，
        ``deleted_from_main`` 还取自没有 return 的 ``cleanup_old_data``（恒 None），
        用 MagicMock 断言"调用过 cleanup_old_data"完全测不出这些问题，
        所以这里改成真库验证真实返回值与归档去向。
        """
        from 存储层.database import ARCHIVE_TABLE, Database

        db = Database(str(tmp_path / 'archive.db'))
        db.insert_data('dev1', 'temp', 25.0, datetime.now() - timedelta(days=40), 'C')

        result = DataArchive(db).archive_data(retention_days=30)

        assert result['archive_table'] == ARCHIVE_TABLE
        assert result['moved_to_archive'] == 1, '过期数据没有按天聚合进归档表'
        assert result['deleted_from_main'] == 1, 'deleted_from_main 必须是真实删除条数'
        assert isinstance(result['deleted_from_main'], int)
        assert 'cutoff_date' in result
        with db.get_connection(readonly=True) as conn:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%archive%'")]
        assert names == [ARCHIVE_TABLE], f'归档散落到多张表: {names}'
        db.close()


class TestParseInterval:
    def test_parse_1min(self, archive):
        assert archive._parse_interval('1min') == 60

    def test_parse_5min(self, archive):
        assert archive._parse_interval('5min') == 300

    def test_parse_15min(self, archive):
        assert archive._parse_interval('15min') == 900

    def test_parse_1hour(self, archive):
        assert archive._parse_interval('1hour') == 3600

    def test_parse_1day(self, archive):
        assert archive._parse_interval('1day') == 86400

    def test_parse_unknown(self, archive):
        assert archive._parse_interval('unknown') == 3600


class TestGroupByInterval:
    def test_group_basic(self, archive):
        data = [
            {'timestamp': '2024-01-01T00:00:00', 'value': 10},
            {'timestamp': '2024-01-01T00:00:30', 'value': 20},
            {'timestamp': '2024-01-01T00:01:00', 'value': 30},
        ]
        buckets = archive._group_by_interval(data, 60)
        assert len(buckets) >= 1

    def test_group_with_datetime(self, archive):
        data = [
            {'timestamp': datetime(2024, 1, 1, 0, 0, 0), 'value': 10},
            {'timestamp': datetime(2024, 1, 1, 0, 0, 30), 'value': 20},
        ]
        buckets = archive._group_by_interval(data, 60)
        assert len(buckets) >= 1


class TestCompressMovingAverage:
    def test_basic(self, archive):
        buckets = {
            1704067200: [
                {'value': 10.0, 'unit': 'C'},
                {'value': 20.0, 'unit': 'C'},
            ],
        }
        result = archive._compress_moving_average(buckets)
        assert len(result) == 1
        assert result[0]['value'] == 15.0
        assert result[0]['count'] == 2

    def test_empty_buckets(self, archive):
        result = archive._compress_moving_average({})
        assert result == []

    def test_none_values_filtered(self, archive):
        buckets = {
            1704067200: [
                {'value': None, 'unit': 'C'},
                {'value': 20.0, 'unit': 'C'},
            ],
        }
        result = archive._compress_moving_average(buckets)
        assert len(result) == 1
        assert result[0]['value'] == 20.0


class TestCompressMaxKeep:
    def test_basic(self, archive):
        buckets = {
            1704067200: [
                {'value': 10.0, 'unit': 'C'},
                {'value': 30.0, 'unit': 'C'},
                {'value': 20.0, 'unit': 'C'},
            ],
        }
        result = archive._compress_max_keep(buckets)
        assert len(result) == 1
        assert result[0]['value'] == 30.0


class TestCompressMinKeep:
    def test_basic(self, archive):
        buckets = {
            1704067200: [
                {'value': 10.0, 'unit': 'C'},
                {'value': 30.0, 'unit': 'C'},
                {'value': 20.0, 'unit': 'C'},
            ],
        }
        result = archive._compress_min_keep(buckets)
        assert len(result) == 1
        assert result[0]['value'] == 10.0


class TestCompressStatistical:
    def test_basic(self, archive):
        buckets = {
            1704067200: [
                {'value': 10.0, 'unit': 'C'},
                {'value': 20.0, 'unit': 'C'},
                {'value': 30.0, 'unit': 'C'},
            ],
        }
        result = archive._compress_statistical(buckets)
        assert len(result) == 1
        assert result[0]['avg'] == 20.0
        assert result[0]['max'] == 30.0
        assert result[0]['min'] == 10.0
        assert result[0]['count'] == 3
        assert result[0]['std'] > 0

    def test_single_value(self, archive):
        buckets = {
            1704067200: [{'value': 10.0, 'unit': 'C'}],
        }
        result = archive._compress_statistical(buckets)
        assert result[0]['std'] == 0


class TestCompressLTTB:
    def test_small_dataset(self, archive):
        buckets = {}
        for i in range(5):
            key = 1704067200 + i * 60
            buckets[key] = [{'value': float(i * 10), 'unit': 'C'}]

        result = archive._compress_lttb(buckets, threshold=10)
        assert len(result) == 5

    def test_large_dataset(self, archive):
        buckets = {}
        for i in range(200):
            key = 1704067200 + i * 60
            buckets[key] = [{'value': float(i), 'unit': 'C'}]

        result = archive._compress_lttb(buckets, threshold=50)
        assert len(result) == 50

    def test_empty(self, archive):
        result = archive._compress_lttb({})
        assert result == []


class TestCompressData:
    def test_compress_data_with_data(self, archive):
        cursor = archive.database.get_connection().cursor()
        cursor.fetchall.return_value = []

        result = archive.compress_data('dev1', 'temp',
                                        datetime(2024, 1, 1), datetime(2024, 1, 2))

        assert result['device_id'] == 'dev1'
        assert result['register_name'] == 'temp'
        assert 'compression_ratio' in result

    def test_compress_data_empty(self, archive):
        archive.database.get_history_data.return_value = []

        result = archive.compress_data('dev1', 'temp',
                                        datetime(2024, 1, 1), datetime(2024, 1, 2))

        assert result['original_count'] == 0
        assert result['compressed_count'] == 0

    def test_compress_data_unknown_algorithm(self, archive):
        result = archive.compress_data('dev1', 'temp',
                                        datetime(2024, 1, 1), datetime(2024, 1, 2),
                                        algorithm='unknown')
        # Should fall back to statistical
        assert result is not None


class TestGetCompressionStats:
    def test_get_compression_stats(self, archive):
        cursor = archive.database.get_connection().cursor()
        cursor.fetchall.return_value = [
            {'device_id': 'dev1', 'register_name': 'temp', 'total_records': 100,
             'earliest_record': '2024-01-01', 'latest_record': '2024-01-02'}
        ]

        result = archive.get_compression_stats()

        assert 'total_devices' in result
        assert 'total_registers' in result
        assert 'total_records' in result

    def test_get_compression_stats_with_filters(self, archive):
        cursor = archive.database.get_connection().cursor()
        cursor.fetchall.return_value = []

        result = archive.get_compression_stats(
            device_id='dev1',
            start_time=datetime(2024, 1, 1),
            end_time=datetime(2024, 1, 2)
        )

        assert result['total_devices'] == 0


# ── DataExport Tests ──

@pytest.fixture
def export_dir():
    tmp = tempfile.mkdtemp()
    yield tmp
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def exporter(export_dir):
    return DataExport(export_dir)


class TestDataExportInit:
    def test_init(self, export_dir):
        exporter = DataExport(export_dir)
        # 必须比较 resolve() 之后的结果。
        # DataExport.__init__ 内部会 `Path(export_dir).resolve()`，而 Windows 的
        # resolve() 会把 8.3 短名规范化成长名：
        #   C:\Users\RUNNER~1\AppData\Local\Temp\tmpXXXX
        #     -> C:\Users\runneradmin\AppData\Local\Temp\tmpXXXX
        # GitHub 的 Windows runner 恰好把 TEMP 设成短名形式，而 tempfile.mkdtemp()
        # 直接沿用 TEMP，于是「已 resolve」与「未 resolve」的字符串不相等 ——
        # 本机（TEMP 本来就是长名）永远看不出来，只有目标平台才暴露。
        # 生产代码是对的（规范化路径是期望行为），错的是这里比较方式太脆。
        assert exporter.export_dir == Path(export_dir).resolve()
        # 顺带确认两条路径确实指向同一个目录（即使字符串形式不同）
        assert exporter.export_dir.samefile(Path(export_dir))


class TestExportCSV:
    def test_export_csv_success(self, exporter):
        data = [{'name': 'temp', 'value': 25.5}, {'name': 'press', 'value': 101.3}]

        filepath = exporter.export_csv(data, 'test.csv')

        assert filepath != ''
        assert os.path.exists(filepath)
        with open(filepath, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert len(rows) == 2

    def test_export_csv_auto_filename(self, exporter):
        data = [{'a': 1}]
        filepath = exporter.export_csv(data)
        assert filepath != ''
        assert 'export_' in filepath

    def test_export_csv_empty_data(self, exporter):
        result = exporter.export_csv([])
        assert result is None

    def test_export_csv_write_error(self, exporter):
        data = [{'a': 1}]
        with patch('builtins.open', side_effect=PermissionError("denied")):
            result = exporter.export_csv(data, 'test.csv')
            assert result == ""


class TestExportExcel:
    def test_export_excel_success(self, exporter):
        data = [{'name': 'temp', 'value': 25.5}]

        filepath = exporter.export_excel(data, 'test.xlsx')

        assert filepath is not None
        assert os.path.exists(filepath)

    def test_export_excel_auto_filename(self, exporter):
        data = [{'a': 1}]
        filepath = exporter.export_excel(data)
        assert filepath is not None

    def test_export_excel_empty_data(self, exporter):
        result = exporter.export_excel([])
        assert result is None

    def test_export_excel_import_error(self, exporter):
        data = [{'a': 1}]
        with patch.dict('sys.modules', {'pandas': None}):
            with patch('builtins.__import__', side_effect=ImportError("no pandas")):
                result = exporter.export_excel(data)
                # 原测试拿到 result 后只写了 `pass`，没有任何断言 —— 恒过。
                # pandas 不可用时必须优雅降级返回 None，而不是把 ImportError 抛出去。
                assert result is None, \
                    f"pandas 不可用时 export_excel 应返回 None，实际 {result!r}"


class TestExportJSON:
    def test_export_json_success(self, exporter):
        data = [{'name': 'temp', 'value': 25.5}]

        filepath = exporter.export_json(data, 'test.json')

        assert filepath is not None
        assert os.path.exists(filepath)
        with open(filepath, 'r', encoding='utf-8') as f:
            loaded = json.load(f)
            assert len(loaded) == 1

    def test_export_json_compact(self, exporter):
        data = [{'a': 1}]
        filepath = exporter.export_json(data, 'test_compact.json', pretty=False)
        assert filepath is not None

    def test_export_json_auto_filename(self, exporter):
        data = [{'a': 1}]
        filepath = exporter.export_json(data)
        assert filepath is not None

    def test_export_json_empty_data(self, exporter):
        result = exporter.export_json([])
        assert result is None

    def test_export_json_write_error(self, exporter):
        data = [{'a': 1}]
        with patch('builtins.open', side_effect=PermissionError("denied")):
            result = exporter.export_json(data, 'test.json')
            assert result is None


class TestExportDeviceData:
    def test_export_csv(self, exporter):
        db = MagicMock()
        db.get_device_registers.return_value = ['temp', 'pressure']
        db.get_history_data.return_value = [
            {'timestamp': '2024-01-01', 'value': 25.5, 'device_id': 'dev1'}
        ]

        result = exporter.export_device_data(db, 'dev1',
                                             datetime(2024, 1, 1), datetime(2024, 1, 2), 'csv')

        assert result is not None

    def test_export_excel(self, exporter):
        db = MagicMock()
        db.get_device_registers.return_value = ['temp']
        db.get_history_data.return_value = [{'value': 25}]

        result = exporter.export_device_data(db, 'dev1',
                                             datetime(2024, 1, 1), datetime(2024, 1, 2), 'excel')

        assert result is not None

    def test_export_json(self, exporter):
        db = MagicMock()
        db.get_device_registers.return_value = ['temp']
        db.get_history_data.return_value = [{'value': 25}]

        result = exporter.export_device_data(db, 'dev1',
                                             datetime(2024, 1, 1), datetime(2024, 1, 2), 'json')

        assert result is not None

    def test_export_unsupported_format(self, exporter):
        db = MagicMock()
        db.get_device_registers.return_value = ['temp']
        db.get_history_data.return_value = [{'value': 25}]

        result = exporter.export_device_data(db, 'dev1',
                                             datetime(2024, 1, 1), datetime(2024, 1, 2), 'xml')

        assert result is None

    def test_export_no_registers(self, exporter):
        db = MagicMock()
        db.get_device_registers.return_value = []
        db.get_history_data.return_value = [{'value': 25}]

        result = exporter.export_device_data(db, 'dev1',
                                             datetime(2024, 1, 1), datetime(2024, 1, 2), 'csv')

        assert result is not None

    def test_export_no_data(self, exporter):
        db = MagicMock()
        db.get_device_registers.return_value = ['temp']
        db.get_history_data.return_value = []

        result = exporter.export_device_data(db, 'dev1',
                                             datetime(2024, 1, 1), datetime(2024, 1, 2), 'csv')

        assert result is None


class TestExportAlarmRecords:
    def test_export_csv(self, exporter):
        db = MagicMock()
        db.get_alarm_records.return_value = [{'alarm_id': 'a1', 'level': 'high'}]

        result = exporter.export_alarm_records(db, format='csv')

        assert result is not None

    def test_export_excel(self, exporter):
        db = MagicMock()
        db.get_alarm_records.return_value = [{'alarm_id': 'a1'}]

        result = exporter.export_alarm_records(db, format='excel')

        assert result is not None

    def test_export_json(self, exporter):
        db = MagicMock()
        db.get_alarm_records.return_value = [{'alarm_id': 'a1'}]

        result = exporter.export_alarm_records(db, format='json')

        assert result is not None

    def test_export_unsupported_format(self, exporter):
        db = MagicMock()
        db.get_alarm_records.return_value = [{'alarm_id': 'a1'}]

        result = exporter.export_alarm_records(db, format='xml')

        assert result is None

    def test_export_no_data(self, exporter):
        db = MagicMock()
        db.get_alarm_records.return_value = []

        result = exporter.export_alarm_records(db)

        assert result is None


class TestListExports:
    def test_list_exports(self, exporter):
        # Create some files first
        exporter.export_csv([{'a': 1}], 'test1.csv')
        exporter.export_json([{'b': 2}], 'test2.json')

        exports = exporter.list_exports()

        assert len(exports) >= 2
        for exp in exports:
            assert 'filename' in exp
            assert 'size_bytes' in exp
            assert 'size_mb' in exp

    def test_list_exports_empty(self, exporter):
        tmp = tempfile.mkdtemp()
        empty_exporter = DataExport(tmp)
        exports = empty_exporter.list_exports()
        assert exports == []
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


class TestDeleteExport:
    def test_delete_existing(self, exporter):
        exporter.export_csv([{'a': 1}], 'to_delete.csv')

        result = exporter.delete_export('to_delete.csv')

        assert result is True

    def test_delete_nonexistent(self, exporter):
        result = exporter.delete_export('nonexistent.csv')

        assert result is False

    def test_delete_error(self, exporter):
        exporter.export_csv([{'a': 1}], 'test.csv')
        with patch.object(Path, 'unlink', side_effect=PermissionError("denied")):
            result = exporter.delete_export('test.csv')
            assert result is False
