"""生命周期 / 关闭（P1 健壮性）回归测试

覆盖两类长期运行必炸的缺陷：
1. 健康检查超时被吞掉 —— 检查项卡死时健康检查自身永久挂死；
2. 后台线程与定时器泄漏 —— 线程无停止路径、定时器在 stop() 后自我复活。

所有用例必须在 5 秒内跑完，因此统一使用短超时 / 短间隔，
并用 time.monotonic() 断言耗时上界（不写会真的跑几分钟的 sleep）。
"""

import threading
import time
from unittest.mock import MagicMock

import pytest

from core.health_checker import HealthCheck, HealthChecker, HealthStatus
from 报警层.alarm_manager import AlarmManager

TIMEOUT = 0.2          # 健康检查超时
MAX_RETURN = 2.0       # run() 必须在此时间内返回
POLL = 0.05            # 定时器/线程轮询间隔


def _hanging_check():
    """返回一个永不返回的检查函数（模拟卡死的检查项）。

    用 Event 而非 sleep：用例结束时可释放，避免遗留长时间睡眠线程。
    """
    release = threading.Event()

    def _hang():
        release.wait(30)

    return _hang, release


@pytest.fixture
def alarm_manager():
    """构造 AlarmManager，并在用例结束后确保后台资源被停止"""
    manager = AlarmManager(MagicMock(), config_path='配置/alarms.yaml')
    try:
        yield manager
    finally:
        manager.stop()


# ============================================================
# P1-1 健康检查超时必须被真正处理
# ============================================================

class TestHealthCheckTimeout:

    def test_timeout_returns_instead_of_hanging(self):
        """检查项卡死时 run() 必须在超时后返回，并标记为 unhealthy"""
        hang, release = _hanging_check()
        check = HealthCheck('hang', hang, timeout=TIMEOUT)

        begin = time.monotonic()
        result = check.run()
        elapsed = time.monotonic() - begin
        release.set()

        assert elapsed < MAX_RETURN, f'健康检查未在超时后返回，耗时 {elapsed:.2f}s'
        assert result['status'] == HealthStatus.UNHEALTHY
        assert result['details']['error'] == 'TimeoutError'
        assert result['duration'] < MAX_RETURN
        # 结果必须落到状态与历史，不能静默丢弃
        assert check.last_result['status'] == HealthStatus.UNHEALTHY
        assert check.history[-1]['status'] == HealthStatus.UNHEALTHY

    def test_health_checker_check_reports_timeout(self):
        """经 HealthChecker.check() 的注册表路径同样返回超时结果"""
        HealthChecker.clear()
        hang, release = _hanging_check()
        HealthChecker.register('hang_check', hang, interval=1, timeout=TIMEOUT)

        begin = time.monotonic()
        result = HealthChecker.check('hang_check')
        elapsed = time.monotonic() - begin
        release.set()

        assert elapsed < MAX_RETURN, f'HealthChecker.check 挂死，耗时 {elapsed:.2f}s'
        assert result['status'] == HealthStatus.UNHEALTHY
        assert result['details']['error'] == 'TimeoutError'

    def test_check_all_marks_timeout_as_unhealthy(self):
        """check() 聚合结果中，超时项必须体现为 unhealthy"""
        HealthChecker.clear()
        hang, release = _hanging_check()
        HealthChecker.register('hang_check', hang, interval=1, timeout=TIMEOUT)
        HealthChecker.register('ok_check', lambda: {'status': HealthStatus.HEALTHY})

        result = HealthChecker.check()
        release.set()

        assert result['checks']['hang_check']['status'] == HealthStatus.UNHEALTHY
        assert result['status'] == HealthStatus.UNHEALTHY

    def test_previous_hung_worker_does_not_stack_new_thread(self):
        """上一次检查仍卡死时，下一次 run() 立即返回，不再叠加工作线程"""
        hang, release = _hanging_check()
        check = HealthCheck('hang', hang, timeout=TIMEOUT)

        first = check.run()
        worker = check._worker

        begin = time.monotonic()
        second = check.run()
        elapsed = time.monotonic() - begin
        release.set()

        assert first['status'] == HealthStatus.UNHEALTHY
        assert second['status'] == HealthStatus.UNHEALTHY
        assert elapsed < TIMEOUT, f'卡死检查项导致重复等待超时，耗时 {elapsed:.2f}s'
        assert check._worker is worker, '卡死时不应再创建新的工作线程'


