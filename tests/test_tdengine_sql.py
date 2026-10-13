"""`timeseries` 层的 SQL 构造 / 清洗 / 执行路径测试（安全相关 + 覆盖缺口）。

覆盖：
* **SQL 清洗原语**（`_sanitize_identifier` / `_escape_value` / `_sanitize_table_name` /
  `_escape_sql_value`）—— 这些是防注入的**唯一屏障**，必须有断言守着；
* `get_create_table_sql` 的**标签值转义**（device_id 等来自配置，可含引号）；
* `_format_query_result` 的列映射；
* `_execute_rest` / `_execute_native` 的**每一条错误路径**（原来基本没覆盖）；
* `write_telemetry` / `write_telemetry_batch` 的 SQL 构造与统计；
* `_ensure_table` 的缓存语义。

⚠️ 两个**已知隐患**在此钉住（不是本轮要改的）：
1. `str(None)` / `str(123)` 会被静默接受成标识符（`'None'` / `'123'` → 后者会被
   `_sanitize_table_name` 加前缀）。**类型错误不会报错，只会生成一个错名字**。
2. `_sanitize_table_name` 把非法字符**替换**成 `_`，于是 `plc-01` 与 `plc_01`
   会**碰撞成同一张子表** → 两个设备的数据写进同一张表（静默混数据）。
   改法（哈希后缀等）会改变既有表名，属破坏性变更，需主人拍板。
"""
import struct  # noqa: F401  (保持与其它协议测试文件一致的头，便于统一维护)
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from timeseries.data_models import (
    TelemetryRecord, _escape_sql_value, _sanitize_table_name, get_create_table_sql,
    get_telemetry_table_name,
)
from timeseries.tdengine_client import TDengineClient

INJECTIONS = [
    "x; DROP TABLE users",
    "x' OR '1'='1",
    'x" OR "1"="1',
    "x`;--",
    "x y",            # 空格
    "x-y",            # 连字符
    "1abc",           # 数字开头
    "",               # 空
]


def _client(**kw):
    kw.setdefault('host', '127.0.0.1')
    return TDengineClient(**kw)


def _record(**kw):
    base = dict(device_id='plc_01', register_name='temperature',
                timestamp=datetime(2026, 10, 13, 5, 0, 0, 123456),
                value=12.5, quality=192, unit='C', protocol='modbus_tcp',
                gateway_id='gw1')
    base.update(kw)
    return TelemetryRecord(**base)


# ---------------------------------------------------------------------------
# 1. 标识符清洗（防注入的唯一屏障）
# ---------------------------------------------------------------------------

class TestSanitizeIdentifier:
    @pytest.mark.parametrize('ok', ['a', '_a', 'plc_01', 'A1_b2', 'x' * 60])
    def test_accepts_valid(self, ok):
        assert _client()._sanitize_identifier(ok) == ok

    @pytest.mark.parametrize('bad', INJECTIONS)
    def test_rejects_injection_and_malformed(self, bad):
        with pytest.raises(ValueError):
            _client()._sanitize_identifier(bad)

    def test_none_is_silently_coerced(self):
        """⚠️ 钉住隐患 1：`str()` 静默兜底 —— 传 None 不报错，得到 'None'。

        'None' 是合法标识符，所以**类型错误被吞掉了**：调用方以为写进了自己的表名，
        实际写进了 `tel_None_...`（表名带 tel_ 前缀，所以不报错、只是名字不对）。
        """
        c = _client()
        assert c._sanitize_identifier(None) == 'None'

    def test_numeric_identifier_is_rejected(self):
        """数字会被 `str()` 成 '123'，但标识符不得以数字开头 → 必须拒绝"""
        with pytest.raises(ValueError):
            _client()._sanitize_identifier(123)

    def test_error_message_mentions_the_value(self):
        with pytest.raises(ValueError) as ei:
            _client()._sanitize_identifier("bad name")
        assert 'bad name' in str(ei.value)


