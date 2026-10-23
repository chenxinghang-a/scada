# -*- coding: utf-8 -*-
"""熔断器的恢复窗口必须按**单调时钟**计时（round 258）。

缺陷形态
--------
`CircuitBreaker` 的 `recovery_timeout`（打开后等多久进半开）表达的是**时长**，
原实现用 `time.time()` 算 `elapsed`。墙钟被回拨一次（NTP 校时、现场机无电池
开机后校时、手工改时间），`elapsed` 变负 → `elapsed >= recovery_timeout`
永远不成立 → 熔断器**永久停在 OPEN**：

* 该设备的采集从此彻底停摆（熔断器就是采集链路的闸门）；
* 界面上只表现为「设备一直离线」；
* 日志里写着「剩余冷却 N 秒」，而这个 N 永远等不到。

判据：**时长用 monotonic，时刻用 wall clock**。熔断器两者都要 ——
窗口用单调时钟，`get_stats()['last_failure']` 仍必须是墙钟 epoch
（接口/看板要显示"最后一次失败是什么时候"）。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

BREAKER_SRC = Path(__file__).resolve().parents[1] / "core" / "circuit_breaker.py"


def _boom():
    raise RuntimeError('device read failed')


def _strip_comments(src: str) -> str:
    return re.sub(r"#.*$", "", src, flags=re.M)


def test_breaker_recovers_after_backward_clock_jump(monkeypatch):
    """墙钟回拨 8 小时后，恢复窗口仍按真实经过的时间到期。"""
    from core.circuit_breaker import CircuitBreaker, CircuitState

    b = CircuitBreaker('dev1', failure_threshold=1, recovery_timeout=0.05)
    with pytest.raises(RuntimeError):
        b.call(_boom)
    assert b.state == CircuitState.OPEN

    # 墙钟被回拨 8 小时（单调时钟不受影响）
    monkeypatch.setattr(time, 'time', lambda: 1_000_000.0)

    time.sleep(0.08)          # 真实经过 80ms > recovery_timeout 50ms
    assert b.state == CircuitState.HALF_OPEN, (
        '恢复窗口已过却仍停在 OPEN —— 说明 elapsed 用的是墙钟，'
        '墙钟回拨会让熔断器永久卡死，该设备的采集再也不会恢复')


def test_remaining_seconds_is_sane_after_backward_jump(monkeypatch):
    """`CircuitBreakerError.remaining_seconds` 不该出现负值或天文数字。"""
    from core.circuit_breaker import CircuitBreaker, CircuitBreakerError

    b = CircuitBreaker('dev2', failure_threshold=1, recovery_timeout=30)
    with pytest.raises(RuntimeError):
        b.call(_boom)

    monkeypatch.setattr(time, 'time', lambda: 1_000_000.0)
    with pytest.raises(CircuitBreakerError) as exc:
        b.call(_boom)
    remaining = exc.value.remaining_seconds
    assert 0 <= remaining <= 30, f'剩余冷却时间不合理: {remaining!r}'


def test_force_open_holds_until_recovery_timeout():
    """手动打开必须真的"打开"：不能因为内部初值是 -inf 而立刻半开。"""
    from core.circuit_breaker import CircuitBreaker, CircuitState

    b = CircuitBreaker('dev3', recovery_timeout=60)
    b.force_open()
    assert b.state == CircuitState.OPEN, (
        'force_open 之后立刻变成 HALF_OPEN —— 单调时钟字段没跟着更新')


def test_last_failure_is_wall_clock_for_display():
    """`get_stats()['last_failure']` 必须是墙钟 epoch（看板要显示日期）。"""
    from core.circuit_breaker import CircuitBreaker

    b = CircuitBreaker('dev4', failure_threshold=1, recovery_timeout=60)
    assert b.get_stats()['last_failure'] is None
    with pytest.raises(RuntimeError):
        b.call(_boom)

    last = b.get_stats()['last_failure']
    assert isinstance(last, (int, float)), f'last_failure 类型不对: {type(last)}'
    assert abs(time.time() - last) < 5.0, (
        f'last_failure={last!r} 看起来是单调时钟的值（与墙钟相差 '
        f'{abs(time.time() - last):.0f}s）—— 看板会显示 1970 年')


def test_window_math_does_not_use_wall_clock():
    """静态守卫：熔断器的窗口计算里不许再出现 `time.time()`。"""
    code = _strip_comments(BREAKER_SRC.read_text(encoding='utf-8'))
    assert not re.search(r'elapsed\s*=\s*time\.time\(\)', code), (
        '`elapsed = time.time() - ...` 又回来了 —— 墙钟回拨会让熔断器永久卡在 OPEN')
    assert 'self._last_failure_time' not in code, (
        '旧的墙钟字段 `_last_failure_time` 还在被引用；'
        '窗口用 `_last_failure_mono`，展示用 `_last_failure_wall`，两者别混')
    assert 'time.monotonic() - self._last_failure_mono' in code
