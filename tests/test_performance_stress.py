"""
性能压力测试
验证系统在高负载下的稳定性
"""
import pytest
import time
import threading
from unittest.mock import patch, MagicMock


class TestDatabasePerformance:
    """数据库性能测试"""

    def test_bulk_insert_performance(self, db):
        """测试批量插入性能"""
        cursor = db.cursor()
        start = time.time()

        # 插入1000条记录
        for i in range(1000):
            cursor.execute('''
                INSERT INTO history_data (device_id, register_name, value, timestamp)
                VALUES (?, ?, ?, datetime('now'))
            ''', (f'device_{i % 10}', f'register_{i % 5}', float(i)))

        db.commit()
        elapsed = time.time() - start

        # 1000条插入应在1秒内完成
        assert elapsed < 1.0, f"批量插入耗时 {elapsed:.2f}s，超过1秒"

    def test_query_performance_with_index(self, db):
        """测试索引查询性能"""
        cursor = db.cursor()

        # 插入测试数据
        for i in range(1000):
            cursor.execute('''
                INSERT INTO history_data (device_id, register_name, value, timestamp)
                VALUES (?, ?, ?, datetime('now', ?))
            ''', (f'device_{i % 10}', f'register_{i % 5}', float(i), f'-{i} seconds'))

        db.commit()

        # 测试索引查询
        start = time.time()
        cursor.execute('''
            SELECT * FROM history_data
            WHERE device_id = ? AND register_name = ?
            ORDER BY timestamp DESC LIMIT 100
        ''', ('device_1', 'register_1'))
        results = cursor.fetchall()
        elapsed = time.time() - start

        # 索引查询应在10ms内完成
        assert elapsed < 0.01, f"索引查询耗时 {elapsed*1000:.2f}ms，超过10ms"
        assert len(results) > 0

    def test_concurrent_read_write(self, db):
        """测试并发读写性能"""
        results = []
        errors = []

        def writer():
            try:
                for i in range(100):
                    cursor = db.cursor()
                    cursor.execute('''
                        INSERT INTO history_data (device_id, register_name, value, timestamp)
                        VALUES (?, ?, ?, datetime('now'))
                    ''', (f'concurrent_device', f'register_{i}', float(i)))
                    db.commit()
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for i in range(100):
                    cursor = db.cursor()
                    cursor.execute('SELECT COUNT(*) FROM history_data')
                    count = cursor.fetchone()[0]
                    results.append(count)
            except Exception as e:
                errors.append(e)

        # 启动并发线程
        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # 验证无错误
        assert len(errors) == 0, f"并发测试错误: {errors}"
        assert len(results) > 0


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

    def test_alarm_check_performance(self):
        """测试报警检查性能"""
        from 报警层.alarm_manager import AlarmManager

        db = MagicMock()
        db.get_alarm_records.return_value = []
        db.get_active_alarms.return_value = []

        manager = AlarmManager(db)

        # 添加100条规则
        for i in range(100):
            manager.rules[f'rule_{i}'] = {
                'id': f'rule_{i}',
                'device_id': f'device_{i % 10}',
                'register_name': f'register_{i % 5}',
                'condition': 'greater_than',
                'threshold': 50.0,
                'level': 'warning',
                'enabled': True,
            }

        # 批量检查
        start = time.time()
        for i in range(100):
            manager.check_alarm(
                device_id=f'device_{i % 10}',
                register_name=f'register_{i % 5}',
                value=60.0,
                timestamp=time.time()
            )
        elapsed = time.time() - start

        # 100次检查应在1秒内完成
        assert elapsed < 1.0, f"报警检查耗时 {elapsed:.2f}s"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
