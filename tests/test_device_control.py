"""
设备控制安全模块测试 - 提升智能层/device_control.py 覆盖率
"""
import pytest
import time
from unittest.mock import MagicMock, patch
from datetime import datetime


@pytest.fixture
def mock_db():
    """模拟数据库"""
    db = MagicMock()
    return db


@pytest.fixture
def device_control(mock_db):
    """DeviceControlSafety 实例"""
    from 智能层.device_control import DeviceControlSafety
    return DeviceControlSafety(mock_db)


class TestDeviceControlInit:
    """初始化测试"""

    def test_init(self, device_control):
        """初始化"""
        assert device_control is not None
        assert device_control._estop_active is False

    def test_init_with_managers(self, mock_db):
        """带管理器初始化"""
        from 智能层.device_control import DeviceControlSafety
        dm = MagicMock()
        am = MagicMock()
        dc = DeviceControlSafety(mock_db, device_manager=dm, alarm_manager=am)
        assert dc.device_manager is dm
        assert dc.alarm_manager is am


class TestEStop:
    """紧急停机测试"""

    def test_trigger_estop(self, device_control):
        """触发紧急停机"""
        result = device_control.trigger_emergency_stop('Test reason')
        assert isinstance(result, dict)
        assert 'success' in result
        assert device_control._estop_active is True

    def test_reset_estop(self, device_control):
        """解除紧急停机"""
        device_control.trigger_emergency_stop('Test')
        result = device_control.reset_emergency_stop('operator1')
        assert isinstance(result, dict)
        assert 'success' in result

    def test_reset_estop_not_active(self, device_control):
        """未激活时解除停机"""
        result = device_control.reset_emergency_stop('operator1')
        assert isinstance(result, dict)

    def test_get_estop_status(self, device_control):
        """获取停机状态"""
        status = device_control.get_estop_status()
        assert 'active' in status


class TestInterlocks:
    """安全联锁测试"""

    def test_add_interlock(self, device_control):
        """添加联锁规则"""
        rule = {
            'id': 'rule1',
            'name': 'High Temp',
            'device_id': 'dev1',
            'register_name': 'temp',
            'condition': 'greater_than',
            'threshold': 80.0,
            'action': 'stop',
            'enabled': True
        }
        result = device_control.add_interlock(rule)
        assert result is True
        assert 'rule1' in device_control._interlock_rules

    def test_remove_interlock(self, device_control):
        """删除联锁规则"""
        rule = {
            'id': 'rule1',
            'name': 'High Temp',
            'device_id': 'dev1',
            'register_name': 'temp',
            'condition': 'greater_than',
            'threshold': 80.0,
            'action': 'stop',
            'enabled': True
        }
        device_control.add_interlock(rule)
        result = device_control.remove_interlock('rule1')
        assert result is True
        assert 'rule1' not in device_control._interlock_rules

    def test_get_interlock_status(self, device_control):
        """获取联锁状态"""
        status = device_control.get_interlock_status()
        assert isinstance(status, dict)

    def test_bypass_interlock(self, device_control):
        """旁路联锁"""
        rule = {
            'id': 'rule1',
            'name': 'High Temp',
            'device_id': 'dev1',
            'register_name': 'temp',
            'condition': 'greater_than',
            'threshold': 80.0,
            'action': 'stop',
            'enabled': True
        }
        device_control.add_interlock(rule)
        result = device_control.bypass_interlock('rule1', 'operator1', 'maintenance')
        assert isinstance(result, bool)

    def test_restore_interlock(self, device_control):
        """恢复联锁"""
        rule = {
            'id': 'rule1',
            'name': 'High Temp',
            'device_id': 'dev1',
            'register_name': 'temp',
            'condition': 'greater_than',
            'threshold': 80.0,
            'action': 'stop',
            'enabled': True
        }
        device_control.add_interlock(rule)
        device_control.bypass_interlock('rule1', 'operator1', 'maintenance')
        result = device_control.restore_interlock('rule1', 'operator1')
        assert isinstance(result, bool)


