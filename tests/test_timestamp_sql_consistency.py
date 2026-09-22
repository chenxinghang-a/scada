"""SQL 时间戳格式一致性回归测试。

背景（2026-09-16 代码质量审计）：
库内 timestamp 由 sqlite3 适配器 ``存储层/database.py::adapt_datetime`` 写成
**空格分隔**的 ``'YYYY-MM-DD HH:MM:SS'``，而 ``datetime.isoformat()`` 默认产出
**'T' 分隔**。SQLite 按文本比较，ASCII 中 ``'T'(0x54) > ' '(0x20)``，于是：

    '2026-08-17 23:59:59' < '2026-08-17T10:00:00'   → True（错误！）

后果分两类，且**都不会报错**，只会静默给出错误结果：
  - ``WHERE timestamp < ?``（清理/归档）：同一天里比 cutoff 更晚的记录被多删。
  - ``WHERE timestamp > ?`` / ``BETWEEN ? AND ?``（统计/报告）：同一天的记录被漏算。

本文件同时做两件事：
  1. 静态扫描：防止新增代码再次写出 ``isoformat()`` 参与 SQL 时间比较。
  2. 行为验证：对已修的 6 个模块断言"同日记录不被误删 / 不被漏算"。
"""
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# 扫描时跳过的目录
_SKIP_DIRS = {
    '.venv', 'dist', 'build', 'node_modules', '.git', '__pycache__',
    'backup', 'tests',
}

# 已知无害的 ``isoformat()`` 命中（(相对路径, 该行应包含的片段) -> 理由）
ALLOWED_HITS = {
    ('core/data_compressor.py', 'Archived:'):
        "gzip 归档文件的注释头，不参与任何 SQL 比较",
    ('core/report_generator.py', '默认产出'):
        "本文件 _sql_ts() 的 docstring，说明的就是这个坑",
}

_SQL_RE = re.compile(
    r'(WHERE|DELETE\s+FROM|SELECT|BETWEEN)[^;]{0,400}?'
    r'(timestamp|created_at|updated_at|_at\b)', re.I)
_PLACEHOLDER_RE = re.compile(r'[<>]=?\s*\?|BETWEEN\s*\?\s*AND\s*\?', re.I)


def _iter_scanned_py():
    """产出待扫描的 (相对路径, 绝对路径)，只含「人写的代码」。

    为什么不用 ``REPO_ROOT.rglob('*.py')`` 直接扫盘：仓库里存在**构建产物**目录
    （``dist-scada-<版本>/``、``build-scada-<版本>/``，见 .gitignore），它们是源码的
    **逐字副本**。扫到它们就会对着构建产物报违规 —— 表现为「本地一打包，守卫就红」。
    **假红比漏报更危险**：它训练人「看到守卫红了就忽略」，等于把守卫废掉。
    2026-09-22 实测踩到（PyInstaller 输出 ``dist-scada-1.3.1037/``）。

    改用 git 的文件清单（受跟踪 + 未被忽略的新文件），天然只覆盖「人写的代码」；
    git 不可用时退回 ``rglob`` + :data:`_SKIP_DIRS`（历史行为）。
    """
    import subprocess

    listed = None
    try:
        proc = subprocess.run(
            ['git', 'ls-files', '-z', '--cached', '--others',
             '--exclude-standard', '--', '*.py'],
            cwd=str(REPO_ROOT),
            capture_output=True,
            timeout=60,
        )
        if proc.returncode == 0:
            # `-z` 用 NUL 分隔，非 ASCII 路径不会被转义
            listed = [p for p in proc.stdout.decode('utf-8', 'replace').split('\0') if p]
    except (OSError, subprocess.SubprocessError):
        listed = None

    if listed is None:
        for path in REPO_ROOT.rglob('*.py'):
            if set(path.parts) & _SKIP_DIRS:
                continue
            rel = str(path.relative_to(REPO_ROOT)).replace('\\', '/')
            yield rel, path
        return

    for rel in listed:
        if set(rel.split('/')[:-1]) & _SKIP_DIRS:
            continue
        yield rel, REPO_ROOT / rel.replace('/', os.sep)


