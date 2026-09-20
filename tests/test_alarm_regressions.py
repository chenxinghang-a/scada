"""报警层 6 项确定性缺陷的针对性回归测试

对应缺陷（审计编号见报告）：
1. delay 规则永不触发 / pending 状态无人消费（含时间戳异常、字符串 delay）
2. 去重记账：把"被抑制、根本没推送"的报警记成已推送 → 冷却窗口压掉真报警；
   两张去重表无容量上限 → 长期运行内存泄漏
3. 严重度命名错乱：洪水检测表用 critical/high/medium/low/info，
   而规则实际产出 critical/warning/info → warning 落到默认档被错误抑制；
   KPI 优先级分布只统计 low/medium/high/critical → 分布恒空
4. 声光/广播接口与实现不一致（trigger_alarm vs activate_alarm、
   speak_alarm vs speak）→ AttributeError 被 except 吞掉 = 全线静默漏报
5. 定时器停止竞态：stop_*_timer 后 _tick 的 finally 重新创建定时器 → 复活
6. 升级机制双套并存（_escalation_timeout 与 AlarmEscalationManager 互不知情）

每个用例都针对"静默失效"设计：断言的是**行为**（有没有真的报警/推送/升级），
而不是"函数被调用过"。
"""

import threading
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from 报警层.alarm_manager import AlarmManager, AlarmFloodDetector
from 报警层.alarm_kpi import AlarmKPI

POLL = 0.05          # 定时器轮询间隔（用例必须秒级跑完）
MAX_RETURN = 2.0     # 回调必须在此时间内出现


def _rule(rule_id='r1', **overrides):
    rule = {
        'id': rule_id,
        'name': '高温报警',
        'device_id': 'd1',
        'register_name': 'temp',
        'condition': 'greater_than',
        'threshold': 80.0,
        'level': 'warning',
        'enabled': True,
    }
    rule.update(overrides)
    return rule


def _write_config(tmp_path, extra_yaml=''):
    cfg = tmp_path / 'alarms.yaml'
    cfg.write_text(
        'alarm_rules: []\n'
        'dedup:\n'
        '  enabled: true\n'
        '  emit_cooldown_seconds: 300\n'
        '  acknowledge_suppress_seconds: 600\n' + extra_yaml,
        encoding='utf-8')
    return cfg


def _make_manager(config_path):
    db = MagicMock()
    db.insert_alarm.return_value = True
    db.acknowledge_alarm.return_value = True
    manager = AlarmManager(db, config_path=str(config_path))
    manager._config_watch_interval = POLL
    return manager


@pytest.fixture
def mgr(tmp_path):
    manager = _make_manager(_write_config(tmp_path))
    try:
        yield manager
    finally:
        manager.stop()


# ============================================================
# 1. delay 规则
# ============================================================

