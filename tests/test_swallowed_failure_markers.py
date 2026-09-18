# -*- coding: utf-8 -*-
"""把「失败」伪装成「正常值」的两处（round 168f）。

共同病征：except 分支不报错、不记日志，而是返回一个**看起来正常的值**，
让读报告的人以为一切正常。

1. `存储层/data_lifecycle.py` 的 `generate_report()`：
   单表查询失败 → `{'count': 0}`。
   既把「查不动」显示成「这张表是空的」，又和正常分支**结构不一致**
   （正常有 earliest/latest，失败没有）—— 按正常结构读报告就 KeyError。

2. `core/ops_tools.py` 的 `DiagnosticExporter._collect_config()`：
   YAML 读取失败 → 值变成字符串 `'(读取失败)'`，**不带原因也不记日志**。
   诊断包本身就是排障用的，「读取失败」四个字等于没信息。
   同文件其他收集段都记了 logger.warning，只有这里漏了。
"""

import logging
import sqlite3

import pytest


# ---------------------------------------------------------------- 生命周期报告

def test_lifecycle_report_failed_lookup_is_marked(tmp_path):
    """查不到的表必须标明失败，且结构要与正常分支一致。"""
    from 存储层.data_lifecycle import DataLifecycleManager

    db = tmp_path / 'empty.db'
    sqlite3.connect(str(db)).close()          # 空库：三张表都不存在

    mgr = DataLifecycleManager(str(db))
    report = mgr.generate_report()
    stats = report['table_stats']

    assert set(stats) == {'history_data', 'alarm_records', 'audit_log'}, \
        f'应覆盖全部策略表，实际 {set(stats)}'

    for name, info in stats.items():
        assert info['count'] is None, \
            f'{name} 查询失败却报了数字 {info["count"]} —— 看起来像"这张表是空的"'
        assert info.get('error'), f'{name} 没带失败原因'
        # 结构一致性：正常分支有的键，失败分支也必须有
        assert 'earliest' in info and 'latest' in info, \
            f'{name} 失败分支结构不完整，按正常结构读会 KeyError: {info}'


def test_lifecycle_report_normal_path_unchanged(tmp_path):
    """正常路径不受影响：真实表要能拿到真实行数。"""
    from 存储层.data_lifecycle import DataLifecycleManager

    db = tmp_path / 'real.db'
    conn = sqlite3.connect(str(db))
    conn.execute('CREATE TABLE history_data (id INTEGER, timestamp TEXT)')
    conn.executemany('INSERT INTO history_data VALUES (?, ?)',
                     [(1, '2026-01-01 00:00:00'), (2, '2026-01-02 00:00:00')])
    conn.commit()
    conn.close()

    mgr = DataLifecycleManager(str(db))
    stats = mgr.generate_report()['table_stats']

    assert stats['history_data']['count'] == 2
    assert 'error' not in stats['history_data']


# ---------------------------------------------------------------- 诊断包配置

def test_config_read_failure_carries_reason(tmp_path, monkeypatch, caplog):
    """YAML 读取失败必须带原因 + 落日志，不能只给一句「读取失败」。"""
    import yaml
    from core.ops_tools import DiagnosticExporter

    def boom(*args, **kwargs):
        raise yaml.YAMLError('第 3 行缩进错误')

    monkeypatch.setattr(yaml, 'safe_load', boom)

    exporter = DiagnosticExporter(output_dir=str(tmp_path))
    with caplog.at_level(logging.WARNING):
        cfg = exporter._collect_config()

    failed = {k: v for k, v in cfg.items()
              if isinstance(v, str) and v.startswith('(读取失败')}
    assert failed, f'没有捕获到读取失败的配置项，实际键: {sorted(cfg)[:8]}'
    for k, v in failed.items():
        assert '第 3 行缩进错误' in v, f'{k} 没带原因: {v}'
    assert any('读取配置' in rec.message for rec in caplog.records), \
        '读取失败没有落日志'


def test_config_read_success_unchanged(tmp_path, monkeypatch):
    """正常路径不受影响：能读出真实的 YAML 内容。"""
    from core.ops_tools import DiagnosticExporter

    exporter = DiagnosticExporter(output_dir=str(tmp_path))
    cfg = exporter._collect_config()

    assert 'devices.yaml' in cfg, f'没读到 devices.yaml，实际: {sorted(cfg)[:8]}'
    assert isinstance(cfg['devices.yaml'], (dict, list))
