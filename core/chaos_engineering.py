"""
混沌工程工具
自动化执行混沌实验，验证系统韧性。

功能:
  - 实验定义与编排
  - 稳态假设验证
  - 实验报告生成
  - 自动回滚机制

使用示例:
    chaos = ChaosEngine()

    # 定义实验
    experiment = chaos.create_experiment(
        name="数据库慢查询影响",
        description="模拟数据库慢查询，验证报警系统是否正常工作",
        steady_state="报警系统能正常检测和推送告警",
        steps=[
            {'action': 'inject', 'target': 'database', 'fault': 'latency', 'params': {'delay_ms': 2000}},
            {'action': 'wait', 'seconds': 30},
            {'action': 'verify', 'check': 'alarm_system_responsive'},
            {'action': 'remove', 'target': 'database'},
        ],
    )

    # 运行实验
    report = chaos.run_experiment(experiment.name)
"""

import threading
import time
import logging
import json
from enum import Enum
from typing import Dict, Any, Optional, Callable, List
from datetime import datetime
from pathlib import Path

from core.fault_injection import FaultInjector, FaultType, FaultSeverity, fault_injector

logger = logging.getLogger(__name__)


class ExperimentState(Enum):
    """实验状态"""
    CREATED = "created"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    ERROR = "error"


class SteadyStateCheck:
    """稳态假设检查"""

    def __init__(self, name: str, check_func: Callable[[], bool], description: str = ""):
        self.name = name
        self.check_func = check_func
        self.description = description

    def execute(self) -> Dict[str, Any]:
        """执行检查"""
        start = time.time()
        try:
            result = self.check_func()
            return {
                'name': self.name,
                'passed': bool(result),
                'duration': time.time() - start,
                'description': self.description,
            }
        except Exception as e:
            return {
                'name': self.name,
                'passed': False,
                'error': str(e),
                'duration': time.time() - start,
                'description': self.description,
            }


class ExperimentStep:
    """实验步骤"""

    def __init__(self, action: str, **params):
        self.action = action
        self.params = params

    def to_dict(self) -> Dict[str, Any]:
        return {'action': self.action, **self.params}


class Experiment:
    """混沌实验"""

    def __init__(
        self,
        name: str,
        description: str = "",
        steady_state: str = "",
        steps: List[Dict[str, Any]] = None,
        rollback_on_failure: bool = True,
        timeout: float = 300.0,
        tags: List[str] = None,
    ):
        self.name = name
        self.description = description
        self.steady_state = steady_state
        self.steps = [ExperimentStep(**s) for s in (steps or [])]
        self.rollback_on_failure = rollback_on_failure
        self.timeout = timeout
        self.tags = tags or []

        # 运行时状态
        self.state = ExperimentState.CREATED
        self.start_time: Optional[float] = None
        self.end_time: Optional[float] = None
        self.results: List[Dict[str, Any]] = []
        self.steady_state_results: List[Dict[str, Any]] = []
        self.error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'description': self.description,
            'steady_state': self.steady_state,
            'state': self.state.value,
            'tags': self.tags,
            'start_time': self.start_time,
            'end_time': self.end_time,
            'duration': (self.end_time - self.start_time) if self.start_time and self.end_time else None,
            'steps_count': len(self.steps),
            'results_count': len(self.results),
            'steady_state_passed': all(r.get('passed') for r in self.steady_state_results) if self.steady_state_results else None,
            'error': self.error,
        }