class TestDelayRule:
    """delay > 0 的规则必须在条件持续成立满 delay 秒后真正报警"""

    def test_not_fired_before_delay(self, mgr):
        emits = []
        mgr._websocket_emit = emits.append
        rule = _rule('r_delay', delay=10)
        t0 = datetime.now()

        mgr._process_alarm_state('r_delay', rule, 'd1', 'temp', 90.0, t0, True)

        assert emits == [], '未到延迟时长却已产生报警（延迟语义失效）'
        state = mgr.alarm_states[('d1', 'temp')]
        assert state['pending'] is True

    def test_fired_after_delay_elapsed(self, mgr):
        emits = []
        mgr._websocket_emit = emits.append
        rule = _rule('r_delay', delay=10)
        t0 = datetime.now()

        mgr._process_alarm_state('r_delay', rule, 'd1', 'temp', 90.0, t0, True)
        mgr._process_alarm_state(
            'r_delay', rule, 'd1', 'temp', 90.0,
            t0 + timedelta(seconds=11), True)

        assert len(emits) == 1, '条件持续超过 delay 秒后仍未报警（delay 规则永不触发）'
        state = mgr.alarm_states[('d1', 'temp')]
        assert state['pending'] is False and state['confirmed'] is True

    def test_condition_recovered_restarts_delay(self, mgr):
        """条件中途恢复 → 状态清除，延迟计时重新开始"""
        emits = []
        mgr._websocket_emit = emits.append
        rule = _rule('r_delay', delay=10)
        t0 = datetime.now()

        mgr._process_alarm_state('r_delay', rule, 'd1', 'temp', 90.0, t0, True)
        # 恢复
        mgr._process_alarm_state(
            'r_delay', rule, 'd1', 'temp', 70.0, t0 + timedelta(seconds=5), False)
        assert ('d1', 'temp') not in mgr.alarm_states

        # 重新成立：从新时刻重新计时，仅 6 秒不够
        t1 = t0 + timedelta(seconds=6)
        mgr._process_alarm_state('r_delay', rule, 'd1', 'temp', 90.0, t1, True)
        mgr._process_alarm_state(
            'r_delay', rule, 'd1', 'temp', 90.0, t1 + timedelta(seconds=6), True)
        assert emits == [], '条件恢复后延迟计时未重置'

        mgr._process_alarm_state(
            'r_delay', rule, 'd1', 'temp', 90.0, t1 + timedelta(seconds=11), True)
        assert len(emits) == 1

    def test_unusable_timestamps_fail_open_instead_of_never_firing(self, mgr):
        """计时基准丢失/不可解析时必须报警（fail-open），不能永远停在 pending"""
        emits = []
        mgr._websocket_emit = emits.append
        rule = _rule('r_delay', delay=10)
        t0 = datetime.now()

        mgr._process_alarm_state('r_delay', rule, 'd1', 'temp', 90.0, t0, True)
        state = mgr.alarm_states[('d1', 'temp')]
        state.pop('confirm_time', None)
        state.pop('first_trigger_time', None)

        mgr._process_alarm_state(
            'r_delay', rule, 'd1', 'temp', 90.0, t0 + timedelta(seconds=1), True)

        assert len(emits) == 1, '时间戳异常时报警被静默丢弃（永远 pending）'

    def test_string_delay_config_is_normalized(self, mgr):
        """配置写成 delay: "10" 时不得抛 TypeError 打断报警检查"""
        emits = []
        mgr._websocket_emit = emits.append
        rule = _rule('r_str', delay='10')
        t0 = datetime.now()

        mgr._process_alarm_state('r_str', rule, 'd1', 'temp', 90.0, t0, True)
        assert emits == []
        mgr._process_alarm_state(
            'r_str', rule, 'd1', 'temp', 90.0, t0 + timedelta(seconds=11), True)
        assert len(emits) == 1

    def test_illegal_delay_config_degrades_to_zero(self):
        assert AlarmManager._normalize_delay('abc', 'r') == 0.0
        assert AlarmManager._normalize_delay(None) == 0.0
        assert AlarmManager._normalize_delay(-5) == 0.0
        assert AlarmManager._normalize_delay('12.5') == 12.5


# ============================================================
# 2. 去重记账与容量
# ============================================================

