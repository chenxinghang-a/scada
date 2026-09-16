# -*- coding: utf-8 -*-
"""静默失效修复回归（round 160）。

本文件覆盖 P1-4（消除静默吞异常）过程中从 `core/` 里挖出的 4 个**真 bug**。
它们的共同点不是"异常被吞掉"本身，而是吞掉之后**语义反了**：系统不报错，
只是给出错误 / 陈旧 / 不完整的结果 —— 也就是本项目最危险的"静默假成功"。

1. `core/chaos_engineering.py` `_check_no_critical_alarms` —— fail-open，
   检查跑不起来反而报告"无严重报警"，混沌实验据此误判稳态通过。
2. `core/etag_support.py` `etag_required` —— 非 JSON 响应拿到**同一个常量 ETag**，
   客户端把该值带到别的接口上会命中 304 拿到空 body → 用陈旧内容。
3. `core/rate_limit_whitelist.py` `_is_ip_whitelisted` —— `try` 包在 `for` 外面，
   一条坏网段让排在它后面的**所有合法网段静默失效**。
4. `core/data_compressor.py` `restore_archive` —— 跳过行静默减少 `rows_restored`，
   函数仍返回 success，运维看不出"恢复不完整"。

每条测试都做过变异验证：把修复还原回去，对应测试必须变红。
"""

import gzip
import json
import os
import sqlite3
import sys

import pytest
from flask import Flask, jsonify, make_response

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# ===========================================================================
# 1. chaos_engineering —— 稳态检查必须 fail-safe
# ===========================================================================

class _FakeAlarmManager:
    """最小报警管理器替身：只需要 `get_active_alarms()`。"""

    def __init__(self, alarms):
        self._alarms = alarms

    def get_active_alarms(self):
        return self._alarms


@pytest.fixture
def clean_registry():
    """每个用例前后清空 ModuleRegistry —— 它是类级单例，会跨用例串味。"""
    from core.module_registry import ModuleRegistry
    ModuleRegistry.clear()
    yield ModuleRegistry
    ModuleRegistry.clear()


def _critical(n):
    return [{'alarm_level': 'critical'} for _ in range(n)]


def test_no_critical_alarms_fails_safe_when_check_cannot_run(clean_registry):
    """检查压根跑不起来时必须返回 False（非稳态），不能 fail-open 成 True。

    修复前 `except` 之后是 `return True`。而 `module_registry` 在生产环境
    从未注册任何模块，`get_instance('alarm_manager')` **每次都抛异常** ——
    于是这个检查实际上是一个恒真的常量，混沌实验永远认为稳态成立。
    """
    from core.chaos_engineering import ChaosEngine

    engine = ChaosEngine()
    # 前置确认：alarm_manager 确实没注册，走的确实是异常路径
    with pytest.raises(KeyError):
        clean_registry.get_instance('alarm_manager')

    assert engine._check_no_critical_alarms() is False


def test_no_critical_alarms_true_when_few_critical(clean_registry):
    """正常路径不受影响：少于 5 个严重报警 = 稳态成立。"""
    from core.chaos_engineering import ChaosEngine

    clean_registry.register_instance('alarm_manager', _FakeAlarmManager(_critical(4)))
    engine = ChaosEngine()

    assert engine._check_no_critical_alarms() is True


def test_no_critical_alarms_false_when_many_critical(clean_registry):
    """正常路径不受影响：达到 5 个严重报警 = 非稳态。"""
    from core.chaos_engineering import ChaosEngine

    clean_registry.register_instance('alarm_manager', _FakeAlarmManager(_critical(5)))
    engine = ChaosEngine()

    assert engine._check_no_critical_alarms() is False


def test_chaos_check_semantics_match_sibling_checks(clean_registry):
    """与同文件的兄弟检查保持一致的 fail-safe 语义。

    `_check_api_responsive` / `_check_database_accessible` 等异常路径都返回 False，
    只有 `_check_no_critical_alarms` 反了 —— 这是明显的笔误而不是有意设计。
    """
    from core.chaos_engineering import ChaosEngine

    engine = ChaosEngine()
    # 未注册任何模块 → 数据库检查同样取不到实例，必须也是 False
    assert engine._check_database_accessible() is False


# ===========================================================================
# 2. etag_support —— 非 JSON 响应不能拿到常量 ETag
# ===========================================================================

def _etag_app():
    from core.etag_support import etag_required

    app = Flask(__name__)

    @app.route('/plain-a')
    @etag_required
    def plain_a():
        return make_response('alpha')

    @app.route('/plain-b')
    @etag_required
    def plain_b():
        return make_response('beta')

    @app.route('/json')
    @etag_required
    def json_ep():
        return jsonify({'v': 1})

    return app


