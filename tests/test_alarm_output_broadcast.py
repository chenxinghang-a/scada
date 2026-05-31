"""
报警输出和广播系统测试 - 提升报警层覆盖率
覆盖: simulated_alarm_output, simulated_broadcast, notification
"""
import pytest
from unittest.mock import MagicMock


class TestSimulatedAlarmOutput:
    """模拟报警输出测试"""

    def test_init_default(self):
        """默认初始化"""
        from 报警层.simulated_alarm_output import SimulatedAlarmOutput
        output = SimulatedAlarmOutput()
        assert output.enabled is True
        assert output.current_state['green'] is True
        assert output.current_state['red'] is False

    def test_init_with_config(self):
        """带配置初始化"""
        from 报警层.simulated_alarm_output import SimulatedAlarmOutput
        output = SimulatedAlarmOutput({'enabled': False})
        assert output.enabled is False

    def test_activate_critical_alarm(self):
        """严重报警"""
        from 报警层.simulated_alarm_output import SimulatedAlarmOutput
        output = SimulatedAlarmOutput()
        result = output.activate_alarm('critical', 'Test critical alarm')
        assert result is True
        assert output.current_state['red'] is True
        assert output.current_state['buzzer'] is True
        assert output.current_state['level'] == 'critical'

    def test_activate_warning_alarm(self):
        """警告报警"""
        from 报警层.simulated_alarm_output import SimulatedAlarmOutput
        output = SimulatedAlarmOutput()
        result = output.activate_alarm('warning', 'Test warning')
        assert result is True
        assert output.current_state['yellow'] is True
        assert output.current_state['buzzer'] is True

    def test_activate_info_alarm(self):
        """信息报警"""
        from 报警层.simulated_alarm_output import SimulatedAlarmOutput
        output = SimulatedAlarmOutput()
        result = output.activate_alarm('info', 'Test info')
        assert result is True
        assert output.current_state['green'] is True
        assert output.current_state['buzzer'] is False

    def test_activate_alarm_disabled(self):
        """禁用时激活报警"""
        from 报警层.simulated_alarm_output import SimulatedAlarmOutput
        output = SimulatedAlarmOutput({'enabled': False})
        result = output.activate_alarm('critical', 'msg')
        assert result is False

    def test_acknowledge(self):
        """消音"""
        from 报警层.simulated_alarm_output import SimulatedAlarmOutput
        output = SimulatedAlarmOutput()
        output.activate_alarm('critical', 'alarm')
        result = output.acknowledge()
        assert result is True
        assert output.current_state['buzzer'] is False
        assert output.current_state['red'] is True  # 灯保持

    def test_acknowledge_disabled(self):
        """禁用时消音"""
        from 报警层.simulated_alarm_output import SimulatedAlarmOutput
        output = SimulatedAlarmOutput({'enabled': False})
        result = output.acknowledge()
        assert result is False

    def test_reset(self):
        """复位"""
        from 报警层.simulated_alarm_output import SimulatedAlarmOutput
        output = SimulatedAlarmOutput()
        output.activate_alarm('critical', 'alarm')
        result = output.reset()
        assert result is True
        assert output.current_state['green'] is True
        assert output.current_state['red'] is False
        assert output.current_state['buzzer'] is False

    def test_reset_disabled(self):
        """禁用时复位"""
        from 报警层.simulated_alarm_output import SimulatedAlarmOutput
        output = SimulatedAlarmOutput({'enabled': False})
        result = output.reset()
        assert result is False

    def test_manual_control(self):
        """手动控制"""
        from 报警层.simulated_alarm_output import SimulatedAlarmOutput
        output = SimulatedAlarmOutput()
        result = output.manual_control(red=True, yellow=False, buzzer=True)
        assert result['success'] is True
        assert output.current_state['red'] is True
        assert output.current_state['buzzer'] is True

    def test_manual_control_disabled(self):
        """禁用时手动控制"""
        from 报警层.simulated_alarm_output import SimulatedAlarmOutput
        output = SimulatedAlarmOutput({'enabled': False})
        result = output.manual_control(red=True)
        assert result['success'] is False

    def test_get_status(self):
        """获取状态"""
        from 报警层.simulated_alarm_output import SimulatedAlarmOutput
        output = SimulatedAlarmOutput()
        status = output.get_status()
        assert status['enabled'] is True
        assert status['mode'] == 'simulated'
        assert 'state' in status

    def test_history_recording(self):
        """历史记录"""
        from 报警层.simulated_alarm_output import SimulatedAlarmOutput
        output = SimulatedAlarmOutput()
        output.activate_alarm('critical', 'alarm1')
        output.acknowledge()
        output.reset()
        assert len(output.history) == 3