class TestDedupBookkeeping:
    """去重表只能记录"确实推送成功"的报警，且必须有容量上限"""

    def test_suppressed_alarm_is_not_recorded_as_emitted(self, mgr):
        """被洪水抑制（未推送）的报警不得写进去重表，否则冷却窗口会压掉真报警"""
        mgr._flood_detector.record_alarm = lambda severity: (
            False, 'suppressed_by_flood_detector')
        emits = []
        mgr._websocket_emit = emits.append
        rule = _rule('r_flood')

        mgr._process_alarm_state('r_flood', rule, 'd1', 'temp', 90.0, datetime.now(), True)

        assert emits == [], '洪水抑制期间仍然推送了报警'
        assert ('r_flood', 'd1', 'temp') not in mgr._emit_history, (
            '被抑制、根本没推送的报警被记成"已推送" —— 冷却窗口内真报警会被静默压掉')

    def test_real_push_is_recorded_and_blocks_repeat(self, mgr):
        """确实推送成功后必须记账，冷却窗口内不再重复推送"""
        emits = []
        mgr._websocket_emit = emits.append
        rule = _rule('r_dedup')

        mgr._trigger_alarm(rule, 'd1', 'temp', 90.0, datetime.now())
        mgr._trigger_alarm(rule, 'd1', 'temp', 91.0, datetime.now())

        assert len(emits) == 1, '冷却窗口未生效，重复推送'
        assert ('r_dedup', 'd1', 'temp') in mgr._emit_history

    def test_acknowledge_suppress_blocks_push(self, mgr):
        emits = []
        mgr._websocket_emit = emits.append
        rule = _rule('r_ack')

        mgr._record_acknowledge('r_ack', 'd1', 'temp')
        mgr._trigger_alarm(rule, 'd1', 'temp', 90.0, datetime.now())

        assert emits == [], '确认抑制窗口未生效'

    def test_emit_history_bounded(self, mgr, monkeypatch):
        monkeypatch.setattr(AlarmManager, '_DEDUP_MAX_ENTRIES', 50)
        for i in range(200):
            mgr._record_emit(f'r{i}', 'd1', 'temp')

        assert len(mgr._emit_history) <= 50, (
            f'去重表无上限，已累积 {len(mgr._emit_history)} 条（长期运行内存泄漏）')
        assert ('r199', 'd1', 'temp') in mgr._emit_history, '最新记录被错误淘汰'

    def test_acknowledge_history_bounded(self, mgr, monkeypatch):
        monkeypatch.setattr(AlarmManager, '_DEDUP_MAX_ENTRIES', 50)
        for i in range(200):
            mgr._record_acknowledge(f'r{i}', 'd1', 'temp')

        assert len(mgr._acknowledge_history) <= 50

    def test_prune_keeps_entries_still_inside_window(self, mgr, monkeypatch):
        """容量清理不得误删仍在冷却窗口内的记录（否则该抑制的又推送了）"""
        monkeypatch.setattr(AlarmManager, '_DEDUP_MAX_ENTRIES', 5)
        mgr.dedup_config.emit_cooldown_seconds = 600
        for i in range(4):
            mgr._record_emit(f'old{i}', 'd1', 'temp')
        mgr._record_emit('fresh', 'd1', 'temp')
        mgr._record_emit('fresh2', 'd1', 'temp')  # 触发一次裁剪

        assert ('fresh', 'd1', 'temp') in mgr._emit_history
        assert ('fresh2', 'd1', 'temp') in mgr._emit_history
        assert mgr._should_emit('fresh', 'd1', 'temp') is False


# ============================================================
# 3. 严重度命名与 KPI 分档
# ============================================================

