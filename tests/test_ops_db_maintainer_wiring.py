"""
数据库维护端点接线 + 运行期闸门（round 242）

背景
----
`/api/ops/db/{vacuum,reindex,analyze,tables,integrity}` 这五个端点在生产环境
**从未成功执行过一次**：

`core/ops_tools.py` 的全局单例 `db_maintainer = DatabaseMaintainer()` 默认
`_db_path = None`，而 `set_db_path()` **全仓库零调用方**。
`run.py` 在 round 162 给 `data_cleaner` 接上了路径，**漏了同一个文件里的
`db_maintainer`**。

于是：
- `vacuum()` 先撞 `os.path.getsize(None)` → TypeError；
- `reindex()` / `analyze()` / `tables` / `integrity` 撞
  `_get_conn()` 的 `ValueError("数据库路径未设置")`；
- 全部被 `except Exception` 收成 `{'status': 'error'}` → `_ops_status_response`
  转 500。运维点「VACUUM」永远看到 500，而且错误信息是
  `stat: path should be string...` 这种，完全指不到根因。

本文件同时锁死**修好之后的另一半**：VACUUM / REINDEX / ANALYZE 在 SQLite 上
要拿独占锁，采集运行期间执行会让批量写入一路等到 `busy_timeout`（30 秒）
超时。修好「永久失败」不加闸门，等于把事故方向从「点了没用」换成
「点一下把采集卡住」。
"""

import os
import sqlite3
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.ops_tools import DatabaseMaintainer  # noqa: E402


@pytest.fixture
def tiny_db(tmp_path):
    """一个真 sqlite 小库（能让 VACUUM/REINDEX/ANALYZE 真跑）"""
    path = tmp_path / "tiny.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"row{i}",) for i in range(50)])
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def wired_maintainer(tiny_db, monkeypatch):
    """把全局单例接到临时库上（模拟 run.py 接线后的状态），用完还原"""
    import 展示层.api.api_ops as api_ops

    saved = api_ops.db_maintainer._db_path
    api_ops.db_maintainer.set_db_path(tiny_db)
    yield api_ops.db_maintainer
    api_ops.db_maintainer.set_db_path(saved)


# ============================================================
# 1. 根因：没接路径时整个功能区是死的
# ============================================================

class TestUnwiredIsDead:
    def test_vacuum_without_path_reports_error(self):
        """未接线时必须明确失败 —— 这是修复前生产环境的真实状态。"""
        result = DatabaseMaintainer().vacuum()
        assert result['status'] == 'error'

    def test_reindex_without_path_reports_error(self):
        result = DatabaseMaintainer().reindex()
        assert result['status'] == 'error'

    def test_analyze_without_path_reports_error(self):
        result = DatabaseMaintainer().analyze()
        assert result['status'] == 'error'

    def test_error_message_is_not_the_real_cause(self):
        """修复前的报错信息指不到根因（getsize(None) 的 TypeError）。

        这条不是要守住这个烂消息，而是记录「为什么当年没人发现」：
        运维看到的是 `stat: path should be string...`，不会想到
        「全局单例没 set_db_path」。
        """
        msg = DatabaseMaintainer().vacuum().get('error', '')
        assert 'path' in msg.lower() or 'None' in msg


# ============================================================
# 2. 接线后必须真的能跑
# ============================================================

class TestWiredWorks:
    def test_vacuum_succeeds(self, wired_maintainer):
        result = wired_maintainer.vacuum()
        assert result['status'] == 'success', result
        assert result['operation'] == 'VACUUM'

    def test_reindex_succeeds(self, wired_maintainer):
        result = wired_maintainer.reindex()
        assert result['status'] == 'success', result

    def test_analyze_succeeds(self, wired_maintainer):
        result = wired_maintainer.analyze()
        assert result['status'] == 'success', result

    def test_integrity_check_succeeds(self, wired_maintainer):
        result = wired_maintainer.integrity_check()
        assert result.get('status') != 'error', result


def test_run_py_wires_db_maintainer():
    """静态守卫：`run.py` 必须给 `db_maintainer` 设路径。

    修复前这里只有 `data_cleaner.set_db_path(...)` —— 同一个 import 块、
    同一批全局单例，漏了一个就整片功能区不可用。
    """
    import pathlib
    import re

    source = (pathlib.Path(PROJECT_ROOT) / 'run.py').read_text(encoding='utf-8')
    assert re.search(r'^\s+db_maintainer\.set_db_path\(', source, flags=re.MULTILINE), \
        'run.py 没有给 db_maintainer 接线 —— /api/ops/db/* 会全部失败'


