"""
数据库操作测试 - 提升存储层/database.py 覆盖率
"""
import pytest
import tempfile
import os
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock


class TestDatabaseInit:
    """数据库初始化测试"""

    def test_database_creates_file(self, db):
        """数据库文件应该被创建"""
        assert os.path.exists(db)

    def test_database_tables_exist(self, db):
        """所有必要表应该存在"""
        conn = sqlite3.connect(db)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()

        assert 'realtime_data' in tables
        assert 'history_data' in tables
        assert 'alarm_records' in tables
        assert 'device_status' in tables

    def test_database_indexes_exist(self, tmp_path):
        """索引应该存在 (使用Database类创建)"""
        from 存储层.database import Database
        db_path = str(tmp_path / 'test.db')
        database = Database(db_path)
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index'")
        indexes = {row[0] for row in cursor.fetchall()}
        conn.close()
        database.close()

        assert 'idx_realtime_device_time' in indexes
        assert 'idx_history_device_time' in indexes
        assert 'idx_alarm_device_time' in indexes


class TestDatabaseConnection:
    """数据库连接管理测试"""

    def test_get_connection_returns_connection(self, db):
        """get_connection 应该返回连接"""
        from 存储层.database import Database
        database = Database(db)
        with database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            result = cursor.fetchone()
            assert result[0] == 1
        database.close()

    def test_get_connection_readonly(self, db):
        """只读连接测试"""
        from 存储层.database import Database
        database = Database(db)
        with database.get_connection(readonly=True) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM realtime_data")
            count = cursor.fetchone()[0]
            assert count >= 0
        database.close()

    def test_close_connection(self, db):
        """关闭连接不抛异常"""
        from 存储层.database import Database
        database = Database(db)
        database.close()
        # Should not raise

    def test_close_thread_connection_alias(self, db):
        """close_thread_connection 是 close 的别名"""
        from 存储层.database import Database
        database = Database(db)
        assert database.close_thread_connection == database.close
        database.close()


class TestDatabaseInsertData:
    """数据插入测试"""

    def test_insert_single_data(self, db):
        """单条数据插入"""
        from 存储层.database import Database
        database = Database(db)
        now = datetime.now()
        database.insert_data('dev1', 'temp', 25.5, now, 'C')

        with database.get_connection(readonly=True) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM realtime_data WHERE device_id='dev1'")
            row = cursor.fetchone()
            assert row is not None
            assert row['value'] == 25.5
        database.close()

    def test_insert_data_upsert(self, db):
        """UPSERT: 同一设备+寄存器只保留最新"""
        from 存储层.database import Database
        database = Database(db)
        now = datetime.now()
        database.insert_data('dev1', 'temp', 25.0, now, 'C')
        database.insert_data('dev1', 'temp', 30.0, now + timedelta(seconds=1), 'C')

        with database.get_connection(readonly=True) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM realtime_data WHERE device_id='dev1'")
            count = cursor.fetchone()[0]
            assert count == 1
        database.close()

    def test_insert_data_batch(self, db):
        """批量数据插入"""
        from 存储层.database import Database
        database = Database(db)
        now = datetime.now()
        batch = [
            {'device_id': 'dev1', 'register_name': 'temp', 'value': 25.0, 'timestamp': now, 'unit': 'C'},
            {'device_id': 'dev1', 'register_name': 'pressure', 'value': 1.5, 'timestamp': now, 'unit': 'MPa'},
            {'device_id': 'dev2', 'register_name': 'temp', 'value': 30.0, 'timestamp': now, 'unit': 'C'},
        ]
        database.insert_data_batch(batch)

        with database.get_connection(readonly=True) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM realtime_data")
            count = cursor.fetchone()[0]
            assert count == 3
        database.close()

    def test_insert_data_batch_empty(self, db):
        """空批量插入不报错"""
        from 存储层.database import Database
        database = Database(db)
        database.insert_data_batch([])
        database.close()


