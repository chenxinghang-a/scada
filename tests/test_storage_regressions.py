# -*- coding: utf-8 -*-
"""存储层 2026-09 缺陷修复的回归测试（缺陷 1~7）。

一个用例对应一条审计结论，全部使用**真实 SQLite 库**（不用 MagicMock）——
这批缺陷的共同点是"mock 看不出来"：字段名对不上、表名对不上、
`DELETE ... WHERE timestamp < '2026-08-21T10:00:00'` 这种文本比较错判，
只有在真库上跑才暴露。

| 用例 | 对应缺陷 |
|---|---|
| ``test_compress_data_*`` | 1. ``compress_data`` 取错字段（必抛 AttributeError） |
| ``test_all_archive_paths_write_single_table`` | 2. 归档表名三套不一致 |
| ``test_archive_data_reports_real_deleted_count`` 等 | 3. ``deleted_from_main`` 恒 None |
| ``test_enforce_retention_policy_*`` | 4. 保留策略会清空 realtime_data |
| ``test_migration_*`` | 5. 迁移读不存在的表（必然 0 行） |
| ``test_unwired_modules_*`` | 6. 零引用模块的未接线标注 |
| ``test_bloat_*`` | 7. 膨胀无自动检测 |
"""
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta

import pytest

from 存储层.data_archive import DataArchive
from 存储层.data_lifecycle import DataLifecycleManager, RetentionPolicy
from 存储层.database import (
    ARCHIVE_TABLE,
    BLOAT_FREE_PAGE_RATIO_THRESHOLD,
    BLOAT_WARN_INTERVAL_SECONDS,
    Database,
)
from timeseries.migration import SQLiteToTDengineMigrator


@pytest.fixture
def db(tmp_path):
    """真实库（每次独立文件，避免互相污染）"""
    return Database(str(tmp_path / 'regression.db'))


def _insert_history(db, rows):
    """rows: [(device_id, register_name, value, days_ago)]，直接写 history_data"""
    now = datetime.now()
    with db.get_connection() as conn:
        conn.executemany(
            'INSERT INTO history_data (device_id, register_name, value, unit, timestamp) '
            'VALUES (?, ?, ?, ?, ?)',
            [(d, r, v, '', (now - timedelta(days=age)).isoformat(sep=' '))
             for d, r, v, age in rows],
        )


def _count(db, table, where=''):
    with db.get_connection(readonly=True) as conn:
        return conn.execute(f'SELECT COUNT(*) FROM {table} {where}').fetchone()[0]


def _archive_tables(db):
    with db.get_connection(readonly=True) as conn:
        return sorted(
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%archive%'"
            )
        )


# ============================================================
# 缺陷 1：compress_data 取错字段 -> 压缩结果必为空
# ============================================================