class TestBypassRequest:
    """旁路请求测试"""

    def test_request_bypass(self, device_control):
        """创建旁路请求"""
        rule = {
            'id': 'rule1',
            'name': 'High Temp',
            'device_id': 'dev1',
            'register_name': 'temp',
            'condition': 'greater_than',
            'threshold': 80.0,
            'action': 'stop',
            'enabled': True
        }
        device_control.add_interlock(rule)
        request_id = device_control.request_bypass('rule1', 'operator1', 'maintenance', 30)
        assert request_id is not None

    def test_approve_bypass(self, device_control):
        """审批旁路请求"""
        rule = {
            'id': 'rule1',
            'name': 'High Temp',
            'device_id': 'dev1',
            'register_name': 'temp',
            'condition': 'greater_than',
            'threshold': 80.0,
            'action': 'stop',
            'enabled': True
        }
        device_control.add_interlock(rule)
        request_id = device_control.request_bypass('rule1', 'operator1', 'maintenance', 30)
        result = device_control.approve_bypass(request_id, 'approver1')
        assert isinstance(result, tuple)

    def test_reject_bypass(self, device_control):
        """拒绝旁路请求"""
        rule = {
            'id': 'rule1',
            'name': 'High Temp',
            'device_id': 'dev1',
            'register_name': 'temp',
            'condition': 'greater_than',
            'threshold': 80.0,
            'action': 'stop',
            'enabled': True
        }
        device_control.add_interlock(rule)
        request_id = device_control.request_bypass('rule1', 'operator1', 'maintenance', 30)
        result = device_control.reject_bypass(request_id, 'approver1', 'Not safe')
        assert isinstance(result, tuple)

    def test_get_pending_bypasses(self, device_control):
        """获取待审批请求"""
        pending = device_control.get_pending_bypasses()
        assert isinstance(pending, list)


class TestWriteSafety:
    """写操作安全测试"""

    def test_write_with_verification(self, device_control):
        """带验证的写操作"""
        # Device not connected, should fail
        result = device_control.write_with_verification('dev1', 0, 100, 'operator1')
        assert isinstance(result, dict)
        assert 'success' in result

    def test_write_whitelist_exists(self, device_control):
        """写白名单存在"""
        assert hasattr(device_control, '_write_whitelist')
        assert isinstance(device_control._write_whitelist, dict)


class TestDeviceHealth:
    """设备健康测试"""

    def test_get_device_health_summary(self, device_control):
        """获取设备健康摘要"""
        summary = device_control.get_device_health_summary()
        assert isinstance(summary, dict)

    def test_get_full_status(self, device_control):
        """获取完整状态"""
        status = device_control.get_full_status()
        assert isinstance(status, dict)


class TestAuditLog:
    """审计日志测试"""

    def test_get_audit_log(self, device_control):
        """获取审计日志"""
        logs = device_control.get_audit_log()
        assert isinstance(logs, list)

    def test_get_audit_log_with_filter(self, device_control):
        """带过滤的审计日志"""
        logs = device_control.get_audit_log(action_filter='write_register')
        assert isinstance(logs, list)


class TestBatchControl:
    """批量控制测试"""

    def test_batch_control_invalid(self, device_control):
        """无效批量操作"""
        result = device_control.batch_control('invalid_action', 'operator1')
        assert isinstance(result, dict)


class TestSafetyLevel:
    """安全等级测试"""

    def test_safety_levels(self):
        """安全等级常量"""
        from 智能层.device_control import SafetyLevel
        assert SafetyLevel.SAFE == 'safe'
        assert SafetyLevel.WARNING == 'warning'
        assert SafetyLevel.CRITICAL == 'critical'
        assert SafetyLevel.EMERGENCY == 'emergency'


class TestBypassRequestModel:
    """旁路请求模型测试"""

    def test_bypass_request_init(self):
        """旁路请求初始化"""
        from 智能层.device_control import BypassRequest
        req = BypassRequest(
            request_id='req1',
            interlock_id='rule1',
            requested_by='operator1',
            requested_at=time.time(),
            expires_at=time.time() + 1800,
            reason='maintenance'
        )
        assert req.request_id == 'req1'
        assert req.status == 'pending'
        assert req.is_approved is False

    def test_bypass_request_is_expired(self):
        """过期检查"""
        from 智能层.device_control import BypassRequest
        req = BypassRequest(
            request_id='req1',
            interlock_id='rule1',
            requested_by='operator1',
            requested_at=time.time() - 3600,
            expires_at=time.time() - 1800,  # already expired
            reason='maintenance'
        )
        assert req.is_expired is True

    def test_bypass_request_is_approved(self):
        """审批检查"""
        from 智能层.device_control import BypassRequest
        req = BypassRequest(
            request_id='req1',
            interlock_id='rule1',
            requested_by='operator1',
            requested_at=time.time(),
            expires_at=time.time() + 1800,
            reason='maintenance',
            approvals=['approver1', 'approver2']
        )
        assert req.is_approved is True