class TestSeverityNaming:

    def _saturate(self, detector, severity='warning'):
        for _ in range(detector.threshold):
            detector.record_alarm(severity)

    def test_warning_is_not_suppressed_during_flood(self):
        """规则实际用 warning；洪水期不得把告警级当最低档静默抑制"""
        detector = AlarmFloodDetector(window_seconds=60, threshold=3)
        self._saturate(detector)

        should_emit, reason = detector.record_alarm('warning')

        assert should_emit is True, f'warning 在洪水期被错误抑制（reason={reason}）'

    def test_critical_not_suppressed_during_flood(self):
        detector = AlarmFloodDetector(window_seconds=60, threshold=3)
        self._saturate(detector)
        assert detector.record_alarm('critical')[0] is True

    def test_info_is_suppressed_during_flood(self):
        """信息级仍应被抑制（洪水抑制并非失效，只是不再误伤告警级）"""
        detector = AlarmFloodDetector(window_seconds=60, threshold=3)
        self._saturate(detector)

        should_emit, reason = detector.record_alarm('info')

        assert should_emit is False
        assert reason == 'suppressed_by_flood_detector'

    def test_unknown_severity_fails_open(self):
        """未知严重度必须留痕且不抑制（漏报方向不可接受）"""
        detector = AlarmFloodDetector(window_seconds=60, threshold=3)
        self._saturate(detector)

        assert detector.record_alarm('unexpected_level')[0] is True

    def test_legacy_five_level_vocabulary_keeps_ordering(self):
        """五级命名（high/medium/low）仍按原优先级序工作"""
        detector = AlarmFloodDetector(window_seconds=60, threshold=3,
                                      suppress_below='high')
        self._saturate(detector, 'high')

        assert detector.record_alarm('high')[0] is True
        assert detector.record_alarm('medium')[0] is False
        assert detector.record_alarm('low')[0] is False

    def test_kpi_priority_distribution_not_empty_for_live_levels(self):
        """现网规则产出 critical/warning/info，分布必须非空"""
        kpi = AlarmKPI(MagicMock())
        alarms = [
            {'alarm_level': 'critical'},
            {'alarm_level': 'warning'},
            {'alarm_level': 'info'},
        ]

        dist = kpi._calculate_priority_distribution(alarms)

        assert dist == {'low': pytest.approx(1 / 3), 'medium': pytest.approx(1 / 3),
                        'high': 0, 'critical': pytest.approx(1 / 3)}, (
            f'warning 未归入任何分档，分布失真: {dist}')

    def test_kpi_priority_distribution_keeps_legacy_levels(self):
        kpi = AlarmKPI(MagicMock())
        alarms = [{'alarm_level': 'low'}, {'alarm_level': 'low'},
                  {'alarm_level': 'medium'}, {'alarm_level': 'high'}]

        dist = kpi._calculate_priority_distribution(alarms)

        assert dist['low'] == 0.5
        assert dist['medium'] == 0.25
        assert dist['high'] == 0.25

    def test_kpi_distribution_unknown_level_is_traced_not_lost(self):
        kpi = AlarmKPI(MagicMock())
        dist = kpi._calculate_priority_distribution([{'alarm_level': '???'}])
        assert dist['low'] == 1.0
        assert sum(dist.values()) == pytest.approx(1.0)


# ============================================================
# 4. 输出接口兼容 + 失败必须可见
# ============================================================

class _ActivateOnlyOutput:
    """只实现 interfaces.IAlarmOutput（activate_alarm），模拟真实/模拟报警输出"""

    enabled = True

    def __init__(self):
        self.calls = []

    def activate_alarm(self, level, message=''):
        self.calls.append((level, message))
        return True


class _SpeakOnlyBroadcast:
    """只实现 interfaces.IBroadcastSystem（speak），模拟真实/模拟广播"""

    enabled = True

    def __init__(self):
        self.calls = []

    def speak(self, text, level='info', area=None, source='manual'):
        self.calls.append({'text': text, 'level': level,
                           'area': area, 'source': source})
        return {'success': True}


class _NoMethodOutput:
    """既没有 trigger_alarm 也没有 activate_alarm（装配错误）"""

    enabled = True

    def get_status(self):
        return {'enabled': True}


class _RaisingOutput:
    enabled = True

    def trigger_alarm(self, **kwargs):
        raise RuntimeError('modbus 断链')


class _FalseReturningOutput:
    enabled = True

    def trigger_alarm(self, **kwargs):
        return False


