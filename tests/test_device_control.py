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


# ============================================================
# 控制点位配置化（2026-09 修复：batch_control 原写死 coil0/reg100）
# ============================================================

def _make_dc(devices, client=None):
    """构造带模拟 device_manager/client 的 DeviceControlSafety"""
    from 智能层.device_control import DeviceControlSafety
    dm = MagicMock()
    dm.devices = devices
    dm.simulation_mode = False
    client = client if client is not None else MagicMock()
    client.connected = True
    client.write_single_coil = MagicMock(return_value=True)
    client.write_single_register = MagicMock(return_value=True)
    dm.get_client.return_value = client
    dm.start_device = MagicMock()
    dm.stop_device = MagicMock()
    return DeviceControlSafety(MagicMock(), device_manager=dm), client


class TestControlPointConfig:
    """起停点位必须来自设备配置，而不是写死的 0/100。"""

    @pytest.fixture(autouse=True)
    def _isolate_interlock_file(self, monkeypatch):
        """把联锁配置路径指向不存在的文件，保证用例不受仓库配置影响"""
        import 智能层.device_control as dc_mod
        monkeypatch.setattr(dc_mod, 'INTERLOCK_CONFIG_PATH', '配置/__test_nonexistent__.yaml')

    def test_start_uses_configured_coil_address(self):
        """start 应写配置的线圈地址(7)，而不是写死的 0"""
        devices = {'plc_a': {
            'protocol': 'modbus_tcp',
            'control': {'start': {'type': 'coil', 'address': 7, 'value': True}},
        }}
        dc, client = _make_dc(devices)
        result = dc.batch_control('start', 'op')
        client.write_single_coil.assert_called_once_with(7, True)
        assert result['success'] is True

    def test_stop_uses_configured_coil_address(self):
        """stop 同样按配置点位写入"""
        devices = {'plc_a': {
            'protocol': 'modbus_tcp',
            'control': {'stop': {'type': 'coil', 'address': 9, 'value': False}},
        }}
        dc, client = _make_dc(devices)
        dc.batch_control('stop', 'op')
        client.write_single_coil.assert_called_once_with(9, False)

    def test_reset_uses_configured_register(self):
        """reset 走寄存器路径时应写配置地址(321)，而不是写死的 100"""
        devices = {'plc_a': {
            'protocol': 'modbus_tcp',
            'control': {'reset': {'type': 'register', 'address': 321, 'value': 0}},
        }}
        dc, client = _make_dc(devices)
        dc.batch_control('reset', 'op')
        client.write_single_register.assert_called_once_with(321, 0)

    def test_flat_control_form_shared_address(self):
        """扁平写法：共用 type/address，各动作取自己的 value"""
        devices = {'plc_a': {
            'protocol': 'modbus_tcp',
            'control': {'type': 'coil', 'address': 3, 'start_value': True, 'stop_value': False},
        }}
        dc, client = _make_dc(devices)
        dc.batch_control('start', 'op')
        dc.batch_control('stop', 'op')
        assert client.write_single_coil.call_args_list[0][0] == (3, True)
        assert client.write_single_coil.call_args_list[1][0] == (3, False)

    def test_missing_control_falls_back_with_warning(self, caplog):
        """未配置 control 时使用显式回退点位，并且必须告警（不再静默硬编码）"""
        import logging
        devices = {'plc_a': {'protocol': 'modbus_tcp'}}
        dc, client = _make_dc(devices)
        with caplog.at_level(logging.WARNING):
            dc.batch_control('start', 'op')
        client.write_single_coil.assert_called_once_with(0, True)
        assert '未配置控制点位' in caplog.text

    def test_invalid_control_type_rejected(self):
        """非法点位类型必须拒绝写入，而不是猜一个点位写下命令"""
        devices = {'plc_a': {
            'protocol': 'modbus_tcp',
            'control': {'start': {'type': 'pwm', 'address': 1, 'value': 1}},
        }}
        dc, client = _make_dc(devices)
        result = dc.batch_control('start', 'op')
        client.write_single_coil.assert_not_called()
        client.write_single_register.assert_not_called()
        assert result['success'] is False

    def test_invalid_control_address_rejected(self):
        """点位地址非法必须拒绝写入"""
        devices = {'plc_a': {
            'protocol': 'modbus_tcp',
            'control': {'start': {'type': 'coil', 'address': 'x', 'value': True}},
        }}
        dc, client = _make_dc(devices)
        result = dc.batch_control('start', 'op')
        client.write_single_coil.assert_not_called()
        assert result['success'] is False

    def test_audit_records_actual_point(self):
        """审计日志必须记录真实写入点位（可追溯）"""
        devices = {'plc_a': {
            'protocol': 'modbus_tcp',
            'control': {'start': {'type': 'coil', 'address': 5, 'value': True}},
        }}
        dc, _ = _make_dc(devices)
        dc.batch_control('start', 'op')
        texts = [entry.get('detail', '') for entry in dc.get_audit_log()]
        assert any('coil#5' in t for t in texts), texts


# ============================================================
# 联锁配置化（2026-09 修复：原硬编码 plc_reactor_01 等幽灵设备）
# ============================================================