def test_generate_etag_of_none_is_a_constant():
    """钉住危险常量本身 —— 说明为什么必须跳过，而不是"顺手"改成别的哈希。

    `generate_etag(None)` = sha256(str(None)) = `dc937b59892604f5`。任何"空 payload"
    都映射到这一个值。若将来有人改了 `generate_etag`，这条测试会提醒他：
    真正的问题不是这个常量长什么样，而是**空 payload 不该有 ETag**。
    """
    from core.etag_support import generate_etag

    assert generate_etag(None) == generate_etag(None) == 'dc937b59892604f5'


def test_non_json_response_gets_no_etag():
    app = _etag_app()
    client = app.test_client()

    for path in ('/plain-a', '/plain-b'):
        resp = client.get(path)
        assert resp.status_code == 200
        assert 'ETag' not in resp.headers, (
            f"{path} 是非 JSON 响应，不该带 ETag —— 修复前它会拿到 sha256(None) 这个常量"
        )


def test_non_json_conditional_request_cannot_yield_stale_304():
    """端到端复现修复前的"静默陈旧内容"。

    客户端从 `/plain-a` 拿到常量 ETag，带到 `/plain-b` 上 → 服务端认为"没变"
    → 返回 304 空 body → 浏览器把 `/plain-a` 的内容当成 `/plain-b` 的结果。
    修复后 `/plain-a` 不带 ETag，这个链路不可能成立。
    """
    app = _etag_app()
    client = app.test_client()

    first = client.get('/plain-a')
    leaked_etag = first.headers.get('ETag') or 'dc937b59892604f5'

    second = client.get('/plain-b', headers={'If-None-Match': leaked_etag})
    assert second.status_code == 200, "非 JSON 接口不该被跨接口 ETag 骗出 304"
    assert second.get_data(as_text=True) == 'beta'


def test_json_response_still_supports_conditional_304():
    """反向验证：JSON 接口的条件请求能力不能被这次修复弄坏。"""
    app = _etag_app()
    client = app.test_client()

    first = client.get('/json')
    etag = first.headers.get('ETag')
    assert etag, "JSON 响应必须仍然带 ETag"

    second = client.get('/json', headers={'If-None-Match': etag})
    assert second.status_code == 304


# ===========================================================================
# 3. rate_limit_whitelist —— 一条坏网段不能让后面全部失效
# ===========================================================================

def test_one_malformed_network_does_not_disable_later_valid_networks():
    """修复前 `try` 包在 `for` 外面：`ip_network()` 对坏网段抛 ValueError 会
    **直接跳出整个 for 循环**，排在它后面的合法网段全部静默失效。

    注意 `add_network()` 会校验，坏网段进不来；但配置加载路径
    （`load_config` / `_whitelisted_networks = config['networks']`）不做校验，
    所以这条路径是真实可达的。
    """
    from core.rate_limit_whitelist import RateLimitWhitelist

    wl = RateLimitWhitelist()
    wl._whitelisted_ips = set()
    wl._whitelisted_networks = ['999.999.999.999/24', '10.0.0.0/8']

    assert wl._is_ip_whitelisted('10.1.2.3') is True, (
        "坏网段排在最前面，把后面的 10.0.0.0/8 一起废掉了"
    )


def test_whitelist_still_denies_outside_ip_and_malformed_request_ip():
    """反向验证：修复不能把 fail-closed 变成 fail-open。"""
    from core.rate_limit_whitelist import RateLimitWhitelist

    wl = RateLimitWhitelist()
    wl._whitelisted_ips = set()
    wl._whitelisted_networks = ['999.999.999.999/24', '10.0.0.0/8']

    assert wl._is_ip_whitelisted('192.168.5.5') is False, "网段外的 IP 必须照常限流"
    assert wl._is_ip_whitelisted('not-an-ip') is False, "非法请求 IP 必须照常限流"


def test_multiple_malformed_networks_do_not_break_valid_ones():
    """多条坏网段穿插在合法网段之间 —— 每一条都只跳过自己。"""
    from core.rate_limit_whitelist import RateLimitWhitelist

    wl = RateLimitWhitelist()
    wl._whitelisted_ips = set()
    wl._whitelisted_networks = [
        'bad-1',
        '10.0.0.0/8',
        'bad-2',
        '172.16.0.0/12',
        'bad-3',
    ]

    assert wl._is_ip_whitelisted('10.9.9.9') is True
    assert wl._is_ip_whitelisted('172.20.1.1') is True
    assert wl._is_ip_whitelisted('8.8.8.8') is False


# ===========================================================================
# 4. data_compressor —— 恢复不完整必须可见
# ===========================================================================

def _write_archive(tmp_path, rows, table='history'):
    """按 `archive_table` 的格式造一个归档：头部 `-- Table: <name>` + 每行一条 JSON。"""
    path = tmp_path / 'arch.gz'
    with gzip.open(path, 'wt', encoding='utf-8') as gz:
        gz.write(f'-- Table: {table}\n')
        for row in rows:
            gz.write(json.dumps(row) + '\n')
    return path


