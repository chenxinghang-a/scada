# -*- coding: utf-8 -*-
"""连接池自动扩缩容不许改写**配置**上限（round 268）。

缺陷形态
--------
`_check_auto_scale()` 的"扩容"分支：

    if usage_rate > self._scale_up_threshold and total < self._max_size:
        new_max = min(total + 5, self._max_size)
        self._max_size = new_max        # ← 把配置值改小了

因为进入分支的前提是 `total < self._max_size`，所以 `total + 5` 通常**小于**
`_max_size`，赋值等于把配置上限**永久调小**：

    max_size=50，池里 10 条、全部占用（使用率 100%）
    → new_max = min(15, 50) = 15 → `_max_size = 15`
    → 之后池最多 15 条，运维配的 50 再也达不到

名字叫"扩容"，实际是"永久缩容"，而且**没有任何日志说配置被改了**。
现场表现：明明配了 50，压测时却在 15 条连接上排队。

判据：**配置值不可变，扩缩容只调"当前生效上限"**，且生效上限必须在
[下限, 配置上限] 之间。
"""

from __future__ import annotations

import time

import pytest


def _make_pool(**kw):
    from core.connection_pool import ConnectionPool

    params = dict(factory=lambda key: object(), max_size=50, min_idle=2,
                  name='t-pool', auto_scale=True)
    params.update(kw)
    return ConnectionPool(**params)


def _fill(pool, n):
    """把池填到 n 条并全部占用（使用率 100%）。"""
    keys = [f'dev{i}' for i in range(n)]
    for k in keys:
        pool.acquire(k)
    return keys


# ---------------------------------------------------------------------------
# 1. 核心回归：扩容不得改小配置上限
# ---------------------------------------------------------------------------

def test_scale_up_does_not_mutate_configured_max():
    """**扩容真的被触发**时，也不许动配置上限。

    ⚠️ 必须先缩容再升负载：池刚建好时 `_dynamic_max == _max_size`，
    扩容分支根本不会进入 —— 那样写的断言对变异是免疫的（实测过）。
    """
    pool = _make_pool(max_size=50, min_idle=2)

    # 先缩容（使用率 0%），让动态上限低于配置上限
    _fill(pool, 10)
    for k in [f'dev{i}' for i in range(10)]:
        pool.release(k)
    pool._last_scale_time = 0
    pool._check_auto_scale()
    assert pool._dynamic_max < 50, '前提不成立：没有缩容'

    # 再高负载触发**扩容**
    for k in [f'dev{i}' for i in range(10)]:
        pool.acquire(k)
    pool._last_scale_time = 0
    pool._check_auto_scale()

    assert pool._max_size == 50, (
        f'配置上限被改成了 {pool._max_size} —— 「扩容」把运维配的 50 永久改小了')
    assert pool.get_stats()['max_size'] == 50
    assert pool.get_stats()['dynamic_max'] <= 50
    pool.shutdown()


def test_dynamic_max_never_exceeds_configured():
    pool = _make_pool(max_size=50)
    _fill(pool, 10)

    for _ in range(20):                   # 反复触发，动态上限也不许越界
        pool._last_scale_time = 0
        pool._check_auto_scale()

    assert pool._dynamic_max <= 50
    pool.shutdown()


# ---------------------------------------------------------------------------
# 2. 缩容：只下调动态上限，且有下限
# ---------------------------------------------------------------------------

def test_scale_down_lowers_dynamic_max_only():
    pool = _make_pool(max_size=50, min_idle=2)
    _fill(pool, 10)
    for k in [f'dev{i}' for i in range(10)]:
        pool.release(k)                   # 全部空闲 → 使用率 0%

    pool._last_scale_time = 0
    pool._check_auto_scale()

    assert pool._dynamic_max < 50, '使用率 0% 却没有缩容'
    assert pool._max_size == 50, '缩容动了配置上限'
    pool.shutdown()


def test_scale_down_has_floor():
    pool = _make_pool(max_size=50, min_idle=2)
    _fill(pool, 6)
    for k in [f'dev{i}' for i in range(6)]:
        pool.release(k)

    for _ in range(30):                   # 反复缩容，必须停在下限
        pool._last_scale_time = 0
        pool._check_auto_scale()

    assert pool._dynamic_max >= 2, (
        f'动态上限缩到 {pool._dynamic_max}，低于 min_idle=2')
    pool.shutdown()


# ---------------------------------------------------------------------------
# 3. 缩容后还能扩回来（这是"动态"的意义）
# ---------------------------------------------------------------------------

def test_dynamic_max_can_grow_back_after_shrink():
    pool = _make_pool(max_size=50, min_idle=2)

    # 先缩容
    _fill(pool, 10)
    for k in [f'dev{i}' for i in range(10)]:
        pool.release(k)
    pool._last_scale_time = 0
    pool._check_auto_scale()
    shrunk = pool._dynamic_max
    assert shrunk < 50

    # 再高负载 → 动态上限回升（旧实现里 `_max_size` 已被改小，永远回不去）
    for k in [f'dev{i}' for i in range(10)]:
        pool.acquire(k)
    for _ in range(10):
        pool._last_scale_time = 0
        pool._check_auto_scale()

    assert pool._dynamic_max > shrunk, '高负载下动态上限没有回升'
    assert pool._dynamic_max <= 50
    pool.shutdown()


# ---------------------------------------------------------------------------
# 4. 生效上限真的约束建连
# ---------------------------------------------------------------------------

def test_dynamic_max_limits_new_connections():
    pool = _make_pool(max_size=50, auto_scale=False)
    pool._dynamic_max = 3                 # 手动压低生效上限

    got = [pool.acquire(f'dev{i}') for i in range(5)]
    assert sum(1 for g in got if g is not None) == 3, (
        '生效上限是 3，却建了更多连接')
    pool.shutdown()


def test_scale_step_is_configurable():
    pool = _make_pool(max_size=50, min_idle=2)
    pool._scale_step = 10
    assert pool._scale_step == 10
    pool.shutdown()


# ---------------------------------------------------------------------------
# 5. 冷却仍然生效（别把好功能改坏）
# ---------------------------------------------------------------------------

def test_cooldown_blocks_repeated_scaling():
    pool = _make_pool(max_size=50, min_idle=2)
    _fill(pool, 10)
    for k in [f'dev{i}' for i in range(10)]:
        pool.release(k)

    pool._last_scale_time = time.monotonic()      # 刚扩缩过
    before = pool._dynamic_max
    pool._check_auto_scale()
    assert pool._dynamic_max == before, '冷却期内又扩缩容了'
    pool.shutdown()
