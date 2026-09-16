"""后台维护任务调度回归测试。

背景（2026-09-16 代码质量审计 P1-5）：
``core/scheduled_tasks.py`` 的 ``task_manager`` 功能完整却**零引用** —— 从未
注册过任务、从未 ``start_all()``；而各模块的清理函数（JWT 黑名单、API 缓存、
nonce、限流计数、离线消息队列、分层缓存）也都没有任何调度器调用，在长期
运行的现场设备上这些结构会无界增长。

同时 ``TaskManager`` 自身还有一个隐藏缺陷：``start_all()`` 在持有
``threading.Lock`` 的情况下调用 ``self.start()``，后者又去获取同一把
**非可重入**锁 → 直接死锁。本文件对它做回归。
"""
import threading
import time

import pytest


def _alive_task_threads():
    return [t for t in threading.enumerate() if t.name.startswith('task-')]


def _run_with_timeout(fn, timeout=5.0):
    """在子线程里跑 fn，返回 (是否跑完, 结果容器)"""
    box = {}

    def _target():
        try:
            box['result'] = fn()
        except BaseException as e:  # noqa: BLE001
            box['error'] = e

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout)
    return (not t.is_alive()), box


# ------------------------------------------------------------------ 调度器本身
def test_start_all_does_not_deadlock():
    """start_all() 必须在持锁状态下安全调用 start()（锁需可重入）。"""
    from core.scheduled_tasks import TaskManager

    mgr = TaskManager()
    mgr.add('noop', lambda: None, interval=3600, description='noop')

    finished, box = _run_with_timeout(mgr.start_all, timeout=5.0)
    try:
        assert finished, (
            'start_all() 死锁：它在持有 _lock 时调用 self.start()，'
            '而 start() 又获取同一把非可重入锁。请把 _lock 改成 RLock。'
        )
        assert 'error' not in box, box.get('error')
        assert mgr.get_status('noop')['status'] == 'running'
    finally:
        finished_stop, _ = _run_with_timeout(mgr.stop_all, timeout=5.0)
        assert finished_stop, 'stop_all() 同样死锁'


def test_add_does_not_orphan_running_task():
    """重复 add() 不得把正在运行的任务从 _tasks 里换掉（换掉就再也停不了）。"""
    from core.scheduled_tasks import TaskManager

    mgr = TaskManager()
    first = mgr.add('t1', lambda: None, interval=3600)
    mgr.start('t1')
    try:
        second = mgr.add('t1', lambda: None, interval=3600)
        assert second is first, '运行中的任务被替换 → 旧线程失控（永久泄漏）'
        assert mgr.get_status('t1')['status'] == 'running'
    finally:
        mgr.stop_all()


def test_stop_is_idempotent():
    """stop() 重复调用不得抛异常。"""
    from core.scheduled_tasks import TaskManager

    mgr = TaskManager()
    mgr.add('t2', lambda: None, interval=3600)
    mgr.start('t2')
    assert mgr.stop('t2') is True
    assert mgr.stop('t2') is True
    assert mgr.get_status('t2')['status'] == 'stopped'


# ------------------------------------------------------------ 维护任务的注册与运行
def _make_database(tmp_path):
    from 存储层.database import Database
    return Database(str(tmp_path / 'maintenance.db'))


def test_build_maintenance_tasks_covers_all_known_structures(tmp_path, auth_manager):
    """审计点名的每个无界增长结构都要有对应任务。"""
    from core.maintenance import DEFAULT_INTERVALS, build_maintenance_tasks

    tasks = build_maintenance_tasks(
        database=_make_database(tmp_path), auth_manager=auth_manager)
    names = {t[0] for t in tasks}

    assert names == set(DEFAULT_INTERVALS), (
        f'维护任务清单与 DEFAULT_INTERVALS 不一致：'
        f'缺 {set(DEFAULT_INTERVALS) - names}，多 {names - set(DEFAULT_INTERVALS)}'
    )
    for name, interval, description, func in tasks:
        assert interval > 0, f'{name} 间隔非法'
        assert callable(func), f'{name} 不可调用'
        assert description, f'{name} 缺少说明'


