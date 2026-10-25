# -*- coding: utf-8 -*-
"""采集调度：启动相位要打散，周期不许累积漂移（round 272）。

两个独立缺陷
------------
**1) 启动脉冲（thundering herd）**

所有设备的首次调度都是 `threading.Timer(0.1, collect_task)` —— 启动后
0.1 秒**一起**开始，之后每 N 秒**一起**打网关和数据库。25 台设备就是
25 倍的瞬时负载，其余时间完全空闲：网关峰值排队、DB 写入成串、
CPU 曲线呈脉冲。真实厂区里这会直接表现为"每 5 秒卡一下"。

**2) 周期漂移（采样率悄悄变低）**

`_schedule_next(interval)` 固定等 `interval`，但本轮采集本身要花时间
（连设备、读寄存器、写队列）。于是**实际周期 = interval + 采集耗时**：
设备 2 秒采集 / 5 秒周期 → 实际 7 秒，采样率比配置低 **30%**。
历史曲线的点间隔与配置对不上，基于采样数的统计也跟着偏。

判据：**长期平均周期必须 ≈ interval**（本轮花了多少，下次就少等多少），
且各设备的相位应当分散。
"""

from __future__ import annotations

import random
import statistics

import pytest


# ---------------------------------------------------------------------------
# 1. 漂移补偿
# ---------------------------------------------------------------------------

def test_no_elapsed_waits_full_interval():
    from 采集层.data_collector import compute_next_delay
    assert compute_next_delay(5.0, 0.0) == 5.0


def test_elapsed_is_compensated():
    """本轮花了 2 秒，下次只该等 3 秒 —— 否则周期变成 7 秒。"""
    from 采集层.data_collector import compute_next_delay
    assert compute_next_delay(5.0, 2.0) == 3.0


def test_long_collection_hits_floor():
    """采集比周期还慢时，不许给出 0/负延迟（那会变成忙等）。"""
    from 采集层.data_collector import compute_next_delay, SCHEDULE_MIN_DELAY_S
    delay = compute_next_delay(5.0, 12.0)
    assert delay == SCHEDULE_MIN_DELAY_S
    assert delay > 0


def test_average_period_matches_config():
    """连续 100 轮、每轮采集 1.5s：平均周期必须接近配置的 5s。"""
    from 采集层.data_collector import compute_next_delay

    interval = 5.0
    collection_cost = 1.5
    total = 0.0
    for _ in range(100):
        delay = compute_next_delay(interval, collection_cost)
        total += delay + collection_cost
    average = total / 100
    assert abs(average - interval) < 0.05, (
        f'平均周期 {average:.2f}s，配置 {interval}s —— 漂移没有补偿')


def test_compute_next_delay_survives_bad_input():
    from 采集层.data_collector import compute_next_delay
    assert compute_next_delay(None, None) == 5.0
    assert compute_next_delay(0, 0) == 5.0
    assert compute_next_delay(5.0, -3.0) == 5.0          # 负耗时按 0 处理
    assert compute_next_delay(float('inf'), 0) > 0


# ---------------------------------------------------------------------------
# 2. 启动抖动
# ---------------------------------------------------------------------------

def test_startup_jitter_is_within_period():
    from 采集层.data_collector import startup_jitter
    for _ in range(50):
        jitter = startup_jitter(5.0)
        assert 0.0 <= jitter <= 5.0


def test_startup_jitter_is_capped():
    """周期很长时（如停机态 30s），抖动也不必超过 5s。"""
    from 采集层.data_collector import startup_jitter, STARTUP_JITTER_MAX_S
    for _ in range(50):
        assert startup_jitter(30.0) <= STARTUP_JITTER_MAX_S


def test_startup_jitter_spreads_phases():
    """25 台设备各自的首次延迟必须分散（否则又回到"一起打"）。"""
    from 采集层.data_collector import startup_jitter

    rng = random.Random(20261026)
    delays = [startup_jitter(5.0, rng) for _ in range(25)]

    assert min(delays) < 1.0, '最早的设备应当接近立即开始'
    assert max(delays) > 4.0, '最晚的设备应当接近一个周期'
    # 标准差足够大才算真的打散（全挤在 0.1s 附近时 std 会很小）
    assert statistics.pstdev(delays) > 1.0, (
        f'25 台设备的首次延迟太集中（std={statistics.pstdev(delays):.2f}）')


def test_startup_jitter_handles_bad_interval():
    from 采集层.data_collector import startup_jitter
    assert 0.0 <= startup_jitter(None) <= 5.0
    assert 0.0 <= startup_jitter(0) <= 5.0


# ---------------------------------------------------------------------------
# 3. 接线检查（静态）
# ---------------------------------------------------------------------------

def test_scheduler_uses_compensation_and_jitter():
    """`_schedule_next` 必须用 `compute_next_delay`，首次调度必须用抖动。"""
    from pathlib import Path
    import re

    src = Path(__file__).resolve().parents[1] / '采集层' / 'data_collector.py'
    code = re.sub(r'#.*$', '', src.read_text(encoding='utf-8'), flags=re.M)

    assert 'compute_next_delay(interval, elapsed)' in code, (
        '_schedule_next 没有做漂移补偿')
    assert 'startup_jitter(base_interval)' in code, (
        '首次调度没有打散相位 —— 所有设备会同时开始')
    assert 'threading.Timer(0.1, collect_task)' not in code, (
        '首次调度又变回固定 0.1s 了')