class ChaosEngine:
    """
    混沌工程引擎

    管理和执行混沌实验，验证系统韧性。
    """

    def __init__(self, injector: FaultInjector = None):
        self._injector = injector or fault_injector
        self._experiments: Dict[str, Experiment] = {}
        self._checks: Dict[str, SteadyStateCheck] = {}
        self._lock = threading.RLock()
        self._running = False

        # 注册默认稳态检查
        self._register_default_checks()

    def _register_default_checks(self):
        """注册默认的稳态检查"""
        self.register_check(
            'alarm_system_responsive',
            lambda: self._check_alarm_responsive(),
            "报警系统能正常响应",
        )
        self.register_check(
            'database_accessible',
            lambda: self._check_database_accessible(),
            "数据库可访问",
        )
        self.register_check(
            'collector_running',
            lambda: self._check_collector_running(),
            "数据采集器正常运行",
        )
        self.register_check(
            'api_responsive',
            lambda: self._check_api_responsive(),
            "API端点正常响应",
        )
        self.register_check(
            'no_critical_alarms',
            lambda: self._check_no_critical_alarms(),
            "无新增严重报警",
        )

    def _check_alarm_responsive(self) -> bool:
        """检查报警系统响应"""
        try:
            from core.module_registry import ModuleRegistry
            alarm_manager = ModuleRegistry.get_instance('alarm_manager')
            if alarm_manager:
                return hasattr(alarm_manager, 'get_active_alarms')
        except Exception:
            pass
        return False

    def _check_database_accessible(self) -> bool:
        """检查数据库可访问"""
        try:
            from core.module_registry import ModuleRegistry
            database = ModuleRegistry.get_instance('database')
            if database:
                with database.get_connection() as conn:
                    conn.execute("SELECT 1")
                return True
        except Exception:
            pass
        return False

    def _check_collector_running(self) -> bool:
        """检查采集器运行状态"""
        try:
            from core.module_registry import ModuleRegistry
            collector = ModuleRegistry.get_instance('data_collector')
            if collector:
                stats = collector.get_stats()
                return stats.get('running', False)
        except Exception:
            pass
        return False

    def _check_api_responsive(self) -> bool:
        """检查API响应（仅检测本地端口可达性，不发起HTTP请求）"""
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            result = sock.connect_ex(('127.0.0.1', 5000))
            sock.close()
            return result == 0
        except Exception:
            return False

    def _check_no_critical_alarms(self) -> bool:
        """检查无新增严重报警"""
        try:
            from core.module_registry import ModuleRegistry
            alarm_manager = ModuleRegistry.get_instance('alarm_manager')
            if alarm_manager:
                active = alarm_manager.get_active_alarms()
                critical = [a for a in active if a.get('alarm_level') == 'critical']
                return len(critical) < 5  # 少于5个严重报警视为正常
        except Exception:
            pass
        return True

    def register_check(self, name: str, check_func: Callable[[], bool], description: str = ""):
        """注册稳态检查"""
        with self._lock:
            self._checks[name] = SteadyStateCheck(name, check_func, description)

    def create_experiment(
        self,
        name: str,
        description: str = "",
        steady_state: str = "",
        steps: List[Dict[str, Any]] = None,
        rollback_on_failure: bool = True,
        timeout: float = 300.0,
        tags: List[str] = None,
    ) -> Experiment:
        """创建实验"""
        experiment = Experiment(
            name=name,
            description=description,
            steady_state=steady_state,
            steps=steps,
            rollback_on_failure=rollback_on_failure,
            timeout=timeout,
            tags=tags,
        )
        with self._lock:
            self._experiments[name] = experiment
        logger.info("创建混沌实验: %s", name)
        return experiment

    def run_experiment(self, name: str) -> Dict[str, Any]:
        """
        运行混沌实验

        执行步骤 → 验证稳态 → 回滚（如失败）
        """
        with self._lock:
            experiment = self._experiments.get(name)
            if not experiment:
                return {'error': f'实验 {name} 不存在'}

        if experiment.state == ExperimentState.RUNNING:
            return {'error': f'实验 {name} 正在运行中'}

        experiment.state = ExperimentState.RUNNING
        experiment.start_time = time.time()
        experiment.results = []
        experiment.steady_state_results = []
        experiment.error = None

        logger.warning("开始混沌实验: %s", name)

        try:
            # 执行前置稳态检查
            pre_checks = self._run_steady_state_checks("前置检查")
            experiment.steady_state_results.extend(pre_checks)
            if not all(r.get('passed') for r in pre_checks):
                experiment.state = ExperimentState.FAILED
                experiment.error = "前置稳态检查未通过"
                experiment.end_time = time.time()
                return experiment.to_dict()

            # 执行实验步骤
            for i, step in enumerate(experiment.steps):
                logger.info("实验 [%s] 步骤 %d/%d: %s", name, i + 1, len(experiment.steps), step.action)
                result = self._execute_step(step, experiment)
                experiment.results.append(result)

                if result.get('status') == 'error':
                    experiment.error = f"步骤 {i + 1} 执行失败: {result.get('error')}"
                    break

            # 执行后置稳态检查
            post_checks = self._run_steady_state_checks("后置检查")
            experiment.steady_state_results.extend(post_checks)

            # 判定结果
            steady_passed = all(r.get('passed') for r in post_checks)
            steps_passed = all(r.get('status') != 'error' for r in experiment.results)

            if steady_passed and steps_passed:
                experiment.state = ExperimentState.PASSED
            else:
                experiment.state = ExperimentState.FAILED
                if not steady_passed:
                    experiment.error = experiment.error or "后置稳态检查未通过"

                # 自动回滚
                if experiment.rollback_on_failure:
                    self._rollback(experiment)

        except Exception as e:
            experiment.state = ExperimentState.ERROR
            experiment.error = str(e)
            logger.error("混沌实验异常: %s - %s", name, e)
            if experiment.rollback_on_failure:
                self._rollback(experiment)

        experiment.end_time = time.time()
        logger.warning(
            "混沌实验结束: %s, 状态=%s, 耗时=%.1fs",
            name, experiment.state.value,
            experiment.end_time - experiment.start_time,
        )
        return experiment.to_dict()

    def _execute_step(self, step: ExperimentStep, experiment: Experiment) -> Dict[str, Any]:
        """执行单个实验步骤"""
        start = time.time()
        try:
            if step.action == 'inject':
                target = step.params.get('target')
                fault_name = step.params.get('fault', 'latency')
                fault_type = FaultType(fault_name)
                params = {k: v for k, v in step.params.items() if k not in ('target', 'fault')}
                self._injector.inject(target, fault_type, **params)
                return {'action': 'inject', 'target': target, 'status': 'ok', 'duration': time.time() - start}

            elif step.action == 'remove':
                target = step.params.get('target')
                self._injector.remove(target)
                return {'action': 'remove', 'target': target, 'status': 'ok', 'duration': time.time() - start}

            elif step.action == 'wait':
                seconds = step.params.get('seconds', 10)
                time.sleep(seconds)
                return {'action': 'wait', 'seconds': seconds, 'status': 'ok', 'duration': time.time() - start}

            elif step.action == 'verify':
                check_name = step.params.get('check')
                with self._lock:
                    check = self._checks.get(check_name)
                if check:
                    result = check.execute()
                    return {'action': 'verify', 'check': check_name, 'status': 'ok' if result['passed'] else 'failed', 'duration': time.time() - start}
                return {'action': 'verify', 'check': check_name, 'status': 'error', 'error': f'检查 {check_name} 未注册', 'duration': time.time() - start}

            elif step.action == 'run':
                func = step.params.get('func')
                if callable(func):
                    func()
                    return {'action': 'run', 'status': 'ok', 'duration': time.time() - start}
                return {'action': 'run', 'status': 'error', 'error': 'func 不可调用', 'duration': time.time() - start}

            else:
                return {'action': step.action, 'status': 'error', 'error': f'未知动作: {step.action}', 'duration': time.time() - start}

        except Exception as e:
            return {'action': step.action, 'status': 'error', 'error': str(e), 'duration': time.time() - start}

    def _run_steady_state_checks(self, phase: str) -> List[Dict[str, Any]]:
        """运行所有稳态检查"""
        results = []
        with self._lock:
            checks = list(self._checks.values())
        for check in checks:
            result = check.execute()
            result['phase'] = phase
            results.append(result)
            if not result['passed']:
                logger.warning("稳态检查未通过 [%s]: %s - %s", phase, check.name, result.get('error', ''))
        return results

    def _rollback(self, experiment: Experiment):
        """回滚实验（清除所有注入）"""
        logger.warning("回滚混沌实验: %s", experiment.name)
        targets = set()
        for step in experiment.steps:
            if step.action == 'inject':
                targets.add(step.params.get('target'))
        for target in targets:
            self._injector.remove(target)
        experiment.state = ExperimentState.ROLLED_BACK

    def get_experiment(self, name: str) -> Optional[Dict[str, Any]]:
        """获取实验信息"""
        with self._lock:
            exp = self._experiments.get(name)
            return exp.to_dict() if exp else None

    def list_experiments(self) -> List[Dict[str, Any]]:
        """列出所有实验"""
        with self._lock:
            return [exp.to_dict() for exp in self._experiments.values()]

    def get_status(self) -> Dict[str, Any]:
        """获取引擎状态"""
        with self._lock:
            return {
                'experiments_count': len(self._experiments),
                'checks_count': len(self._checks),
                'injector_status': self._injector.get_status(),
                'experiments': [exp.to_dict() for exp in self._experiments.values()],
                'checks': [
                    {'name': c.name, 'description': c.description}
                    for c in self._checks.values()
                ],
            }

    def generate_report(self, name: str) -> str:
        """生成实验报告（文本格式）"""
        with self._lock:
            exp = self._experiments.get(name)
            if not exp:
                return f"实验 {name} 不存在"

        lines = [
            "=" * 60,
            f"混沌实验报告: {exp.name}",
            "=" * 60,
            f"描述: {exp.description}",
            f"稳态假设: {exp.steady_state}",
            f"状态: {exp.state.value}",
            f"开始时间: {datetime.fromtimestamp(exp.start_time).isoformat() if exp.start_time else 'N/A'}",
            f"结束时间: {datetime.fromtimestamp(exp.end_time).isoformat() if exp.end_time else 'N/A'}",
            f"耗时: {exp.end_time - exp.start_time:.1f}s" if exp.start_time and exp.end_time else "耗时: N/A",
            "",
            "--- 稳态检查 ---",
        ]

        for check in exp.steady_state_results:
            status = "✓ 通过" if check.get('passed') else "✗ 未通过"
            lines.append(f"  [{status}] {check.get('name')}: {check.get('description', '')}")
            if check.get('error'):
                lines.append(f"         错误: {check['error']}")

        lines.append("")
        lines.append("--- 实验步骤 ---")
        for i, result in enumerate(exp.results):
            status = result.get('status', 'unknown')
            lines.append(f"  步骤 {i+1}: {result.get('action')} → {status}")
            if result.get('error'):
                lines.append(f"         错误: {result['error']}")

        if exp.error:
            lines.append("")
            lines.append(f"--- 错误信息 ---")
            lines.append(f"  {exp.error}")

        lines.append("")
        lines.append("=" * 60)
        return "\n".join(lines)


# 全局混沌工程引擎
chaos_engine = ChaosEngine()