# ============================================================
# 3. 运行期闸门：采集跑着的时候默认不许锁库
# ============================================================

def _set_running(app, value):
    app.data_collector.running = value


class TestMaintenanceGuard:
    def test_vacuum_blocked_while_collecting(self, app, client, auth_headers, wired_maintainer):
        _set_running(app, True)
        try:
            r = client.post('/api/ops/db/vacuum', headers=auth_headers)
        finally:
            _set_running(app, False)

        assert r.status_code == 409, f'期望 409，实际 {r.status_code}: {r.get_json()}'
        body = r.get_json()
        assert '独占' in (body.get('error') or body.get('message') or '')

    def test_reindex_blocked_while_collecting(self, app, client, auth_headers, wired_maintainer):
        _set_running(app, True)
        try:
            r = client.post('/api/ops/db/reindex', headers=auth_headers, json={})
        finally:
            _set_running(app, False)
        assert r.status_code == 409

    def test_analyze_blocked_while_collecting(self, app, client, auth_headers, wired_maintainer):
        _set_running(app, True)
        try:
            r = client.post('/api/ops/db/analyze', headers=auth_headers)
        finally:
            _set_running(app, False)
        assert r.status_code == 409

    def test_allowed_when_collector_idle(self, app, client, auth_headers, wired_maintainer):
        _set_running(app, False)
        r = client.post('/api/ops/db/vacuum', headers=auth_headers)
        assert r.status_code == 200, f'采集未运行却被拦：{r.get_json()}'

    def test_force_bypasses_guard(self, app, client, auth_headers, wired_maintainer):
        _set_running(app, True)
        try:
            r = client.post('/api/ops/db/vacuum?force=1', headers=auth_headers)
        finally:
            _set_running(app, False)
        assert r.status_code == 200, f'force=1 应放行：{r.get_json()}'

    def test_force_accepts_common_truthy_spellings(self, app, client, auth_headers, wired_maintainer):
        for raw in ('1', 'true', 'TRUE', 'yes'):
            _set_running(app, True)
            try:
                r = client.post(f'/api/ops/db/vacuum?force={raw}', headers=auth_headers)
            finally:
                _set_running(app, False)
            assert r.status_code == 200, f'force={raw} 没放行'

    def test_empty_force_value_does_not_bypass(self, app, client, auth_headers, wired_maintainer):
        """`?force=`（空串）不能当成强制 —— `request.args.get(k, default)`
        在 `?k=` 时返回的是空串而不是默认值，这是本仓库踩过的坑。"""
        _set_running(app, True)
        try:
            r = client.post('/api/ops/db/vacuum?force=', headers=auth_headers)
        finally:
            _set_running(app, False)
        assert r.status_code == 409, '空 force 被当成强制了'

    def test_rejection_is_audited(self, app, client, auth_headers, wired_maintainer, monkeypatch):
        import 展示层.api.api_ops as api_ops

        seen = {}
        real = api_ops.ops_audit.log_operation

        def spy(operation, **kw):
            seen['operation'] = operation
            seen.update(kw)
            return real(operation, **kw)

        monkeypatch.setattr(api_ops.ops_audit, 'log_operation', spy)

        _set_running(app, True)
        try:
            client.post('/api/ops/db/vacuum', headers=auth_headers)
        finally:
            _set_running(app, False)

        assert seen.get('operation') == 'db_vacuum'
        assert seen.get('result') == 'rejected', '被闸门拒绝也要留痕'


def test_all_three_endpoints_use_the_guard():
    """静态守卫：三个会锁库的端点都必须调 `_maintenance_guard`。

    带 `^\\s+` 行首锚点 —— 不带的话会命中函数**定义**处，把调用删掉
    照样匹配（round 240 踩过这个坑）。
    """
    import pathlib
    import re

    source = (pathlib.Path(PROJECT_ROOT) / '展示层' / 'api' / 'api_ops.py').read_text(encoding='utf-8')
    calls = re.findall(r'^\s+blocked = _maintenance_guard\(', source, flags=re.MULTILINE)
    assert len(calls) == 3, f'只有 {len(calls)} 个端点接了闸门，应为 3（vacuum/reindex/analyze）'