class TestSimulatedBroadcast:
    """模拟广播系统测试"""

    def test_init_default(self):
        """默认初始化"""
        from 报警层.simulated_broadcast import SimulatedBroadcastSystem
        bs = SimulatedBroadcastSystem()
        assert bs.enabled is True
        assert len(bs.areas) > 0

    def test_init_with_config(self):
        """带配置初始化"""
        from 报警层.simulated_broadcast import SimulatedBroadcastSystem
        bs = SimulatedBroadcastSystem({
            'enabled': False,
            'areas': ['Zone A', 'Zone B']
        })
        assert bs.enabled is False
        assert bs.areas == ['Zone A', 'Zone B']

    def test_speak(self):
        """广播发言"""
        from 报警层.simulated_broadcast import SimulatedBroadcastSystem
        bs = SimulatedBroadcastSystem()
        result = bs.speak('Test broadcast', level='warning', area='车间A')
        assert result['success'] is True
        assert 'area' in result

    def test_speak_disabled(self):
        """禁用时发言"""
        from 报警层.simulated_broadcast import SimulatedBroadcastSystem
        bs = SimulatedBroadcastSystem({'enabled': False})
        result = bs.speak('Test')
        assert result['success'] is False

    def test_speak_invalid_area(self):
        """无效区域使用'all'"""
        from 报警层.simulated_broadcast import SimulatedBroadcastSystem
        bs = SimulatedBroadcastSystem()
        result = bs.speak('Test', area='nonexistent')
        assert result['area'] == 'all'

    def test_speak_no_area(self):
        """无指定区域"""
        from 报警层.simulated_broadcast import SimulatedBroadcastSystem
        bs = SimulatedBroadcastSystem()
        result = bs.speak('Test')
        assert result['area'] == 'all'

    def test_get_areas(self):
        """获取区域列表"""
        from 报警层.simulated_broadcast import SimulatedBroadcastSystem
        bs = SimulatedBroadcastSystem()
        areas = bs.get_areas()
        assert len(areas) > 0

    def test_get_history(self):
        """获取历史"""
        from 报警层.simulated_broadcast import SimulatedBroadcastSystem
        bs = SimulatedBroadcastSystem()
        bs.speak('Broadcast 1')
        bs.speak('Broadcast 2')
        history = bs.get_history()
        assert len(history) == 2

    def test_get_history_limit(self):
        """历史限制"""
        from 报警层.simulated_broadcast import SimulatedBroadcastSystem
        bs = SimulatedBroadcastSystem()
        for i in range(5):
            bs.speak(f'Broadcast {i}')
        history = bs.get_history(limit=3)
        assert len(history) == 3

    def test_get_status(self):
        """获取状态"""
        from 报警层.simulated_broadcast import SimulatedBroadcastSystem
        bs = SimulatedBroadcastSystem()
        status = bs.get_status()
        assert status['enabled'] is True
        assert status['mode'] == 'simulated'

    def test_history_overflow(self):
        """历史记录溢出处理"""
        from 报警层.simulated_broadcast import SimulatedBroadcastSystem
        bs = SimulatedBroadcastSystem()
        # Add more than 1000 records to trigger truncation
        for i in range(1002):
            bs.speak(f'Message {i}')
        # After overflow, history should be truncated to 500
        assert len(bs.history) <= 1002


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
