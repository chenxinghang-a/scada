"""
性能压力测试
验证系统在高负载下的稳定性
"""
import gc
import os
import tempfile
import time
import threading
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture
def db():
    """独立的测试数据库（真实 Database 实例，临时文件隔离）

    conftest 中的 `db` fixture 返回的是数据库文件路径（供 Database(path) 使用），
    本文件的用例需要的是带 SCADA 表结构/索引的 Database 实例，因此在此覆盖。
    """
    from 存储层.database import Database

    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    database = Database(path)
    try:
        yield database
    finally:
        database.close()
        gc.collect()
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(path + suffix)
            except OSError:
                pass


class TestDatabasePerformance:
    """数据库性能测试"""

    def test_bulk_insert_performance(self, db):
        """测试批量插入性能"""
        start = time.time()

        # 插入1000条记录（同一事务内批量提交）
        with db.get_connection() as conn:
            cursor = conn.cursor()
            for i in range(1000):
                cursor.execute('''
                    INSERT INTO history_data (device_id, register_name, value, timestamp)
                    VALUES (?, ?, ?, datetime('now'))
                ''', (f'device_{i % 10}', f'register_{i % 5}', float(i)))

        elapsed = time.time() - start

        # 1000条插入应在1秒内完成（本机实测约5ms，余量约200倍，非脆弱阈值）
        assert elapsed < 1.0, f"批量插入耗时 {elapsed:.2f}s，超过1秒"

        # 数据完整性：1000条必须全部落库，不能为了跑得快而丢数据
        with db.get_connection(readonly=True) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM history_data')
            assert cursor.fetchone()[0] == 1000

    def test_query_performance_with_index(self, db):
        """测试索引查询性能"""
        # 插入测试数据
        with db.get_connection() as conn:
            cursor = conn.cursor()
            for i in range(1000):
                cursor.execute('''
                    INSERT INTO history_data (device_id, register_name, value, timestamp)
                    VALUES (?, ?, ?, datetime('now', ?))
                ''', (f'device_{i % 10}', f'register_{i % 5}', float(i), f'-{i} seconds'))

        query = '''
            SELECT * FROM history_data
            WHERE device_id = ? AND register_name = ?
            ORDER BY timestamp DESC LIMIT 100
        '''
        params = ('device_1', 'register_1')

        # 关键断言（确定性）：查询必须走复合索引，而不是退化成全表扫描。
        # 原用例只断言"10ms 内完成"，即使索引缺失/未生效也可能因数据量小而通过，
        # 属于既脆弱又测不到索引的墙钟断言。
        with db.get_connection(readonly=True) as conn:
            cursor = conn.cursor()
            cursor.execute(f'EXPLAIN QUERY PLAN {query}', params)
            plan = ' '.join(str(row[3]) for row in cursor.fetchall())
        assert 'idx_history_device_register_time' in plan, f"未使用复合索引: {plan}"

        # 结果正确性 + 宽松的墙钟护栏（实测约0.2ms，50ms 仅用于捕捉数量级退化）
        start = time.time()
        with db.get_connection(readonly=True) as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            results = cursor.fetchall()
        elapsed = time.time() - start

        assert len(results) == 100
        assert elapsed < 0.05, f"索引查询耗时 {elapsed*1000:.2f}ms，超过50ms"

    def test_concurrent_read_write(self, db):
        """测试并发读写性能"""
        results = []
        errors = []

        def writer():
            try:
                for i in range(100):
                    with db.get_connection() as conn:
                        cursor = conn.cursor()
                        cursor.execute('''
                            INSERT INTO history_data (device_id, register_name, value, timestamp)
                            VALUES (?, ?, ?, datetime('now'))
                        ''', ('concurrent_device', f'register_{i}', float(i)))
            except Exception as e:
                errors.append(e)
            finally:
                # 关闭本线程的 thread-local 连接
                db.close()

        def reader():
            try:
                for i in range(100):
                    with db.get_connection(readonly=True) as conn:
                        cursor = conn.cursor()
                        cursor.execute('SELECT COUNT(*) FROM history_data')
                        count = cursor.fetchone()[0]
                        results.append(count)
            except Exception as e:
                errors.append(e)
            finally:
                db.close()

        # 启动并发线程（Database 使用 thread-local 连接，每个线程独立连接，
        # WAL 模式下读写可真正并发）
        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # 验证无死锁
        assert not any(t.is_alive() for t in threads), "并发线程超时未结束（可能死锁）"
        # 验证无错误
        assert len(errors) == 0, f"并发测试错误: {errors}"
        assert len(results) == 100

        # 验证写入无丢失：并发写入的100条记录应全部落库
        with db.get_connection(readonly=True) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM history_data WHERE device_id = 'concurrent_device'"
            )
            assert cursor.fetchone()[0] == 100