def test_optional_dependencies_are_skipped_not_fatal():
    """缺 database / auth_manager 时只跳过对应任务，其余照常注册。"""
    from core.maintenance import build_maintenance_tasks

    names = {t[0] for t in build_maintenance_tasks()}
    assert 'jwt_blacklist_cleanup' not in names, 'auth_manager 为 None 时不应注册黑名单清理'
    assert 'data_archive' not in names, 'database 为 None 时不应注册归档任务'
    # 纯模块级函数不依赖任何对象，必须照常注册
    assert 'api_cache_cleanup' in names
    assert 'nonce_cache_cleanup' in names
    assert 'rate_limiter_cleanup' in names


def test_every_maintenance_task_function_runs(tmp_path, auth_manager):
    """逐个真正执行一遍 —— 这条能抓到模块路径写错/签名不匹配。"""
    from core.maintenance import build_maintenance_tasks

    tasks = build_maintenance_tasks(
        database=_make_database(tmp_path), auth_manager=auth_manager)
    assert tasks, '没有可执行的任务'

    for name, _interval, _description, func in tasks:
        try:
            func()
        except Exception as e:  # noqa: BLE001
            pytest.fail(f'维护任务 {name} 执行失败: {type(e).__name__}: {e}')


def test_start_maintenance_registers_starts_and_stops(tmp_path, auth_manager):
    """注册 → 启动 → 全部 running → 停止 → 全部 stopped。"""
    from core import maintenance

    database = _make_database(tmp_path)
    try:
        started = maintenance.start_maintenance(
            database=database, auth_manager=auth_manager)
        assert started, '没有任何维护任务被启动'

        status = maintenance.get_maintenance_status()
        assert set(started) <= set(status)
        for name in started:
            assert status[name]['status'] == 'running', f'{name} 未进入 running'

        stopped = maintenance.stop_maintenance()
        assert set(stopped) == set(started), f'停止的任务集合不一致: {stopped} vs {started}'
        after = maintenance.get_maintenance_status()
        for name in started:
            assert after[name]['status'] == 'stopped', f'{name} 停止后状态不对'
    finally:
        maintenance.stop_maintenance()


def test_start_maintenance_is_idempotent(tmp_path, auth_manager):
    """重复调用 start_maintenance() 不得起第二套线程。"""
    from core import maintenance

    database = _make_database(tmp_path)
    try:
        maintenance.start_maintenance(database=database, auth_manager=auth_manager)
        first_count = len(_alive_task_threads())
        assert first_count > 0, '第一轮没有起任何任务线程'

        maintenance.start_maintenance(database=database, auth_manager=auth_manager)
        time.sleep(0.2)  # 给可能的重复线程一点时间冒出来
        second_count = len(_alive_task_threads())
        assert second_count == first_count, (
            f'重复启动产生了额外的任务线程: {first_count} → {second_count}'
        )
    finally:
        maintenance.stop_maintenance()


# ------------------------------------------------------------------ 运维接口
def test_maintenance_endpoints_registered(app):
    """运维接口必须存在，且变更类端点要 admin 权限（缺失应返回 401/403 而非 404）。"""
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert '/api/ops/maintenance/tasks' in rules, '缺少维护任务状态接口'
    assert '/api/ops/maintenance/tasks/<name>/run' in rules, '缺少手动触发接口'

    client = app.test_client()
    resp = client.get('/api/ops/maintenance/tasks')
    assert resp.status_code in (401, 403), (
        f'状态接口未鉴权（期望 401/403，实际 {resp.status_code}）'
    )
    resp = client.post('/api/ops/maintenance/tasks/api_cache_cleanup/run')
    assert resp.status_code in (401, 403), (
        f'手动触发接口未鉴权（期望 401/403，实际 {resp.status_code}）'
    )