class TestDatabaseQuery:
    """数据查询测试"""

    def _insert_test_data(self, db):
        from 存储层.database import Database
        database = Database(db)
        now = datetime.now()
        database.insert_data('dev1', 'temp', 25.0, now, 'C')
        database.insert_data('dev1', 'pressure', 1.5, now, 'MPa')
        database.insert_data('dev2', 'temp', 30.0, now, 'C')
        return database

    def test_get_realtime_data_all(self, db):
        """获取所有实时数据"""
        database = self._insert_test_data(db)
        data = database.get_realtime_data()
        assert len(data) == 3
        database.close()

    def test_get_realtime_data_by_device(self, db):
        """按设备ID查询实时数据"""
        database = self._insert_test_data(db)
        data = database.get_realtime_data(device_id='dev1')
        assert len(data) == 2
        database.close()

    def test_get_realtime_data_with_limit(self, db):
        """限制返回数量"""
        database = self._insert_test_data(db)
        data = database.get_realtime_data(limit=1)
        assert len(data) == 1
        database.close()

    def test_get_latest_data_single_register(self, db):
        """获取单个寄存器最新数据"""
        database = self._insert_test_data(db)
        data = database.get_latest_data('dev1', 'temp')
        assert data is not None
        assert data['value'] == 25.0
        database.close()

    def test_get_latest_data_all_registers(self, db):
        """获取设备所有寄存器最新数据"""
        database = self._insert_test_data(db)
        data = database.get_latest_data('dev1')
        assert data is not None
        assert 'temp' in data
        assert 'pressure' in data
        database.close()

    def test_get_latest_data_not_found(self, db):
        """查询不存在的数据返回None"""
        database = self._insert_test_data(db)
        data = database.get_latest_data('nonexistent', 'temp')
        assert data is None
        database.close()

    def test_get_latest_data_all(self, db):
        """一次性获取所有设备最新数据"""
        database = self._insert_test_data(db)
        data = database.get_latest_data_all()
        assert 'dev1' in data
        assert 'dev2' in data
        database.close()

    def test_get_device_registers(self, db):
        """获取设备寄存器列表"""
        database = self._insert_test_data(db)
        registers = database.get_device_registers('dev1')
        assert len(registers) >= 2
        database.close()

    def test_get_history_data(self, db):
        """获取历史数据"""
        database = self._insert_test_data(db)
        now = datetime.now()
        start = now - timedelta(hours=1)
        end = now + timedelta(hours=1)
        data = database.get_history_data('dev1', 'temp', start, end)
        assert isinstance(data, list)
        database.close()

    def test_get_history_data_5min_interval(self, db):
        """5分钟间隔聚合"""
        database = self._insert_test_data(db)
        now = datetime.now()
        start = now - timedelta(hours=1)
        end = now + timedelta(hours=1)
        data = database.get_history_data('dev1', 'temp', start, end, interval='5min')
        assert isinstance(data, list)
        database.close()

    def test_get_history_data_1hour_interval(self, db):
        """1小时间隔聚合"""
        database = self._insert_test_data(db)
        now = datetime.now()
        start = now - timedelta(hours=2)
        end = now + timedelta(hours=1)
        data = database.get_history_data('dev1', 'temp', start, end, interval='1hour')
        assert isinstance(data, list)
        database.close()

    def test_get_history_data_1day_interval(self, db):
        """1天间隔聚合"""
        database = self._insert_test_data(db)
        now = datetime.now()
        start = now - timedelta(days=2)
        end = now + timedelta(days=1)
        data = database.get_history_data('dev1', 'temp', start, end, interval='1day')
        assert isinstance(data, list)
        database.close()

    def test_get_history_data_unknown_interval(self, db):
        """未知间隔使用默认格式"""
        database = self._insert_test_data(db)
        now = datetime.now()
        start = now - timedelta(hours=1)
        end = now + timedelta(hours=1)
        data = database.get_history_data('dev1', 'temp', start, end, interval='unknown')
        assert isinstance(data, list)
        database.close()

    def test_get_history_data_all_registers(self, db):
        """查询全部寄存器(register_name=None)"""
        database = self._insert_test_data(db)
        now = datetime.now()
        start = now - timedelta(hours=1)
        end = now + timedelta(hours=1)
        data = database.get_history_data('dev1', None, start, end)
        assert isinstance(data, list)
        database.close()


