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
