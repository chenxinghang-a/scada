# -*- coding: utf-8 -*-
"""报警 KPI 闸门：抖动信号下报警次数必须有界（round 269）。

为什么要有这个闸门
------------------
round 262 用真实历史数据证明：配上 delay + deadband 后，同一份数据从
**2,017 次报警降到 145 次（-92.8%）**。但那个结论依赖两件事同时成立：

1. `配置/alarms.yaml` 里每条规则都配了 delay / deadband；
2. `报警层/alarm_engine.py` 的判定**真的**用了它们。

任何一处被后来的改动悄悄抹掉（有人嫌"报警太慢"把 delay 改回 0、重构时
把死区判断漏掉），真实数据上就退回两千次报警 —— 而这种退化**不会报错**，
只会表现为"报警变多了"，没人会注意到。

所以这里用**确定性合成信号**（不依赖真实库，CI 里也能跑）把行为钉住：
围绕阈值小幅抖动 60 个周期，每条规则最多 1 次报警；去掉抑制则应有几十次。

判据来源：ISA-18.2 对 chattering alarm 的定义（同一报警反复触发/清除）。
一个纯粹在阈值附近抖动的信号，正确处理是**只报一次**，不是报六十次。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALARMS_YAML = PROJECT_ROOT / '配置' / 'alarms.yaml'

T0 = datetime(2026, 10, 25, 0, 0, 0)
POINTS = 600          # 600 点 × 5s = 50 分钟
PERIODS = 60          # 抖动 60 个周期 → 无抑制时应产生 60 次报警
SAMPLE_SECONDS = 5


def _rules():
    cfg = yaml.safe_load(ALARMS_YAML.read_text(encoding='utf-8')) or {}
    return [r for r in cfg.get('alarm_rules') or [] if r.get('enabled', True)]


def _chatter_series(rule: dict):
    """围绕阈值小幅抖动的确定性序列（幅度 < deadband、周期 < delay）。

    这正是现场最典型的抖动形态：值在阈值附近来回蹭。
    用正弦而非随机数 —— 结果可复现，不需要固定 seed。
    """
    threshold = float(rule['threshold'])
    deadband = float(rule.get('deadband') or 0.02)
    amp = deadband * 0.6
    for i in range(POINTS):
        value = threshold + amp * math.sin(2 * math.pi * PERIODS * i / POINTS)
        yield value, T0 + timedelta(seconds=SAMPLE_SECONDS * i)


def _replay(rule: dict, **override) -> int:
    """用**生产判定引擎**回放，返回报警次数。"""
    from 报警层.alarm_engine import decide, new_state

    effective = dict(rule)
    effective.update(override)

    state = None
    fires = 0
    for value, ts in _chatter_series(rule):
        decision = decide(effective, state, value, ts)
        if decision.action == 'fire':
            fires += 1
            state = new_state(rule['id'], rule['device_id'],
                              rule['register_name'], decision, ts)
        elif decision.action == 'clear':
            state = None
        elif decision.action in ('pending', 'update'):
            if state is None:
                state = new_state(rule['id'], rule['device_id'],
                                  rule['register_name'], decision, ts)
            else:
                state.update(decision.updates)
    return fires


# ---------------------------------------------------------------------------
# 1. 主闸门：仓库配置下，抖动只该报一次
# ---------------------------------------------------------------------------

def test_each_rule_reports_chatter_at_most_once():
    """每条规则在 60 个抖动周期里最多报 1 次。"""
    offenders = []
    for rule in _rules():
        fires = _replay(rule)
        if fires > 1:
            offenders.append(f"{rule['id']}: {fires} 次"
                             f"（delay={rule.get('delay')}, deadband={rule.get('deadband')}）")
    assert not offenders, (
        '抖动信号下报警反复触发（ISA-18.2 的 chattering alarm）：\n  '
        + '\n  '.join(offenders)
        + '\n检查 delay/deadband 是否被清零，或 alarm_engine 是否还在用它们')


def test_total_alarms_are_bounded():
    """整份配置的总报警次数必须有界（当前实测 10 条规则各 1 次）。"""
    total = sum(_replay(r) for r in _rules())
    assert total <= 2 * len(_rules()), (
        f'60 个抖动周期产生了 {total} 次报警（规则数 {len(_rules())}）—— 抑制失效')


# ---------------------------------------------------------------------------
# 2. 反向对照：没有抑制时必须炸开
# ---------------------------------------------------------------------------

def test_without_damping_chatter_explodes():
    """把 delay/deadband 清零 → 报警次数应接近抖动周期数（证明信号真的在抖）。

    没有这条对照，上面那条可能只是"信号没碰到阈值"造成的假绿。
    """
    total_bare = sum(_replay(r, delay=0, deadband=0) for r in _rules())
    expected = PERIODS * len(_rules())
    assert total_bare >= expected * 0.8, (
        f'去掉抑制后只有 {total_bare} 次报警（预期约 {expected}）—— '
        '说明测试信号本身没有产生抖动，主闸门是假绿')


def test_damping_reduces_alarms_by_an_order_of_magnitude():
    """有抑制 vs 无抑制至少差一个数量级。"""
    damped = sum(_replay(r) for r in _rules())
    bare = sum(_replay(r, delay=0, deadband=0) for r in _rules())
    assert bare >= damped * 10, (
        f'抑制只把报警从 {bare} 降到 {damped}，不足一个数量级 —— 抑制效果被削弱了')


# ---------------------------------------------------------------------------
# 3. 配置层：每条规则都得配（静态，防"忘了配"）
# ---------------------------------------------------------------------------

def test_every_rule_has_damping_configured():
    missing = [r['id'] for r in _rules()
               if not r.get('delay') or not r.get('deadband')]
    assert not missing, (
        f'这些规则没有配 delay/deadband：{missing} —— '
        '不配就等于允许抖动报警')


def test_deadband_is_smaller_than_measurement_span():
    """死区不能大到"永远不报警"（死区 ≥ 阈值本身的 20% 就属于配置错误）。"""
    bad = []
    for rule in _rules():
        threshold = abs(float(rule['threshold']))
        deadband = float(rule.get('deadband') or 0)
        if threshold and deadband > threshold * 0.2:
            bad.append(f"{rule['id']}: deadband={deadband} vs threshold={threshold}")
    assert not bad, '死区相对阈值过大，会掩盖真实越限：\n  ' + '\n  '.join(bad)
