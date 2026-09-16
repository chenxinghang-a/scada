"""ModuleRegistry 生产接线回归测试（审计 P2-7）。

背景（2026-09-16 代码质量审计）：
``core/module_registry.py`` 的 ``ModuleRegistry`` 功能完整，但**生产环境从未注册过
任何模块** —— ``ModuleRegistry.register`` 只在测试里被调用过。后果是三处静默失效：

1. ``/modules`` API（``展示层/api/api_health.py``）永远返回空字典
2. ``core/chaos_engineering.py`` 的 4 个稳态检查全部退化成常量：
   - ``_check_alarm_responsive`` → 恒 ``False``
   - ``_check_database_accessible`` → 恒 ``False``
   - ``_check_collector_running`` → 恒 ``False``
   - ``_check_no_critical_alarms`` → 恒 ``True``（永远"正常"）
   因为 ``get_instance()`` 抛的 ``KeyError`` 被 ``except: pass`` 吞掉
3. ``core/health_checker.py::_emit_health_alert`` **发不出健康告警** ——
   系统不健康时操作员永远收不到通知（同样是 KeyError 被吞）

第 3 条尤其严重：它正是"静默假死"的又一处 —— 监控自己坏了，而没人知道。

另外 ``get_instance()`` 此前只允许 ``INITIALIZED``，模块一旦 ``start()`` 变成
``RUNNING`` 就再也取不到实例，对"运行期才去查依赖"的调用方是致命的。
"""
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------ register_instance
class _FakeAlarmManager:
    def __init__(self):
        self.emitted = []
        self.active = []

    def _emit_websocket_alarm(self, payload):
        self.emitted.append(payload)

    def get_active_alarms(self):
        return list(self.active)


class _FakeDatabase:
    def __init__(self):
        self.queried = 0

    def get_connection(self):
        import contextlib

        @contextlib.contextmanager
        def _cm():
            self.queried += 1
            yield _FakeConn()
        return _cm()


class _FakeConn:
    def execute(self, _sql):
        return self


class _FakeCollector:
    def __init__(self, running=True):
        self._running = running

    def get_stats(self):
        return {'running': self._running}


def test_register_instance_marks_initialized_and_keeps_identity():
    """register_instance 必须原样保留传入的对象，不得再造一个。"""
    from core.module_registry import ModuleRegistry, ModuleStatus

    sentinel = _FakeAlarmManager()
    returned = ModuleRegistry.register_instance('alarm_manager', sentinel)

    assert returned is sentinel
    assert ModuleRegistry.get_instance('alarm_manager') is sentinel
    info = ModuleRegistry.get_status('alarm_manager')
    assert info['status'] == ModuleStatus.INITIALIZED.value
    assert info['has_instance'] is True


def test_register_instance_does_not_reconstruct():
    """不能走 initialize() 的构造路径 —— 那会造出第二个实例。"""
    from core.module_registry import ModuleRegistry

    class Counted:
        instances = 0

        def __init__(self):
            Counted.instances += 1

    existing = Counted()
    assert Counted.instances == 1
    ModuleRegistry.register_instance('database', existing)
    assert Counted.instances == 1, 'register_instance 又构造了一次实例'
    assert ModuleRegistry.get_instance('database') is existing


@pytest.mark.parametrize('status_name', ['RUNNING', 'PAUSED'])
def test_get_instance_accepts_running_and_paused(status_name):
    """回归：模块 start() 后变成 RUNNING，此前 get_instance 会抛 RuntimeError。"""
    from core.module_registry import ModuleRegistry, ModuleStatus

    obj = object()
    ModuleRegistry.register_instance('collector', obj)
    ModuleRegistry.set_status('collector', getattr(ModuleStatus, status_name))

    assert ModuleRegistry.get_instance('collector') is obj, (
        f'{status_name} 状态下取不到实例 —— 运行期查依赖的调用方会全部失效'
    )


def test_get_instance_still_rejects_registered_and_error():
    """放宽不等于不管：没实例的（REGISTERED）和出错的仍须拒绝。"""
    from core.module_registry import ModuleRegistry, ModuleStatus

    class NeverInit:
        def __init__(self):
            pass

    ModuleRegistry.register('never_init', NeverInit)
    with pytest.raises(RuntimeError):
        ModuleRegistry.get_instance('never_init')

    ModuleRegistry.register_instance('broken', object())
    ModuleRegistry.set_status('broken', ModuleStatus.ERROR, RuntimeError('boom'))
    with pytest.raises(RuntimeError):
        ModuleRegistry.get_instance('broken')

    with pytest.raises(KeyError):
        ModuleRegistry.get_instance('not_registered_at_all')


