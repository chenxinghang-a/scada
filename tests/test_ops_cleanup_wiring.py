# -*- coding: utf-8 -*-
"""运维清理链路的「静默假成功」回归测试（round 162）。

背景
----
`core/ops_tools.py:696` 的全局单例是 `data_cleaner = DataCleaner()`，
而 `DataCleaner.__init__` 的 `_db_path` 默认 `None` —— 且 `set_db_path()`
**全仓库无任何调用方**。于是：

1. `DataCleaner().clean_history_data(90)` → `sqlite3.connect(None)` 抛
   `TypeError: expected str, bytes or os.PathLike object, not NoneType`；
2. 这个异常被 `except Exception` 收成 `{'status': 'error', ...}`（吞掉）；
3. `展示层/api/api_ops.py` 又把它无条件包进 `success_response()` →
   **HTTP 200 + body 里 `status:'error'`**。

净效果：管理员点「清理历史数据」，接口回 200，前端显示成功，**一行都没删**。
这就是本项目的招牌病「静默假成功」。

原有测试抓不到它，因为 `tests/test_silent_failure_fixes.py:353` 写的是
`DataCleaner(db_path=str(db)).clean_audit_logs(...)` —— **显式传了路径**，
只验证了 SQL 表名对不对，验证不到生产端的路径接线。

修复三处：
- `core/ops_tools.py`：`_require_db_path()` 未配置时给出**可执行**的报错
  （而不是 `NoneType` 这种看不出该干什么的）；
- `run.py`：建库之后接线 `data_cleaner.set_db_path(str(db_path))`；
- `展示层/api/api_ops.py`：`_cleanup_response()` 把 `status:'error'` 变成
  真正的错误响应，调用方**没法**忽略。
"""

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ===========================================================================
# DataCleaner 本身：未接线时必须给出可执行的报错
# ===========================================================================

def test_data_cleaner_without_db_path_gives_actionable_error():
    """未配置库路径时，报错必须**指向解决办法**，不能是 NoneType。

    `expected str, bytes or os.PathLike object, not NoneType` 这种信息
    在日志里看不出该做什么；`请先调用 set_db_path()` 才可执行。
    """
    from core.ops_tools import DataCleaner

    result = DataCleaner().clean_history_data(retention_days=90)

    assert result['status'] == 'error'
    assert 'set_db_path' in result['error'], result['error']
    assert 'NoneType' not in result['error'], (
        f'又退回到不可执行的 NoneType 报错了: {result["error"]}'
    )


def test_data_cleaner_clean_audit_logs_also_fails_loudly():
    """`clean_audit_logs` 走同一条 `_require_db_path()`，也要给可执行报错。"""
    from core.ops_tools import DataCleaner

    result = DataCleaner().clean_audit_logs(retention_days=30)

    assert result['status'] == 'error'
    assert 'set_db_path' in result['error'], result['error']


def test_data_cleaner_with_path_really_deletes(tmp_path):
    """反向验证：接线正确时**真的删了行**。

    没有这条，一个"永远返回 error"的假实现也能让上面两条通过。
    """
    from core.ops_tools import DataCleaner

    db_file = tmp_path / 'scada.db'
    conn = sqlite3.connect(str(db_file))
    conn.execute('CREATE TABLE history_data (timestamp TEXT, value REAL)')
    old = (datetime.now() - timedelta(days=200)).isoformat(sep=' ')
    fresh = datetime.now().isoformat(sep=' ')
    conn.executemany('INSERT INTO history_data VALUES (?, ?)', [(old, 1.0), (fresh, 2.0)])
    conn.commit()
    conn.close()

    cleaner = DataCleaner()
    cleaner.set_db_path(str(db_file))
    result = cleaner.clean_history_data(retention_days=90)

    assert result['status'] == 'success', result
    assert result['deleted_rows'] == 1, result

    conn = sqlite3.connect(str(db_file))
    remaining = conn.execute('SELECT COUNT(*) FROM history_data').fetchone()[0]
    conn.close()
    assert remaining == 1, f'200 天前的记录没被删掉，库里还剩 {remaining} 行'


# ===========================================================================
# 接口层：失败必须回非 200
# ===========================================================================

