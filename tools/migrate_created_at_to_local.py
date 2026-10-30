#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把历史数据里「UTC 的 created_at」迁移到与 timestamp 同一尺度（本地时间）。

背景（round 326）
-----------------
``created_at`` 原本吃 SQLite 的 ``DEFAULT CURRENT_TIMESTAMP``（**UTC**），
而 ``timestamp`` 由 Python ``datetime.now()`` 写入（**本地时间**）。
实测 ``data/scada.db`` 三张表各 5000 行样本，``timestamp - created_at``
恒为 **+8 小时**（本机 UTC+8）。

代码侧已改成显式写 ``created_at = datetime.now()``，**新数据不再有问题**；
但历史行的 ``created_at`` 仍是 UTC，与同行的 ``timestamp`` 差一个时区 ——
跨字段比较、按 created_at 分天的报表仍会错 8 小时。

本工具只做一件事：把**差值恰好等于本地时区偏移**的那些行的 ``created_at``
平移一个时区偏移。差值不是偏移量的行（例如「陈旧缓存值」导致 timestamp
远早于写入时刻）**不动**。

幂等性
------
迁移后差值变成 ≈0，第二次运行匹配不到任何行。另在 ``schema_migrations``
里记一条版本号，重复运行直接跳过。

用法
----
    python tools/migrate_created_at_to_local.py --dry-run        # 只统计
    python tools/migrate_created_at_to_local.py --apply          # 真正改
    python tools/migrate_created_at_to_local.py --apply --db data/scada.db
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import sqlite3
import sys

VERSION = "20261030_created_at_local_scale"
TABLES = ("realtime_data", "history_data", "alarm_records")
DEFAULT_DBS = ("data/scada.db", "data/scada_simulated.db")


def local_offset_hours() -> float:
    """本机当前 UTC 偏移（小时，东八区 = 8.0）。"""
    off = _dt.datetime.now().astimezone().utcoffset()
    return off.total_seconds() / 3600.0 if off else 0.0


def ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")


def already_applied(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (VERSION,)
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def count_candidates(conn: sqlite3.Connection, table: str, offset: float) -> int:
    """差值落在 [offset-1min, offset+1min] 的行数（= 确认是时区差的行）。"""
    tol = 1.0 / 1440.0          # 1 分钟，单位「天」
    sql = (
        f"SELECT COUNT(*) FROM {table} "
        f"WHERE timestamp IS NOT NULL AND created_at IS NOT NULL "
        f"AND ABS(julianday(timestamp) - julianday(created_at) - ?) < ?"
    )
    return conn.execute(sql, (offset / 24.0, tol)).fetchone()[0]


def count_mismatched(conn: sqlite3.Connection, table: str) -> int:
    """差值明显不是 0、也不是偏移量的行（陈旧数据等），仅用于报告。"""
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} "
        f"WHERE timestamp IS NOT NULL AND created_at IS NOT NULL"
    ).fetchone()[0]


def migrate_table(conn: sqlite3.Connection, table: str, offset: float) -> int:
    tol = 1.0 / 1440.0
    sign = "+" if offset >= 0 else "-"
    mod = f"{sign}{abs(offset):g} hours"
    cur = conn.execute(
        f"UPDATE {table} SET created_at = datetime(created_at, ?) "
        f"WHERE timestamp IS NOT NULL AND created_at IS NOT NULL "
        f"AND ABS(julianday(timestamp) - julianday(created_at) - ?) < ?",
        (mod, offset / 24.0, tol),
    )
    return cur.rowcount


def process(db_path: str, apply: bool, offset: float) -> int:
    if not os.path.exists(db_path):
        print(f"  跳过（不存在）: {db_path}")
        return 0

    size_gb = os.path.getsize(db_path) / 2**30
    print(f"\n=== {db_path}  ({size_gb:.2f} GB) ===")

    conn = sqlite3.connect(db_path)
    try:
        ensure_migrations_table(conn)
        if already_applied(conn):
            print(f"  已迁移过（{VERSION}），跳过。")
            return 0

        plan = []
        for t in TABLES:
            try:
                n = count_candidates(conn, t, offset)
                total = count_mismatched(conn, t)
            except sqlite3.Error as e:
                print(f"  {t}: 查询失败 {e}")
                continue
            plan.append((t, n, total))
            print(f"  {t:<16} 待平移 {n:>7} 行 / 共 {total:>7} 行")

        todo = sum(n for _, n, _ in plan)
        if todo == 0:
            print("  没有需要迁移的行（差值不匹配本机时区偏移）。")
            return 0

        if not apply:
            print(f"  [dry-run] 将平移 {todo} 行的 created_at（偏移 {offset:+g} 小时）。"
                  f"加 --apply 执行。")
            return todo

        print(f"  开始平移（偏移 {offset:+g} 小时）…", flush=True)
        moved = 0
        for t, _, _ in plan:
            n = migrate_table(conn, t, offset)
            moved += n
            print(f"    {t:<16} 更新 {n} 行", flush=True)
        conn.execute(
            "INSERT OR REPLACE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (VERSION, _dt.datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        print(f"  完成：共更新 {moved} 行，已记录迁移版本 {VERSION}")
        return moved
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只统计）")
    ap.add_argument("--db", action="append", help="指定数据库（可多次）；默认 data/scada*.db")
    args = ap.parse_args()

    offset = local_offset_hours()
    print(f"本机 UTC 偏移：{offset:+g} 小时")
    if offset == 0:
        print("⚠️ 本机就在 UTC —— 无法靠差值区分「UTC 的 created_at」与「本地时间」，"
              "本工具不适用（也没必要：两者本来就相等）。")
        return 0

    dbs = args.db or list(DEFAULT_DBS)
    total = 0
    for db in dbs:
        total += process(db, args.apply, offset)

    if not args.apply and total:
        print(f"\n合计待迁移 {total} 行。确认后加 --apply 执行（建议先停服务/备份库）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