class TestOutputInterfaceCompat:

    def test_activate_only_implementation_is_reached(self, mgr):
        """按 interfaces 装配只提供 activate_alarm 的实现时，声光必须真的触发"""
        output = _ActivateOnlyOutput()
        mgr.alarm_output = output
        mgr._websocket_emit = lambda data: None

        mgr._trigger_alarm(_rule('r_out', level='critical'), 'd1', 'temp',
                           90.0, datetime.now())

        assert output.calls == [('critical', '高温报警')], (
            '只实现 activate_alarm 的声光输出未被调用（AttributeError 被静默吞掉）')
        assert mgr._get_output_error_counts()['alarm_output'] == 0

    def test_speak_only_implementation_is_reached(self, mgr):
        """按 interfaces 装配只提供 speak 的广播实现时，广播必须真的发出"""
        broadcast = _SpeakOnlyBroadcast()
        mgr.broadcast_system = broadcast
        mgr._websocket_emit = lambda data: None

        mgr._trigger_alarm(_rule('r_out2', level='critical'), 'd1', 'temp',
                           90.0, datetime.now())

        assert len(broadcast.calls) == 1, (
            '只实现 speak 的广播系统未被调用（speak_alarm AttributeError 被吞掉）')
        assert broadcast.calls[0]['source'] == 'alarm'
        assert broadcast.calls[0]['level'] == 'critical'
        assert mgr._get_output_error_counts()['broadcast'] == 0

    def test_trigger_alarm_implementation_still_preferred(self, mgr):
        """AlarmOutput（trigger_alarm）路径保持不变"""
        output = MagicMock()
        output.enabled = True
        mgr.alarm_output = output
        mgr._websocket_emit = lambda data: None

        mgr._trigger_alarm(_rule('r_out3'), 'd1', 'temp', 90.0, datetime.now())

        output.trigger_alarm.assert_called_once()
        output.activate_alarm.assert_not_called()

    def test_real_interface_implementations_are_wired(self, mgr):
        """真实实现（RealAlarmOutput / RealBroadcastSystem）按接口装配可用"""
        from 报警层.real_alarm_output import RealAlarmOutput
        from 报警层.real_broadcast import RealBroadcastSystem

        output = RealAlarmOutput({'enabled': True})
        # 不触硬件：打桩掉 Modbus 写与闪烁/脉冲线程
        output._write_do = lambda address, value: True
        output._start_flash = lambda pattern: None
        output._start_buzzer_pulse = lambda: None
        broadcast = RealBroadcastSystem({'enabled': True})
        broadcast._publish = lambda topic, payload: True

        mgr.alarm_output = output
        mgr.broadcast_system = broadcast
        mgr._websocket_emit = lambda data: None

        mgr._trigger_alarm(_rule('r_real', level='critical'), 'd1', 'temp',
                           90.0, datetime.now())

        assert output.current_state['level'] == 'critical'
        assert broadcast.history[-1]['success'] is True
        counts = mgr._get_output_error_counts()
        assert counts['alarm_output'] == 0 and counts['broadcast'] == 0

    def test_missing_methods_are_visible_not_swallowed(self, mgr):
        """实现两个方法都没有时必须 ERROR + 计数，绝不静默"""
        mgr.alarm_output = _NoMethodOutput()
        mgr._websocket_emit = lambda data: None

        mgr._trigger_alarm(_rule('r_missing'), 'd1', 'temp', 90.0, datetime.now())

        assert mgr._get_output_error_counts()['alarm_output'] == 1

    def test_output_exception_is_counted(self, mgr):
        mgr.alarm_output = _RaisingOutput()
        mgr._websocket_emit = lambda data: None

        mgr._trigger_alarm(_rule('r_raise'), 'd1', 'temp', 90.0, datetime.now())

        assert mgr._get_output_error_counts()['alarm_output'] == 1

    def test_output_explicit_failure_is_counted(self, mgr):
        mgr.alarm_output = _FalseReturningOutput()
        mgr._websocket_emit = lambda data: None

        mgr._trigger_alarm(_rule('r_false'), 'd1', 'temp', 90.0, datetime.now())

        assert mgr._get_output_error_counts()['alarm_output'] == 1

    def test_broadcast_failure_is_counted(self, mgr):
        class _BrokenBroadcast:
            enabled = True

            def speak(self, text, level='info', area=None, source='manual'):
                raise RuntimeError('MQTT 断链')

        mgr.broadcast_system = _BrokenBroadcast()
        mgr._websocket_emit = lambda data: None

        mgr._trigger_alarm(_rule('r_bcast'), 'd1', 'temp', 90.0, datetime.now())

        assert mgr._get_output_error_counts()['broadcast'] == 1

    def test_websocket_failure_is_counted(self, mgr):
        def _boom(_data):
            raise RuntimeError('ws 断开')

        mgr._websocket_emit = _boom
        mgr._trigger_alarm(_rule('r_ws'), 'd1', 'temp', 90.0, datetime.now())

        assert mgr._get_output_error_counts()['websocket'] == 1

    def test_error_counts_are_exposed_in_statistics(self, mgr):
        mgr.alarm_output = _NoMethodOutput()
        mgr._websocket_emit = lambda data: None
        mgr._trigger_alarm(_rule('r_stats'), 'd1', 'temp', 90.0, datetime.now())

        stats = mgr.get_alarm_statistics()
        assert stats['output']['errors']['alarm_output'] == 1

    def test_simulated_output_and_broadcast_end_to_end(self, mgr):
        """已装配的模拟实现（无硬件）同样必须真的产出声光/广播"""
        from 报警层.simulated_alarm_output import SimulatedAlarmOutput
        from 报警层.simulated_broadcast import SimulatedBroadcastSystem

        output = SimulatedAlarmOutput()
        broadcast = SimulatedBroadcastSystem()
        mgr.alarm_output = output
        mgr.broadcast_system = broadcast
        mgr._websocket_emit = lambda data: None

        mgr._trigger_alarm(_rule('r_sim', level='critical'), 'd1', 'temp',
                           90.0, datetime.now())

        assert output.current_state['red'] is True
        assert len(broadcast.history) == 1
        assert mgr._get_output_error_counts() == {
            'alarm_output': 0, 'broadcast': 0, 'websocket': 0}