class TestConnectionPoolPerformance:
    """连接池性能测试"""

    def test_connection_reuse(self):
        """测试连接复用性能"""
        from core.connection_pool import ConnectionPool

        call_count = 0
        def factory(key):
            nonlocal call_count
            call_count += 1
            return MagicMock()

        pool = ConnectionPool(factory, max_size=10)

        # 获取并释放连接多次
        start = time.time()
        for i in range(100):
            conn = pool.acquire(f'device_{i % 10}')
            pool.release(conn)

        elapsed = time.time() - start

        # 100次获取/释放应在1秒内完成
        assert elapsed < 1.0, f"连接池操作耗时 {elapsed:.2f}s"
        # 连接应该被复用，不应该创建100个
        assert call_count <= 10, f"创建了 {call_count} 个连接，预期最多10个"


class TestDataCollectorPerformance:
    """数据采集器性能测试"""

    def test_batch_processing_performance(self):
        """测试批处理性能"""
        from 采集层.data_collector import DiskBackedQueue
        import tempfile
        import os

        with tempfile.TemporaryDirectory() as tmpdir:
            queue = DiskBackedQueue(maxsize=10000, persist_dir=tmpdir)

            # 批量入队
            start = time.time()
            for i in range(1000):
                queue.put({
                    'device_id': f'device_{i % 10}',
                    'register_name': f'register_{i % 5}',
                    'value': float(i),
                    'timestamp': time.time()
                })
            elapsed_put = time.time() - start

            # 批量出队
            start = time.time()
            while not queue.empty():
                queue.get_nowait()
            elapsed_get = time.time() - start

            # 性能断言
            assert elapsed_put < 1.0, f"批量入队耗时 {elapsed_put:.2f}s"
            assert elapsed_get < 1.0, f"批量出队耗时 {elapsed_get:.2f}s"


class TestAlarmPerformance:
    """报警系统性能测试"""

    def test_alarm_check_performance(self, tmp_path):
        """测试报警检查性能"""
        from 报警层.alarm_manager import AlarmManager

        db = MagicMock()
        db.get_alarm_records.return_value = []
        db.get_active_alarms.return_value = []

        # 临时配置文件，避免测试写入仓库内的 配置/alarms.yaml
        manager = AlarmManager(db, config_path=str(tmp_path / 'alarms_test.yaml'))

        # 添加100条规则（必须走 add_rule：直接改 manager.rules 不会重建规则索引，
        # check_alarm 会因索引未命中直接返回，性能断言会变成空转）
        for i in range(100):
            manager.add_rule({
                'id': f'rule_{i}',
                'name': f'Rule {i}',
                'device_id': f'device_{i % 10}',
                'register_name': f'register_{i % 5}',
                'condition': 'greater_than',
                'threshold': 50.0,
                'level': 'warning',
                'enabled': True,
            })

        # 批量检查
        start = time.time()
        for i in range(100):
            manager.check_alarm(
                device_id=f'device_{i % 10}',
                register_name=f'register_{i % 5}',
                value=60.0,
                timestamp=datetime.now()
            )
        elapsed = time.time() - start

        # 确认检查真的命中了规则并触发报警，否则耗时断言无意义
        assert manager.alarm_states, "报警检查未命中任何规则，性能断言无意义"
        # 100次检查应在1秒内完成
        assert elapsed < 1.0, f"报警检查耗时 {elapsed:.2f}s"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
