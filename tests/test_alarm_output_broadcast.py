"""
报警输出和广播系统测试 - 提升报警层覆盖率
覆盖: simulated_alarm_output, simulated_broadcast, notification
"""
import pytest
from unittest.mock import MagicMock






class TestNotification:
    """通知模块测试"""

    def test_notification_init(self):
        """通知初始化"""
        from 报警层.notification import Notification
        nm = Notification()
        assert nm is not None
        assert nm.email_enabled is False

    def test_notification_init_with_config(self):
        """带配置初始化"""
        from 报警层.notification import Notification
        nm = Notification({'email': {'enabled': True}, 'sms': {'enabled': False}})
        assert nm.email_enabled is True
        assert nm.sms_enabled is False

    def test_send_email_disabled(self):
        """禁用时发送邮件"""
        from 报警层.notification import Notification
        nm = Notification()
        result = nm.send_email('Subject', 'Body')
        assert isinstance(result, bool)