# ============================================================
# 5. 定时器停止竞态
# ============================================================

class TestTimerStopRace:

    def test_escalation_timer_does_not_resurrect(self, mgr):
        calls = []
        tick_started = threading.Event()

        def slow_check():
            calls.append(time.monotonic())
            tick_started.set()
            time.sleep(0.2)  # 让 stop 发生在 _tick 的 finally 之前

        mgr.check_escalation = slow_check
        mgr._escalation_interval = POLL
        mgr._start_escalation_timer()

        assert tick_started.wait(MAX_RETURN), '升级定时器未触发'
        mgr.stop_escalation_timer()
        calls_at_stop = len(calls)

        time.sleep(0.4)

        assert len(calls) == calls_at_stop, '定时器在 stop() 后自我复活'
        assert mgr._escalation_timer is None

    def test_flood_timer_does_not_resurrect(self, mgr):
        calls = []
        mgr._flood_detector.check_flood_end = lambda: calls.append(1)
        mgr._flood_check_interval = POLL
        mgr._start_flood_timer()

        deadline = time.monotonic() + MAX_RETURN
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls, '洪水定时器未触发'

        mgr.stop_flood_timer()
        calls_at_stop = len(calls)
        time.sleep(0.3)

        assert len(calls) == calls_at_stop, '洪水定时器在 stop() 后自我复活'
        assert mgr._flood_timer is None

    def test_timers_restart_after_stop(self, mgr):
        """停止后必须能再次启动（stop 标志被清除），否则升级/洪水检查永久失效"""
        mgr._escalation_interval = POLL
        mgr.stop_escalation_timer()
        mgr._start_escalation_timer()
        assert mgr._escalation_timer is not None
        assert mgr._escalation_timer_stop.is_set() is False


# ============================================================
# 6. 升级机制单一化
# ============================================================

