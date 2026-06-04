"""
回归测试套件
确保新改动不破坏现有功能
"""
import pytest
from unittest.mock import patch, MagicMock


class TestCoreModuleImports:
    """核心模块导入测试"""

    def test_import_alarm_manager(self):
        """测试报警管理器导入"""
        from 报警层.alarm_manager import AlarmManager
        assert AlarmManager is not None

    def test_import_data_collector(self):
        """测试数据采集器导入"""
        from 采集层.data_collector import DataCollector
        assert DataCollector is not None

    def test_import_device_manager(self):
        """测试设备管理器导入"""
        from 采集层.device_manager import DeviceManager
        assert DeviceManager is not None

    def test_import_database(self):
        """测试数据库模块导入"""
        from 存储层.database import Database
        assert Database is not None

    def test_import_auth_manager(self):
        """测试认证管理器导入"""
        from 用户层.auth import AuthManager
        assert AuthManager is not None

    def test_import_connection_pool(self):
        """测试连接池导入"""
        from core.connection_pool import ConnectionPool
        assert ConnectionPool is not None

    def test_import_health_checker(self):
        """测试健康检查器导入"""
        from core.health_checker import HealthChecker
        assert HealthChecker is not None


class TestDatabaseOperations:
    """数据库基本操作回归测试"""

    def test_create_tables(self, db):
        """测试表创建"""
        cursor = db.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        assert 'history_data' in tables
        assert 'alarm_records' in tables
        assert 'realtime_data' in tables

    def test_insert_and_query(self, db):
        """测试插入和查询"""
        cursor = db.cursor()
        cursor.execute('''
            INSERT INTO history_data (device_id, register_name, value, timestamp)
            VALUES (?, ?, ?, datetime('now'))
        ''', ('test_device', 'test_register', 42.0))
        db.commit()

        cursor.execute('SELECT value FROM history_data WHERE device_id = ?', ('test_device',))
        result = cursor.fetchone()
        assert result[0] == 42.0

    def test_transaction_rollback(self, db):
        """测试事务回滚"""
        cursor = db.cursor()
        try:
            cursor.execute('''
                INSERT INTO history_data (device_id, register_name, value, timestamp)
                VALUES (?, ?, ?, datetime('now'))
            ''', ('rollback_device', 'test', 1.0))
            # 故意触发错误
            cursor.execute('INVALID SQL')
        except Exception:
            db.rollback()

        # 验证数据未插入
        cursor.execute('SELECT COUNT(*) FROM history_data WHERE device_id = ?', ('rollback_device',))
        count = cursor.fetchone()[0]
        assert count == 0


class TestAlarmManagerRegression:
    """报警管理器回归测试"""

    def test_alarm_manager_initialization(self, db):
        """测试报警管理器初始化"""
        from 报警层.alarm_manager import AlarmManager
        manager = AlarmManager(db)
        assert manager is not None
        assert hasattr(manager, 'rules')
        assert hasattr(manager, 'alarm_states')

    def test_add_and_remove_rule(self, db):
        """测试添加和删除规则"""
        from 报警层.alarm_manager import AlarmManager
        manager = AlarmManager(db)

        rule = {
            'id': 'test_rule',
            'name': 'Test Rule',
            'device_id': 'test_device',
            'register_name': 'test_register',
            'condition': 'greater_than',
            'threshold': 50.0,
            'level': 'warning',
            'enabled': True,
        }

        # 添加规则
        manager.add_rule(rule)
        assert 'test_rule' in manager.rules

        # 删除规则
        manager.remove_rule('test_rule')
        assert 'test_rule' not in manager.rules

    def test_alarm_check_basic(self, db):
        """测试基本报警检查"""
        from 报警层.alarm_manager import AlarmManager
        manager = AlarmManager(db)

        # 添加规则
        manager.rules['test_rule'] = {
            'id': 'test_rule',
            'device_id': 'test_device',
            'register_name': 'temperature',
            'condition': 'greater_than',
            'threshold': 50.0,
            'level': 'warning',
            'enabled': True,
        }

        # 测试触发
        result = manager.check_alarm(
            device_id='test_device',
            register_name='temperature',
            value=60.0,
            timestamp=1234567890.0
        )
        assert result is not None


class TestDeviceManagerRegression:
    """设备管理器回归测试"""

    def test_device_manager_initialization(self):
        """测试设备管理器初始化"""
        from 采集层.device_manager import DeviceManager
        manager = DeviceManager()
        assert manager is not None
        assert hasattr(manager, 'devices')
        assert hasattr(manager, 'clients')

    def test_add_device(self):
        """测试添加设备"""
        from 采集层.device_manager import DeviceManager
        manager = DeviceManager()

        device_config = {
            'id': 'test_device',
            'name': 'Test Device',
            'protocol': 'modbus_tcp',
            'host': '192.168.1.1',
            'port': 502,
        }

        result = manager.add_device(device_config)
        assert result is True
        assert 'test_device' in manager.devices


class TestConnectionPoolRegression:
    """连接池回归测试"""

    def test_connection_pool_initialization(self):
        """测试连接池初始化"""
        from core.connection_pool import ConnectionPool

        factory = lambda key: MagicMock()
        pool = ConnectionPool(factory, max_size=10)
        assert pool is not None

    def test_connection_acquire_release(self):
        """测试连接获取和释放"""
        from core.connection_pool import ConnectionPool

        factory = lambda key: MagicMock()
        pool = ConnectionPool(factory, max_size=10)

        conn = pool.acquire('test_device')
        assert conn is not None

        pool.release(conn)

        # 再次获取应该复用
        conn2 = pool.acquire('test_device')
        assert conn2 is conn


class TestHealthCheckerRegression:
    """健康检查器回归测试"""

    def test_health_checker_initialization(self):
        """测试健康检查器初始化"""
        from core.health_checker import HealthChecker
        checker = HealthChecker()
        assert checker is not None

    def test_register_check(self):
        """测试注册检查"""
        from core.health_checker import HealthChecker
        checker = HealthChecker()

        def test_check():
            return {'status': 'healthy', 'message': 'OK'}

        checker.register_check('test', test_check)
        assert 'test' in checker._checks


class TestAPIRoutesRegression:
    """API路由回归测试"""

    def test_health_endpoint(self, client):
        """测试健康检查端点"""
        resp = client.get('/api/health/status')
        assert resp.status_code == 200

    def test_devices_endpoint_requires_auth(self, client):
        """测试设备端点需要认证"""
        resp = client.get('/api/devices')
        assert resp.status_code == 401

    def test_alarms_endpoint_requires_auth(self, client):
        """测试报警端点需要认证"""
        resp = client.get('/api/alarms')
        assert resp.status_code == 401


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