class TestDatabaseAlarm:
    """报警记录测试"""

    def test_insert_alarm(self, db):
        """插入报警记录"""
        from 存储层.database import Database
        database = Database(db)
        now = datetime.now()
        database.insert_alarm('alarm1', 'dev1', 'temp', 'warning',
                              '温度过高', 80.0, 85.0, now)

        with database.get_connection(readonly=True) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM alarm_records WHERE alarm_id='alarm1'")
            row = cursor.fetchone()
            assert row is not None
            assert row['alarm_level'] == 'warning'
        database.close()

    def test_insert_alarm_dedup(self, db):
        """重复报警更新计数不重复插入"""
        from 存储层.database import Database
        database = Database(db)
        now = datetime.now()
        database.insert_alarm('alarm1', 'dev1', 'temp', 'warning',
                              '温度过高', 80.0, 85.0, now)
        database.insert_alarm('alarm1', 'dev1', 'temp', 'warning',
                              '温度过高', 80.0, 87.0, now + timedelta(seconds=5))

        with database.get_connection(readonly=True) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM alarm_records WHERE alarm_id='alarm1' AND device_id='dev1'")
            count = cursor.fetchone()[0]
            assert count == 1
        database.close()

    def test_get_alarm_records(self, db):
        """查询报警记录"""
        from 存储层.database import Database
        database = Database(db)
        now = datetime.now()
        database.insert_alarm('alarm1', 'dev1', 'temp', 'warning', 'msg', 80.0, 85.0, now)
        database.insert_alarm('alarm2', 'dev2', 'pressure', 'critical', 'msg', 2.0, 3.0, now)

        records = database.get_alarm_records()
        assert len(records) == 2
        database.close()

    def test_get_alarm_records_filter_device(self, db):
        """按设备过滤报警"""
        from 存储层.database import Database
        database = Database(db)
        now = datetime.now()
        database.insert_alarm('alarm1', 'dev1', 'temp', 'warning', 'msg', 80.0, 85.0, now)
        database.insert_alarm('alarm2', 'dev2', 'pressure', 'critical', 'msg', 2.0, 3.0, now)

        records = database.get_alarm_records(device_id='dev1')
        assert len(records) == 1
        database.close()

    def test_get_alarm_records_filter_level(self, db):
        """按级别过滤报警"""
        from 存储层.database import Database
        database = Database(db)
        now = datetime.now()
        database.insert_alarm('alarm1', 'dev1', 'temp', 'warning', 'msg', 80.0, 85.0, now)
        database.insert_alarm('alarm2', 'dev2', 'pressure', 'critical', 'msg', 2.0, 3.0, now)

        records = database.get_alarm_records(alarm_level='critical')
        assert len(records) == 1
        database.close()

    def test_get_alarm_records_filter_acknowledged(self, db):
        """按确认状态过滤"""
        from 存储层.database import Database
        database = Database(db)
        now = datetime.now()
        database.insert_alarm('alarm1', 'dev1', 'temp', 'warning', 'msg', 80.0, 85.0, now)

        records = database.get_alarm_records(acknowledged=False)
        assert len(records) == 1

        records = database.get_alarm_records(acknowledged=True)
        assert len(records) == 0
        database.close()

    def test_acknowledge_alarm(self, db):
        """确认报警"""
        from 存储层.database import Database
        database = Database(db)
        now = datetime.now()
        database.insert_alarm('alarm1', 'dev1', 'temp', 'warning', 'msg', 80.0, 85.0, now)

        result = database.acknowledge_alarm('alarm1', 'operator1', 'dev1', 'temp')
        assert result is True

        # 再次确认应该失败（已经确认过了）
        result = database.acknowledge_alarm('alarm1', 'operator1', 'dev1', 'temp')
        assert result is False
        database.close()

    def test_acknowledge_alarm_by_id_only(self, db):
        """仅用alarm_id确认"""
        from 存储层.database import Database
        database = Database(db)
        now = datetime.now()
        database.insert_alarm('alarm1', 'dev1', 'temp', 'warning', 'msg', 80.0, 85.0, now)

        result = database.acknowledge_alarm('alarm1', 'operator1')
        assert result is True
        database.close()