class TestEscalationSingleTrack:

    @pytest.fixture
    def manager_with_rules(self, tmp_path):
        cfg = _write_config(tmp_path, (
            'escalation:\n'
            '  timeout_seconds: 9999\n'
            '  check_interval: 1\n'
            '  rules:\n'
            '    - level: 1\n'
            '      timeout_seconds: 600\n'
            '      actions: [notify]\n'
            '      notify_roles: [operator]\n'
            '      message_template: "告警超时: {alarm_message}"\n'
            '    - level: 2\n'
            '      timeout_seconds: 1800\n'
            '      actions: [notify, broadcast]\n'
            '      notify_roles: [engineer]\n'
            '      message_template: "告警升级: {alarm_message}"\n'))
        manager = _make_manager(cfg)
        try:
            yield manager
        finally:
            manager.stop()

    @pytest.fixture
    def manager_with_timeout_only(self, tmp_path):
        cfg = _write_config(tmp_path, 'escalation:\n  timeout_seconds: 700\n')
        manager = _make_manager(cfg)
        try:
            yield manager
        finally:
            manager.stop()

    def test_escalation_timeout_follows_manager_rules(self, manager_with_rules):
        """_escalation_timeout 必须与管理器规则同步，不能各跑一套"""
        assert manager_with_rules._escalation_manager is not None
        assert manager_with_rules._escalation_timeout == 600, (
            '单级超时仍取配置的 9999，与升级管理器的 600/1800 规则各跑一套')

    def test_config_timeout_becomes_a_rule_when_rules_absent(self, manager_with_timeout_only):
        """只配 timeout_seconds 时，必须生成同超时的规则，而不是走默认 300/900/1800"""
        rules = manager_with_timeout_only._escalation_manager.get_rules()
        assert [r['timeout_seconds'] for r in rules] == [700]
        assert manager_with_timeout_only._escalation_timeout == 700

    def test_manager_own_thread_is_not_started(self, manager_with_rules):
        """只有报警升级定时器这一个驱动器"""
        escalation_manager = manager_with_rules._escalation_manager
        assert escalation_manager._running is False, '管理器自带线程仍在跑（两套调度并存）'
        assert escalation_manager._thread is None
        assert manager_with_rules._escalation_timer is not None

    def test_check_escalation_delegates_and_notifies_external_callbacks(
            self, manager_with_rules):
        """多级升级结果必须按既有契约通知 add_escalation_callback 的调用方"""
        manager = manager_with_rules
        escalated = []
        manager.add_escalation_callback(escalated.append)
        rule = _rule('r_esc')
        manager._process_alarm_state(
            'r_esc', rule, 'd1', 'temp', 90.0, datetime.now(), True)
        # 把升级计时回拨到超过首级规则（600s）之后
        tracked = manager._escalation_manager._states['r_esc:d1:temp']
        tracked.first_trigger_time -= 601

        manager.check_escalation()
        manager.check_escalation()  # 同一级不得重复升级

        assert len(escalated) == 1, f'外部升级回调应恰好收到 1 次，实际 {len(escalated)}'
        assert escalated[0]['alarm_id'] == 'r_esc'
        assert escalated[0]['device_id'] == 'd1'
        assert escalated[0]['register_name'] == 'temp'
        assert escalated[0]['escalation_level'] == 1
        assert manager.alarm_states[('d1', 'temp')]['escalated'] is True

    def test_cleared_alarm_stops_escalation_tracking(self, manager_with_rules):
        """报警恢复正常后必须撤销升级跟踪，否则会被升级到"已恢复的报警"上"""
        manager = manager_with_rules
        rule = _rule('r_clear')
        manager._process_alarm_state(
            'r_clear', rule, 'd1', 'temp', 90.0, datetime.now(), True)
        assert manager._escalation_manager._states, '报警未被升级管理器跟踪'

        manager._process_alarm_state(
            'r_clear', rule, 'd1', 'temp', 70.0, datetime.now(), False)

        assert not manager._escalation_manager._states, (
            '报警已清除，升级管理器仍在跟踪 → 会对已恢复的报警升级')

    def test_reset_alarm_stops_escalation_tracking(self, manager_with_rules):
        manager = manager_with_rules
        rule = _rule('r_reset')
        manager._process_alarm_state(
            'r_reset', rule, 'd1', 'temp', 90.0, datetime.now(), True)

        manager.reset_alarm('d1')

        assert not manager._escalation_manager._states

    def test_legacy_single_timeout_path_still_works_without_manager(self, mgr):
        """未配 escalation 段时保留单级超时升级（向后兼容）"""
        assert mgr._escalation_manager is None
        mgr._escalation_timeout = 0
        escalated = []
        mgr.add_escalation_callback(escalated.append)
        rule = _rule('r_legacy')
        mgr._process_alarm_state(
            'r_legacy', rule, 'd1', 'temp', 90.0, datetime.now(), True)

        mgr.check_escalation()

        assert len(escalated) == 1
        assert escalated[0]['alarm_id'] == 'r_legacy'
        assert 'escalation_reason' in escalated[0]
