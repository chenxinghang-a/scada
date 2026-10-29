# -*- coding: utf-8 -*-
"""报警规则离线回放 —— 用**真实历史数据**比较不同 delay/deadband 的报警量。

为什么需要它
------------
"加个死区就能少很多报警"是经验之谈，不是证据。要证明，只能拿真实采样序列
跑一遍判定引擎，数出"改前/改后各产生多少次报警"。

关键在于：回放用的判定逻辑必须与运行时**完全同一份**，否则证明无效。
所以这里直接调用 `报警层/alarm_engine.py` 的 `decide()` —— 和
`AlarmManager._process_alarm_state` 用的是同一个函数（round 262 抽出来的）。

用法
----
    # 现状（用配置里的 delay/deadband）
    python tools/alarm_replay.py --db data/scada.db --config 配置/alarms.yaml

    # 假设所有规则 delay=10s、deadband 取规则里的默认
    python tools/alarm_replay.py --db data/scada.db --delay 10

    # 对比：现状 vs 建议参数（同一次运行，两张表）
    python tools/alarm_replay.py --db data/scada.db --compare

退出码恒为 0（度量工具，不是闸门）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from 报警层.alarm_engine import (      # noqa: E402  —— 必须在 sys.path 调整之后
    CLEAR, FIRE, decide, new_state,
)


def _parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    text = str(value).strip().replace('T', ' ')
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def load_rules(config_path: str) -> list[dict]:
    import yaml
    cfg = yaml.safe_load(Path(config_path).read_text(encoding='utf-8')) or {}
    return cfg.get('alarm_rules') or []


def replay_rule(conn: sqlite3.Connection, rule: dict, *,
                delay: float | None = None,
                deadband: float | None = None) -> dict:
    """把一条规则在一个库上的历史序列跑一遍，数报警次数。

    Returns:
        {'rule_id', 'points', 'fires', 'clears', 'pending_only', 'active_at_end'}
    """
    effective = dict(rule)
    if delay is not None:
        effective['delay'] = delay
    if deadband is not None:
        effective['deadband'] = deadband

    rows = conn.execute(
        'SELECT value, timestamp FROM history_data '
        'WHERE device_id = ? AND register_name = ? ORDER BY timestamp',
        (rule['device_id'], rule['register_name'])).fetchall()

    state = None
    fires = clears = pending_only = 0
    points = 0
    for value, ts_raw in rows:
        ts = _parse_ts(ts_raw)
        if ts is None:
            continue
        points += 1
        decision = decide(effective, state, value, ts)
        if decision.action == FIRE:
            fires += 1
            state = new_state(rule['id'], rule['device_id'],
                              rule['register_name'], decision, ts)
        elif decision.action == CLEAR:
            clears += 1
            state = None
        elif decision.action in ('pending', 'update'):
            if state is None:
                state = new_state(rule['id'], rule['device_id'],
                                  rule['register_name'], decision, ts)
                pending_only += 1
            else:
                state.update(decision.updates)

    return {
        'rule_id': rule['id'],
        'device_id': rule['device_id'],
        'register_name': rule['register_name'],
        'points': points,
        'fires': fires,
        'clears': clears,
        'pending_only': pending_only,
        'active_at_end': state is not None,
    }


#: 建议参数（工程取值：按物理量纲给死区，按采样周期给延迟）
#: 依据：温度/压力/电量的常规仪表精度与工艺波动，不是拍脑袋
SUGGESTED = {
    'temp_high': {'delay': 10, 'deadband': 2.0},        # 温度 ±2℃
    'temp_low': {'delay': 10, 'deadband': 2.0},
    'env_temp_high': {'delay': 10, 'deadband': 2.0},
    'pressure_high': {'delay': 5, 'deadband': 0.02},    # 压力 0.02 MPa
    'pressure_low': {'delay': 5, 'deadband': 0.02},
    'voltage_high': {'delay': 5, 'deadband': 2.0},      # 电压 2V
    'voltage_low': {'delay': 5, 'deadband': 2.0},
    'current_high': {'delay': 5, 'deadband': 2.0},      # 电流 2A
    'ph_low': {'delay': 15, 'deadband': 0.1},           # pH 0.1
    'ph_high': {'delay': 15, 'deadband': 0.1},
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='用真实历史数据回放报警规则')
    ap.add_argument('--db', required=True, help='数据库路径')
    ap.add_argument('--config', default='配置/alarms.yaml')
    ap.add_argument('--delay', type=float, help='统一覆盖 delay（秒）')
    ap.add_argument('--deadband', type=float, help='统一覆盖 deadband')
    ap.add_argument('--compare', action='store_true',
                    help='同时跑"现状"和"建议参数"并对比')
    ap.add_argument('--json', help='输出 JSON')
    args = ap.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        print(f'数据库不存在: {db}', file=sys.stderr)
        return 2

    rules = load_rules(args.config)
    if not rules:
        print('配置里没有 alarm_rules', file=sys.stderr)
        return 2

    conn = sqlite3.connect(f'file:{db.as_posix()}?mode=ro', uri=True)
    try:
        current = [replay_rule(conn, r) for r in rules]
        suggested = None
        if args.compare:
            suggested = []
            for r in rules:
                params = SUGGESTED.get(r['id'], {})
                suggested.append(replay_rule(
                    conn, r, delay=params.get('delay'), deadband=params.get('deadband')))
        elif args.delay is not None or args.deadband is not None:
            suggested = [replay_rule(conn, r, delay=args.delay,
                                     deadband=args.deadband) for r in rules]
    finally:
        conn.close()

    cur_total = sum(x['fires'] for x in current)
    print(f'# 报警规则离线回放（真实历史数据）\n')
    print(f'数据库：`{db}`')
    print(f'规则数：{len(rules)}　历史点数合计：{sum(x["points"] for x in current)}')
    print()
    print('## 现状（配置里的 delay / deadband）\n')
    print('| 规则 | 设备/寄存器 | 点数 | 报警次数 | 清除次数 |')
    print('|---|---|---|---|---|')
    for x, r in zip(current, rules, strict=True):
        print(f'| {x["rule_id"]} | {x["device_id"]}/{x["register_name"]} '
              f'| {x["points"]} | **{x["fires"]}** | {x["clears"]} |')
    print(f'| **合计** | | | **{cur_total}** | |')
    print()

    if suggested:
        sug_total = sum(x['fires'] for x in suggested)
        print('## 建议参数（delay + deadband）\n')
        print('| 规则 | delay | deadband | 报警次数 | 相对现状 |')
        print('|---|---|---|---|---|')
        for x, r, c in zip(suggested, rules, current, strict=True):
            p = SUGGESTED.get(r['id'], {})
            delta = x['fires'] - c['fires']
            pct = (f'{delta / c["fires"] * 100:+.0f}%' if c['fires'] else '—')
            print(f'| {x["rule_id"]} | {p.get("delay", args.delay)}s '
                  f'| {p.get("deadband", args.deadband)} | **{x["fires"]}** | {pct} |')
        print(f'| **合计** | | | **{sug_total}** | '
              f'**{(sug_total - cur_total) / cur_total * 100:+.0f}%** |')
        print()
        print(f'结论：同样的历史数据，报警次数 {cur_total} → {sug_total}'
              f'（{(sug_total - cur_total) / cur_total * 100:+.1f}%）。')

    if args.json:
        Path(args.json).write_text(json.dumps(
            {'current': current, 'suggested': suggested}, ensure_ascii=False, indent=2),
            encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