INTERLOCK_SAMPLE = {
    'id': 'il_temp_high',
    'name': '温度超限联锁',
    'priority': 1,
    'enabled': True,
    'condition': {'type': 'threshold', 'device_id': 'plc_a', 'register': 'temperature',
                  'operator': '>', 'value': 95.0},
    'action': {'type': 'write_register', 'device_id': 'plc_a', 'register': 'heater_enable',
               'value': 0},
    'alarm': {'level': 'critical', 'message': '温度超限联锁触发'},
}


class TestInterlockConfigDriven:

    @pytest.fixture(autouse=True)
    def _isolate_interlock_file(self, monkeypatch):
        import 智能层.device_control as dc_mod
        monkeypatch.setattr(dc_mod, 'INTERLOCK_CONFIG_PATH', '配置/__test_nonexistent__.yaml')

    def test_no_config_installs_no_interlocks(self):
        """没有联锁配置就不装规则（不再内置幽灵设备名的假联锁）"""
        from 智能层.device_control import DeviceControlSafety
        dc = DeviceControlSafety(MagicMock())
        assert dc._interlock_rules == {}

    def test_no_interlocks_means_no_phantom_rules(self):
        """确认不再注入 plc_reactor_01 这类配置中不存在的设备规则"""
        from 智能层.device_control import DeviceControlSafety
        dc = DeviceControlSafety(MagicMock())
        assert dc._interlock_rules == {}
        assert dc.get_interlock_status() == {}

    def test_interlocks_loaded_from_config_injection(self):
        """构造参数注入的联锁规则应被装载"""
        from 智能层.device_control import DeviceControlSafety
        dc = DeviceControlSafety(MagicMock(), config={'interlocks': [INTERLOCK_SAMPLE]})
        assert 'il_temp_high' in dc._interlock_rules
        assert dc._interlock_rules['il_temp_high']['priority'] == 1
        assert dc._interlock_states['il_temp_high'] is False

    def test_interlocks_loaded_from_device_config(self):
        """设备配置内的 interlocks 段应被装载"""
        from 智能层.device_control import DeviceControlSafety
        dm = MagicMock()
        dm.devices = {'plc_a': {'protocol': 'modbus_tcp', 'interlocks': [INTERLOCK_SAMPLE]}}
        dc = DeviceControlSafety(MagicMock(), device_manager=dm)
        assert 'il_temp_high' in dc._interlock_rules

    def test_invalid_rules_are_skipped(self):
        """缺 id / 缺 condition 的规则必须被丢弃（不得半装）"""
        from 智能层.device_control import DeviceControlSafety
        bad_rules = [
            {'name': 'no id'},
            {'id': 'il_bad'},                                   # 缺 condition/action
            {'id': 'il_bad2', 'condition': {}, 'action': {}},    # 结构合法但内容空
            'not-a-dict',
        ]
        dc = DeviceControlSafety(MagicMock(), config={'interlocks': bad_rules})
        assert 'il_bad' not in dc._interlock_rules
        assert 'il_bad2' in dc._interlock_rules  # 结构合法的仍会装载（条件求值由引擎兜底）

    def test_configured_interlock_triggers_and_writes(self):
        """配置化联锁在条件满足时触发，并按寄存器名解析地址后写入"""
        from 智能层.device_control import DeviceControlSafety
        dm = MagicMock()
        dm.devices = {'plc_a': {
            'protocol': 'modbus_tcp',
            'registers': [{'name': 'heater_enable', 'address': 10}],
        }}
        client = MagicMock()
        client.write_single_register = MagicMock(return_value=True)
        dm.get_client.return_value = client

        dc = DeviceControlSafety(MagicMock(), device_manager=dm,
                                 config={'interlocks': [INTERLOCK_SAMPLE]})
        dc.check_interlocks('plc_a', 'temperature', 99.0)

        assert dc._interlock_states['il_temp_high'] is True
        client.write_single_register.assert_called_once_with(10, 0)

    def test_interlock_not_triggered_below_threshold(self):
        """条件不满足时不得触发"""
        from 智能层.device_control import DeviceControlSafety
        dc = DeviceControlSafety(MagicMock(), config={'interlocks': [INTERLOCK_SAMPLE]})
        dc.check_interlocks('plc_a', 'temperature', 20.0)
        assert dc._interlock_states['il_temp_high'] is False


class TestWriteLimitsConfigDriven:

    def test_device_write_limits_override_builtin(self):
        """设备配置里的 write_limits 应覆盖内置演示量程"""
        from 智能层.device_control import DeviceControlSafety
        dm = MagicMock()
        dm.devices = {'plc_a': {'write_limits': {'temperature_setpoint': [0, 200]}}}
        dc = DeviceControlSafety(MagicMock(), device_manager=dm)
        assert dc._write_limits['plc_a']['temperature_setpoint'] == (0.0, 200.0)

    def test_invalid_write_limits_ignored(self):
        """非法量程应被忽略（不得写入脏数据）"""
        from 智能层.device_control import DeviceControlSafety
        dm = MagicMock()
        dm.devices = {'plc_a': {'write_limits': {
            'bad_range': [10, 0],
            'bad_type': 'oops',
        }}}
        dc = DeviceControlSafety(MagicMock(), device_manager=dm)
        assert 'bad_range' not in dc._write_limits.get('plc_a', {})
        assert 'bad_type' not in dc._write_limits.get('plc_a', {})
