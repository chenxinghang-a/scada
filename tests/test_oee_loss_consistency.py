# -*- coding: utf-8 -*-
"""OEE 六大损失必须与 A/P/Q 用**同一个**运行时长（round 233）。

缺陷
----
`calculate_oee()` 里：

    actual_run = sd['actual_run_time'] + extra_time      # 含「当前正在跑」的这段

`availability` / `performance` 都用这个 `actual_run`；
但 `_calculate_losses()` 内部又自己取了一次 `sd['actual_run_time']`
（**只含已结算的部分**，不含当前运行段）。

于是同一份响应里两个数字互相矛盾：

* 设备连续运行、中间没切换过状态时，`actual_run_time` 一直是 0
  （只有 `update_device_state` 切换状态时才累加），
  于是 `_calculate_losses` 走进 `actual_run > 0` 的 else 分支 →
  **`性能损失_秒` 恒为 0**；
* 而同一份响应里的 `performance` 已经算出来只有 27% 左右。

也就是说：设备跑了一小时、只做了 1/4 的活，损失报表却说「没有速度损失」。
一切换状态（结算一次），同一个指标又突然出现 —— 数字随「有没有刚切换过状态」跳变。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from 智能层.oee_calculator import OEECalculator


@pytest.fixture
def calc():
    c = OEECalculator(MagicMock())
    yield c
    try:
        c.stop()
    except Exception:
        pass


def _prepare_running_shift(calc, run_seconds: int = 3600):
    """建一个「已经连续跑了 run_seconds 秒」的班次（不切换状态）。"""
    calc.start_shift('d1', planned_hours=8)
    calc.set_theoretical_rate('d1', 360)        # 360 件/小时 → 理想节拍 10 秒/件
    calc.update_device_state('d1', 'running')
    # 把「本段开始时间」往前拨：模拟设备已经连续跑了一小时、期间没切换状态
    calc.device_states['d1']['since'] = datetime.now() - timedelta(seconds=run_seconds)
    calc.record_production('d1', count=100, good_count=95)
    return run_seconds


# ---------------------------------------------------------------------------
# 1. 性能损失必须与 performance 自洽
# ---------------------------------------------------------------------------

def test_speed_loss_is_not_zero_while_performance_is_low(calc):
    _prepare_running_shift(calc)
    oee = calc.calculate_oee('d1')
    assert oee is not None

    # 1 小时只做 100 件、理想节拍 10 秒 → 性能率约 0.28
    assert oee['performance'] < 0.5, oee['performance']

    assert oee['losses']['性能损失_秒'] > 0, (
        f"performance={oee['performance']} 但性能损失=0 —— "
        '损失用的是「已结算运行时长」(=0)，和 performance 用的不是同一个数'
    )


def test_speed_loss_matches_the_reported_rate(calc):
    """损失秒数必须能由 performance 反推出来：理想产出 - 实际产出。"""
    run_seconds = _prepare_running_shift(calc)
    oee = calc.calculate_oee('d1')

    ideal_cycle = 10.0                        # 3600/360
    expected_loss = (run_seconds / ideal_cycle - 100) * ideal_cycle
    assert oee['losses']['性能损失_秒'] == pytest.approx(expected_loss, rel=0.05), (
        f"性能损失 {oee['losses']['性能损失_秒']}s 与 performance={oee['performance']} 不自洽"
    )


def test_speed_loss_stable_across_state_transition(calc):
    """切换一次状态（结算运行时长）后，同一个班次的损失不能突变。

    这是本缺陷最直观的症状：切换前损失=0，切换后突然变成几千秒。
    """
    _prepare_running_shift(calc)
    before = calc.calculate_oee('d1')['losses']['性能损失_秒']

    # 结算当前运行段（running → stopped），此时 actual_run_time 变成 3600
    calc.update_device_state('d1', 'stopped')
    after = calc.calculate_oee('d1')['losses']['性能损失_秒']

    assert after > 0
    assert before == pytest.approx(after, rel=0.1), (
        f'切换状态前 {before}s、切换后 {after}s —— 同一个班次的损失因为'
        '「刚结算过一次」而突变，说明两个分支用的是不同的运行时长'
    )


# ---------------------------------------------------------------------------
# 2. 回归：A/P/Q 与损失在已结算状态下仍然正确
# ---------------------------------------------------------------------------

def test_losses_consistent_when_shift_fully_settled(calc):
    _prepare_running_shift(calc)
    calc.update_device_state('d1', 'stopped')   # 全部结算

    oee = calc.calculate_oee('d1')
    planned = oee['planned_production_time']
    actual = oee['actual_run_time']

    # 可用率损失 = 计划时间 - 实际运行时间
    assert oee['losses']['可用率损失_秒'] == pytest.approx(planned - actual, rel=0.02)
    assert oee['losses']['可用率损失占比_百分比'] == pytest.approx(
        (1 - oee['availability']) * 100, rel=0.02)
    # 不良品 5 件 × 10 秒
    assert oee['losses']['不良品数'] == 5
    assert oee['losses']['质量损失_秒'] == pytest.approx(50, rel=0.02)
