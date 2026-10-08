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











# ===========================================================================
# 3. rate_limit_whitelist —— 一条坏网段不能让后面全部失效
# ===========================================================================







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








# ===========================================================================
# 5. 审计清理打在了不存在的表上（p1-indexes 在落地索引时发现）
# ===========================================================================




