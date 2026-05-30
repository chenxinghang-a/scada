"""
报警管理器测试 - 提升报警层/alarm_manager.py 覆盖率
"""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime


@pytest.fixture
def mock_db():
    """模拟数据库"""
    db = MagicMock()
    db.get_alarm_records.return_value = []
    db.get_active_alarms.return_value = []
    db.acknowledge_alarm.return_value = True
    return db


@pytest.fixture
def alarm_config():
    """报警配置"""
    return {
        'rules': [
            {
                'id': 'alarm_high_temp',
                'name': 'High Temperature',
                'device_id': 'test_motor_01',
                'register_name': 'temperature',
                'condition': 'greater_than',
                'threshold': 80.0,
                'level': 'warning',
                'message': 'Motor temperature exceeds 80C',
                'enabled': True,
                'severity': 3,
                'likelihood': 3,
            },
        ]
    }


class TestAlarmManagerInit:
    """报警管理器初始化"""

    def test_init_basic(self, mock_db):
        """基本初始化"""
        from 报警层.alarm_manager import AlarmManager
        am = AlarmManager(mock_db)
        assert am is not None

    def test_init_with_output(self, mock_db):
        """带输出初始化"""
        from 报警层.alarm_manager import AlarmManager
        am = AlarmManager(mock_db, alarm_output=MagicMock(), broadcast_system=MagicMock())
        assert am is not None


class TestAlarmRules:
    """报警规则测试"""

    def test_get_active_alarms(self, mock_db):
        """获取活动报警"""
        from 报警层.alarm_manager import AlarmManager
        am = AlarmManager(mock_db)
        alarms = am.get_active_alarms()
        assert isinstance(alarms, list)

    def test_get_alarm_statistics(self, mock_db):
        """获取报警统计"""
        from 报警层.alarm_manager import AlarmManager
        am = AlarmManager(mock_db)
        stats = am.get_alarm_statistics()
        assert isinstance(stats, dict)

    def test_acknowledge_alarm(self, mock_db):
        """确认报警"""
        from 报警层.alarm_manager import AlarmManager
        am = AlarmManager(mock_db)
        # acknowledge_alarm takes (alarm_id, device_id, register_name, acknowledged_by)
        result = am.acknowledge_alarm('alarm1', 'dev1', 'temp', 'operator1')
        assert isinstance(result, bool)


class TestAlarmDedup:
    """报警去重测试"""

    def test_get_dedup_config(self, mock_db):
        """获取去重配置"""
        from 报警层.alarm_manager import AlarmManager
        am = AlarmManager(mock_db)
        config = am.get_dedup_config()
        assert isinstance(config, dict)

    def test_update_dedup_config(self, mock_db):
        """更新去重配置"""
        from 报警层.alarm_manager import AlarmManager
        am = AlarmManager(mock_db)
        result = am.update_dedup_config({'emit_cooldown_seconds': 60})
        assert isinstance(result, dict)


class TestAlarmOutput:
    """报警输出测试"""

    def test_alarm_output_none(self, mock_db):
        """无报警输出时"""
        from 报警层.alarm_manager import AlarmManager
        am = AlarmManager(mock_db)
        # alarm_output should be None by default
        assert am.alarm_output is None

    def test_broadcast_system_none(self, mock_db):
        """无广播系统时"""
        from 报警层.alarm_manager import AlarmManager
        am = AlarmManager(mock_db)
        assert am.broadcast_system is None

    def test_reset_alarm(self, mock_db):
        """复位报警"""
        from 报警层.alarm_manager import AlarmManager
        am = AlarmManager(mock_db)
        # Should not raise
        am.reset_alarm()


class TestAlarmCheck:
    """报警检查测试"""

    def test_check_alarm_no_rules(self, mock_db):
        """无规则时检查报警"""
        from 报警层.alarm_manager import AlarmManager
        am = AlarmManager(mock_db)
        # Should not raise even with no rules
        am.check_alarm('dev1', 'temp', 25.0, datetime.now())

    def test_rules_attribute(self, mock_db):
        """rules属性存在"""
        from 报警层.alarm_manager import AlarmManager
        am = AlarmManager(mock_db)
        assert hasattr(am, 'rules')
        assert isinstance(am.rules, dict)

    def test_rebuild_rules_index(self, mock_db):
        """重建规则索引"""
        from 报警层.alarm_manager import AlarmManager
        am = AlarmManager(mock_db)
        am._rebuild_rules_index()
        # Should not raise
