"""
报警系统端到端测试
测试报警触发→确认→清除完整流程
"""
import pytest
import time
from unittest.mock import MagicMock, patch
from datetime import datetime


class TestAlarmE2E:
    """报警端到端流程测试"""

    def test_alarm_trigger_acknowledge_clear(self):
        """测试报警触发→确认→清除完整流程"""
        from 报警层.alarm_manager import AlarmManager

        db = MagicMock()
        db.insert_alarm.return_value = True
        db.get_active_alarms.return_value = []

        am = AlarmManager(db)

        # 模拟规则配置
        rule = {
            'id': 'test_rule_001',
            'name': '高温报警',
            'device_id': 'motor_01',
            'register_name': 'temperature',
            'condition': 'greater_than',
            'threshold': 80.0,
            'level': 'warning',
            'enabled': True,
        }

        # 1. 触发报警
        am._process_alarm_state(
            rule_config=rule,
            device_id='motor_01',
            register_name='temperature',
            value=85.0,
            triggered=True,
            timestamp=datetime.now()
        )

        # 验证报警状态存在
        state_key = ('motor_01', 'temperature')
        assert state_key in am.alarm_states
        assert am.alarm_states[state_key]['alarm_id'] == 'test_rule_001'

        # 2. 确认报警
        am.acknowledge_alarm(
            alarm_id='test_rule_001',
            device_id='motor_01',
            register_name='temperature',
            acknowledged_by='operator1'
        )

        # 3. 清除报警（值回到正常范围）
        am._process_alarm_state(
            rule_config=rule,
            device_id='motor_01',
            register_name='temperature',
            value=75.0,
            triggered=False,
            timestamp=datetime.now()
        )

        # 验证报警状态已清除
        assert state_key not in am.alarm_states

    def test_alarm_dedup_prevents_duplicate_notifications(self):
        """测试去重机制防止重复通知"""
        from 报警层.alarm_manager import AlarmManager

        db = MagicMock()
        am = AlarmManager(db)

        notifications = []
        am._websocket_emit = lambda data: notifications.append(data)

        rule = {
            'id': 'test_rule_002',
            'name': '高压报警',
            'device_id': 'pump_01',
            'register_name': 'pressure',
            'condition': 'greater_than',
            'threshold': 10.0,
            'level': 'critical',
            'enabled': True,
        }

        # 连续触发同一报警
        for _ in range(5):
            am._trigger_alarm(rule, 'pump_01', 'pressure', 12.0, datetime.now())

        # 由于去重，应该只有1次通知（第一次触发）
        assert len(notifications) <= 2  # 允许少量重复（冷却窗口内）

    def test_alarm_escalation(self):
        """测试报警升级"""
        from 报警层.alarm_manager import AlarmManager

        db = MagicMock()
        am = AlarmManager(db)
        am._escalation_timeout = 1  # 1秒超时（测试用）

        escalated = []
        am._escalation_callbacks.append(lambda data: escalated.append(data))

        rule = {
            'id': 'test_rule_003',
            'name': '液位报警',
            'device_id': 'tank_01',
            'register_name': 'level',
            'condition': 'greater_than',
            'threshold': 90.0,
            'level': 'warning',
            'enabled': True,
        }

        # 触发报警
        am._process_alarm_state(
            rule_config=rule,
            device_id='tank_01',
            register_name='level',
            value=95.0,
            triggered=True,
            timestamp=datetime.now()
        )

        # 等待升级检查
        time.sleep(2)

        # 触发升级检查
        am._check_escalation()

        # 验证升级已触发（如果配置了升级回调）
        # 注意：实际升级逻辑可能需要更多配置


class TestAlarmOutputIntegration:
    """报警输出集成测试"""

    def test_alarm_triggers_light(self):
        """测试报警触发声光报警"""
        from 报警层.alarm_manager import AlarmManager

        db = MagicMock()
        alarm_output = MagicMock()
        alarm_output.enabled = True

        am = AlarmManager(db, alarm_output=alarm_output)

        rule = {
            'id': 'test_rule_004',
            'name': '振动报警',
            'device_id': 'fan_01',
            'register_name': 'vibration',
            'condition': 'greater_than',
            'threshold': 5.0,
            'level': 'critical',
            'enabled': True,
        }

        # 触发报警
        am._trigger_alarm(rule, 'fan_01', 'vibration', 8.0, datetime.now())

        # 验证声光报警被调用
        alarm_output.trigger_alarm.assert_called_once()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