class TestEscapeValue:
    def test_escapes_backslash_then_quote(self):
        assert _client()._escape_value("a\\b") == "a\\\\b"
        assert _client()._escape_value("a'b") == "a\\'b"

    def test_strips_nul(self):
        assert '\x00' not in _client()._escape_value("a\x00b")

    @pytest.mark.parametrize('payload', ["'; DROP TABLE t; --", "a' OR '1'='1"])
    def test_result_has_no_unescaped_quote(self, payload):
        """元守卫：转义后不得留下**未转义**的单引号（否则包进 '...' 就逃逸了）"""
        out = _client()._escape_value(payload)
        assert out.count("'") == out.count("\\'"), f'仍有裸引号: {out!r}'

    def test_same_as_data_models_helper(self):
        """两处转义实现必须一致（否则修了一处漏一处）"""
        for v in ["a'b", 'a\\b', 'a\x00b', "'; --"]:
            assert _client()._escape_value(v) == _escape_sql_value(v)


# ---------------------------------------------------------------------------
# 2. 表名生成与碰撞隐患
# ---------------------------------------------------------------------------

class TestTableName:
    @pytest.mark.parametrize('raw,expected', [
        ('plc_01', 'plc_01'),
        ('plc-01', 'plc_01'),
        ('plc 01', 'plc_01'),
        ("plc'01", 'plc_01'),
        ('1plc', '_1plc'),
        ('', ''),
    ])
    def test_sanitize_table_name(self, raw, expected):
        assert _sanitize_table_name(raw) == expected

    def test_collision_caveat(self):
        """⚠️ 钉住隐患 2：不同设备 ID 会**碰撞成同一张子表**。

        `plc-01` 与 `plc_01` 都被清洗成 `plc_01` → 两个设备的数据写进同一张表。
        这是**静默混数据**（查询时看不出异常，只是两个设备的点混在一起）。
        改法（加哈希后缀等）会改变既有表名，属破坏性变更 → 需主人拍板。
        """
        assert get_telemetry_table_name('plc-01', 'temp') == \
               get_telemetry_table_name('plc_01', 'temp')

    def test_table_name_is_sql_safe(self):
        """注入串进来也必须得到一个「只有字母数字下划线」的表名"""
        name = get_telemetry_table_name("a'; DROP TABLE t; --", 'r')
        assert name.replace('_', '').isalnum()
        assert "'" not in name and ' ' not in name and ';' not in name

    def test_prefixes_are_stable(self):
        assert get_telemetry_table_name('d', 'r').startswith('tel_')
        assert _sanitize_table_name(None) == 'None'


# ---------------------------------------------------------------------------
# 3. 建表 SQL 的标签转义
# ---------------------------------------------------------------------------

class TestCreateTableSql:
    def test_tag_values_are_escaped(self):
        sql = get_create_table_sql('t1', 'device_telemetry',
                                   {'device_id': "a'b", 'register_name': 'r'})
        assert "\\'" in sql, '标签值里的单引号没有被转义'
        assert sql.startswith('CREATE TABLE IF NOT EXISTS t1 USING device_telemetry TAGS (')

    def test_injection_in_tag_value_is_neutralised(self):
        """真正的性质：**没有未转义的单引号** → 无法闭合字符串 → 无法逃逸。

        （「注入串不出现在 SQL 里」是错的判据：它当然会出现 —— 作为**被引号包住的
        字符串字面量**。安全性质是「它只能以字面量形式出现」。）
        """
        sql = get_create_table_sql('t1', 'st', {'device_id': "x'); DROP TABLE s; --"})
        # 剥掉所有「已转义引号」后，剩下的必须是**成对的定界引号**（1 个标签 → 2 个）
        assert sql.replace("\\'", '').count("'") == 2, f'存在未转义引号: {sql}'
        assert sql.startswith('CREATE TABLE IF NOT EXISTS t1 USING st TAGS (')

    def test_empty_tags(self):
        assert get_create_table_sql('t1', 'st', {}) == \
            'CREATE TABLE IF NOT EXISTS t1 USING st TAGS ()'


# ---------------------------------------------------------------------------
# 4. 查询结果格式化
# ---------------------------------------------------------------------------