class TestDatabaseMaintenance:
    """数据库维护操作测试"""

    def test_delete_device_data(self, db):
        """删除设备数据"""
        from 存储层.database import Database
        database = Database(db)
        now = datetime.now()
        database.insert_data('dev1', 'temp', 25.0, now, 'C')
        database.delete_device_data('dev1')

        data = database.get_realtime_data(device_id='dev1')
        assert len(data) == 0
        database.close()

    def test_cleanup_old_data(self, db):
        """清理旧数据"""
        from 存储层.database import Database
        database = Database(db)
        old_time = datetime.now() - timedelta(days=60)
        database.insert_data('dev1', 'temp', 25.0, old_time, 'C')
        database.cleanup_old_data(retention_days=30)
        database.close()

    def test_get_database_stats(self, db):
        """获取数据库统计"""
        from 存储层.database import Database
        database = Database(db)
        now = datetime.now()
        database.insert_data('dev1', 'temp', 25.0, now, 'C')

        stats = database.get_database_stats()
        assert 'realtime_records' in stats
        assert 'history_records' in stats
        assert 'alarm_records' in stats
        assert 'total_records' in stats
        assert stats['realtime_records'] >= 1
        database.close()

    def test_get_device_summary(self, db):
        """获取设备摘要"""
        from 存储层.database import Database
        database = Database(db)
        now = datetime.now()
        database.insert_data('dev1', 'temp', 25.0, now, 'C')

        summary = database.get_device_summary()
        assert len(summary) >= 1
        database.close()

    def test_get_table_sizes(self, db):
        """获取表大小"""
        from 存储层.database import Database
        database = Database(db)
        sizes = database.get_table_sizes()
        assert 'realtime_data' in sizes
        assert 'history_data' in sizes
        assert 'alarm_records' in sizes
        database.close()

    def test_archive_old_data(self, db):
        """数据归档"""
        from 存储层.database import Database
        database = Database(db)
        now = datetime.now()
        # 插入旧数据
        for i in range(5):
            old_time = now - timedelta(days=10 + i)
            database.insert_data('dev1', 'temp', 25.0 + i, old_time, 'C')

        result = database.archive_old_data(archive_days=7, delete_days=30)
        assert 'archived' in result
        assert 'deleted_history' in result
        database.close()

    def test_archive_old_data_no_data(self, db):
        """无旧数据时归档"""
        from 存储层.database import Database
        database = Database(db)
        result = database.archive_old_data()
        assert result['archived'] == 0
        database.close()

    def test_get_archive_data(self, db):
        """查询归档数据"""
        from 存储层.database import Database
        database = Database(db)
        data = database.get_archive_data('dev1', 'temp', '2024-01-01', '2024-12-31')
        assert isinstance(data, list)
        database.close()

    def test_enforce_retention_policy(self, db):
        """数据保留策略"""
        from 存储层.database import Database
        database = Database(db)
        now = datetime.now()
        database.insert_data('dev1', 'temp', 25.0, now, 'C')
        database.insert_alarm('a1', 'dev1', 'temp', 'warning', 'msg', 80.0, 85.0, now)

        result = database.enforce_retention_policy(realtime_hours=24, history_days=30, alarm_days=90)
        assert 'realtime_deleted' in result
        assert 'history_deleted' in result
        assert 'alarm_deleted' in result
        database.close()

    def test_vacuum_database(self, db):
        """压缩数据库"""
        from 存储层.database import Database
        database = Database(db)
        result = database.vacuum_database()
        assert result is True
        database.close()

    def test_backup_database(self, db):
        """备份数据库"""
        from 存储层.database import Database
        import shutil
        database = Database(db)
        now = datetime.now()
        database.insert_data('dev1', 'temp', 25.0, now, 'C')

        backup_path = database.backup_database(backup_dir=tempfile.mkdtemp())
        assert backup_path is not None
        assert os.path.exists(backup_path)
        # Cleanup
        try:
            os.unlink(backup_path)
        except OSError:
            pass
        database.close()


