# -*- coding: utf-8 -*-
"""故障预测的「剩余天数」必须按真实采样间隔换算（round 237）。

缺陷
----
`_predict_failure()` 用 `slope`（每个**采样点**的增量）乘上一个**写死的
`sample_interval=60` 秒**来算天数：

    seconds_to_limit = (Δ值 / slope) * 60
    days = seconds_to_limit / 86400

而采样间隔是**设备决定的**：`配置/devices.yaml` 里就有
`collection_interval: 5`（5 秒一个点）。间隔 5 秒时真实时间只有算出来的
**1/12** —— 明天就到限，界面上写「12 天后」。

讽刺的是 `_analyze_trend()` 手里就有真实时间戳（`ts_epoch`），
还算出了每小时变化量，只是预测那一侧没用它。

修法：`_analyze_trend` 多给一个 `slope_per_hour`（斜率换算到「每小时」），
`_predict_failure` 用它换算天数；只有拿不到时间戳时才退回按配置间隔估算，
并且**明确记日志**（不再静默假设 60 秒）。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from 智能层.predictive_maintenance import PredictiveMaintenance


def _pm(sample_interval: int = 60) -> PredictiveMaintenance:
    pm = PredictiveMaintenance(MagicMock(), config={'sample_interval': sample_interval})
    # 当前值 19、上限 3619 → 还差 3600（配合下面的 5s/60s 采样，
    # 期望值分别是 5 小时 / 60 小时，数字干净便于断言）
    pm.thresholds['d1:temp'] = {'upper': 3619.0, 'lower': None}
    return pm


def _window(n: int, interval_s: int, start_value: float = 0.0, step: float = 1.0):
    """n 个点，每 interval_s 秒一个，值按 step 线性上升。"""
    return [
        {'value': start_value + step * i, 'ts_epoch': 1_700_000_000.0 + interval_s * i}
        for i in range(n)
    ]


def _predict(pm, window):
    trend = pm._analyze_trend(window)
    return trend, pm._predict_failure('d1', 'temp', window, trend)


# ---------------------------------------------------------------------------
# 1. 5 秒采样：天数必须按真实间隔算（旧实现会报大 12 倍）
# ---------------------------------------------------------------------------

def test_days_to_limit_uses_real_interval_5s():
    pm = _pm()
    w = _window(n=20, interval_s=5)          # 19 个间隔 = 95 秒
    trend, pred = _predict(pm, w)

    assert pred is not None
    # 斜率 = 1/采样点 → 每小时 = 1 * 19 / (95/3600) = 720
    assert trend['slope_per_hour'] == pytest.approx(720.0, rel=0.01)

    # 还差 3600，每小时涨 720 → 正好 5 小时
    assert pred['hours_to_limit'] == pytest.approx(5.0, rel=0.05), (
        f"剩余 {pred['hours_to_limit']} 小时，与真实采样间隔（5s）算出来的 5 小时不符"
    )
    assert pred['days_to_limit'] == pytest.approx(5.0 / 24, rel=0.05)

    # 旧实现按「每分钟一个点」算 → 60 小时（12 倍）
    legacy_hours = (3619.0 - 19.0) / 1.0 * 60 / 3600
    assert pred['hours_to_limit'] < legacy_hours / 6, (
        f"剩余 {pred['hours_to_limit']} 小时仍接近旧实现（{legacy_hours:.1f}h）—— "
        '说明还在按写死的 60 秒采样换算'
    )


def test_days_to_limit_unchanged_for_60s_devices():
    """60 秒采样的设备结果应与旧实现一致（回归：别把正常场景改坏）。"""
    pm = _pm()
    w = _window(n=20, interval_s=60)
    _trend, pred = _predict(pm, w)

    # 60 秒采样：每小时涨 60 → 3600/60 = 60 小时（与旧实现一致）
    assert pred['hours_to_limit'] == pytest.approx(60.0, rel=0.02)


# ---------------------------------------------------------------------------
# 2. 拿不到时间戳 → 退回配置间隔，并留痕
# ---------------------------------------------------------------------------

def test_falls_back_to_config_interval_without_timestamps(caplog):
    pm = _pm(sample_interval=30)
    w = [{'value': float(i), 'ts_epoch': 0.0} for i in range(20)]   # 没有有效时间跨度
    trend = pm._analyze_trend(w)
    assert trend['slope_per_hour'] == 0

    pred = pm._predict_failure('d1', 'temp', w, trend)
    assert pred is not None, '没有时间戳时应退回估算，而不是直接不给预测'
    # 退回：Δ/slope * 30s → 3600*30s = 30 小时
    assert pred['hours_to_limit'] == pytest.approx(30.0, rel=0.02)
    assert '采样间隔' in caplog.text, (
        f'退回估算必须留日志（否则又是一次静默假设）: {caplog.text!r}'
    )


# ---------------------------------------------------------------------------
# 3. 趋势字段回归
# ---------------------------------------------------------------------------

def test_trend_still_reports_direction_and_rate():
    pm = _pm()
    w = _window(n=20, interval_s=5)
    trend = pm._analyze_trend(w)

    assert trend['direction'] == 'rising'
    assert trend['r_squared'] == pytest.approx(1.0, abs=1e-6)
    assert trend['slope'] == pytest.approx(1.0, rel=1e-6)
    # change_rate 是「绝对变化量/小时」，不是百分比
    assert trend['change_rate'] == pytest.approx(20 * 1.0 / (95 / 3600), rel=0.02)


def test_no_trend_returns_none():
    pm = _pm()
    w = [{'value': 5.0, 'ts_epoch': 1_700_000_000.0 + i * 5} for i in range(20)]
    _trend, pred = _predict(pm, w)
    assert pred is None
