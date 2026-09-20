"""
Tests for 采集层.data_collector: DataCollector lifecycle, queue, keyword matching, dispatch
"""

import pytest
import queue
import threading
import time
import math
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

from 采集层.data_collector import DataCollector, DataQualityAssessor, _has_keyword


# ============================================================
# _has_keyword Tests
# ============================================================

class TestHasKeyword:

    def test_matches_power_keyword(self):
        """Matches power-related keywords case-insensitively"""
        assert _has_keyword('active_power', ('power', 'watt', 'kw')) is True

    def test_matches_case_insensitive(self):
        """Keyword matching is case-insensitive"""
        assert _has_keyword('TEMPERATURE', ('temperature',)) is True

    def test_no_match(self):
        """Returns False when no keyword matches"""
        assert _has_keyword('flow_rate', ('temperature', 'pressure')) is False

    def test_partial_match(self):
        """Matches substring within register name"""
        assert _has_keyword('motor_speed_rpm', ('speed',)) is True

    def test_empty_name(self):
        """Empty name returns False"""
        assert _has_keyword('', ('power',)) is False

    def test_empty_keywords(self):
        """Empty keywords tuple returns False"""
        assert _has_keyword('power', ()) is False


# ============================================================
# DataCollector Lifecycle Tests
# ============================================================

class TestDataCollectorLifecycle:

    @pytest.fixture
    def mocks(self):
        """Create mock dependencies for DataCollector"""
        dm = MagicMock()
        dm.get_all_devices.return_value = {
            'pump_01': {
                'protocol': 'modbus_tcp',
                'enabled': True,
                'collection_interval': 5,
                'registers': []
            },
        }
        dm.get_device_status.return_value = {'connected': True, 'stats': {'state': 'running'}}
        db = MagicMock()
        alarm = MagicMock()
        return dm, db, alarm

    def test_start_sets_running(self, mocks):
        """start() sets running flag to True"""
        dm, db, alarm = mocks
        collector = DataCollector(dm, db, alarm)
        collector.start()
        assert collector.running is True
        collector.stop()

    def test_start_creates_process_thread(self, mocks):
        """start() creates a data processing thread"""
        dm, db, alarm = mocks
        collector = DataCollector(dm, db, alarm)
        collector.start()
        assert collector.process_thread is not None
        assert collector.process_thread.is_alive()
        collector.stop()

    def test_double_start_warns(self, mocks):
        """Calling start() twice logs warning, doesn't crash"""
        dm, db, alarm = mocks
        collector = DataCollector(dm, db, alarm)
        collector.start()
        collector.start()  # second start should be no-op
        assert collector.running is True
        collector.stop()

    def test_stop_clears_running(self, mocks):
        """stop() clears running flag"""
        dm, db, alarm = mocks
        collector = DataCollector(dm, db, alarm)
        collector.start()
        collector.stop()
        assert collector.running is False

    def test_stop_clears_tasks(self, mocks):
        """stop() clears all device tasks"""
        dm, db, alarm = mocks
        collector = DataCollector(dm, db, alarm)
        collector.start()
        collector.stop()
        assert len(collector.tasks) == 0

    def test_stop_drains_queue(self, mocks):
        """stop() drains the data queue"""
        dm, db, alarm = mocks
        collector = DataCollector(dm, db, alarm)
        collector.start()
        # Manually add items to queue
        for i in range(10):
            collector.data_queue.put_nowait({'device_id': 'test', 'value': i})
        collector.stop()
        assert collector.data_queue.empty()


# ============================================================
# Data Queue Tests
# ============================================================

class TestDataQueue:

    def test_queue_maxsize(self):
        """DataCollector creates queue with maxsize 200000"""
        dm = MagicMock()
        dm.get_all_devices.return_value = {}
        collector = DataCollector(dm, MagicMock())
        assert collector.data_queue.maxsize == 200000

    def test_queue_put_get(self):
        """Can put and get items from data queue"""
        dm = MagicMock()
        dm.get_all_devices.return_value = {}
        collector = DataCollector(dm, MagicMock())
        item = {'device_id': 'd1', 'register_name': 'temp', 'value': 25.0, 'timestamp': datetime.now(), 'unit': 'C'}
        collector.data_queue.put_nowait(item)
        got = collector.data_queue.get_nowait()
        assert got['value'] == 25.0