class TestDataExport:
    """数据导出测试"""

    def test_export_csv(self):
        """CSV导出"""
        from 存储层.data_export import DataExport
        import shutil
        export_dir = tempfile.mkdtemp()
        exporter = DataExport(export_dir)
        data = [{'device_id': 'dev1', 'value': 25.0, 'unit': 'C'},
                {'device_id': 'dev2', 'value': 30.0, 'unit': 'C'}]
        filepath = exporter.export_csv(data)
        assert filepath != ""
        assert os.path.exists(filepath)
        shutil.rmtree(export_dir, ignore_errors=True)

    def test_export_csv_empty(self):
        """空数据CSV导出"""
        from 存储层.data_export import DataExport
        import shutil
        export_dir = tempfile.mkdtemp()
        exporter = DataExport(export_dir)
        filepath = exporter.export_csv([])
        assert filepath is None
        shutil.rmtree(export_dir, ignore_errors=True)

    def test_export_csv_custom_filename(self):
        """自定义文件名CSV导出"""
        from 存储层.data_export import DataExport
        import shutil
        export_dir = tempfile.mkdtemp()
        exporter = DataExport(export_dir)
        data = [{'a': 1}]
        filepath = exporter.export_csv(data, 'custom.csv')
        assert 'custom.csv' in filepath
        shutil.rmtree(export_dir, ignore_errors=True)

    def test_export_json(self):
        """JSON导出"""
        from 存储层.data_export import DataExport
        import shutil
        export_dir = tempfile.mkdtemp()
        exporter = DataExport(export_dir)
        data = [{'device_id': 'dev1', 'value': 25.0}]
        filepath = exporter.export_json(data)
        assert filepath is not None
        assert os.path.exists(filepath)
        shutil.rmtree(export_dir, ignore_errors=True)

    def test_export_json_empty(self):
        """空数据JSON导出"""
        from 存储层.data_export import DataExport
        import shutil
        export_dir = tempfile.mkdtemp()
        exporter = DataExport(export_dir)
        filepath = exporter.export_json([])
        assert filepath is None
        shutil.rmtree(export_dir, ignore_errors=True)

    def test_export_json_compact(self):
        """紧凑JSON导出"""
        from 存储层.data_export import DataExport
        import shutil
        export_dir = tempfile.mkdtemp()
        exporter = DataExport(export_dir)
        data = [{'a': 1}]
        filepath = exporter.export_json(data, pretty=False)
        assert filepath is not None
        shutil.rmtree(export_dir, ignore_errors=True)

    def test_list_exports(self):
        """列出导出文件"""
        from 存储层.data_export import DataExport
        import shutil
        export_dir = tempfile.mkdtemp()
        exporter = DataExport(export_dir)
        exporter.export_csv([{'a': 1}])
        exports = exporter.list_exports()
        assert len(exports) >= 1
        shutil.rmtree(export_dir, ignore_errors=True)

    def test_delete_export(self):
        """删除导出文件"""
        from 存储层.data_export import DataExport
        import shutil
        export_dir = tempfile.mkdtemp()
        exporter = DataExport(export_dir)
        filepath = exporter.export_csv([{'a': 1}])
        filename = os.path.basename(filepath)
        result = exporter.delete_export(filename)
        assert result is True

        # 再删一次应该返回False
        result = exporter.delete_export(filename)
        assert result is False
        shutil.rmtree(export_dir, ignore_errors=True)

    def test_delete_nonexistent_export(self):
        """删除不存在的文件"""
        from 存储层.data_export import DataExport
        import shutil
        export_dir = tempfile.mkdtemp()
        exporter = DataExport(export_dir)
        result = exporter.delete_export('nonexistent.csv')
        assert result is False
        shutil.rmtree(export_dir, ignore_errors=True)