class TestFormatQueryResult:
    def test_uses_column_meta(self):
        c = _client()
        result = {'column_meta': [['ts'], ['value']], 'data': [['2026', 1.0], ['2027', 2.0]]}
        rows = c._format_query_result(result)
        assert rows == [{'ts': '2026', 'value': 1.0}, {'ts': '2027', 'value': 2.0}]

    def test_explicit_columns(self):
        c = _client()
        rows = c._format_query_result({'data': [[1, 2]]}, columns=['a', 'b'])
        assert rows == [{'a': 1, 'b': 2}]

    @pytest.mark.parametrize('result', [{}, {'data': []}, {'data': None}, None])
    def test_empty_inputs(self, result):
        assert _client()._format_query_result(result) == []

    def test_short_row_skips_missing_columns(self):
        """列数比行长短时只填有值的列，不得用 None 冒充（会让下游把缺失当 0/空）"""
        c = _client()
        rows = c._format_query_result({'data': [[1]]}, columns=['a', 'b'])
        assert rows == [{'a': 1}]

    def test_extra_row_values_are_ignored(self):
        c = _client()
        assert c._format_query_result({'data': [[1, 2, 3]]}, columns=['a']) == [{'a': 1}]


# ---------------------------------------------------------------------------
# 5. 执行路径与错误分支
# ---------------------------------------------------------------------------

class TestExecuteDispatch:
    def test_dispatch_by_use_rest(self, monkeypatch):
        c = _client(use_rest=True)
        monkeypatch.setattr(c, '_execute_rest', lambda sql: {'via': 'rest'})
        monkeypatch.setattr(c, '_execute_native', lambda sql: {'via': 'native'})
        assert c._execute_sql('X')['via'] == 'rest'

        c2 = _client(use_rest=False)
        monkeypatch.setattr(c2, '_execute_rest', lambda sql: {'via': 'rest'})
        monkeypatch.setattr(c2, '_execute_native', lambda sql: {'via': 'native'})
        assert c2._execute_sql('X')['via'] == 'native'


class TestExecuteRest:
    @staticmethod
    def _post(monkeypatch, *, status=200, payload=None, exc=None):
        def fake_post(url, data=None, auth=None, headers=None, timeout=None):
            if exc:
                raise exc
            resp = MagicMock()
            resp.status_code = status
            resp.json.return_value = payload
            return resp
        import timeseries.tdengine_client as mod
        monkeypatch.setattr(mod.requests, 'post', fake_post)

    def test_success(self, monkeypatch):
        self._post(monkeypatch, payload={'code': 0, 'data': [[1]]})
        assert _client()._execute_rest('SELECT 1')['code'] == 0

    def test_application_error_sets_last_error(self, monkeypatch):
        self._post(monkeypatch, payload={'code': 1, 'desc': 'syntax error'})
        c = _client()
        assert c._execute_rest('BAD') is None
        assert c.stats['errors'] == 1 and c.stats['last_error'] == 'syntax error'

    def test_http_error(self, monkeypatch):
        self._post(monkeypatch, status=500, payload={})
        c = _client()
        assert c._execute_rest('X') is None and c.stats['errors'] == 1

    def test_transport_exception(self, monkeypatch):
        self._post(monkeypatch, exc=RuntimeError('connection refused'))
        c = _client()
        assert c._execute_rest('X') is None
        assert c.stats['errors'] == 1 and 'connection refused' in c.stats['last_error']

    def test_sql_is_sent_as_utf8_body(self, monkeypatch):
        seen = {}

        def fake_post(url, data=None, auth=None, headers=None, timeout=None):
            seen['url'], seen['data'], seen['auth'] = url, data, auth
            resp = MagicMock(status_code=200)
            resp.json.return_value = {'code': 0}
            return resp
        import timeseries.tdengine_client as mod
        monkeypatch.setattr(mod.requests, 'post', fake_post)

        c = _client(host='h', port=6041)
        c._execute_rest("SELECT '中文'")
        assert seen['url'] == 'http://h:6041/rest/sql'
        assert seen['data'] == "SELECT '中文'".encode('utf-8')
        assert seen['auth'] == ('root', 'taosdata')


class TestExecuteNative:
    def test_no_connection(self):
        c = _client(use_rest=False)
        c._connection = None
        assert c._execute_native('X') is None

    def test_query_returns_columns_and_rows(self):
        c = _client(use_rest=False)
        cur = MagicMock()
        cur.description = [('ts',), ('value',)]
        cur.fetchall.return_value = [('2026', 1.0)]
        c._connection = MagicMock()
        c._connection.cursor.return_value = cur

        r = c._execute_native('SELECT ...')
        assert r['code'] == 0
        assert r['column_meta'] == [['ts'], ['value']]
        assert r['data'] == [('2026', 1.0)]

    def test_write_returns_rows_affected(self):
        c = _client(use_rest=False)
        cur = MagicMock()
        cur.description = None
        cur.rowcount = 3
        c._connection = MagicMock()
        c._connection.cursor.return_value = cur

        assert c._execute_native('INSERT ...')['rows_affected'] == 3

    def test_exception_sets_last_error(self):
        c = _client(use_rest=False)
        c._connection = MagicMock()
        c._connection.cursor.side_effect = RuntimeError('boom')
        assert c._execute_native('X') is None
        assert c.stats['errors'] == 1 and c.stats['last_error'] == 'boom'