# ============================================================
# Stats Tests
# ============================================================

class TestStats:

    def test_initial_stats(self):
        """Initial stats are zeroed"""
        dm = MagicMock()
        dm.get_all_devices.return_value = {}
        collector = DataCollector(dm, MagicMock())
        stats = collector.get_stats()
        assert stats['total_collections'] == 0
        assert stats['successful_collections'] == 0

    def test_stats_thread_safety(self):
        """_inc_stat is thread-safe"""
        dm = MagicMock()
        dm.get_all_devices.return_value = {}
        collector = DataCollector(dm, MagicMock())
        barrier = threading.Barrier(10)

        def increment():
            barrier.wait(timeout=5)
            for _ in range(100):
                collector._inc_stat('total_collections')

        threads = [threading.Thread(target=increment) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert collector.stats['total_collections'] == 1000


# ============================================================
# Dynamic Interval Tests
# ============================================================

class TestDynamicInterval:

    def test_fault_state_returns_short_interval(self):
        """Fault state returns 1s interval"""
        dm = MagicMock()
        dm.get_all_devices.return_value = {}
        dm.get_device_status.return_value = {'stopped': True, 'stats': {}}
        collector = DataCollector(dm, MagicMock())
        interval = collector._get_dynamic_interval('dev1', 5)
        assert interval == 1

    def test_running_state_returns_running_interval(self):
        """Running state returns 5s interval"""
        dm = MagicMock()
        dm.get_all_devices.return_value = {}
        dm.get_device_status.return_value = {'stats': {'state': 'running'}}
        collector = DataCollector(dm, MagicMock())
        interval = collector._get_dynamic_interval('dev1', 5)
        assert interval == 5

    def test_idle_state_returns_idle_interval(self):
        """Idle state returns 10s interval"""
        dm = MagicMock()
        dm.get_all_devices.return_value = {}
        dm.get_device_status.return_value = {'stats': {'state': 'idle'}}
        collector = DataCollector(dm, MagicMock())
        interval = collector._get_dynamic_interval('dev1', 5)
        assert interval == 10

    def test_stopped_state_returns_stopped_interval(self):
        """Stopped state returns 30s interval"""
        dm = MagicMock()
        dm.get_all_devices.return_value = {}
        dm.get_device_status.return_value = {'stats': {'state': 'stopped'}}
        collector = DataCollector(dm, MagicMock())
        interval = collector._get_dynamic_interval('dev1', 5)
        assert interval == 30

    def test_unknown_state_returns_base_interval(self):
        """Unknown state falls back to base interval"""
        dm = MagicMock()
        dm.get_all_devices.return_value = {}
        dm.get_device_status.return_value = {'stats': {'state': 'unknown'}}
        collector = DataCollector(dm, MagicMock())
        interval = collector._get_dynamic_interval('dev1', 7)
        assert interval == 7

    def test_exception_returns_base_interval(self):
        """Exception in status lookup returns base interval"""
        dm = MagicMock()
        dm.get_all_devices.return_value = {}
        dm.get_device_status.side_effect = RuntimeError('fail')
        collector = DataCollector(dm, MagicMock())
        interval = collector._get_dynamic_interval('dev1', 5)
        assert interval == 5

    def test_low_health_score_returns_fault_interval(self):
        """Low predictive health score returns fault interval"""
        dm = MagicMock()
        dm.get_all_devices.return_value = {}
        dm.get_device_status.return_value = {'stats': {'state': 'running'}}
        pm = MagicMock()
        pm.get_health_scores.return_value = {
            'dev1:temp': {'device_id': 'dev1', 'health_score': 30}
        }
        collector = DataCollector(dm, MagicMock(), predictive_maintenance=pm)
        interval = collector._get_dynamic_interval('dev1', 5)
        assert interval == 1

    def test_warning_health_score_returns_warning_interval(self):
        """Warning health score returns warning interval (2s)"""
        dm = MagicMock()
        dm.get_all_devices.return_value = {}
        dm.get_device_status.return_value = {'stats': {'state': 'running'}}
        pm = MagicMock()
        pm.get_health_scores.return_value = {
            'dev1:temp': {'device_id': 'dev1', 'health_score': 50}
        }
        collector = DataCollector(dm, MagicMock(), predictive_maintenance=pm)
        interval = collector._get_dynamic_interval('dev1', 5)
        assert interval == 2


# ============================================================
# Dispatch Intelligence Tests
# ============================================================

class TestDispatchIntelligence:

    def _make_collector(self, **kwargs):
        dm = MagicMock()
        dm.get_all_devices.return_value = {}
        return DataCollector(dm, MagicMock(), **kwargs)

    def test_dispatch_calls_predictive_maintenance(self):
        """Dispatch calls feed_data on predictive_maintenance module"""
        pm = MagicMock()
        collector = self._make_collector(predictive_maintenance=pm)
        data = {'device_id': 'd1', 'register_name': 'temp', 'value': 25.0, 'timestamp': datetime.now()}
        collector._dispatch_intelligence(data)
        pm.feed_data.assert_called_once_with('d1', 'temp', 25.0, data['timestamp'])

    def test_dispatch_calls_edge_decision(self):
        """Dispatch calls update_data on edge_decision module"""
        ed = MagicMock()
        collector = self._make_collector(edge_decision=ed)
        data = {'device_id': 'd1', 'register_name': 'temp', 'value': 25.0, 'timestamp': datetime.now()}
        collector._dispatch_intelligence(data)
        ed.update_data.assert_called_once_with('d1:temp', 25.0)

    def test_dispatch_oee_status_update(self):
        """Dispatch routes status register to OEE calculator"""
        oee = MagicMock()
        collector = self._make_collector(oee_calculator=oee)
        data = {'device_id': 'd1', 'register_name': 'machine_status', 'value': 2, 'timestamp': datetime.now()}
        collector._dispatch_intelligence(data)
        oee.update_device_state.assert_called_once_with('d1', 'running')

    def test_dispatch_oee_count_update(self):
        """Dispatch routes count register to OEE calculator with auto good_count"""
        oee = MagicMock()
        collector = self._make_collector(oee_calculator=oee)
        data = {'device_id': 'd1', 'register_name': 'production_count', 'value': 100, 'timestamp': datetime.now()}
        collector._dispatch_intelligence(data)
        oee.record_production.assert_called_once_with('d1', count=100, good_count=98)

    def test_dispatch_spc_temperature(self):
        """Dispatch routes temperature to SPC analyzer"""
        spc = MagicMock()
        collector = self._make_collector(spc_analyzer=spc)
        data = {'device_id': 'd1', 'register_name': 'temperature', 'value': 80.0, 'timestamp': datetime.now()}
        collector._dispatch_intelligence(data)
        spc.feed_data.assert_called_once_with('d1', 'temperature', 80.0)

    def test_dispatch_energy_power(self):
        """Dispatch routes power data to energy manager"""
        em = MagicMock()
        collector = self._make_collector(energy_manager=em)
        data = {'device_id': 'd1', 'register_name': 'active_power', 'value': 5000, 'timestamp': datetime.now()}
        collector._dispatch_intelligence(data)
        em.feed_power_data.assert_called_once()

    def test_dispatch_device_control_interlock(self):
        """Dispatch calls check_interlocks on device_control"""
        dc = MagicMock()
        collector = self._make_collector(device_control=dc)
        data = {'device_id': 'd1', 'register_name': 'temp', 'value': 100.0, 'timestamp': datetime.now()}
        collector._dispatch_intelligence(data)
        dc.check_interlocks.assert_called_once_with('d1', 'temp', 100.0)

    def test_dispatch_vibration(self):
        """Dispatch routes data to vibration analyzer"""
        va = MagicMock()
        collector = self._make_collector(vibration_analyzer=va)
        data = {'device_id': 'd1', 'register_name': 'vibration_x', 'value': 2.5, 'timestamp': datetime.now()}
        collector._dispatch_intelligence(data)
        va.feed_data.assert_called_once_with('d1', 'vibration_x', 2.5, data['timestamp'])


# ============================================================
# Device Task Management Tests
# ============================================================

class TestDeviceTaskManagement:

    def test_start_device_task_when_not_running(self):
        """start_device_task skips if collector not running"""
        dm = MagicMock()
        dm.get_all_devices.return_value = {}
        collector = DataCollector(dm, MagicMock())
        collector.running = False
        collector.start_device_task('d1', {'protocol': 'modbus_tcp', 'enabled': True})
        assert 'd1' not in collector.tasks

    def test_start_device_task_disabled_device(self):
        """start_device_task skips disabled devices"""
        dm = MagicMock()
        dm.get_all_devices.return_value = {}
        collector = DataCollector(dm, MagicMock())
        collector.running = True
        collector.start_device_task('d1', {'protocol': 'modbus_tcp', 'enabled': False})
        assert 'd1' not in collector.tasks

    def test_remove_device_task(self):
        """remove_device_task removes task and cancels timer"""
        dm = MagicMock()
        dm.get_all_devices.return_value = {}
        collector = DataCollector(dm, MagicMock())
        # Simulate a running task
        timer = MagicMock()
        collector.tasks['d1'] = timer
        collector.remove_device_task('d1')
        timer.cancel.assert_called_once()
        assert 'd1' not in collector.tasks


# ============================================================
# DataQualityAssessor Tests (OPC UA Standard)
# ============================================================

class TestDataQualityAssessor:

    def test_good_quality_normal_value(self):
        """Normal value with running device returns GOOD"""
        q = DataQualityAssessor.assess(
            value=25.0, register_name='temperature',
            device_status='running'
        )
        assert q == DataQualityAssessor.GOOD

    def test_good_quality_default_status(self):
        """Unknown device status with normal value returns GOOD"""
        q = DataQualityAssessor.assess(
            value=100.0, register_name='pressure',
            device_status='unknown'
        )
        assert q == DataQualityAssessor.GOOD

    def test_bad_comm_failure_offline(self):
        """Offline device returns BAD_COMM_FAILURE"""
        q = DataQualityAssessor.assess(
            value=25.0, register_name='temperature',
            device_status='offline'
        )
        assert q == DataQualityAssessor.BAD_COMM_FAILURE

    def test_bad_comm_failure_disconnected(self):
        """Disconnected device returns BAD_COMM_FAILURE"""
        q = DataQualityAssessor.assess(
            value=25.0, register_name='temperature',
            device_status='disconnected'
        )
        assert q == DataQualityAssessor.BAD_COMM_FAILURE

    def test_bad_sensor_failure_fault(self):
        """Faulty device returns BAD_SENSOR_FAILURE"""
        q = DataQualityAssessor.assess(
            value=25.0, register_name='temperature',
            device_status='fault'
        )
        assert q == DataQualityAssessor.BAD_SENSOR_FAILURE

    def test_bad_none_value(self):
        """None value returns BAD"""
        q = DataQualityAssessor.assess(
            value=None, register_name='temperature',
            device_status='running'
        )
        assert q == DataQualityAssessor.BAD

    def test_bad_nan_value(self):
        """NaN value returns BAD"""
        q = DataQualityAssessor.assess(
            value=float('nan'), register_name='temperature',
            device_status='running'
        )
        assert q == DataQualityAssessor.BAD

    def test_bad_sensor_failure_out_of_range(self):
        """Extremely large value returns BAD_SENSOR_FAILURE"""
        q = DataQualityAssessor.assess(
            value=1e11, register_name='temperature',
            device_status='running'
        )
        assert q == DataQualityAssessor.BAD_SENSOR_FAILURE

    def test_bad_sensor_failure_negative_out_of_range(self):
        """Extremely large negative value returns BAD_SENSOR_FAILURE"""
        q = DataQualityAssessor.assess(
            value=-1e11, register_name='temperature',
            device_status='running'
        )
        assert q == DataQualityAssessor.BAD_SENSOR_FAILURE

    def test_uncertain_stale_data(self):
        """Unchanged value for >300s returns UNCERTAIN_LAST_USABLE"""
        q = DataQualityAssessor.assess(
            value=25.0, register_name='temperature',
            device_status='running',
            last_value=25.0,
            last_time=time.time() - 301
        )
        assert q == DataQualityAssessor.UNCERTAIN_LAST_USABLE

    def test_good_recent_same_value(self):
        """Same value within 300s returns GOOD"""
        q = DataQualityAssessor.assess(
            value=25.0, register_name='temperature',
            device_status='running',
            last_value=25.0,
            last_time=time.time() - 100
        )
        assert q == DataQualityAssessor.GOOD

    def test_good_changed_value(self):
        """Changed value returns GOOD even if old value is stale"""
        q = DataQualityAssessor.assess(
            value=26.0, register_name='temperature',
            device_status='running',
            last_value=25.0,
            last_time=time.time() - 400
        )
        assert q == DataQualityAssessor.GOOD

    def test_good_no_last_value(self):
        """No previous value returns GOOD"""
        q = DataQualityAssessor.assess(
            value=25.0, register_name='temperature',
            device_status='running',
            last_value=None,
            last_time=None
        )
        assert q == DataQualityAssessor.GOOD

    def test_quality_code_values(self):
        """Quality codes match OPC UA standard values"""
        assert DataQualityAssessor.GOOD == 192
        assert DataQualityAssessor.UNCERTAIN == 104
        assert DataQualityAssessor.BAD == 0
        assert DataQualityAssessor.BAD_SENSOR_FAILURE == 4
        assert DataQualityAssessor.BAD_COMM_FAILURE == 6
        assert DataQualityAssessor.BAD_OUT_OF_SERVICE == 8
        assert DataQualityAssessor.UNCERTAIN_SENSOR_CAL == 80
        assert DataQualityAssessor.UNCERTAIN_LAST_USABLE == 64

    def test_offline_takes_precedence_over_none(self):
        """Offline status takes precedence over None value"""
        q = DataQualityAssessor.assess(
            value=None, register_name='temperature',
            device_status='offline'
        )
        assert q == DataQualityAssessor.BAD_COMM_FAILURE

    def test_fault_takes_precedence_over_nan(self):
        """Fault status takes precedence over NaN value"""
        q = DataQualityAssessor.assess(
            value=float('nan'), register_name='temperature',
            device_status='fault'
        )
        assert q == DataQualityAssessor.BAD_SENSOR_FAILURE

    def test_integer_value(self):
        """Integer value is handled correctly"""
        q = DataQualityAssessor.assess(
            value=100, register_name='count',
            device_status='running'
        )
        assert q == DataQualityAssessor.GOOD

    def test_zero_value(self):
        """Zero value returns GOOD (valid for many sensors)"""
        q = DataQualityAssessor.assess(
            value=0.0, register_name='pressure',
            device_status='running'
        )
        assert q == DataQualityAssessor.GOOD


# ============================================================
# 路径解析回归（2026-09 审计 P2-5）
# ============================================================

class TestPersistDirPathResolution:
    """``DiskBackedQueue`` 的持久化目录必须是绝对路径。

    为什么值得单独立测试：``DEFAULT_PERSIST_DIR = 'data/queue'`` 是**相对**
    字面量。它只在「CWD 恰好是仓库根」时才落到项目数据目录；从服务、计划
    任务、PyInstaller 产物启动时会在启动目录下**另建一个 data/queue**，
    于是待发数据分裂成两处 —— 重启后 ``pending_data.jsonl`` 看着是空的、
    数据对不上，而日志里没有任何异常。这是典型的"静默错库"故障。

    注意 ``SCADA_QUEUE_PERSIST_DIR`` 环境变量若已是绝对路径，
    ``paths.resolve`` 走 passthrough，所以现有测试（传 tmp 目录）不受影响。
    """

    def test_default_persist_dir_is_absolute(self, monkeypatch, tmp_path):
        """不传参 + 无环境变量时，落点必须在 BASE_DIR 下而非 CWD"""
        import os
        from 采集层.data_collector import DiskBackedQueue

        monkeypatch.delenv('SCADA_QUEUE_PERSIST_DIR', raising=False)
        # 把 BASE_DIR 指到 tmp，避免往真实项目 data/ 写探针文件
        import paths
        monkeypatch.setattr(paths, 'BASE_DIR', tmp_path)

        q = DiskBackedQueue(maxsize=10)
        assert q._persist_dir.is_absolute(), '持久化目录不是绝对路径'
        assert q._persist_dir == tmp_path / 'data' / 'queue'

    def test_persist_dir_independent_of_cwd(self, monkeypatch, tmp_path):
        """切换 CWD 不会改变落点 —— 锁死"与 CWD 无关"这个契约"""
        import os
        import paths
        from 采集层.data_collector import DiskBackedQueue

        monkeypatch.delenv('SCADA_QUEUE_PERSIST_DIR', raising=False)
        base = tmp_path / 'fake_root'
        base.mkdir()
        monkeypatch.setattr(paths, 'BASE_DIR', base)

        old = os.getcwd()
        elsewhere = tmp_path / 'elsewhere'
        elsewhere.mkdir()
        os.chdir(elsewhere)
        try:
            q = DiskBackedQueue(maxsize=10)
            assert q._persist_dir == base / 'data' / 'queue'
            # 关键断言：绝不能落到当前工作目录
            assert not (elsewhere / 'data' / 'queue').exists(), (
                '持久化目录被错误地建在了 CWD 下（CWD 依赖 bug 复现）'
            )
        finally:
            os.chdir(old)

    def test_explicit_absolute_persist_dir_is_honored(self, tmp_path):
        """显式传绝对路径时必须原样使用（测试隔离依赖这一点）"""
        from 采集层.data_collector import DiskBackedQueue

        target = tmp_path / 'explicit'
        q = DiskBackedQueue(maxsize=10, persist_dir=str(target))
        assert q._persist_dir == target

    def test_env_var_absolute_is_honored(self, monkeypatch, tmp_path):
        """环境变量给绝对路径时不能被 BASE_DIR 拼接污染"""
        from 采集层.data_collector import DiskBackedQueue

        target = tmp_path / 'from_env'
        monkeypatch.setenv('SCADA_QUEUE_PERSIST_DIR', str(target))
        q = DiskBackedQueue(maxsize=10)
        assert q._persist_dir == target

    def test_pending_file_lives_under_persist_dir(self, monkeypatch, tmp_path):
        """持久化文件路径必须由解析后的目录派生"""
        import paths
        from 采集层.data_collector import DiskBackedQueue

        monkeypatch.delenv('SCADA_QUEUE_PERSIST_DIR', raising=False)
        monkeypatch.setattr(paths, 'BASE_DIR', tmp_path)

        q = DiskBackedQueue(maxsize=10)
        assert q._persist_file.name == 'pending_data.jsonl'
        assert q._persist_file.parent == q._persist_dir
        assert q._persist_file.is_absolute()

    def test_ctor_does_not_write_into_repo_data_dir(self, monkeypatch, tmp_path):
        """构造 DiskBackedQueue 绝不能在仓库真实 data/queue 下落盘。

        这是上面那个 106MB / 80 万行 ``pending_data.jsonl`` 事故的直接回归测试。
        用 `paths.resolve` 之后默认落点是**仓库内**的 ``data/queue``（真实数据目录），
        所以只要某个测试忘了设 ``SCADA_QUEUE_PERSIST_DIR``，就会往生产数据文件里
        追加探针数据 —— 而且下一个测试 `_recover_from_disk()` 会把它读走并
        ``unlink()``，导致一批**与队列无关的**测试集体飘红（数据完整性 / 灾备 /
        批量缩容最先暴露），单跑每个用例却又都是绿的。

        这里把 BASE_DIR 指到 tmp，然后断言仓库真实目录下没有新文件产生 ——
        相当于把「路径解析必须尊重 BASE_DIR」和「测试不许污染生产数据」两件事
        同时钉死。
        """
        import paths
        from 采集层.data_collector import DiskBackedQueue

        repo_queue = Path(paths.PROJECT_ROOT) / 'data' / 'queue'
        real_probe = repo_queue / 'pending_data.jsonl'
        size_before = real_probe.stat().st_size if real_probe.is_file() else None

        monkeypatch.delenv('SCADA_QUEUE_PERSIST_DIR', raising=False)
        monkeypatch.setattr(paths, 'BASE_DIR', tmp_path)

        q = DiskBackedQueue(maxsize=10)
        # 落点跟着 BASE_DIR 走，而不是跟着 PROJECT_ROOT 走
        assert q._persist_dir == tmp_path / 'data' / 'queue'
        assert repo_queue not in q._persist_dir.parents

        # 真实生产文件的大小不能变（构造流程只 mkdir，不该写 probe）
        size_after = real_probe.stat().st_size if real_probe.is_file() else None
        assert size_after == size_before, (
            f'构造队列时污染了仓库真实队列文件：'
            f'{size_before} → {size_after} 字节'
        )