# ------------------------------------------------------------ 混沌工程稳态检查
class TestChaosSteadyStateChecks:
    """这 4 个检查此前因为查不到模块而全部退化成常量。"""

    def _engine(self):
        from core.chaos_engineering import ChaosEngine
        return ChaosEngine()

    def test_alarm_responsive(self):
        from core.module_registry import ModuleRegistry

        assert self._engine()._check_alarm_responsive() is False, '未注册时应为 False'
        ModuleRegistry.register_instance('alarm_manager', _FakeAlarmManager())
        assert self._engine()._check_alarm_responsive() is True, (
            '注册了 alarm_manager 后仍返回 False —— 模块查找依然失败'
        )

    def test_database_accessible(self):
        from core.module_registry import ModuleRegistry

        assert self._engine()._check_database_accessible() is False
        db = _FakeDatabase()
        ModuleRegistry.register_instance('database', db)
        assert self._engine()._check_database_accessible() is True
        assert db.queried >= 1, '没有真的去查库'

    def test_collector_running(self):
        from core.module_registry import ModuleRegistry

        assert self._engine()._check_collector_running() is False
        ModuleRegistry.register_instance('data_collector', _FakeCollector(running=True))
        assert self._engine()._check_collector_running() is True

        ModuleRegistry.register_instance('data_collector', _FakeCollector(running=False))
        assert self._engine()._check_collector_running() is False, '采集器已停却报正常'

    def test_no_critical_alarms(self):
        from core.module_registry import ModuleRegistry

        alarms = _FakeAlarmManager()
        ModuleRegistry.register_instance('alarm_manager', alarms)
        assert self._engine()._check_no_critical_alarms() is True

        alarms.active = [{'alarm_level': 'critical'}] * 5
        assert self._engine()._check_no_critical_alarms() is False, (
            '有 5 个严重报警却仍报"正常" —— 该检查此前恒为 True'
        )


# ------------------------------------------------------------ 健康告警必须发得出去
def test_health_alert_reaches_alarm_manager():
    """系统不健康时，告警必须真的发到报警管理器。

    此前 ``ModuleRegistry.get_instance('alarm_manager')`` 抛 KeyError 被
    ``except Exception: logger.debug`` 吞掉 → **健康告警永远发不出去**。
    """
    from core.health_checker import HealthChecker
    from core.module_registry import ModuleRegistry

    alarms = _FakeAlarmManager()
    ModuleRegistry.register_instance('alarm_manager', alarms)

    HealthChecker._emit_health_alert({
        'unhealthy_checks': ['database'],
        'degraded_checks': ['api_latency'],
    })

    assert len(alarms.emitted) == 2, (
        f'健康告警没有送达报警管理器（收到 {len(alarms.emitted)} 条）'
    )
    levels = {a['alarm_level'] for a in alarms.emitted}
    assert levels == {'critical', 'warning'}, levels
    assert any(a['dedup_key'] == 'health:database:unhealthy' for a in alarms.emitted)


def test_health_alert_without_registry_is_silent_not_crashing():
    """没注册报警管理器时应静默跳过（best-effort），不得抛异常。"""
    from core.health_checker import HealthChecker

    HealthChecker._emit_health_alert({'unhealthy_checks': ['x'], 'degraded_checks': []})


# ------------------------------------------------------------ 生产入口必须接线
def test_run_py_registers_core_modules():
    """静态守卫：run.py 必须把核心模块注册进注册表，否则 P2-7 会复发。"""
    source = (REPO_ROOT / 'run.py').read_text(encoding='utf-8')
    missing = [name for name in ('database', 'device_manager',
                                 'alarm_manager', 'data_collector')
               if f"register_instance('{name}'" not in source]
    assert not missing, (
        'run.py 未注册以下核心模块实例 —— /modules 会返回空、混沌稳态检查会退化成'
        f'常量、健康告警会发不出去：{missing}'
    )


def test_modules_status_is_non_empty_after_wiring():
    """接线后 /modules 的数据源（get_status）不应再是空字典。"""
    from core.module_registry import ModuleRegistry

    ModuleRegistry.register_instance('database', _FakeDatabase())
    ModuleRegistry.register_instance('alarm_manager', _FakeAlarmManager())

    status = ModuleRegistry.get_status()
    assert status, '/modules 仍会返回空'
    assert all(info['has_instance'] for info in status.values())


def test_registry_is_thread_safe_under_concurrent_registration():
    """并发注册不得丢模块（注册表是全局单例，启动路径可能多线程）。"""
    from core.module_registry import ModuleRegistry

    errors = []

    def _worker(idx):
        try:
            ModuleRegistry.register_instance(f'mod_{idx}', object())
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors, errors
    assert len(ModuleRegistry.get_status()) >= 20