# ---------------------------------------------------------------------------
# 6. 写入 SQL 构造
# ---------------------------------------------------------------------------

def _capture(c):
    """把 _execute_sql 换成记录器，返回被记录的 SQL 列表"""
    seen: list[str] = []

    def fake(sql):
        seen.append(sql)
        return {'code': 0}

    c._execute_sql = fake
    return seen


class TestWriteSql:
    def test_single_write_sql_shape(self):
        c = _client()
        sqls = _capture(c)
        c.write_telemetry(_record())

        insert = [s for s in sqls if s.startswith('INSERT')]
        assert len(insert) == 1
        s = insert[0]
        assert s.startswith('INSERT INTO tel_plc_01_temperature VALUES (')
        assert "'2026-10-13 05:00:00.123'" in s, '时间戳应为毫秒精度字符串'
        assert s.endswith('12.5, 192)')
        assert c.stats['writes'] == 1

    def test_write_failure_does_not_count_write(self):
        c = _client()
        c._execute_sql = lambda sql: None
        c.write_telemetry(_record())
        assert c.stats['writes'] == 0

    def test_injection_in_device_id_is_neutralised(self):
        """device_id 里塞注入串 → 表名必须干净、标签值必须转义"""
        c = _client()
        sqls = _capture(c)
        c.write_telemetry(_record(device_id="a'; DROP TABLE t; --"))

        # 表名必须干净（不含引号/分号/空格）
        insert = [s for s in sqls if s.startswith('INSERT')][0]
        table = insert.split()[2]
        assert table.replace('_', '').isalnum(), f'表名含非法字符: {table}'

        # 建表语句里标签值必须全部转义 —— 没有未转义引号，注入串就只能是数据。
        # 剥掉「已转义引号」后剩下的必须全是**成对的定界引号**（5 个标签 → 10 个）。
        create = [s for s in sqls if s.startswith('CREATE')][0]
        assert create.replace("\\'", '').count("'") == 10, f'存在未转义引号: {create}'

    def test_batch_groups_by_table(self):
        c = _client()
        sqls = _capture(c)
        c.write_telemetry_batch([
            _record(device_id='d1', register_name='r1'),
            _record(device_id='d1', register_name='r1'),
            _record(device_id='d2', register_name='r1'),
        ])
        inserts = [s for s in sqls if s.startswith('INSERT')]
        assert len(inserts) == 2, '应按子表分组，每个子表一条 INSERT'
        big = max(inserts, key=len)
        assert big.count("('") == 2, '同表两条记录应合并进一条 INSERT'
        assert c.stats['writes'] == 3

    def test_empty_batch_does_nothing(self):
        c = _client()
        sqls = _capture(c)
        c.write_telemetry_batch([])
        assert sqls == []

    def test_batch_failure_does_not_count(self):
        c = _client()
        c._execute_sql = lambda sql: None
        c.write_telemetry_batch([_record()])
        assert c.stats['writes'] == 0


class TestEnsureTable:
    def test_creates_once_then_caches(self):
        c = _client()
        sqls = _capture(c)
        c._ensure_table('t1', 'device_telemetry', {'device_id': 'd'})
        c._ensure_table('t1', 'device_telemetry', {'device_id': 'd'})
        assert len([s for s in sqls if s.startswith('CREATE TABLE')]) == 1
        assert 't1' in c._created_tables

    def test_failure_is_not_cached(self):
        """建表失败不得写进缓存 —— 否则那张表**永远不会再被创建**"""
        c = _client()
        c._execute_sql = lambda sql: None
        c._ensure_table('t1', 'device_telemetry', {})
        assert 't1' not in c._created_tables


class TestStats:
    def test_get_stats_returns_copy(self):
        c = _client()
        s = c.get_stats()
        s['writes'] = 999
        assert c.stats['writes'] == 0