class TestCompressDataRegression:
    """get_history_data 返回的是聚合桶 {time_bucket, avg_value, sample_count}，
    而 compress_data 原先按 {timestamp, value} 取值 —— 取到 None 后
    `None.timestamp()` 直接抛 AttributeError，等于功能不存在。"""

    def _seed_minutes(self, db, count=180):
        now = datetime.now()
        for i in range(count):
            # 每 3 分钟一个值，值域稳定可预期：20 + (i // 60)
            db.insert_data('dev1', 'temp', 20.0 + (i // 60), now - timedelta(minutes=i), 'C')

    def test_compress_data_returns_real_buckets(self, db):
        self._seed_minutes(db)
        now = datetime.now()

        result = DataArchive(db).compress_data(
            'dev1', 'temp', now - timedelta(hours=4), now,
            interval='1hour', algorithm='statistical',
        )

        assert result['original_count'] == 180, '应按 1min 桶取到 180 条'
        assert result['compressed_count'] > 0, (
            '压缩结果为空 —— 这就是取错字段的旧行为（修复前必抛 AttributeError）'
        )
        assert result['compressed_count'] < result['original_count']
        assert result['data'], 'data 不能为空'
        for point in result['data']:
            # 每条都必须是"有时间戳、有值"的完整点，不能是 None
            assert point['timestamp'], f'时间戳丢了: {point!r}'
            assert datetime.fromisoformat(point['timestamp']) is not None
            assert point['avg'] is not None and 20.0 <= point['avg'] <= 22.0
            assert point['count'] >= 1

    @pytest.mark.parametrize('algorithm', ['moving_average', 'max_keep', 'min_keep', 'lttb'])
    def test_compress_data_all_algorithms_work(self, db, algorithm):
        """五种算法都走归一化后的数据，不能只有 statistical 能跑"""
        self._seed_minutes(db, count=60)
        now = datetime.now()

        result = DataArchive(db).compress_data(
            'dev1', 'temp', now - timedelta(hours=2), now,
            interval='1hour', algorithm=algorithm,
        )

        assert result['compressed_count'] > 0
        assert all(p['value'] is not None for p in result['data'])

    def test_compress_data_empty_range(self, db):
        now = datetime.now()
        result = DataArchive(db).compress_data(
            'nobody', 'nothing', now - timedelta(hours=1), now)
        assert result['original_count'] == 0
        assert result['data'] == []

    def test_normalize_handles_raw_rows_too(self):
        """归一化同时兼容原始采样行（老调用方/单测传 timestamp/value）"""
        rows = [
            {'timestamp': '2024-01-01T00:00:00', 'value': 10.0, 'unit': 'C'},
            {'time_bucket': '2024-01-01 00:01:00', 'avg_value': 20.0, 'sample_count': 3},
            {'time_bucket': None, 'avg_value': 1.0},          # 缺时间 -> 丢弃
            {'time_bucket': '2024-01-01 00:02:00', 'avg_value': None},  # 缺值 -> 丢弃
        ]
        out = DataArchive._normalize_history_rows(rows)
        assert len(out) == 2
        assert out[0]['value'] == 10.0
        assert out[1]['count'] == 3


# ============================================================
# 缺陷 2/3：归档表名统一 + 归档返回值
# ============================================================

class TestArchiveUnification:
    def test_all_archive_paths_write_single_table(self, db, tmp_path):
        """三条归档路径必须都落到 ARCHIVE_TABLE，不得再出现第二张归档表"""
        _insert_history(db, [
            ('dev1', 'temp', 1.0, 400),   # 归档 + 删除
            ('dev1', 'temp', 2.0, 10),    # 归档，保留原始行
        ])

        # 路径 1：DataArchive.archive_data（原先写 history_data_archive）
        DataArchive(db).archive_data(retention_days=30)

        # 路径 2：DataLifecycleManager（原先写 {table}_archive）
        mgr = DataLifecycleManager(str(tmp_path / 'regression.db'))
        mgr.policies = {
            'history_data': RetentionPolicy('历史数据', 'history_data', 90,
                                            archive_enabled=True, archive_days=7)
        }
        lifecycle = mgr.execute_lifecycle(database=db)
        action = lifecycle['actions'][0]
        assert 'error' not in action, f'生命周期归档报错: {action}'
        assert action['archive_table'] == ARCHIVE_TABLE
        assert mgr.resolve_archive_table('history_data') == ARCHIVE_TABLE
        # 未显式配置时，非 history_data 表仍沿用 {table}_archive
        assert mgr.resolve_archive_table('alarm_records') == 'alarm_records_archive'

        # 路径 3：Database.archive_old_data（唯一的规范实现）
        db.archive_old_data(archive_days=7, delete_days=30)

        assert _archive_tables(db) == [ARCHIVE_TABLE], (
            f'归档散落到了多张表: {_archive_tables(db)}'
        )
        assert _count(db, ARCHIVE_TABLE) >= 1, '归档聚合行没写进去'
        # 规范实现能被读出来
        archived = db.get_archive_data('dev1', 'temp', '2000-01-01', '2100-01-01')
        assert archived, 'get_archive_data 读不到归档（表名/结构不一致）'
        assert set(archived[0]) >= {'device_id', 'register_name', 'avg_value',
                                    'sample_count', 'archive_date'}

    def test_archive_data_reports_real_deleted_count(self, db):
        """缺陷 3：deleted_from_main 原先取的是 cleanup_old_data 的返回值，
        而那个方法没有 return -> 恒为 None。"""
        _insert_history(db, [('dev1', 'temp', float(i), 60 + i) for i in range(4)])

        result = DataArchive(db).archive_data(retention_days=30)

        assert result['deleted_from_main'] == 4, (
            f"应删除 4 条过期行，实际 {result['deleted_from_main']!r}"
        )
        assert isinstance(result['deleted_from_main'], int)
        assert result['moved_to_archive'] >= 1
        assert result['archive_table'] == ARCHIVE_TABLE
        assert _count(db, 'history_data') == 0

    def test_cleanup_old_data_returns_deleted_count(self, db):
        _insert_history(db, [('dev1', 'temp', 1.0, 60), ('dev1', 'temp', 2.0, 45),
                             ('dev1', 'temp', 3.0, 2)])

        assert db.cleanup_old_data(retention_days=30) == 2
        assert _count(db, 'history_data') == 1
        # 空库/无过期数据时返回 0 而不是 None
        assert db.cleanup_old_data(retention_days=30) == 0


# ============================================================
# 缺陷 4：realtime_data 保留策略（统一结论：不按时间清理）
# ============================================================

class TestRealtimeRetentionPolicy:
    def test_enforce_retention_policy_keeps_stale_realtime(self, db):
        """停机设备/慢采样点位的"当前值"必须留着 —— 它没有归档、删了就没了。"""
        now = datetime.now()
        db.insert_data('dev1', 'temp', 25.0, now - timedelta(hours=48), 'C')
        # insert_data 会同时写 history_data，所以历史上共 3 条：48h（留）、1d（留）、60d（删）
        _insert_history(db, [('dev1', 'temp', 1.0, 60), ('dev1', 'temp', 2.0, 1)])
        db.insert_alarm('a1', 'dev1', 'temp', 'warning', 'old', 80.0, 85.0,
                        now - timedelta(days=200))

        result = db.enforce_retention_policy(history_days=30, alarm_days=90)

        assert _count(db, 'realtime_data') == 1, '最新值缓存被清空（设备列表当前值会集体消失）'
        latest = db.get_latest_data('dev1', 'temp')
        assert latest is not None and latest['value'] == 25.0
        assert _count(db, 'history_data') == 2, '保留期内（48h/1d）的历史数据被误删'
        assert _count(db, 'alarm_records') == 0
        assert result == {'history_deleted': 1, 'alarm_deleted': 1}
        assert 'realtime_deleted' not in result, (
            '不该再返回恒为 0 的 realtime_deleted —— 会让人误以为"确实清理过"'
        )

    def test_cleanup_old_data_keeps_realtime(self, db):
        now = datetime.now()
        db.insert_data('dev1', 'temp', 30.0, now - timedelta(days=90), 'C')
        _insert_history(db, [('dev1', 'temp', 1.0, 90)])

        db.cleanup_old_data(retention_days=30)

        assert _count(db, 'realtime_data') == 1
        assert _count(db, 'history_data') == 0

    def test_archive_old_data_keeps_realtime(self, db):
        now = datetime.now()
        db.insert_data('dev1', 'temp', 30.0, now - timedelta(days=400), 'C')
        _insert_history(db, [('dev1', 'temp', 1.0, 400)])

        result = db.archive_old_data(archive_days=7, delete_days=30)

        assert result['deleted_realtime'] == 0
        assert _count(db, 'realtime_data') == 1
        assert _count(db, 'history_data') == 0


# ============================================================
# 缺陷 5：SQLite -> TDengine 迁移读的是不存在的表
# ============================================================

class _FakeTDengine:
    """TDengine 替身：只记录写入，不做真实连接"""

    def __init__(self, queriable=True):
        self.telemetry = []
        self.alarms = []
        self.queriable = queriable

    def connect(self):
        return True

    def init_tables(self):
        pass

    def write_telemetry_batch(self, records):
        self.telemetry.extend(records)

    def write_alarm(self, record):
        self.alarms.append(record)

    def _execute_sql(self, sql):
        if not self.queriable:
            return None
        if 'device_telemetry' in sql:
            return {'code': 0, 'data': [{'column_meta': [['count(*)', 'BIGINT', 8]],
                                         'data': [[len(self.telemetry)]]}]}
        return {'code': 0, 'data': [{'column_meta': [['count(*)', 'BIGINT', 8]],
                                     'data': [[len(self.alarms)]]}]}


@pytest.fixture
def seeded_db(tmp_path):
    path = str(tmp_path / 'migrate_src.db')
    database = Database(path)
    now = datetime.now()
    for i in range(5):
        database.insert_data('dev1', 'temp', 20.0 + i, now - timedelta(minutes=i), 'C')
    database.insert_alarm('a1', 'dev1', 'temp', 'warning', 'over temp', 80.0, 85.0, now)
    return path


class TestMigrationRegression:
    """原先 SELECT ... FROM telemetry / alarms —— 这两张表不存在，
    查询异常被 catch 掉，统计恒为 0（伪实现）。"""

    def test_migrate_all_reads_history_and_alarm_tables(self, seeded_db):
        client = _FakeTDengine()
        stats = SQLiteToTDengineMigrator(seeded_db, client).migrate_all(batch_size=2)

        assert stats['errors'] == 0, f'迁移过程报错: {stats}'
        assert stats['telemetry_migrated'] == 5, (
            f"遥测一条都没迁过去（表名不匹配的旧行为），实际 {stats['telemetry_migrated']}"
        )
        assert stats['alarms_migrated'] == 1
        assert {r.device_id for r in client.telemetry} == {'dev1'}
        assert all(r.quality == 192 for r in client.telemetry), 'history_data 无质量码，应回填 GOOD'
        assert client.alarms[0].alarm_id == 'a1'
        assert client.alarms[0].level == 'warning'

    def test_verify_migration_counts_match(self, seeded_db):
        client = _FakeTDengine()
        migrator = SQLiteToTDengineMigrator(seeded_db, client)
        migrator.migrate_all(batch_size=10)

        migrator.connect_sqlite()
        try:
            results = migrator.verify_migration()
        finally:
            migrator.disconnect_sqlite()

        assert results['telemetry']['sqlite_count'] == 5
        assert results['telemetry']['tdengine_count'] == 5
        assert results['telemetry']['match'] is True
        assert results['alarms']['match'] is True

    def test_verify_migration_reports_unverified_when_not_queryable(self, seeded_db):
        """查不到 TDengine 侧行数时必须是"未校验(None)"，不能伪装成"不一致(False)" """
        migrator = SQLiteToTDengineMigrator(seeded_db, _FakeTDengine(queriable=False))
        migrator.connect_sqlite()
        try:
            results = migrator.verify_migration()
        finally:
            migrator.disconnect_sqlite()

        assert results['telemetry']['sqlite_count'] == 5
        assert results['telemetry']['tdengine_count'] is None
        assert results['telemetry']['match'] is None

    def test_migrate_all_on_empty_db_skips_gracefully(self, tmp_path):
        """源库没有 history_data/alarm_records 时应跳过而不是炸掉"""
        empty = tmp_path / 'empty.db'
        sqlite3.connect(str(empty)).close()

        stats = SQLiteToTDengineMigrator(str(empty), _FakeTDengine()).migrate_all()

        assert stats['errors'] == 0
        assert stats['telemetry_migrated'] == 0


# ============================================================
# 缺陷 6：零引用模块的处置（标注未接线 + 行为固化）
# ============================================================

class TestUnwiredModules:
    def test_modules_are_marked_unwired(self):
        from 存储层 import data_consistency, data_lifecycle, data_lineage

        for module in (data_consistency, data_lifecycle, data_lineage):
            assert module.WIRED is False, (
                f'{module.__name__} 声称已接线，请同步更新本测试与模块 docstring'
            )
            assert '未接线' in (module.__doc__ or ''), (
                f'{module.__name__} 缺少"未接线"标注 —— 零引用模块必须显式说明状态'
            )

    def test_consistency_checker_reports_real_schema(self, db):
        """行为固化：能跑通、能报出结论（未接线不等于坏掉）"""
        from 存储层.data_consistency import ConsistencyChecker

        db.insert_data('dev1', 'temp', 25.0, datetime.now(), 'C')
        report = ConsistencyChecker(db.db_path).generate_report()

        assert report['summary']['status'] in ('healthy', 'issues_found')
        names = {c['check_name'] for c in report['details']}
        assert names == {'table_integrity', 'data_formats', 'duplicates',
                         'orphaned_records', 'timestamps'}
        # 存储层自己不建 users 表（由用户层建），因此缺表是可预期的结论之一
        assert any('users' in issue for issue in report['details'][0]['issues'])

    def test_lineage_tracker_graph_is_consistent(self):
        """行为固化：节点/边/报告这些公开 API 保持可用。

        注意这里**不调用** ``get_upstream``/``get_downstream`` —— 它们曾经
        会死锁，放在本用例里会让"回归"表现为整个会话挂住而不是一条红色用例。
        那两个方法由下面的超时守卫用例专门覆盖。
        """
        from 存储层.data_lineage import DataLineageTracker

        tracker = DataLineageTracker()
        tracker.track_data_flow('dev1', 'temp', 'modbus_source', 'history_table')
        graph = tracker.get_lineage_graph()

        assert graph['node_count'] >= 10
        assert any(e['target_id'] == 'history_table' for e in graph['edges'])
        report = tracker.generate_report()
        assert report['summary']['total_edges'] == len(graph['edges'])
        assert report['summary']['node_types']['source'] >= 1

    def test_lineage_dfs_does_not_deadlock(self):
        """回归（本次新增发现）：``get_upstream``/``get_downstream`` 原先在
        ``with self._lock:`` 里递归，而 ``self._lock`` 是不可重入的
        ``threading.Lock`` —— 同线程第二层递归再取锁就永久阻塞。
        只要图里存在一条边，这两个公开 API 就会**死锁**（调用方卡死、
        无异常、无日志），这也是本模块"零引用"迟迟没被发现的可能原因之一。

        用带超时的线程跑：死锁时测试失败而不是把整个测试会话挂住。
        """
        import threading

        from 存储层.data_lineage import DataLineageTracker

        tracker = DataLineageTracker()
        tracker.track_data_flow('dev1', 'temp', 'modbus_source', 'history_table')

        out = {}

        def _run():
            out['down'] = tracker.get_downstream('modbus_source:dev1')
            out['up'] = tracker.get_upstream('history_table')
            out['impact'] = tracker.get_impact_analysis('modbus_source:dev1')

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()
        worker.join(timeout=5)

        assert not worker.is_alive(), (
            'get_downstream/get_upstream 卡死 —— 持锁递归导致的死锁回归了'
        )
        assert out['down'] == ['modbus_source:dev1', 'history_table']
        assert out['up'] == ['history_table', 'modbus_source:dev1']
        assert out['impact']['affected_count'] >= 1


# ============================================================
# 缺陷 7：膨胀自动检测（空闲页占比）
# ============================================================

class TestBloatDetection:
    @staticmethod
    def _make_bloat(db, rows=5000):
        """制造"删了大量数据但文件不缩"的膨胀：free_page_ratio 会飙高"""
        now = datetime.now()
        db.insert_data_batch([
            {'device_id': 'dev2', 'register_name': f'r{i}', 'value': 1.0,
             'timestamp': now, 'unit': ''}
            for i in range(rows)
        ])
        with db.get_connection() as conn:
            conn.execute('DELETE FROM history_data')
            conn.execute("DELETE FROM realtime_data WHERE device_id = 'dev2'")

    @staticmethod
    def _bloat_diag(db) -> str:
        """把「判定膨胀」用到的全部输入打出来，供断言失败时排障。

        为什么需要：这三条用例在 **CI 上红、本机全绿**，而远端唯一公开可读的
        排障通道是 annotation（job log 需管理员权限，403）—— annotation 只包含
        断言消息。没有数值就只能猜（上一轮我就猜错了方向：以为 `check_bloat` 的
        quick 口径与 dbstat 口径不一致，实际上 `free_page_ratio` 两条路径都来自
        `PRAGMA freelist_count`，`include_dbstats` 只影响 `data_bytes_mb`）。
        把数值写进消息，下一次 CI 的 annotation 直接给出根因。
        """
        quick = db.get_fragmentation_stats(include_dbstats=False)
        full = db.get_fragmentation_stats(include_dbstats=True)
        lg = logging.getLogger('存储层.database')
        return (
            f'\n  quick: free_page_ratio={quick["free_page_ratio"]} '
            f'freelist={quick["free_page_count"]} page_count={quick["page_count"]} '
            f'is_bloated={quick["is_bloated"]}'
            f'\n  full : free_page_ratio={full["free_page_ratio"]} '
            f'data_bytes_mb={full["data_bytes_mb"]} '
            f'size_to_data_ratio={full["size_to_data_ratio"]}'
            f'\n  阈值={BLOAT_FREE_PAGE_RATIO_THRESHOLD}'
            f'\n  logger: effective_level='
            f'{logging.getLevelName(lg.getEffectiveLevel())} '
            f'propagate={lg.propagate} manager.disable={logging.root.manager.disable}'
            f'\n  节流: _last_bloat_warn_at={getattr(db, "_last_bloat_warn_at", None)} '
            f'now={time.monotonic():.0f} 间隔={BLOAT_WARN_INTERVAL_SECONDS}'
        )

    def test_healthy_db_is_not_bloated(self, db):
        db.insert_data('dev1', 'temp', 25.0, datetime.now(), 'C')

        stats = db.get_fragmentation_stats()

        assert stats['free_page_ratio'] == 0.0
        assert stats['is_bloated'] is False
        assert stats['page_count'] > 0
        # WAL 模式下刚写入的数据可能还在 -wal 文件里，所以看"主库+WAL"的总占用
        assert stats['total_size_mb'] >= stats['database_size_mb']
        assert stats['total_size_mb'] > 0

    def test_bloated_db_is_detected_and_warned(self, db, caplog):
        self._make_bloat(db)
        # checkpoint 让数据落进主库文件，便于"文件大小 vs 实际数据量"对比
        db.wal_checkpoint()
        # 节流是实例级的，但显式归零可以让本用例不依赖"这个实例此前没告警过"
        db._last_bloat_warn_at = 0.0

        # 测量与告警都放进同一个 caplog 上下文 —— 原先 `db.check_bloat()` 写在
        # `with` 块**外面**，`caplog.at_level` 已经失效，是否捕获取决于当时
        # logger 的生效级别（会被别的测试污染）。
        with caplog.at_level(logging.WARNING, logger='存储层.database'):
            stats = db.get_fragmentation_stats()
            db.check_bloat()

        assert stats['free_page_ratio'] >= BLOAT_FREE_PAGE_RATIO_THRESHOLD, (
            f"构造的膨胀没被量化: {stats}{self._bloat_diag(db)}"
        )
        assert stats['is_bloated'] is True
        assert stats['free_page_count'] > 0
        assert stats['free_bytes_mb'] > 0
        # 文件大小 vs 实际数据量：真实数据远小于磁盘占用
        if stats['data_bytes_mb'] is not None:
            assert stats['data_bytes_mb'] < stats['total_size_mb']
            assert stats['size_to_data_ratio'] is not None
            assert stats['size_to_data_ratio'] > 1

        # 告警必须由 check_bloat 触发
        assert any('膨胀' in rec.message for rec in caplog.records), (
            '空闲页占比过高却没有告警日志 —— 缺陷 7 的核心就是"没有自动检测"'
            f'{self._bloat_diag(db)}'
            f'\n  caplog 记录数={len(caplog.records)}'
        )

    def test_bloat_warning_is_throttled(self, db, caplog):
        """告警必须节流：get_database_stats 会被健康检查/WebSocket 每 10 秒调一次，
        不节流就会用同一条 warning 淹没日志（那正是当初没发现的另一面）。"""
        self._make_bloat(db)
        db._last_bloat_warn_at = 0.0

        with caplog.at_level(logging.WARNING, logger='存储层.database'):
            for _ in range(3):
                db.check_bloat()

        warnings = [rec for rec in caplog.records if '膨胀' in rec.message]
        assert len(warnings) == 1, (
            f'同一实例重复告警了 {len(warnings)} 次'
            f'{self._bloat_diag(db)}'
            f'\n  caplog 记录数={len(caplog.records)}'
        )

    def test_get_database_stats_exposes_free_page_ratio(self, db, caplog):
        self._make_bloat(db)
        db._last_bloat_warn_at = 0.0

        with caplog.at_level(logging.WARNING, logger='存储层.database'):
            stats = db.get_database_stats()

        assert 'free_page_ratio' in stats
        assert 'database_size_mb' in stats
        assert 'free_bytes_mb' in stats
        assert stats['free_page_ratio'] >= BLOAT_FREE_PAGE_RATIO_THRESHOLD, (
            f'get_database_stats 报的膨胀指标没到阈值{self._bloat_diag(db)}'
        )
        assert stats['is_bloated'] is True
        assert any('膨胀' in rec.message for rec in caplog.records), (
            'get_database_stats 是给运维/健康检查看的入口，必须顺带告警'
            f'{self._bloat_diag(db)}'
            f'\n  caplog 记录数={len(caplog.records)}'
        )

    def test_check_bloat_on_empty_db_is_safe(self, tmp_path):
        """页面统计取不到时不应抛异常（例如库刚建好还没写过）"""
        empty = tmp_path / 'blank.db'
        empty.touch()
        stats = Database(str(empty)).get_fragmentation_stats()
        assert stats['is_bloated'] is False