def _scan_bare_isoformat():
    """返回 [(相对路径, 行号, 行内容)]，即疑似参与 SQL 时间比较的 isoformat()"""
    hits = []
    for rel, path in _iter_scanned_py():
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for idx, line in enumerate(lines):
            if 'isoformat()' not in line:
                continue
            window = '\n'.join(lines[max(0, idx - 6):idx + 8])
            if _SQL_RE.search(window) or _PLACEHOLDER_RE.search(window):
                hits.append((rel, idx + 1, line.strip()))
    return hits


def test_no_bare_isoformat_in_sql_comparisons():
    """不得再用 isoformat() 生成 SQL 时间比较值（应用 isoformat(sep=' ')）。"""
    offenders = []
    for rel, lineno, text in _scan_bare_isoformat():
        if any(rel == allowed_file and snippet in text
               for (allowed_file, snippet) in ALLOWED_HITS):
            continue
        offenders.append(f'{rel}:{lineno}  {text}')

    assert not offenders, (
        "以下位置用 datetime.isoformat() 生成 SQL 时间比较值。\n"
        "库内 timestamp 是空格分隔格式，'T' 分隔会导致同日记录被误删/漏算。\n"
        "请改为 .isoformat(sep=' ')：\n  " + '\n  '.join(offenders)
    )


def test_scan_excludes_build_output():
    """构建产物目录不得进入扫描集 —— 假红比漏报更危险。

    回归背景（2026-09-22）：本地跑完 PyInstaller（输出 ``dist-scada-1.3.1037/``）后，
    本文件与 ``test_silent_exception_guard.py`` 双双变红，报的全是
    **构建产物里的源码副本**。历史实现按目录名精确匹配跳过（只认 ``dist`` / ``build``），
    认不出 ``dist-scada-*`` / ``build-scada-*`` 这类带后缀的产物目录。
    修法是「只扫 git 清单」（见 :func:`_iter_scanned_py`），这条测试钉住该性质。
    """
    scanned = list(_iter_scanned_py())
    assert scanned, '扫描集为空，守卫已失效'
    # 只看**目录段**：`build.py` / `build.bat` / `release-manifest.json` 是文件，不算
    offenders = [
        rel for rel, _ in scanned
        if len(rel.split('/')) > 1
        and rel.split('/')[0].startswith(('dist', 'build', 'release'))
    ]
    assert not offenders, f'扫描集混入构建产物: {offenders[:5]}'


def test_adapter_output_is_space_separated():
    """写库路径（sqlite3 适配器）必须是空格分隔，这是上述一切的前提。"""
    from 存储层.database import adapt_datetime

    assert adapt_datetime(datetime(2026, 8, 17, 10, 30, 0)) == '2026-08-17 10:30:00'
    # 与 datetime 直接作为 SQL 参数写入的结果一致
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE t (ts TEXT)')
    conn.execute('INSERT INTO t VALUES (?)', (datetime(2026, 8, 17, 10, 30, 0),))
    stored = conn.execute('SELECT ts FROM t').fetchone()[0]
    conn.close()
    assert stored == '2026-08-17 10:30:00', f'库内格式变了: {stored!r}'


# ---------------------------------------------------------------- 行为验证
def _ts(dt: datetime) -> str:
    """按库内格式写入测试数据"""
    return dt.isoformat(sep=' ')


def _make_history_db(tmp_path, rows):
    db = tmp_path / 'hist.db'
    conn = sqlite3.connect(str(db))
    conn.execute('''
        CREATE TABLE history_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            register_name TEXT NOT NULL,
            value REAL,
            unit TEXT,
            timestamp DATETIME NOT NULL
        )
    ''')
    # report_generator 会一并查报警表；建出来避免它走 except 分支把统计吞成 0
    conn.execute('''
        CREATE TABLE alarm_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            register_name TEXT,
            alarm_level TEXT,
            alarm_message TEXT,
            timestamp DATETIME NOT NULL,
            acknowledged INTEGER DEFAULT 0
        )
    ''')
    conn.executemany(
        'INSERT INTO history_data (device_id, register_name, value, unit, timestamp)'
        ' VALUES (?, ?, ?, ?, ?)', rows)
    conn.commit()
    conn.close()
    return str(db)