def _make_db(tmp_path):
    db = tmp_path / 'scada.db'
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE history (id INTEGER PRIMARY KEY, ts TEXT, v REAL)')
    conn.commit()
    conn.close()
    return db


def test_restore_reports_skipped_rows_instead_of_silently_under_counting(tmp_path):
    """有一行写不进去时，必须显式告诉调用方"跳了几行、为什么跳"。

    修复前：只 `logger.debug` 一行、`rows_restored` 静默偏少，函数照样返回
    success —— 运维看到"恢复完成"就以为数据齐了。
    """
    from core.data_compressor import DataCompressor

    db = _make_db(tmp_path)
    rows = [
        {'id': 1, 'ts': '2026-01-01T00:00:00', 'v': 1.0},
        {'id': 2, 'ts': '2026-01-02T00:00:00', 'v': 2.0},
        # 这一行带了一个表里不存在的列 → sqlite3.OperationalError
        {'id': 3, 'ts': '2026-01-03T00:00:00', 'v': 3.0, 'no_such_column': 9},
    ]
    archive = _write_archive(tmp_path, rows)

    compressor = DataCompressor(str(db), str(tmp_path / 'archives'))
    result = compressor.restore_archive(str(archive))

    assert result['rows_restored'] == 2
    assert result['rows_skipped'] == 1, "跳过的行必须被计数，不能静默吞掉"
    assert result['skip_reasons'], "至少要说明跳过原因"
    assert 'no_such_column' in next(iter(result['skip_reasons']))


def test_restore_reports_zero_skipped_on_clean_archive(tmp_path):
    """反向验证：全部写得进去时 `rows_skipped` 必须是 0，不能虚报。"""
    from core.data_compressor import DataCompressor

    db = _make_db(tmp_path)
    rows = [
        {'id': 1, 'ts': '2026-01-01T00:00:00', 'v': 1.0},
        {'id': 2, 'ts': '2026-01-02T00:00:00', 'v': 2.0},
    ]
    archive = _write_archive(tmp_path, rows)

    compressor = DataCompressor(str(db), str(tmp_path / 'archives'))
    result = compressor.restore_archive(str(archive))

    assert result['rows_restored'] == 2
    assert result['rows_skipped'] == 0
    assert result['skip_reasons'] == {}


def test_restore_result_is_still_a_superset_of_the_old_shape(tmp_path):
    """兼容性：老字段一个都不能少（调用方可能直接取 `rows_restored`）。"""
    from core.data_compressor import DataCompressor

    db = _make_db(tmp_path)
    archive = _write_archive(tmp_path, [{'id': 1, 'ts': '2026-01-01T00:00:00', 'v': 1.0}])

    compressor = DataCompressor(str(db), str(tmp_path / 'archives'))
    result = compressor.restore_archive(str(archive))

    for key in ('table', 'rows_restored', 'archive_file'):
        assert key in result, f"原有字段 {key} 不能丢"
    assert result['table'] == 'history'


# ===========================================================================
# 5. 审计清理打在了不存在的表上（p1-indexes 在落地索引时发现）
# ===========================================================================

def test_clean_audit_logs_targets_the_real_table_name(tmp_path):
    """审计清理必须打在真表 `audit_log` 上，而不是凭空写出来的 `audit_logs`。

    真表由 `用户层/audit_logger.py::AuditLogger._init_db()` 创建，名字是**单数**
    `audit_log`（还有 `idx_audit_timestamp` 等索引为证）。而
    `core/ops_tools.py::DataCleaner.clean_audit_logs` 里写的是 `audit_logs`
    → 每次 `sqlite3.OperationalError: no such table: audit_logs`
    → 返回 `status: 'error'`。也就是说这条运维清理**从来没有真正执行过**。

    这条测试不 mock、不建自己的表 —— 直接用 `AuditLogger` 建真库再清理，
    表名一旦对不上就会红。
    """
    from 用户层.audit_logger import AuditLogger
    from core.ops_tools import DataCleaner

    db = tmp_path / 'audit.db'
    audit = AuditLogger(db_path=str(db))
    audit.log_operation(user='admin', action='login', target='system', result='success')

    result = DataCleaner(db_path=str(db)).clean_audit_logs(retention_days=30)

    assert result['status'] == 'success', (
        f"审计日志清理打在了错的表上（应为 audit_log）: {result}"
    )
    assert 'deleted_rows' in result


def test_audit_table_is_flagged_as_sensitive(tmp_path):
    """`DataAccessAuditor.SENSITIVE_TABLES` 里也必须写对表名。

    写错的话，对审计日志本身的访问不会被计入敏感访问统计 —— 审计系统
    **漏掉对审计系统的访问**，这本身就很讽刺。
    """
    from core.data_access_audit import DataAccessAuditor

    assert 'audit_log' in DataAccessAuditor.SENSITIVE_TABLES
    assert 'audit_logs' not in DataAccessAuditor.SENSITIVE_TABLES

