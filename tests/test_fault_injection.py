"""
故障注入与混沌工程测试
验证 fault_injection.py 和 chaos_engineering.py
"""

import time
import threading
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.fault_injection import (
    FaultInjector, FaultType, FaultSeverity, FaultInjection, FaultScenarios,
)
from core.chaos_engineering import ChaosEngine, ExperimentState


class TestFaultInjection:
    """故障注入测试"""

    def setup_method(self):
        self.injector = FaultInjector()

    def test_inject_latency(self):
        """延迟注入"""
        inj = self.injector.inject('test_target', FaultType.LATENCY, delay_ms=50, duration=10)
        assert inj is not None
        assert inj.is_active

        start = time.time()
        self.injector.apply('test_target')
        elapsed = time.time() - start
        assert elapsed >= 0.04  # 至少40ms延迟

    def test_inject_exception(self):
        """异常注入"""
        self.injector.inject(
            'db', FaultType.EXCEPTION,
            exception_class=ConnectionError,
            exception_message='模拟断连',
            duration=10,
        )
        with pytest.raises(ConnectionError, match='模拟断连'):
            self.injector.apply('db')

    def test_inject_timeout(self):
        """超时注入"""
        self.injector.inject('svc', FaultType.TIMEOUT, timeout_seconds=0.1, duration=10)
        with pytest.raises(TimeoutError):
            self.injector.apply('svc')

    def test_inject_network_partition(self):
        """网络分区注入"""
        self.injector.inject('net', FaultType.NETWORK_PARTITION, duration=10)
        with pytest.raises(ConnectionError, match='网络分区'):
            self.injector.apply('net')

    def test_inject_intermittent(self):
        """间歇性故障注入"""
        self.injector.inject('api', FaultType.INTERMITTENT, failure_rate=1.0, duration=10)
        # failure_rate=1.0 意味着100%失败
        with pytest.raises(ConnectionError):
            self.injector.apply('api')

    def test_inject_data_corruption(self):
        """数据损坏注入"""
        self.injector.inject('data', FaultType.DATA_CORRUPTION, corrupted_value='CORRUPTED', duration=10)
        result = self.injector.apply('data')
        # apply 返回 True 表示故障已触发
        assert result is True

    def test_remove_injection(self):
        """移除故障注入"""
        self.injector.inject('target1', FaultType.LATENCY, delay_ms=10, duration=60)
        assert self.injector.check('target1') is not None

        removed = self.injector.remove('target1')
        assert removed is True
        assert self.injector.check('target1') is None

    def test_remove_nonexistent(self):
        """移除不存在的注入"""
        assert self.injector.remove('nonexistent') is False

    def test_clear_all(self):
        """清除所有注入"""
        self.injector.inject('a', FaultType.LATENCY, delay_ms=1, duration=60)
        self.injector.inject('b', FaultType.EXCEPTION, exception_class=ValueError, duration=60)
        assert len(self.injector.get_active()) == 2

        self.injector.clear_all()
        assert len(self.injector.get_active()) == 0

    def test_global_disable(self):
        """全局禁用时忽略注入"""
        self.injector.enabled = False
        result = self.injector.inject('x', FaultType.LATENCY, delay_ms=1, duration=60)
        assert result is None

    def test_injection_expiration(self):
        """注入过期"""
        inj = self.injector.inject('exp', FaultType.LATENCY, delay_ms=1, duration=0.1)
        assert inj.is_active
        time.sleep(0.2)
        assert not inj.is_active
        assert self.injector.check('exp') is None

    def test_triggered_count(self):
        """触发计数"""
        self.injector.inject('cnt', FaultType.LATENCY, delay_ms=1, duration=60)
        for _ in range(5):
            self.injector.apply('cnt')
        inj = self.injector.check('cnt')
        assert inj.triggered_count == 5

    def test_get_status(self):
        """获取状态"""
        self.injector.inject('s1', FaultType.LATENCY, delay_ms=1, duration=60)
        status = self.injector.get_status()
        assert 'global_enabled' in status
        assert 'active_count' in status
        assert status['active_count'] == 1

    def test_decorate(self):
        """装饰器注入"""
        self.injector.decorate('fn', FaultType.LATENCY, delay_ms=10, duration=60)

        @self.injector.decorate('fn2', FaultType.LATENCY, delay_ms=1, duration=60)
        def my_func():
            return 42

        assert my_func() == 42