def test_clean_history_data_keeps_same_day_records(tmp_path):
    """DataCleaner.clean_history_data 不得删掉保留期内的同日记录。"""
    from core.ops_tools import DataCleaner

    now = datetime.now()
    cutoff = now - timedelta(days=2)
    inside = cutoff + timedelta(hours=1)      # 保留期内，且与 cutoff 同一天
    outside = cutoff - timedelta(days=1)      # 真的过期

    db = _make_history_db(tmp_path, [
        ('dev1', 'reg1', 1.0, 'C', _ts(inside)),
        ('dev1', 'reg2', 2.0, 'C', _ts(outside)),
    ])

    result = DataCleaner(db_path=db).clean_history_data(retention_days=2)
    assert result['status'] == 'success', result

    conn = sqlite3.connect(db)
    left = conn.execute('SELECT register_name FROM history_data').fetchall()
    conn.close()
    assert [r[0] for r in left] == ['reg1'], (
        "保留期内的同日记录被误删（isoformat 的 'T' 分隔把 cutoff 抬高了）"
    )
    assert result['deleted_rows'] == 1, result


def test_clean_audit_logs_keeps_same_day_records(tmp_path):
    """DataCleaner.clean_audit_logs 不得删掉保留期内的同日记录。"""
    from core.ops_tools import DataCleaner

    db = tmp_path / 'audit.db'
    conn = sqlite3.connect(str(db))
    conn.execute('CREATE TABLE audit_log (id INTEGER PRIMARY KEY, timestamp DATETIME)')
    now = datetime.now()
    cutoff = now - timedelta(days=30)
    conn.execute('INSERT INTO audit_log VALUES (1, ?)', (_ts(cutoff + timedelta(hours=1)),))
    conn.execute('INSERT INTO audit_log VALUES (2, ?)', (_ts(cutoff - timedelta(days=1)),))
    conn.commit()
    conn.close()

    result = DataCleaner(db_path=str(db)).clean_audit_logs(retention_days=30)
    assert result['status'] == 'success', result

    conn = sqlite3.connect(str(db))
    left = [r[0] for r in conn.execute('SELECT id FROM audit_log').fetchall()]
    conn.close()
    assert left == [1], "保留期内的同日审计日志被误删"


def test_data_compressor_cutoff_matches_stored_format(tmp_path):
    """DataCompressor 的 cutoff 不得把保留期内的同日记录算作待压缩。"""
    from core.data_compressor import DataCompressor

    now = datetime.now()
    cutoff = now - timedelta(days=1)
    db = _make_history_db(tmp_path, [
        ('dev1', 'reg1', 1.0, 'C', _ts(cutoff + timedelta(hours=1))),
    ])
    compressor = DataCompressor(db, archive_dir=str(tmp_path / 'archive'))

    result = compressor.compress_old_data(table='history_data', days=1)
    assert result['rows_compressed'] == 0, (
        f"保留期内的同日记录被当成过期数据压缩了: {result}"
    )

    conn = sqlite3.connect(db)
    remaining = conn.execute('SELECT COUNT(*) FROM history_data').fetchone()[0]
    conn.close()
    assert remaining == 1


def test_report_generator_includes_same_day_start_records(tmp_path):
    """ReportGenerator 不得漏算起始日当天的记录（BETWEEN 下界被抬高）。"""
    from core.report_generator import ReportGenerator

    now = datetime.now()
    start = now - timedelta(days=1)
    db = _make_history_db(tmp_path, [
        # 落在 [start, end] 窗口内，且与 start 同一天
        ('dev1', 'reg1', 1.0, 'C', _ts(start + timedelta(hours=1))),
    ])

    report = ReportGenerator(db).generate_device_report('dev1', period='day')
    assert report['summary']['data_points'] == 1, (
        f"起始日当天的记录被漏算: {report['summary']}"
    )
