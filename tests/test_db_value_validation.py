"""
落库层的数值校验（round 246）

缺陷
----
`Database.insert_data_batch()` 是数据落库的**最后一道关**，但只捕获
`KeyError/TypeError`（字段缺失），**完全不校验 `value` 的类型与有限性**。

SQLite 是**弱类型**，非法值不会报错，只会静默变味。实测（真实 Database）：

    normal  42.5  → real   42.5      ✅
    nan     nan   → null   None      ❌ 与「没有数据」无法区分
    inf     inf   → real   inf       ❌ 趋势图无穷远点，AVG/MAX 全变 inf
    'abc'   'abc' → text   'abc'     ❌ SUM/AVG 遇它按 0，聚合静默偏小
    None    None  → null   None      ❌ 静默丢一条

上游（采集层 `_decode_register` / `_collect_from_cache`）已经修过两处，
但上游永远修不完：手工灌数、导入脚本、新协议客户端都会绕过它。
**落库层不校验 = 污染无底**。这是本仓库反复出现的模式：
防御要放在最靠近数据的地方，而不是指望每个调用方都记得。

修复：`value` 过 `try_finite_float`，非有限值/非数值一律跳过并 warning，
同时计入 `get_skipped_invalid_count()`（只有日志的话没人会去翻）。
"""

import os
import sqlite3
import sys
from datetime import datetime

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from 存储层.database import Database  # noqa: E402


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "t.db"))
    yield database
    database.close()


def _row(reg, value, **over):
    base = {
        'device_id': 'D1',
        'register_name': reg,
        'value': value,
        'unit': '',
        'timestamp': datetime.now(),
    }
    base.update(over)
    return base


def _history_regs(db):
    with sqlite3.connect(db.db_path) as conn:
        return {r[0] for r in conn.execute("SELECT register_name FROM history_data")}


def _history_type(db, reg):
    with sqlite3.connect(db.db_path) as conn:
        row = conn.execute(
            "SELECT value, typeof(value) FROM history_data WHERE register_name = ?", (reg,)
        ).fetchone()
    return row


# ============================================================
# 1. 非法值不得入库
# ============================================================

class TestInvalidRejected:
    @pytest.mark.parametrize('bad,label', [
        (float('nan'), 'nan'),
        (float('inf'), 'inf'),
        (float('-inf'), '-inf'),
        ('abc', 'text'),
        (None, 'none'),
        ([1, 2], 'list'),
        ({'a': 1}, 'dict'),
    ])
    def test_not_written(self, db, bad, label):
        db.insert_data_batch([_row('r_bad', bad)])
        assert 'r_bad' not in _history_regs(db), f'{label} 被写进了 history_data'

    @pytest.mark.parametrize('bad', [float('nan'), float('inf'), 'abc', None])
    def test_not_in_realtime_either(self, db, bad):
        db.insert_data_batch([_row('r_bad', bad)])
        # 没有数据时 get_latest_data 返回 None（不是空 dict），用 falsy 判断
        assert not db.get_latest_data('D1'), 'realtime_data 里出现了非法值'

    def test_nan_does_not_become_null(self, db):
        """SQLite 会把 float('nan') 绑成 NULL —— 读回来与「没数据」无法区分。"""
        db.insert_data_batch([_row('r_nan', float('nan'))])
        assert _history_type(db, 'r_nan') is None

    def test_inf_does_not_pollute_stats(self, db):
        """inf 会污染 AVG/MAX —— 必须拒。"""
        db.insert_data_batch([_row('r_inf', float('inf'))])
        with sqlite3.connect(db.db_path) as conn:
            n = conn.execute("SELECT COUNT(*) FROM history_data WHERE value = 9e999").fetchone()[0]
        assert n == 0

    def test_text_does_not_become_text_column(self, db):
        """`value` 列声明 REAL，SQLite 弱类型允许塞 TEXT。"""
        db.insert_data_batch([_row('r_txt', 'abc')])
        assert _history_type(db, 'r_txt') is None


# ============================================================
# 2. 合法值不受影响
# ============================================================

class TestValidAccepted:
    def test_float(self, db):
        db.insert_data_batch([_row('r1', 42.5)])
        assert _history_type(db, 'r1') == (42.5, 'real')

    def test_int(self, db):
        db.insert_data_batch([_row('r2', 7)])
        assert _history_type(db, 'r2') == (7.0, 'real')

    def test_numeric_string_is_normalized(self, db):
        """'42.5' 是「数字的字符串形式」，应该被接受并规范成 float。

        这和 'abc' 不同：前者是同一个值的不同表示，后者根本不是数。
        """
        db.insert_data_batch([_row('r3', '42.5')])
        assert _history_type(db, 'r3') == (42.5, 'real')

    def test_bool_becomes_number(self, db):
        db.insert_data_batch([_row('r4', True)])
        assert _history_type(db, 'r4') == (1.0, 'real')

    def test_zero_is_valid(self, db):
        """0 是合法值，不能因为 falsy 被当成缺失。"""
        db.insert_data_batch([_row('r5', 0)])
        assert _history_type(db, 'r5') == (0.0, 'real')


# ============================================================
# 3. 一批里混好坏：好的要进、坏的要跳（不能整批丢）
# ============================================================

def test_mixed_batch_keeps_good_rows(db):
    db.insert_data_batch([
        _row('ok1', 1.0),
        _row('bad_nan', float('nan')),
        _row('ok2', 2.0),
        _row('bad_txt', 'oops'),
        _row('ok3', 3.0),
    ])

    regs = _history_regs(db)
    assert {'ok1', 'ok2', 'ok3'} <= regs
    assert not ({'bad_nan', 'bad_txt'} & regs)


# ============================================================
# 4. 必须可观测
# ============================================================

def test_skipped_counter(db):
    assert db.get_skipped_invalid_count() == 0

    db.insert_data_batch([_row('a', float('nan')), _row('b', 'x'), _row('c', 1.0)])

    assert db.get_skipped_invalid_count() == 2

    db.insert_data_batch([_row('d', float('inf'))])
    assert db.get_skipped_invalid_count() == 3


def test_missing_field_also_counted(db):
    """缺字段和非法值都属于「被拒的记录」，计数口径要一致。"""
    db.insert_data_batch([{'device_id': 'D1'}])
    assert db.get_skipped_invalid_count() == 1


def test_skip_is_logged(db, caplog):
    with caplog.at_level('WARNING'):
        db.insert_data_batch([_row('r', float('nan'))])

    assert any('非数值' in r.getMessage() for r in caplog.records), \
        '非法值被静默跳过了 —— 日志里看不到痕迹'


# ============================================================
# 5. 静态守卫
# ============================================================

def test_insert_data_batch_validates_value():
    import pathlib
    import re

    source = (pathlib.Path(PROJECT_ROOT) / '存储层' / 'database.py').read_text(encoding='utf-8')
    body = source.split('def insert_data_batch', 1)[1].split('def delete_device_data', 1)[0]

    assert re.search(r'try_finite_float\(d\[.value.\]\)', body), \
        'insert_data_batch 没有校验 value —— 非法值会静默污染 history_data'
    assert 'continue' in body, '校验分支必须 continue（跳过该条，不整批丢）'