class TestFaultScenarios:
    """预定义故障场景测试"""

    def test_scenario_database_slow(self):
        injector = FaultInjector()
        inj = FaultScenarios.database_slow(injector, duration=5)
        assert inj.fault_type == FaultType.LATENCY
        assert inj.severity == FaultSeverity.HIGH

    def test_scenario_device_network_loss(self):
        injector = FaultInjector()
        inj = FaultScenarios.device_network_loss(injector, duration=5)
        assert inj.fault_type == FaultType.INTERMITTENT

    def test_scenario_api_overload(self):
        injector = FaultInjector()
        inj = FaultScenarios.api_overload(injector, duration=5)
        assert inj.fault_type == FaultType.SLOW_RESPONSE


class TestChaosEngineering:
    """混沌工程测试"""

    def setup_method(self):
        self.injector = FaultInjector()
        # 使用不注册默认检查的引擎（测试环境无运行中的系统）
        self.chaos = ChaosEngine.__new__(ChaosEngine)
        self.chaos._injector = self.injector
        self.chaos._experiments = {}
        self.chaos._checks = {}
        self.chaos._lock = __import__('threading').RLock()
        self.chaos._running = False

    def test_create_experiment(self):
        """创建实验"""
        exp = self.chaos.create_experiment(
            name="test_exp",
            description="测试实验",
            steps=[{'action': 'wait', 'seconds': 0}],
        )
        assert exp.name == "test_exp"
        assert exp.state == ExperimentState.CREATED

    def test_list_experiments(self):
        """列出实验"""
        self.chaos.create_experiment(name="exp1")
        self.chaos.create_experiment(name="exp2")
        exps = self.chaos.list_experiments()
        assert len(exps) == 2

    def test_run_simple_experiment(self):
        """运行简单实验（仅等待步骤）"""
        exp = self.chaos.create_experiment(
            name="simple",
            description="简单测试",
            steps=[
                {'action': 'wait', 'seconds': 0},
            ],
            rollback_on_failure=False,
        )
        result = self.chaos.run_experiment("simple")
        assert result['state'] == 'passed'

    def test_run_inject_remove_experiment(self):
        """运行注入+移除实验"""
        exp = self.chaos.create_experiment(
            name="inject_test",
            steps=[
                {'action': 'inject', 'target': 'test_svc', 'fault': 'latency', 'delay_ms': 1, 'duration': 10},
                {'action': 'wait', 'seconds': 0},
                {'action': 'remove', 'target': 'test_svc'},
            ],
        )
        result = self.chaos.run_experiment("inject_test")
        assert result['state'] == 'passed'

    def test_run_nonexistent_experiment(self):
        """运行不存在的实验"""
        result = self.chaos.run_experiment("nonexistent")
        assert 'error' in result

    def test_generate_report(self):
        """生成报告"""
        self.chaos.create_experiment(
            name="report_test",
            description="报告测试",
            steady_state="系统正常",
            steps=[{'action': 'wait', 'seconds': 0}],
        )
        self.chaos.run_experiment("report_test")
        report = self.chaos.generate_report("report_test")
        assert "报告测试" in report
        assert "混沌实验报告" in report

    def test_get_status(self):
        """获取引擎状态"""
        self.chaos.create_experiment(name="s1")
        status = self.chaos.get_status()
        assert 'experiments_count' in status
        assert status['experiments_count'] == 1

    def test_rollback_on_failure(self):
        """失败时自动回滚"""
        self.chaos.create_experiment(
            name="rollback_test",
            steps=[
                {'action': 'inject', 'target': 'rb_svc', 'fault': 'exception', 'exception_class': RuntimeError, 'exception_message': 'fail', 'duration': 60},
                {'action': 'verify', 'check': 'nonexistent_check_xyz'},  # 不存在的检查会失败
            ],
            rollback_on_failure=True,
        )
        result = self.chaos.run_experiment("rollback_test")
        # 注入步骤成功但验证步骤失败 → 应回滚
        assert result['state'] in ('failed', 'rolled_back')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
