# -*- coding: utf-8 -*-
"""备份恢复演练（round 273）：每天真的验一次"备份能不能用"。

为什么需要
----------
`Database.backup_database()` 从很早就有，`restore_database()` 是 round 261 才补的
——但两者都是**被调用时才执行**。生产上没人每天去点一次"恢复"，于是：

* 备份文件可能是坏的（备份时磁盘满、进程被杀、只写了半截）；
* 备份可能**早就停了**（定时任务挂了、路径变了），而没人发现；
* 等到真出事要恢复时，才发现最近一个可用备份是三个月前的。

这三件事的共同点是**只在最需要备份的那天才暴露**。所以这里做成每日演练：
找到最新备份 → 只读打开 → `integrity_check` + 必需表 + 行数 → 记录结果。
失败就报警（`backup_drill_failed`），恢复后自动清除。

为什么不真的"恢复一遍"
----------------------
把 500MB 的库整份复制到临时目录再恢复，每天一次是不必要的磁盘与 IO 开销。
`integrity_check` 能发现截断/坏页；必需表与行数能发现"备份的是空库/别的库"；
再叠加**新鲜度**（最近一次备份多久以前）就覆盖了实际会遇到的失败模式。
真正的"恢复"路径由 `tests/test_backup_restore_drill.py` 每次 CI 验证。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

#: 备份目录（与 `Database.backup_database` 的默认值一致）
DEFAULT_BACKUP_DIR = 'data/backups'

#: 备份"太旧"的阈值（小时）。超过说明定时备份已经停了。
STALE_BACKUP_HOURS = 48

#: 恢复时必须存在的表
REQUIRED_TABLES = ('realtime_data', 'history_data', 'alarm_records')

#: 最近一次演练结果（供 `/api/health` 详情读取）。
#: 演练是每天一次的维护任务，健康页却是随时查的 —— 不缓存的话，
#: 要么健康页看不到这项，要么每次请求都去跑一遍完整性校验（几百 MB IO）。
_LAST_RESULT: dict | None = None


def get_last_result() -> dict | None:
    """最近一次演练结果（`None` 表示本次进程还没跑过）。"""
    return _LAST_RESULT


def find_latest_backup(backup_dir: str | Path) -> Path | None:
    """在备份目录里找最新的 `.db` 文件（按修改时间）。"""
    directory = Path(backup_dir)
    if not directory.is_dir():
        return None
    candidates = [p for p in directory.glob('*.db') if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def verify_backup(path: str | Path) -> tuple[bool, dict]:
    """只读校验一个备份文件。

    Returns:
        `(ok, detail)`；detail 含 `integrity` / `tables` / `rows` / `problems`。
    """
    target = Path(path)
    detail: dict = {'path': str(target), 'problems': [], 'rows': {}}

    if not target.exists():
        detail['problems'].append('备份文件不存在')
        return False, detail
    if target.stat().st_size == 0:
        detail['problems'].append('备份文件是 0 字节')
        return False, detail

    try:
        conn = sqlite3.connect(f'file:{target.as_posix()}?mode=ro', uri=True)
    except sqlite3.Error as exc:
        detail['problems'].append(f'无法打开: {exc}')
        return False, detail

    try:
        integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
        detail['integrity'] = integrity
        if integrity != 'ok':
            detail['problems'].append(f'完整性校验失败: {integrity}')

        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in REQUIRED_TABLES if t not in tables]
        detail['tables'] = sorted(tables & set(REQUIRED_TABLES))
        if missing:
            detail['problems'].append(f'缺少必需的表: {missing}')

        for table in REQUIRED_TABLES:
            if table not in tables:
                continue
            try:
                detail['rows'][table] = conn.execute(
                    f'SELECT COUNT(*) FROM {table}').fetchone()[0]
            except sqlite3.Error as exc:
                detail['problems'].append(f'统计 {table} 行数失败: {exc}')
    except sqlite3.Error as exc:
        detail['problems'].append(f'读取失败: {exc}')
    finally:
        conn.close()

    return not detail['problems'], detail


def run_drill(database=None, backup_dir: str = DEFAULT_BACKUP_DIR,
              alarm_manager=None, now: datetime | None = None) -> dict:
    """跑一次演练。

    Args:
        database: 生产库（用于对比行数；可选）
        backup_dir: 备份目录
        alarm_manager: 报警管理器（失败时报警）
        now: 参考时刻（便于测试）

    Returns:
        `{'ok': bool, 'backup': str|None, 'age_hours': float|None, 'problems': [...]}`
    """
    now = now or datetime.now()
    result: dict = {'ok': False, 'backup': None, 'age_hours': None,
                    'problems': [], 'checked_at': now.isoformat(timespec='seconds')}

    latest = find_latest_backup(backup_dir)
    if latest is None:
        result['problems'].append(f'备份目录里没有任何 .db 文件: {backup_dir}')
        return _finish(result, alarm_manager)

    result['backup'] = str(latest)
    age_hours = (now.timestamp() - latest.stat().st_mtime) / 3600.0
    result['age_hours'] = round(age_hours, 1)
    if age_hours > STALE_BACKUP_HOURS:
        result['problems'].append(
            f'最近一次备份是 {age_hours:.0f} 小时前（阈值 {STALE_BACKUP_HOURS}h）'
            f'—— 定时备份可能已经停了')

    ok, detail = verify_backup(latest)
    result['detail'] = detail
    result['problems'].extend(detail['problems'])

    # 行数对比：备份里的数据量不应远小于生产库（说明备份的是空库/旧库）
    if database is not None and detail.get('rows'):
        try:
            live = database.get_table_sizes()
            for table, backup_rows in detail['rows'].items():
                live_rows = live.get(table)
                if live_rows is None:
                    # round 298：`get_table_sizes()` 现在**不再**把查询失败填成 0，
                    # 所以 None 明确表示"生产库这张表查不到"。
                    # 这时不能说"没得比就跳过" —— 备份完整性无法校验本身
                    # 就是演练要报的问题（否则空备份 + 库被锁 = 演练通过）。
                    result['problems'].append(
                        f'{table} 生产库行数查不到 —— 备份完整性无法校验')
                    continue
                if live_rows == 0:
                    continue
                if backup_rows < live_rows * 0.5:
                    result['problems'].append(
                        f'{table} 备份只有 {backup_rows} 行，生产库有 {live_rows} 行'
                        f'（不足一半）—— 备份可能不完整')
        except Exception as exc:      # noqa: BLE001 —— 对比失败不算演练失败
            logger.debug('备份行数对比失败: %s', exc)

    result['ok'] = not result['problems']
    return _finish(result, alarm_manager)


def _finish(result: dict, alarm_manager) -> dict:
    """统一收尾：落日志/报警 + 缓存结果（供健康页读取）。

    ⚠️ 所有返回路径都必须走这里 —— 早退（找不到备份）如果绕过缓存，
    健康页会一直显示"尚未执行演练"，而实际上演练每天都跑、每天都在失败。
    """
    global _LAST_RESULT
    _LAST_RESULT = result
    _report(result, alarm_manager)
    return result


def _report(result: dict, alarm_manager) -> None:
    """把演练结果落到日志/报警（成功则清除上次的失败报警）。"""
    if result['ok']:
        logger.info("备份恢复演练通过: %s（%.1f 小时前）",
                    result['backup'], result['age_hours'] or 0)
        if alarm_manager is not None:
            try:
                alarm_manager.clear_external_alarm(
                    'backup_drill_failed', 'backup_drill', 'latest')
            except Exception as exc:      # noqa: BLE001
                logger.debug('清除备份演练报警失败: %s', exc)
        return

    logger.error("备份恢复演练失败: %s", '；'.join(result['problems']))
    if alarm_manager is not None:
        try:
            alarm_manager.raise_external_alarm(
                alarm_id='backup_drill_failed',
                message='备份不可用：' + '；'.join(result['problems'][:3]),
                level='critical',
                device_id='backup_drill',
                register_name='latest',
            )
        except Exception as exc:      # noqa: BLE001 —— 演练本身不能炸掉维护线程
            logger.error('备份演练报警登记失败: %s', exc)
