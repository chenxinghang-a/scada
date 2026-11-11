"""报警健康度分析：standing / stale / orphan 三类"坏报警"。

依据：ISA-18.2 / IEC 62682《Management of Alarm Systems for the Process
Industries》。标准区分了几类会拖垮操作员判断的报警：

* **standing alarm**：条件长期不恢复，一直挂在列表顶部；
* **stale alarm**：长时间没有刷新（数据不再更新），但仍显示为活跃；
* **orphan alarm**：产生它的规则已不存在（配置删除/改名），没人能收尾它。

本模块只做"分类 + 计数"，不改任何状态；调用方据此决定是收尾、降级为
系统事件，还是进维护工单。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

#: 超过这个时长仍未恢复 → standing
DEFAULT_STANDING_HOURS = 24.0
#: 超过这个时长没有刷新 → stale
DEFAULT_STALE_MINUTES = 30.0


def _as_naive(value: Any) -> datetime | None:
    """把 datetime/ISO 字符串统一成 naive datetime（跨时区不比较 aware/naive）。"""
    dt: datetime | None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _norm_rules(rules: Any) -> set[str]:
    if isinstance(rules, dict):
        return {str(k) for k in rules}
    if isinstance(rules, Iterable):
        ids = set()
        for item in rules:
            if isinstance(item, dict) and item.get('id'):
                ids.add(str(item['id']))
            elif isinstance(item, str):
                ids.add(item)
        return ids
    return set()


def analyse(alarm_states: dict, rules: Any, now: datetime | None = None, *,
            standing_hours: float = DEFAULT_STANDING_HOURS,
            stale_minutes: float = DEFAULT_STALE_MINUTES) -> dict[str, Any]:
    """把活跃报警分成 orphan / standing / stale，并给出计数。

    Args:
        alarm_states: `{(alarm_id, device_id, register_name): state_dict}`
        rules: 当前规则集合（dict 或 list）
        now: 便于测试注入的当前时间
    """
    now = now or datetime.now()
    rule_ids = _norm_rules(rules)
    standing_delta = timedelta(hours=standing_hours)
    stale_delta = timedelta(minutes=stale_minutes)

    orphan: list[dict[str, Any]] = []
    standing: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    unacked = 0

    for key, state in (alarm_states or {}).items():
        if not isinstance(state, dict):
            continue
        try:
            alarm_id, device_id, register_name = key
        except (TypeError, ValueError):
            alarm_id, device_id, register_name = (
                state.get('alarm_id'), state.get('device_id'),
                state.get('register_name'))

        first = _as_naive(state.get('first_trigger_time'))
        last = _as_naive(state.get('last_trigger_time')) or first
        age_hours = ((now - first).total_seconds() / 3600.0) if first else None
        idle_minutes = ((now - last).total_seconds() / 60.0) if last else None
        acked = bool(state.get('acknowledged'))
        if not acked:
            unacked += 1

        item = {
            'alarm_id': alarm_id, 'device_id': device_id,
            'register_name': register_name,
            'age_hours': round(age_hours, 2) if age_hours is not None else None,
            'idle_minutes': round(idle_minutes, 1) if idle_minutes is not None else None,
            'acknowledged': acked,
        }
        if alarm_id is not None and str(alarm_id) not in rule_ids:
            orphan.append(item)
        elif age_hours is not None and age_hours * 3600.0 >= standing_delta.total_seconds():
            standing.append(item)
        elif idle_minutes is not None and idle_minutes >= stale_delta.total_seconds() / 60.0:
            stale.append(item)

    total = len(alarm_states or {})
    return {
        'total': total,
        'unacknowledged': unacked,
        'ack_rate': round((total - unacked) / total, 3) if total else None,
        'counts': {'orphan': len(orphan), 'standing': len(standing),
                   'stale': len(stale)},
        'orphan': orphan,
        'standing': standing,
        'stale': stale,
        'thresholds': {'standing_hours': standing_hours,
                       'stale_minutes': stale_minutes},
    }
