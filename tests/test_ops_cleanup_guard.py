# -*- coding: utf-8 -*-
"""清理链的入参校验 + 锁库保护（round 248）。

背景
----
round 162/242 修掉了清理链的两类事故：
- round 162：`data_cleaner` 没接线 → 接口 200 + `status:'error'`（静默假成功）；
- round 242：VACUUM/REINDEX 只接线不加闸门 → 「点一下把采集卡住」。

本轮在**真库副本**上实测，发现删除链还有两个没修完的方向：

1. **入参零校验**（手滑一次 = 全表删除）
   - `POST /api/ops/cleanup/history {"retention_days": 0}` → cutoff = 现在
     → `timestamp < now` 命中**全部**历史（实测 201 万行 100% 被删）；
   - `retention_days: -1` → cutoff 在未来 → 同样全删；
   - `keep_count: 0` → `backups[0:]` → 全部备份被删（实测 7/7）。
   定时路径（`core/retention_scheduler.py`）早就把 `days <= 0` 回退成默认值，
   **手动接口却没有任何校验** —— 运维手滑、前端传未初始化的 `undefined`，
   就是一次全表删除。

2. **单条 DELETE 饿死采集**
   实测 473 MB / 201 万行的真库：`clean_history_data(90)` 一条 DELETE
   跑 14.4 秒，**整个事务持有写锁**；并发采集写入等待 32.8 秒后
   `database is locked` 失败（主库 `busy_timeout=30000`）。
   清理一次，采集丢一批数据。

修法
----
- `core/safe_numeric.require_positive_int`：删数据方向上的入参一律
  >= 1 的整数，非法抛 `ValueError`（调用方 bug → 400，不是 500）。
  放在 `safe_numeric` 而不是 `ops_tools`：`存储层/database.py` 的
  保留策略方法也要用同一道校验，而它不能依赖运维工具层。
- `_delete_older_than` / `_delete_older_than_chunked`：分块删除 +
  **每块提交**（块间放锁，采集写入能挤进来）。总耗时基本不变，
  但不再饿死采集。
- API 层：`retention_days` / `keep_count` 非法 → 400 并说清边界；
  `cleanup/history` 加采集运行期闸门（409 + `?force=1`，同 round 242）。
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.ops_tools import DataCleaner, _delete_older_than  # noqa: E402


def _make_db(path, rows):
    """建一个带 history_data 的真 sqlite 库。rows = [(days_ago, value), ...]"""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE history_data (id INTEGER PRIMARY KEY, device_id TEXT,"
        " register_name TEXT, value REAL, unit TEXT, timestamp TEXT)")
    now = datetime.now()
    conn.executemany(
        "INSERT INTO history_data (device_id, register_name, value, unit, timestamp)"
        " VALUES (?,?,?,?,?)",
        [('D', 'r', v, '', (now - timedelta(days=d)).isoformat(sep=' '))
         for d, v in rows],
    )
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def cleaner_db(tmp_path):
    return _make_db(tmp_path / "c.db", [(1, 1.0), (5, 2.0), (100, 3.0), (200, 4.0)])


class _CommitCountingConn:
    """sqlite3.Connection 的代理：数 commit 次数。

    ``sqlite3.Connection`` 是 C 类型，不能给实例挂属性
    （``conn.commit = spy`` 会 AttributeError），所以用代理。
    只转发 `_delete_older_than` 用到的两个方法。
    """

    def __init__(self, conn):
        self._conn = conn
        self.commits = 0

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def commit(self):
        self.commits += 1
        return self._conn.commit()

    def close(self):
        return self._conn.close()


# ===========================================================================
# 1. 入参校验：0 / 负数 / 非整数 一律 ValueError（API 层转 400）
# ===========================================================================

class TestRetentionParamValidation:
    @pytest.mark.parametrize('bad', [0, -1, -90])
    def test_zero_and_negative_days_rejected(self, cleaner_db, bad):
        """0/负数会让 cutoff 落在"现在"或未来 → 命中全部历史。

        修复前 `clean_history_data(0)` 在真库上删掉 2012565/2012565 行。
        """
        with pytest.raises(ValueError, match='retention_days'):
            DataCleaner(cleaner_db).clean_history_data(bad)

    @pytest.mark.parametrize('bad', ['90', True, 1.5, None, [90]])
    def test_non_int_rejected(self, cleaner_db, bad):
        """字符串 / bool / float / None 一律拒。

        `True` 要特别盯：bool 是 int 的子类，`True` 会变成 1 ——
        看起来"合法"，实则把 90 天策略改成 1 天。
        """
        with pytest.raises(ValueError, match='retention_days'):
            DataCleaner(cleaner_db).clean_history_data(bad)

    def test_audit_logs_validated_too(self, cleaner_db):
        with pytest.raises(ValueError):
            DataCleaner(cleaner_db).clean_audit_logs(0)

    def test_log_files_validated_too(self, tmp_path):
        with pytest.raises(ValueError):
            DataCleaner().clean_log_files(log_dir=str(tmp_path), retention_days=0)

    def test_keep_count_zero_rejected(self, tmp_path):
        """keep_count=0 → backups[0:] → 全部备份被删（实测 7/7）。"""
        with pytest.raises(ValueError, match='keep_count'):
            DataCleaner().clean_old_backups(backup_dir=str(tmp_path), keep_count=0)

    def test_keep_count_negative_rejected(self, tmp_path):
        with pytest.raises(ValueError, match='keep_count'):
            DataCleaner().clean_old_backups(backup_dir=str(tmp_path), keep_count=-1)

    def test_valid_params_still_work(self, cleaner_db):
        """反向验证：校验没有把正常路径堵死。"""
        result = DataCleaner(cleaner_db).clean_history_data(30)
        assert result['status'] == 'success', result
        assert result['deleted_rows'] == 2      # 100/200 天的两条
        assert result['retention_days'] == 30

    def test_one_day_is_the_floor(self, tmp_path):
        """1 天是允许的下界（不是 0）—— 删光和不留痕之间选不留痕。"""
        db = _make_db(tmp_path / "floor.db", [(0, 1.0), (100, 2.0), (200, 3.0)])
        result = DataCleaner(db).clean_history_data(1)
        assert result['status'] == 'success'
        assert result['deleted_rows'] == 2      # 100/200 天的两条，当天的留下


# ===========================================================================
# 2. 分块删除：正确性 + 每块提交（放锁）
# ===========================================================================

class TestChunkedDelete:
    def test_deletes_exactly_the_matching_rows(self, tmp_path):
        rows = [(1, 1.0)] * 3 + [(100, 2.0)] * 7
        db = _make_db(tmp_path / "chunk.db", rows)
        conn = sqlite3.connect(db)
        try:
            deleted = _delete_older_than(
                conn, 'history_data', 'timestamp',
                (datetime.now() - timedelta(days=30)).isoformat(sep=' '),
                chunk=2)                       # 小块 → 多轮循环
        finally:
            conn.close()
        assert deleted == 7
        left = sqlite3.connect(db).execute(
            "SELECT COUNT(*) FROM history_data").fetchone()[0]
        assert left == 3

    def test_commits_once_per_chunk(self, tmp_path):
        """每块提交 = 块间放锁，这是「不饿死采集」的机制本身。

        用代理对象数 commit 次数是**确定性**断言（不靠并发时序），
        直接钉住「分批提交」这个行为。

        为什么用代理而不是 `conn.commit = spy`：``sqlite3.Connection``
        是 C 类型，不允许给实例挂新属性（会抛 AttributeError）。
        """
        db = _make_db(tmp_path / "spy.db", [(100, 1.0)] * 5)
        conn = _CommitCountingConn(sqlite3.connect(db))
        try:
            deleted = _delete_older_than(
                conn, 'history_data', 'timestamp',
                (datetime.now() - timedelta(days=30)).isoformat(sep=' '),
                chunk=2)                       # 5 行 / 每块 2 行 → 3 块
        finally:
            conn.close()
        assert deleted == 5
        assert conn.commits == 3, (
            f'commit 次数 {conn.commits} != 3 —— 没有按块提交，'
            f'锁会一直持到删除结束，采集写入照样被饿死')

    def test_empty_result_commits_once_and_returns_zero(self, tmp_path):
        """没有匹配行时也要走完循环（一次 commit + rowcount=0 < chunk → 返回）。"""
        db = _make_db(tmp_path / "empty.db", [(1, 1.0)])
        conn = _CommitCountingConn(sqlite3.connect(db))
        try:
            deleted = _delete_older_than(
                conn, 'history_data', 'timestamp',
                (datetime.now() - timedelta(days=30)).isoformat(sep=' '),
                chunk=100)
        finally:
            conn.close()
        assert deleted == 0
        assert conn.commits == 1

    def test_lock_is_released_between_chunks(self, tmp_path):
        """块间提交后，另一个连接**立刻**能拿到写锁（不等删除结束）。

        做法是确定性的：不依赖时序 —— 在每个 commit 点上当场从另一个
        连接尝试写入。块间不放锁的话，这些尝试会全部失败。

        （刻意没有写「边删边插」的时序版测试：那类用例在 Windows +
         Defender 扫描临时目录时会偶发，宁可要确定性的弱断言。）
        """
        db = _make_db(tmp_path / "lock.db", [(100, 1.0)] * 6)
        inner = sqlite3.connect(db)
        probe_failures = []

        class _ProbingConn(_CommitCountingConn):
            def commit(self):
                result = super().commit()
                # commit 已放锁：此时另一个连接必须能立刻写入。
                # 探针行用**未来**时间戳 —— 否则它自己匹配删除条件，
                # 会被后续块删掉，把 deleted 计数搅乱。
                other = sqlite3.connect(db, timeout=0.2)
                try:
                    other.execute(
                        "INSERT INTO history_data (device_id, register_name,"
                        " value, unit, timestamp) VALUES ('LIVE','r',9.0,'','2099-01-01')")
                    other.commit()
                except Exception as e:      # noqa: BLE001
                    probe_failures.append(e)
                finally:
                    other.close()
                return result

        conn = _ProbingConn(inner)
        try:
            deleted = _delete_older_than(
                conn, 'history_data', 'timestamp',
                (datetime.now() - timedelta(days=30)).isoformat(sep=' '),
                chunk=2)                       # 6 行 → 3 个 commit 点
        finally:
            conn.close()

        assert deleted == 6, deleted
        assert not probe_failures, (
            f'commit 之后别的连接仍写不进去: {probe_failures[:1]}')


# ===========================================================================
# 3. Database 保留策略方法：同样要有下限（公开 API，不只靠上游好心）
# ===========================================================================

class TestDatabaseRetentionValidation:
    @pytest.fixture
    def db(self, tmp_path):
        from 存储层.database import Database
        database = Database(str(tmp_path / "r.db"))
        yield database
        database.close()

    @pytest.mark.parametrize('bad', [0, -1])
    def test_enforce_retention_policy_rejects(self, db, bad):
        with pytest.raises(ValueError):
            db.enforce_retention_policy(history_days=bad, alarm_days=90)

    def test_enforce_archive_retention_rejects_zero(self, db):
        """days=0 会把整个归档表删光 —— 那是原始采样删后唯一剩的东西。"""
        with pytest.raises(ValueError):
            db.enforce_archive_retention(days=0)

    def test_enforce_retention_policy_still_works(self, db):
        """反向验证：正常调用照旧，且分块删除结果正确。

        注意断言对象是 **history_data**（本方法清的就是它）——
        `get_latest_data` 读的是 realtime_data（当前值缓存），
        拿它断言会永远为空，那是另一张表。
        """
        from datetime import datetime as _dt
        now = _dt.now()
        with db.get_connection() as conn:
            conn.executemany(
                "INSERT INTO history_data (device_id, register_name, value, unit, timestamp)"
                " VALUES (?,?,?,?,?)",
                [('D', 'r', 1.0, '', (now - timedelta(days=d)).isoformat(sep=' '))
                 for d in (1, 100)])

        result = db.enforce_retention_policy(history_days=30, alarm_days=90)
        assert result['history_deleted'] == 1

        left = db.get_connection().__enter__().execute(
            "SELECT COUNT(*) FROM history_data").fetchone()[0]
        assert left == 1, '分块删除把新数据也删了'


# ===========================================================================
# 4. API 层：400 / 409 / force
# ===========================================================================

class TestCleanupApiValidation:
    @pytest.mark.parametrize('payload', [
        {'retention_days': 0},
        {'retention_days': -1},
        {'retention_days': '90'},
        {'retention_days': None},
        {'retention_days': 1.5},
    ])
    def test_history_rejects_bad_days_with_400(self, client, auth_headers, payload):
        """修复前这些入参直接进 SQL，0/负数是全表删除。"""
        resp = client.post('/api/ops/cleanup/history',
                           json=payload, headers=auth_headers)
        assert resp.status_code == 400, (
            f'入参 {payload} 回了 {resp.status_code} —— '
            f'0/负数会删光全部历史，必须是 400')
        assert 'retention_days' in json.dumps(resp.get_json(), ensure_ascii=False)

    def test_backups_rejects_keep_count_zero(self, client, auth_headers):
        resp = client.post('/api/ops/cleanup/backups',
                           json={'keep_count': 0}, headers=auth_headers)
        assert resp.status_code == 400
        assert 'keep_count' in json.dumps(resp.get_json(), ensure_ascii=False)

    def test_logs_rejects_zero_days(self, client, auth_headers):
        resp = client.post('/api/ops/cleanup/logs',
                           json={'retention_days': 0}, headers=auth_headers)
        assert resp.status_code == 400


class TestCleanupHistoryGuard:
    @pytest.fixture
    def wired_cleaner(self, tmp_path, monkeypatch):
        """把全局单例接到临时库（模拟 run.py 接线后的状态）"""
        from 展示层.api import api_ops
        db = _make_db(tmp_path / "guard.db", [(100, 1.0)] * 10)
        cleaner = DataCleaner(db)
        monkeypatch.setattr(api_ops, 'data_cleaner', cleaner)
        return cleaner

    def test_blocked_while_collecting(self, app, client, auth_headers, wired_cleaner):
        app.data_collector.running = True
        resp = client.post('/api/ops/cleanup/history',
                           json={'retention_days': 90}, headers=auth_headers)
        assert resp.status_code == 409, (
            f'采集运行中也允许跑长事务删除？回了 {resp.status_code}')

    def test_force_bypasses_guard(self, app, client, auth_headers, wired_cleaner):
        app.data_collector.running = True
        resp = client.post('/api/ops/cleanup/history?force=1',
                           json={'retention_days': 90}, headers=auth_headers)
        assert resp.status_code == 200, resp.get_json()
        assert resp.get_json()['data']['deleted_rows'] == 10

    def test_allowed_when_collector_idle(self, app, client, auth_headers, wired_cleaner):
        app.data_collector.running = False
        resp = client.post('/api/ops/cleanup/history',
                           json={'retention_days': 90}, headers=auth_headers)
        assert resp.status_code == 200, resp.get_json()

    def test_empty_force_is_not_force(self, app, client, auth_headers, wired_cleaner):
        """`?force=` 空串不算强制（老坑：args.get(k, default) 在 ?k= 时返回空串）。"""
        app.data_collector.running = True
        resp = client.post('/api/ops/cleanup/history?force=',
                           json={'retention_days': 90}, headers=auth_headers)
        assert resp.status_code == 409

    def test_rejection_is_audited(self, app, client, auth_headers, wired_cleaner):
        """被拒也要留审计 —— 事后能查到「谁在采集运行时点了清理」。

        审计日志是**进程内全局**单例，跨测试累积，所以必须取
        before/after 差集，不能只看「有没有一条」。
        """
        app.data_collector.running = True
        before = api_ops_audit_recent()
        client.post('/api/ops/cleanup/history',
                    json={'retention_days': 90}, headers=auth_headers)
        new = [r for r in api_ops_audit_recent() if r not in before]
        assert any(r.get('operation') == 'cleanup_history'
                   and r.get('result') == 'rejected' for r in new), \
            f'被拒的清理没有写审计（或 result 不是 rejected）: {new}'


def api_ops_audit_recent():
    from core.ops_tools import ops_audit
    return ops_audit.get_recent(limit=50)