# ============================================================
# P1-2 线程与定时器必须可停止、停止后不复活
# ============================================================

class TestAlarmManagerLifecycle:

    def test_stop_terminates_config_watcher_thread(self, alarm_manager):
        """stop() 后配置热重载线程不再存活"""
        manager = alarm_manager
        manager._config_watch_interval = POLL

        # 注意：其他测试（test_alarm_layer / test_alarm_manager ...）也会构造
        # AlarmManager，而它们的 __init__ 会启动热重载线程且从不调 stop()，
        # 于是进程里可能已经存在同名孤儿线程。所以这里**不能**断言"全进程
        # 没有 alarm-config-watcher 线程"（那样会被别的测试污染），
        # 只能断言"本实例启动的那一条已退出，且没有新增泄漏"。
        pre_existing = {t for t in threading.enumerate()
                        if t.name == 'alarm-config-watcher'}

        manager._start_config_watcher()
        thread = manager._config_watcher_thread
        assert thread is not None and thread.is_alive()

        manager.stop()

        assert not thread.is_alive(), 'stop() 后配置热重载线程仍在运行'
        assert manager._config_watcher_thread is None
        assert manager._config_watcher_running is False

        leaked = {t for t in threading.enumerate()
                  if t.name == 'alarm-config-watcher' and t.is_alive()} - pre_existing
        assert not leaked, f'stop() 后本实例的热重载线程仍存活: {leaked}'

    def test_config_watcher_stop_is_idempotent(self, alarm_manager):
        """重复停止配置热重载线程不抛异常"""
        manager = alarm_manager
        manager._config_watch_interval = POLL
        manager._start_config_watcher()

        manager.stop_config_watcher()
        manager.stop_config_watcher()

        assert manager._config_watcher_thread is None

    def test_escalation_timer_does_not_resurrect_after_stop(self, alarm_manager):
        """在 _tick 执行期间 stop()，定时器不得自我复活"""
        manager = alarm_manager
        calls = []
        tick_started = threading.Event()

        def slow_check():
            calls.append(time.monotonic())
            tick_started.set()
            time.sleep(0.2)  # 模拟 tick 回调执行较慢

        manager.check_escalation = slow_check
        manager._escalation_interval = POLL
        manager._start_escalation_timer()

        assert tick_started.wait(MAX_RETURN), '升级定时器未触发'

        manager.stop_escalation_timer()
        assert manager._escalation_timer is None

        calls_at_stop = len(calls)
        time.sleep(0.4)  # 若会自我复活，0.05s 周期内必然再触发多次

        assert len(calls) == calls_at_stop, '定时器在 stop() 后仍被回调（自我复活）'
        assert manager._escalation_timer is None

    def test_flood_timer_does_not_resurrect_after_stop(self, alarm_manager):
        """告警洪水检查定时器同样在 stop() 后不再重排"""
        manager = alarm_manager
        calls = []
        manager._flood_detector.check_flood_end = lambda: calls.append(1)
        manager._flood_check_interval = POLL
        manager._start_flood_timer()

        deadline = time.monotonic() + MAX_RETURN
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls, '洪水检查定时器未触发'

        manager.stop_flood_timer()
        calls_at_stop = len(calls)
        time.sleep(0.3)

        assert len(calls) == calls_at_stop, '洪水定时器在 stop() 后仍被回调'
        assert manager._flood_timer is None

    def test_stop_is_idempotent(self, alarm_manager):
        """stop()/shutdown() 连续调用不抛异常"""
        manager = alarm_manager

        manager.stop()
        manager.stop()
        manager.shutdown()

        assert manager._escalation_timer is None
        assert manager._flood_timer is None
        assert manager._config_watcher_thread is None

    def test_stop_cancels_timers_started_by_constructor(self, alarm_manager):
        """构造时启动的两个定时器可被 stop() 彻底取消"""
        manager = alarm_manager
        assert manager._escalation_timer is not None
        assert manager._flood_timer is not None

        manager.stop()

        assert manager._escalation_timer is None
        assert manager._flood_timer is None
