# -*- coding: utf-8 -*-
"""报警绩效 KPI 基线（ISA-18.2）—— 从**真实数据库**量，不靠口头需求。

为什么要有这个工具
------------------
"报警是否合理"不是靠感觉判断的。ISA-18.2（IEC 62682）给了一组可量化的判据，
运维审计和第三方评审看的也是这些数：

| KPI | 判据 | 为什么 |
|---|---|---|
| 报警率 | 稳态 ≤ 6 条/小时/操作员（≤144 条/天） | 超过就是"报警泛滥"，人会开始无视报警 |
| 泛滥（flood） | 10 分钟内 > 10 条 | ISA-18.2 的 flood 定义 |
| 抖动（chattering） | 同一报警 1 分钟内 ≥ 3 次 | 说明缺 delay/deadband，是 bad actor 的主要来源 |
| 确认率 | 绝大多数报警应被确认 | 长期不确认 = 操作员已习惯性忽略（normalized deviance） |
| 优先级分布 | critical 不应占大头 | 全部 critical 等于没有优先级 |
| Bad actor | 前 10 条应 < 总报警数的 10% | 少数点位的重复报警会淹没真正重要的 |
| 重复定义 | 同一 (设备, 寄存器) 不应有多条规则 | 一次越限弹两条，人为制造双倍报警 |

用法
----
    python tools/alarm_kpi_report.py --db data/scada.db [--db data/scada_simulated.db]
    python tools/alarm_kpi_report.py --db data/scada.db --json out.json --md out.md

退出码恒为 0（它是**度量**工具，不是闸门）；读不到库才返回 2。
真正当闸门用的是 `tests/test_alarm_kpi_baseline.py`（对着基线文件比）。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

#: ISA-18.2 判据阈值
ISA_TARGETS = {
    'max_alarms_per_hour': 6.0,        # 每操作员每小时的稳态上限
    'flood_window_minutes': 10,        # 泛滥窗口
    'flood_threshold': 10,             # 窗口内条数
    'chatter_window_seconds': 60,      # 抖动窗口
    'chatter_threshold': 3,            # 窗口内次数
    'bad_actor_top_n': 10,
    'bad_actor_share_limit': 0.10,     # 前 N 条占比上限
}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}


def _parse_ts(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace('T', ' ')
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def collect(db_path: str) -> dict:
    """从一个数据库算出全部 KPI。库不存在/没有报警表时抛 FileNotFoundError。"""
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(db_path)

    conn = sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cols = _table_columns(conn, 'alarm_records')
        if not cols:
            raise FileNotFoundError(f'{db_path}: 没有 alarm_records 表')

        total = conn.execute('SELECT COUNT(*) FROM alarm_records').fetchone()[0]
        first_ts, last_ts = conn.execute(
            'SELECT MIN(timestamp), MAX(timestamp) FROM alarm_records').fetchone()

        result: dict = {
            'db': str(path),
            'generated_at': datetime.now().isoformat(timespec='seconds'),
            'has_cleared_column': 'cleared' in cols,
            'total': total,
            'first_alarm': str(first_ts),
            'last_alarm': str(last_ts),
        }

        start, end = _parse_ts(first_ts), _parse_ts(last_ts)
        hours = max((end - start).total_seconds() / 3600.0, 1e-9) if start and end else 0.0
        result['span_hours'] = round(hours, 2)
        result['span_days'] = round(hours / 24.0, 2)
        result['alarms_per_hour'] = round(total / hours, 2) if hours else None
        result['alarms_per_day'] = round(total / (hours / 24.0), 2) if hours else None
        result['rate_within_isa_target'] = (
            None if result['alarms_per_hour'] is None
            else result['alarms_per_hour'] <= ISA_TARGETS['max_alarms_per_hour']
        )

        # 级别分布
        by_level = {r[0]: r[1] for r in conn.execute(
            'SELECT alarm_level, COUNT(*) FROM alarm_records GROUP BY 1 ORDER BY 2 DESC')}
        result['by_level'] = by_level
        result['critical_share'] = (
            round(by_level.get('critical', 0) / total, 4) if total else None)

        # 确认率
        acked = conn.execute('SELECT COUNT(*) FROM alarm_records WHERE acknowledged = 1').fetchone()[0]
        result['acknowledged'] = acked
        result['ack_rate'] = round(acked / total, 4) if total else None

        # 恢复情况（round 260 之后才有 cleared 列）
        if 'cleared' in cols:
            cleared = conn.execute(
                'SELECT COUNT(*) FROM alarm_records WHERE cleared = 1').fetchone()[0]
            result['cleared'] = cleared
            result['clear_rate'] = round(cleared / total, 4) if total else None
            result['still_active'] = total - cleared
        else:
            result['cleared'] = None
            result['clear_rate'] = None
            result['still_active'] = None
            result['note'] = '该库没有 cleared 列（round 260 之前建的库），"仍在报警"无法判定'

        # Bad actor：按 (设备, 寄存器, 报警ID) 聚合
        top = conn.execute('''
            SELECT device_id, register_name, alarm_id, COUNT(*) AS n
            FROM alarm_records
            GROUP BY 1, 2, 3
            ORDER BY n DESC
            LIMIT ?
        ''', (ISA_TARGETS['bad_actor_top_n'],)).fetchall()
        result['bad_actors'] = [
            {'device_id': r[0], 'register_name': r[1], 'alarm_id': r[2],
             'count': r[3], 'share': round(r[3] / total, 4) if total else None}
            for r in top
        ]
        top_sum = sum(r[3] for r in top)
        result['bad_actor_share'] = round(top_sum / total, 4) if total else None
        result['bad_actor_within_limit'] = (
            None if result['bad_actor_share'] is None
            else result['bad_actor_share'] <= ISA_TARGETS['bad_actor_share_limit']
        )

        # 重复定义：同一 (设备, 寄存器) 挂了多条不同规则
        # ⚠️ 只保留前 5 个 id 做示例：曾经有 `edge_{秒}` 那种"每秒一个新 ID"的
        #    路径，一条查询能带出两万个 id，把报告本身淹掉（round 261 实测）。
        dup = conn.execute('''
            SELECT device_id, register_name, COUNT(DISTINCT alarm_id) AS n,
                   GROUP_CONCAT(DISTINCT alarm_id) AS ids
            FROM alarm_records
            GROUP BY 1, 2
            HAVING n > 1
            ORDER BY n DESC
            LIMIT 20
        ''').fetchall()
        result['duplicate_definitions'] = []
        for r in dup:
            ids = str(r[3] or '').split(',')
            result['duplicate_definitions'].append({
                'device_id': r[0],
                'register_name': r[1],
                'rule_count': r[2],
                'sample_ids': ids[:5],
                'truncated': len(ids) > 5,
            })

        # 抖动：同一 key 相邻两条间隔 < 60s 的次数（用窗口函数，SQLite ≥3.25）
        try:
            chatter = conn.execute(f'''
                WITH ordered AS (
                    SELECT alarm_id, device_id, register_name, timestamp,
                           LAG(timestamp) OVER (
                               PARTITION BY alarm_id, device_id, register_name
                               ORDER BY timestamp) AS prev
                    FROM alarm_records
                )
                SELECT COUNT(*) FROM ordered
                WHERE prev IS NOT NULL
                  AND (julianday(timestamp) - julianday(prev)) * 86400.0
                      < {ISA_TARGETS['chatter_window_seconds']}
            ''').fetchone()[0]
            result['chatter_pairs'] = chatter
            result['chatter_share'] = round(chatter / total, 4) if total else None
        except sqlite3.Error as exc:      # 老 SQLite 没有窗口函数
            result['chatter_pairs'] = None
            result['chatter_share'] = None
            result['chatter_error'] = str(exc)

        # 泛滥：10 分钟桶内 > 10 条
        flood = conn.execute('''
            SELECT bucket, n FROM (
                SELECT strftime('%Y-%m-%d %H:', timestamp)
                       || printf('%02d', (CAST(strftime('%M', timestamp) AS INTEGER) / 10) * 10)
                       AS bucket,
                       COUNT(*) AS n
                FROM alarm_records
                GROUP BY bucket
            ) WHERE n > ?
            ORDER BY n DESC
            LIMIT 5
        ''', (ISA_TARGETS['flood_threshold'],)).fetchall()
        result['worst_10min_buckets'] = [
            {'bucket': r[0], 'count': r[1]} for r in flood]

        # 级别 × 确认 交叉（看哪一类报警最被忽略）
        cross = Counter()
        for r in conn.execute('SELECT alarm_level, acknowledged, COUNT(*) FROM alarm_records GROUP BY 1,2'):
            cross[(r[0], bool(r[1]))] += r[2]
        result['level_ack_cross'] = {
            f'{level}|{"ack" if ack else "unack"}': n for (level, ack), n in cross.items()}

        return result
    finally:
        conn.close()


def _fmt(value, digits=2):
    if value is None:
        return '—'
    if isinstance(value, float):
        return f'{value:.{digits}f}'
    return str(value)


def render_markdown(reports: list[dict]) -> str:
    lines = ['# 报警绩效 KPI 基线（ISA-18.2）', '']
    lines.append(f'生成时间：{reports[0]["generated_at"]}')
    lines.append('')
    for rep in reports:
        lines.append(f'## `{rep["db"]}`')
        lines.append('')
        lines.append(f'- 时间跨度：{rep["first_alarm"]} → {rep["last_alarm"]}'
                     f'（{_fmt(rep["span_days"], 1)} 天）')
        lines.append(f'- 报警总数：**{rep["total"]}**')
        rate = rep['alarms_per_hour']
        verdict = '✅ 在 ISA 目标内' if rep.get('rate_within_isa_target') else '❌ 超出 ISA 目标（≤6/小时）'
        lines.append(f'- 报警率：**{_fmt(rate)} 条/小时**（{_fmt(rep["alarms_per_day"])} 条/天）{verdict}')
        lines.append(f'- 确认率：{_fmt(rep["ack_rate"] * 100 if rep["ack_rate"] is not None else None, 1)}%'
                     f'（{rep["acknowledged"]}/{rep["total"]}）')
        if rep.get('clear_rate') is not None:
            lines.append(f'- 恢复率：{_fmt(rep["clear_rate"] * 100, 1)}%'
                         f'（仍在报警 {rep["still_active"]} 条）')
        else:
            lines.append(f'- 恢复率：**无法判定**（{rep.get("note", "")}）')
        lines.append(f'- critical 占比：{_fmt(rep["critical_share"] * 100 if rep["critical_share"] is not None else None, 1)}%')
        if rep.get('chatter_share') is not None:
            lines.append(f'- 抖动（相邻间隔<60s 的对数）：{rep["chatter_pairs"]}'
                         f'（占 {_fmt(rep["chatter_share"] * 100, 1)}%）')
        lines.append('')
        lines.append('### 级别分布')
        lines.append('')
        lines.append('| 级别 | 条数 |')
        lines.append('|---|---|')
        for level, n in rep['by_level'].items():
            lines.append(f'| {level} | {n} |')
        lines.append('')
        lines.append('### Bad actor（前 10）')
        lines.append('')
        lines.append('| 设备 | 寄存器 | 报警ID | 条数 | 占比 |')
        lines.append('|---|---|---|---|---|')
        for a in rep['bad_actors']:
            lines.append(f'| {a["device_id"]} | {a["register_name"]} | {a["alarm_id"]} '
                         f'| {a["count"]} | {_fmt(a["share"] * 100, 1)}% |')
        lines.append('')
        if rep['duplicate_definitions']:
            lines.append('### ⚠️ 重复定义（同一寄存器挂了多条规则）')
            lines.append('')
            for d in rep['duplicate_definitions']:
                sample = ', '.join(d['sample_ids'])
                more = f' …（共 {d["rule_count"]} 个）' if d['truncated'] else ''
                lines.append(f'- `{d["device_id"]}/{d["register_name"]}`：'
                             f'{d["rule_count"]} 条 → {sample}{more}')
            lines.append('')
        if rep['worst_10min_buckets']:
            lines.append('### ⚠️ 最严重的 10 分钟窗口（ISA 泛滥阈值 >10）')
            lines.append('')
            for b in rep['worst_10min_buckets']:
                lines.append(f'- {b["bucket"]}：{b["count"]} 条')
            lines.append('')
    return '\n'.join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='从真实数据库算 ISA-18.2 报警 KPI')
    ap.add_argument('--db', action='append', required=True,
                    help='数据库路径，可重复给多个')
    ap.add_argument('--json', help='把结果写成 JSON')
    ap.add_argument('--md', help='把结果写成 Markdown 报告')
    args = ap.parse_args(argv)

    reports = []
    for db in args.db:
        try:
            reports.append(collect(db))
        except FileNotFoundError as exc:
            print(f'[skip] {exc}', file=sys.stderr)
    if not reports:
        print('没有任何可用的数据库', file=sys.stderr)
        return 2

    text = render_markdown(reports)
    print(text)
    if args.md:
        Path(args.md).write_text(text, encoding='utf-8')
    if args.json:
        Path(args.json).write_text(
            json.dumps(reports, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
