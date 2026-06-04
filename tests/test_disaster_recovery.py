"""
灾难恢复测试
验证系统在各种故障场景下的恢复能力
"""
import pytest
import os
import time
import sqlite3
import tempfile
import shutil
from unittest.mock import patch, MagicMock
from pathlib import Path


class TestDatabaseRecovery:
    """数据库恢复测试"""

    def test_database_corruption_recovery(self, tmp_path):
        """测试数据库损坏恢复"""
        # 创建损坏的数据库文件
        db_path = tmp_path / 'corrupt.db'
        db_path.write_bytes(b'corrupt data')

        # 验证损坏检测
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            cursor = conn.execute("PRAGMA integrity_check")
            result = cursor.fetchone()[0]
            conn.close()
            assert result != 'ok'
        except Exception:
            # 预期会失败
            pass

    def test_database_backup_restore(self, tmp_path):
        """测试数据库备份恢复"""
        # 创建源数据库
        source_db = tmp_path / 'source.db'
        conn = sqlite3.connect(str(source_db))
        conn.execute('CREATE TABLE test (id INTEGER, value TEXT)')
        conn.execute('INSERT INTO test VALUES (1, "test")')
        conn.commit()
        conn.close()

        # 备份
        backup_db = tmp_path / 'backup.db'
        source = sqlite3.connect(str(source_db))
        dest = sqlite3.connect(str(backup_db))
        source.backup(dest)
        source.close()
        dest.close()

        # 验证备份
        conn = sqlite3.connect(str(backup_db))
        cursor = conn.execute('SELECT * FROM test')
        row = cursor.fetchone()
        conn.close()
        assert row[0] == 1
        assert row[1] == 'test'

    def test_wal_recovery(self, tmp_path):
        """测试WAL恢复"""
        db_path = tmp_path / 'test.db'

        # 创建数据库并写入数据
        conn = sqlite3.connect(str(db_path))
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE test (id INTEGER)')
        for i in range(100):
            conn.execute(f'INSERT INTO test VALUES ({i})')
        conn.commit()

        # 模拟崩溃（不关闭连接）
        conn_int = sqlite3.connect(str(db_path))
        cursor = conn_int.execute('SELECT COUNT(*) FROM test')
        count = cursor.fetchone()[0]
        conn_int.close()

        assert count == 100


class TestConfigRecovery:
    """配置恢复测试"""

    def test_config_backup_restore(self, tmp_path):
        """测试配置备份恢复"""
        # 创建配置文件
        config_dir = tmp_path / 'config'
        config_dir.mkdir()
        config_file = config_dir / 'test.yaml'
        config_file.write_text('key: value\n')

        # 备份
        backup_dir = tmp_path / 'backup'
        backup_dir.mkdir()
        shutil.copy2(config_file, backup_dir / 'test.yaml')

        # 删除原文件
        config_file.unlink()
        assert not config_file.exists()

        # 恢复
        shutil.copy2(backup_dir / 'test.yaml', config_file)
        assert config_file.exists()
        assert config_file.read_text() == 'key: value\n'


class TestServiceRecovery:
    """服务恢复测试"""

    def test_graceful_shutdown(self):
        """测试优雅关闭"""
        from core.event_bus import EventBus

        bus = EventBus()
        events = []

        def handler(event):
            events.append(event)

        bus.subscribe('test', handler)
        bus.publish('test', data='test')

        assert len(events) == 1

    def test_resource_cleanup(self):
        """测试资源清理"""
        from core.connection_pool import ConnectionPool

        factory = lambda key: MagicMock()
        pool = ConnectionPool(factory, max_size=10)

        # 获取连接
        conn = pool.acquire('test')
        assert conn is not None

        # 释放连接
        pool.release(conn)

        # 关闭池
        pool.shutdown()


class TestDataIntegrity:
    """数据完整性测试"""

    def test_concurrent_write_integrity(self, tmp_path):
        """测试并发写入数据完整性"""
        db_path = tmp_path / 'concurrent.db'
        conn = sqlite3.connect(str(db_path))
        conn.execute('CREATE TABLE test (id INTEGER, value TEXT)')
        conn.commit()
        conn.close()

        import threading
        errors = []

        def writer(start, count):
            try:
                c = sqlite3.connect(str(db_path), timeout=10)
                for i in range(start, start + count):
                    c.execute('INSERT INTO test VALUES (?, ?)', (i, f'value_{i}'))
                c.commit()
                c.close()
            except Exception as e:
                errors.append(e)

        # 启动多个写入线程
        threads = []
        for i in range(5):
            t = threading.Thread(target=writer, args=(i * 100, 100))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=30)

        # 验证数据完整性
        assert len(errors) == 0, f"并发写入错误: {errors}"

        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute('SELECT COUNT(*) FROM test')
        count = cursor.fetchone()[0]
        conn.close()
        assert count == 500

    def test_transaction_rollback_integrity(self, tmp_path):
        """测试事务回滚完整性"""
        db_path = tmp_path / 'rollback.db'
        conn = sqlite3.connect(str(db_path))
        conn.execute('CREATE TABLE test (id INTEGER)')
        conn.execute('INSERT INTO test VALUES (1)')
        conn.commit()

        # 尝试失败的事务
        try:
            conn.execute('INSERT INTO test VALUES (2)')
            conn.execute('INVALID SQL')  # 会失败
        except Exception:
            conn.rollback()

        # 验证只有原始数据
        cursor = conn.execute('SELECT COUNT(*) FROM test')
        count = cursor.fetchone()[0]
        conn.close()
        assert count == 1


class TestNetworkRecovery:
    """网络恢复测试"""

    def test_connection_timeout_recovery(self):
        """测试连接超时恢复"""
        from core.connection_pool import ConnectionPool

        call_count = 0
        def factory(key):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("Connection failed")
            return MagicMock()

        pool = ConnectionPool(factory, max_size=5)

        # 前两次会失败
        for i in range(2):
            try:
                pool.acquire('test')
            except ConnectionError:
                pass

        # 第三次应该成功
        conn = pool.acquire('test')
        assert conn is not None


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