def test_cleanup_history_returns_error_status_when_cleaner_unconfigured(
        client, auth_headers, monkeypatch):
    """清理失败必须回非 200。

    这是本文件最核心的一条：修复前这里是 **HTTP 200**，
    失败信息只藏在 body 的 `status:'error'` 里，前端看 HTTP 状态就显示"成功"。
    """
    from core.ops_tools import DataCleaner
    from 展示层.api import api_ops

    monkeypatch.setattr(api_ops, 'data_cleaner', DataCleaner())   # 未接线

    resp = client.post('/api/ops/cleanup/history',
                       json={'retention_days': 90}, headers=auth_headers)

    assert resp.status_code >= 400, (
        f'清理失败了却回 HTTP {resp.status_code} —— 又变成静默假成功了'
    )
    body = json.dumps(resp.get_json(), ensure_ascii=False)
    assert 'set_db_path' in body, body


def test_cleanup_history_returns_200_and_really_deletes(
        client, auth_headers, monkeypatch, tmp_path):
    """反向验证：接线正确时必须 200，且**真的删了行**。

    没有这条，"把所有清理接口一律改成 500"这种假实现也能让上面那条通过。
    """
    from core.ops_tools import DataCleaner
    from 展示层.api import api_ops

    db_file = tmp_path / 'scada.db'
    conn = sqlite3.connect(str(db_file))
    conn.execute('CREATE TABLE history_data (timestamp TEXT, value REAL)')
    old = (datetime.now() - timedelta(days=200)).isoformat(sep=' ')
    fresh = datetime.now().isoformat(sep=' ')
    conn.executemany('INSERT INTO history_data VALUES (?, ?)', [(old, 1.0), (fresh, 2.0)])
    conn.commit()
    conn.close()

    cleaner = DataCleaner()
    cleaner.set_db_path(str(db_file))
    monkeypatch.setattr(api_ops, 'data_cleaner', cleaner)

    resp = client.post('/api/ops/cleanup/history',
                       json={'retention_days': 90}, headers=auth_headers)

    assert resp.status_code == 200, resp.get_json()
    payload = resp.get_json()
    assert payload['success'] is True
    assert payload['data']['deleted_rows'] == 1, payload

    conn = sqlite3.connect(str(db_file))
    remaining = conn.execute('SELECT COUNT(*) FROM history_data').fetchone()[0]
    conn.close()
    assert remaining == 1, '接口回了 200 但一行都没删'


@pytest.mark.parametrize('endpoint,payload', [
    ('/api/ops/cleanup/backups', {'keep_count': 5}),
    ('/api/ops/cleanup/logs', {'retention_days': 30}),
])
def test_other_cleanup_endpoints_are_fail_closed(endpoint, payload, client,
                                                 auth_headers, monkeypatch):
    """另外两个清理接口也必须是 fail-closed（别只修了一个）。"""
    from 展示层.api import api_ops

    class ExplodingCleaner:
        def clean_old_backups(self, **_kw):
            raise RuntimeError('模拟：备份目录不可写')

        def clean_log_files(self, **_kw):
            raise RuntimeError('模拟：日志目录不可写')

    monkeypatch.setattr(api_ops, 'data_cleaner', ExplodingCleaner())

    resp = client.post(endpoint, json=payload, headers=auth_headers)

    # 这两条走的是"抛异常 → except → error_response(500)"那条路，
    # 本来就 fail-closed；这里锁死它别被改回 success_response。
    assert resp.status_code >= 400, resp.get_json()


# ===========================================================================
# 接线静态守卫
# ===========================================================================

def test_run_py_wires_data_cleaner_db_path():
    """静态守卫：`run.py` 必须接线 `data_cleaner.set_db_path()`。

    为什么需要这条：**所有测试都显式传 db_path**，所以接线被误删后
    测试套件会全绿，而生产端清理接口又悄悄退回"HTTP 200 + status:'error'"。
    只有盯住源码本身才能防住这种回归。
    """
    run_src = (Path(__file__).resolve().parent.parent / 'run.py').read_text(encoding='utf-8')

    assert 'data_cleaner.set_db_path' in run_src, (
        'run.py 里没有接线 data_cleaner.set_db_path() —— '
        '管理端清理接口会重新变成静默假成功'
    )
    # 必须排在建库之后（否则拿到的是还没建出来的路径）
    assert run_src.index('Database(db_path)') < run_src.index('data_cleaner.set_db_path'), \
        'set_db_path 必须在建库之后调用'