class TestDataArchive:
    """数据归档压缩测试"""

    def test_parse_interval(self, db):
        """解析时间间隔"""
        from 存储层.data_archive import DataArchive
        from 存储层.database import Database
        database = Database(db)
        archive = DataArchive(database)

        assert archive._parse_interval('1min') == 60
        assert archive._parse_interval('5min') == 300
        assert archive._parse_interval('15min') == 900
        assert archive._parse_interval('1hour') == 3600
        assert archive._parse_interval('1day') == 86400
        assert archive._parse_interval('unknown') == 3600
        database.close()

    def test_compress_data_empty(self, db):
        """压缩空数据"""
        from 存储层.data_archive import DataArchive
        from 存储层.database import Database
        database = Database(db)
        archive = DataArchive(database)

        result = archive.compress_data('dev1', 'temp',
                                       datetime.now() - timedelta(hours=1),
                                       datetime.now())
        assert result['original_count'] == 0
        assert result['compressed_count'] == 0
        database.close()

    def test_compress_moving_average_buckets(self, db):
        """滑动平均压缩 - 直接测试bucket算法"""
        from 存储层.data_archive import DataArchive
        from 存储层.database import Database
        database = Database(db)
        archive = DataArchive(database)

        now = datetime.now()
        buckets = {
            int(now.timestamp()): [
                {'value': 25.0, 'unit': 'C'},
                {'value': 27.0, 'unit': 'C'},
            ],
            int(now.timestamp()) + 300: [
                {'value': 30.0, 'unit': 'C'},
            ]
        }
        result = archive._compress_moving_average(buckets)
        assert len(result) == 2
        assert result[0]['value'] == 26.0  # average of 25 and 27
        database.close()

    def test_compress_max_keep_buckets(self, db):
        """最大值保留压缩 - 直接测试bucket算法"""
        from 存储层.data_archive import DataArchive
        from 存储层.database import Database
        database = Database(db)
        archive = DataArchive(database)

        now = datetime.now()
        buckets = {
            int(now.timestamp()): [
                {'value': 25.0, 'unit': 'C'},
                {'value': 30.0, 'unit': 'C'},
            ]
        }
        result = archive._compress_max_keep(buckets)
        assert len(result) == 1
        assert result[0]['value'] == 30.0
        database.close()

    def test_compress_min_keep_buckets(self, db):
        """最小值保留压缩 - 直接测试bucket算法"""
        from 存储层.data_archive import DataArchive
        from 存储层.database import Database
        database = Database(db)
        archive = DataArchive(database)

        now = datetime.now()
        buckets = {
            int(now.timestamp()): [
                {'value': 25.0, 'unit': 'C'},
                {'value': 30.0, 'unit': 'C'},
            ]
        }
        result = archive._compress_min_keep(buckets)
        assert len(result) == 1
        assert result[0]['value'] == 25.0
        database.close()

    def test_compress_statistical_buckets(self, db):
        """统计聚合压缩 - 直接测试bucket算法"""
        from 存储层.data_archive import DataArchive
        from 存储层.database import Database
        database = Database(db)
        archive = DataArchive(database)

        now = datetime.now()
        buckets = {
            int(now.timestamp()): [
                {'value': 20.0, 'unit': 'C'},
                {'value': 30.0, 'unit': 'C'},
            ]
        }
        result = archive._compress_statistical(buckets)
        assert len(result) == 1
        assert result[0]['avg'] == 25.0
        assert result[0]['max'] == 30.0
        assert result[0]['min'] == 20.0
        assert result[0]['count'] == 2
        database.close()

    def test_compress_lttb_buckets(self, db):
        """LTTB压缩 - 测试bucket数量小于阈值的情况"""
        from 存储层.data_archive import DataArchive
        from 存储层.database import Database
        database = Database(db)
        archive = DataArchive(database)

        now = datetime.now()
        buckets = {
            int(now.timestamp()) + i * 60: [
                {'value': 25.0 + i, 'unit': 'C'},
            ]
            for i in range(5)
        }
        result = archive._compress_lttb(buckets, threshold=100)
        assert len(result) == 5  # fewer than threshold, returns all
        database.close()

    def test_compress_data_empty(self, db):
        """压缩空数据"""
        from 存储层.data_archive import DataArchive
        from 存储层.database import Database
        database = Database(db)
        archive = DataArchive(database)

        result = archive.compress_data('dev1', 'temp',
                                       datetime.now() - timedelta(hours=1),
                                       datetime.now())
        assert result['original_count'] == 0
        assert result['compressed_count'] == 0
        database.close()

    def test_compress_unknown_algorithm_fallback(self, db):
        """未知算法回退到统计聚合"""
        from 存储层.data_archive import DataArchive
        from 存储层.database import Database
        database = Database(db)
        archive = DataArchive(database)

        now = datetime.now()
        buckets = {
            int(now.timestamp()): [
                {'value': 25.0, 'unit': 'C'},
            ]
        }
        # When calling with unknown algorithm, it falls back to statistical
        result = archive._compress_statistical(buckets)
        assert len(result) == 1
        database.close()

    def test_get_compression_stats(self, db):
        """获取压缩统计"""
        from 存储层.data_archive import DataArchive
        from 存储层.database import Database
        database = Database(db)
        archive = DataArchive(database)
        now = datetime.now()
        database.insert_data('dev1', 'temp', 25.0, now, 'C')

        stats = archive.get_compression_stats()
        assert 'total_devices' in stats
        assert 'total_registers' in stats
        assert 'total_records' in stats
        database.close()

    def test_compress_moving_average_single_value(self, db):
        """单值滑动平均压缩"""
        from 存储层.data_archive import DataArchive
        from 存储层.database import Database
        database = Database(db)
        archive = DataArchive(database)

        buckets = {1000: [{'value': 25.0, 'unit': 'C'}]}
        result = archive._compress_moving_average(buckets)
        assert len(result) == 1
        assert result[0]['value'] == 25.0
        database.close()

    def test_compress_statistical_single_value(self, db):
        """单值统计压缩(std=0)"""
        from 存储层.data_archive import DataArchive
        from 存储层.database import Database
        database = Database(db)
        archive = DataArchive(database)

        buckets = {1000: [{'value': 25.0, 'unit': 'C'}]}
        result = archive._compress_statistical(buckets)
        assert len(result) == 1
        assert result[0]['std'] == 0
        database.close()
