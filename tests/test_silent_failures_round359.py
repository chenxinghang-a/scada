# -*- coding: utf-8 -*-
"""round 359：`DIContainer` 的死锁与循环依赖。

两个问题叠在同一处：
1. `_lock` 是 `threading.Lock`（不可重入），而 `resolve()` 在**持锁时**调用
   `service['factory'](*dependencies)`。只要工厂内部再 `resolve()` 一个服务，
   同线程二次申请这把锁 → **进程死锁**，表现为「服务卡住不响应」，没有任何报错。
2. 没有循环依赖检测：A→B→A 会一路递归到 `RecursionError`，
   栈里几十层 `resolve`，看不出谁依赖了谁。
"""

from __future__ import annotations

import threading

import pytest


@pytest.fixture(autouse=True)
def _clean():
    from core.di_container import DIContainer
    DIContainer.clear_all()
    yield
    DIContainer.clear_all()


def test_factory_may_resolve_other_services():
    """工厂内部再 `resolve()` 不能死锁 —— 原来会让整个进程卡住。"""
    from core.di_container import DIContainer as C

    C.register('base', lambda: 1)
    C.register('derived', lambda: ('derived', C.resolve('base')))

    result = {}

    def run():
        result['value'] = C.resolve('derived')

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=5)

    assert not t.is_alive(), '工厂内部 resolve 死锁了（`_lock` 仍是不可重入的 Lock）'
    assert result['value'] == ('derived', 1)


def test_circular_dependency_reports_full_chain():
    """A→B→A 必须报出完整环路，而不是 RecursionError。"""
    from core.di_container import DIContainer as C

    C.register('p', lambda: C.resolve('q'))
    C.register('q', lambda: C.resolve('p'))

    with pytest.raises(RuntimeError) as ei:
        C.resolve('p')
    msg = str(ei.value)
    assert '循环依赖' in msg
    assert 'p' in msg and 'q' in msg, f'报错里没有完整环路: {msg}'


def test_self_dependency_reports():
    from core.di_container import DIContainer as C
    C.register('selfish', lambda: C.resolve('selfish'))

    with pytest.raises(RuntimeError, match='循环依赖'):
        C.resolve('selfish')


def test_normal_dependency_chain_still_works():
    """反向对照：正常的三级依赖必须能解析，且不误报循环。"""
    from core.di_container import DIContainer as C

    C.register('a', lambda: 'A')
    C.register('b', lambda: C.resolve('a') + 'B')
    C.register('c', lambda: C.resolve('b') + 'C')

    assert C.resolve('c') == 'ABC'
