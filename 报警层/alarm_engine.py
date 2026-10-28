# -*- coding: utf-8 -*-
"""报警判定引擎（纯函数）—— 运行时 / 离线回放 / 测试共用的**唯一**判定逻辑。

为什么要把这段抽出来
--------------------
"一条采样值该不该触发报警、该不该清除"是整个报警系统的核心，而它原来埋
在 `AlarmManager._process_alarm_state` 里（带锁、带日志、直接改 `alarm_states`）。
后果有两个：

1. **无法离线验证**：想知道"把 deadband 从 0 改成 2 能少多少报警"，
   只能上线试，或者另写一份"差不多的"模拟 —— 而另一份实现等于没有证据
   （项目历史上吃过"平行实现"的亏）。
2. **判定逻辑被 IO 包住**：想加一条边界用例，得先造 manager、造 db、造锁。

所以这里把它抽成**纯函数**：输入 `(规则, 上一条状态, 当前值, 时间戳)`，
输出 `(动作, 状态变更)`。不碰数据库、不拿锁、不打日志。
运行时由 `AlarmManager` 调用、离线回放由 `tools/alarm_replay.py` 调用、
单元测试直接调用 —— 三处共用同一份判定，回放结果才有意义。

判定语义（ISA-18.2）
--------------------
* **死区/迟滞（deadband）**：触发用精确阈值，清除要回到 `阈值 ∓ deadband`，
  避免值在阈值附近抖动导致反复触发/清除。
* **延迟确认（delay）**：条件首次成立只挂 pending，持续满 `delay` 秒才真正
  报警；中途恢复则计时重新开始。这样单点毛刺不会产生报警。
* **非有限值**（NaN/Inf）表示"读数无效/不知道"，既不清除也不触发（由调用方
  提前拦掉；这里再做一次兜底）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.safe_numeric import try_finite_float

#: 动作常量
FIRE = 'fire'          # 真正产生报警（推送/声光/落库）
PENDING = 'pending'    # 条件成立但未满 delay，只挂起
UPDATE = 'update'      # 已在报警中，刷新时间/计数
CLEAR = 'clear'        # 条件恢复，清除报警
NONE = 'none'          # 什么都不做（例如非有限值）


def normalize_delay(raw: Any, rule_id: str = '') -> float:
    """把规则的 `delay` 配置归一为秒数（float，非负）。

    配置里写成字符串（`delay: "30"`）时，旧实现的 `delay > 0` 会直接抛
    TypeError，把整条报警检查路径打断（调用方 except 吞掉 → 报警丢失）。
    `delay: .inf`（YAML 合法字面量）也要挡住：`max(0.0, inf)` 仍是 inf，
    等于这条规则永不报警。
    """
    value = try_finite_float(raw)
    if value is None or value <= 0:
        return 0.0
    return value


def normalize_deadband(raw: Any) -> float:
    """死区归一为非负数（NaN/inf/负数/字符串 → 0）。"""
    value = try_finite_float(raw)
    if value is None or value < 0:
        return 0.0
    return value


def evaluate_condition(rule_config: dict, value: float, *,
                       active: bool, deadband: float = 0.0) -> bool:
    """判断条件是否成立（含非对称死区/迟滞）。

    非对称逻辑：
    - **触发**（当前没在报警）：精确阈值判断
    - **保持**（当前正在报警）：阈值 ± deadband，需要更大偏移才清除

    例如：阈值 100、死区 2、`greater_than`
    - 触发：`value > 100`
    - 保持：`value > 98`（要降到 98 以下才清除）

    Args:
        rule_config: 规则（读 condition / threshold）
        value: 当前采样值（必须有限）
        active: 该规则当前是否处于报警状态
        deadband: 死区（同一规则内恒定）

    Returns:
        bool: 条件是否成立
    """
    condition = rule_config.get('condition')
    threshold = rule_config.get('threshold')
    if threshold is None or not isinstance(threshold, (int, float)):
        return False
    threshold = float(threshold)
    db = normalize_deadband(deadband)

    if condition in ('greater_than', 'gt'):
        return value > (threshold - db) if active else value > threshold
    if condition in ('less_than', 'lt'):
        return value < (threshold + db) if active else value < threshold
    if condition in ('greater_equal', 'gte'):
        return value >= (threshold - db) if active else value >= threshold
    if condition in ('less_equal', 'lte'):
        return value <= (threshold + db) if active else value <= threshold
    if condition in ('equal_to', 'equal', 'eq'):
        return abs(value - threshold) <= db if db > 0 else value == threshold
    if condition in ('not_equal_to', 'not_equal', 'ne'):
        return abs(value - threshold) > db if db > 0 else value != threshold
    if condition == 'in_range':
        low = rule_config.get('threshold_min', threshold)
        high = rule_config.get('threshold_max', threshold)
        return float(low) <= value <= float(high)
    if condition == 'out_of_range':
        low = rule_config.get('threshold_min', threshold)
        high = rule_config.get('threshold_max', threshold)
        return value < float(low) or value > float(high)
    # 未知条件 → 不触发（保持旧行为，但由调用方负责记日志）
    return False


@dataclass
class Decision:
    """判定结果：动作 + 需要写回状态的字段。"""
    action: str
    updates: dict = field(default_factory=dict)
    elapsed: float | None = None
    reason: str = ''


def _elapsed_seconds(anchor: Any, now: Any) -> float | None:
    """两个时间点之间的秒数；类型/时区不一致或缺失时返回 None。"""
    if not isinstance(anchor, datetime) or not isinstance(now, datetime):
        return None
    try:
        delta = (now - anchor).total_seconds()
    except TypeError:      # naive vs aware
        return None
    if not math.isfinite(delta):
        return None
    return delta


def decide(rule_config: dict, state: dict | None, value: Any,
           timestamp: datetime) -> Decision:
    """核心判定：给"上一条状态 + 当前采样"，返回该做什么。

    Args:
        rule_config: 规则配置（condition/threshold/delay/deadband）
        state: 该 (规则, 设备, 寄存器) 当前的状态字典；没有报警时为 None
        value: 当前采样值
        timestamp: 采样时间

    Returns:
        Decision: action ∈ {FIRE, PENDING, UPDATE, CLEAR, NONE}，
        以及需要合并进状态字典的字段。
    """
    rule_id = rule_config.get('id') or ''

    # 非有限值：读数无效 ≠ 恢复正常。既不清除也不触发（fail-safe）。
    numeric = try_finite_float(value)
    if numeric is None:
        return Decision(NONE, reason='非有限值/非数值')

    delay = normalize_delay(rule_config.get('delay', 0), rule_id)
    deadband = normalize_deadband(rule_config.get('deadband', 0))

    active = bool(state) and state.get('alarm_id') is not None
    triggered = evaluate_condition(rule_config, numeric, active=active,
                                   deadband=deadband)

    if not triggered:
        if not active:
            return Decision(NONE, reason='未触发且无状态')
        return Decision(CLEAR, {'last_value': numeric},
                        reason='条件恢复')

    if not active:
        # 新报警：delay>0 先挂起，否则立即触发
        if delay > 0:
            return Decision(PENDING, {
                'first_trigger_time': timestamp,
                'last_trigger_time': timestamp,
                'trigger_count': 1,
                'confirm_time': timestamp,
                'pending': True,
                'confirmed': False,
                'last_value': numeric,
            }, reason=f'延迟确认 {delay}s')
        return Decision(FIRE, {
            'first_trigger_time': timestamp,
            'last_trigger_time': timestamp,
            'trigger_count': 1,
            'pending': False,
            'confirmed': False,
            'last_value': numeric,
        }, reason='立即触发')

    # 已激活
    if state.get('pending'):
        anchor = state.get('confirm_time') or state.get('first_trigger_time')
        elapsed = _elapsed_seconds(anchor, timestamp)
        if elapsed is None:
            # 计时不可用（时间戳缺失/类型或时区不一致）：旧实现会让报警永远
            # 停在 pending —— 即"配了 delay 的规则永不报警"。这里按到期处理
            # （fail-open，宁可多报一次也不静默漏报），由调用方记 warning。
            elapsed = delay
        if elapsed >= delay:
            return Decision(FIRE, {
                'pending': False,
                'confirmed': True,
                'first_trigger_time': timestamp,
                'last_trigger_time': timestamp,
                'last_value': numeric,
            }, elapsed=elapsed, reason='延迟到期')
        return Decision(PENDING, {
            'last_trigger_time': timestamp,
            'last_value': numeric,
        }, elapsed=elapsed, reason='延迟未到期')

    return Decision(UPDATE, {
        'last_trigger_time': timestamp,
        # round 303：**trigger_count 只在「重新触发」时 +1，与库里一致**。
        # 原来这里每个采样点都 +1：一条持续 10 小时的报警（5s 采样）在看板上
        # 显示「触发 7200 次」，而历史报表里是 1 次 —— 同一个字段名两套语义。
        # 做报警合理性评审（tools/alarm_kpi_report.py）时判断不了到底刷了多少次，
        # 抖动抑制的效果也就无法验证。
        # 活跃期的采样点数另存 `sample_count`：它有自己的一类消费方
        # （波形密度、稳定性评估），不该和「触发了几次」共用一个字段。
        'sample_count': int(state.get('sample_count') or 0) + 1,
        'last_value': numeric,
    }, reason='持续报警')


def new_state(rule_id: str, device_id: str, register_name: str,
              decision: Decision, timestamp: datetime) -> dict:
    """由判定结果构造一份完整状态（用于首次登记）。"""
    state = {
        'alarm_id': rule_id,
        'device_id': device_id,
        'register_name': register_name,
        'first_trigger_time': timestamp,
        'last_trigger_time': timestamp,
        'trigger_count': 1,
        # round 303：活跃期采样点数（与 trigger_count 分开，见 decide 的 UPDATE 分支）
        'sample_count': 0,
        'confirmed': False,
        'acknowledged': False,
        'pending': decision.action == PENDING,
    }
    state.update({k: v for k, v in decision.updates.items()
                  if k not in ('alarm_id', 'device_id', 'register_name')})
    return state
