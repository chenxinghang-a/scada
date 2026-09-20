# -*- coding: utf-8 -*-
"""压缩膨胀的 SCADA 数据库。

背景：scada_simulated.db 达 3.1GB，但库内 actual 数据仅约 27MB
      （history 4.8MB + alarms 22MB + realtime 28KB）—— 3.1GB 全是空闲页/碎片。
      膨胀会让 SQLite 写入变慢甚至失败，表现为"采集在跑但数据进不了库、
      队列文件持续膨胀"。

做法：VACUUM INTO 生成紧凑副本（不动原库），校验行数一致后再由调用方替换。
      相比直接 VACUUM，VACUUM INTO 不需要在原库上做原地重写，风险更低。
"""
import os
import sqlite3
import sys
import time

SRC = 'data/scada_simulated.db'
DST = 'data/scada_simulated_compact.db'
TABLES = ['realtime_data', 'history_data', 'alarm_records', 'device_status', 'history_archive']


def count(db: str):
    out = {}
    c = sqlite3.connect(db, timeout=60)
    c.execute('PRAGMA busy_timeout=60000')
    for t in TABLES:
        try:
            out[t] = c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        except sqlite3.Error as e:
            out[t] = f'ERR:{e}'
    c.close()
    return out


def main() -> int:
    if not os.path.exists(SRC):
        print(f'源库不存在: {SRC}')
        return 1
    if os.path.exists(DST):
        print(f'目标已存在，先删除: {DST}')
        os.remove(DST)

    before_size = os.path.getsize(SRC)
    before_rows = count(SRC)
    print(f'原始库: {before_size / 1e6:.1f} MB')
    print(f'原始行数: {before_rows}')
    sys.stdout.flush()

    t0 = time.time()
    c = sqlite3.connect(SRC, timeout=60)
    c.execute('PRAGMA busy_timeout=60000')
    try:
        c.execute(f"VACUUM INTO '{DST}'")
    finally:
        c.close()

    elapsed = time.time() - t0
    after_size = os.path.getsize(DST)
    after_rows = count(DST)

    print(f'压缩耗时: {elapsed:.1f}s')
    print(f'压缩后: {after_size / 1e6:.1f} MB ({before_size / after_size:.0f}x 缩小)')
    print(f'压缩后行数: {after_rows}')

    ok = before_rows == after_rows
    print('行数一致:', ok)
    if not ok:
        print('!! 行数不一致，不要替换原库')
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
