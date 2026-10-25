# -*- coding: utf-8 -*-
"""运维清单里提到的字段必须**真的存在**（round 278）。

为什么要有这条
--------------
`SCADA-深度审计` 第十一节是给交接用的运维自检清单，里面写了一批接口字段名
（`queue_pending_count` / `output.broken_channels` / `device_freshness` …）。

**文档里的字段名最容易腐烂**：代码改个名，文档照旧写着旧名字，接手的人
照着查，得到的是"这个字段不存在"，然后开始怀疑整个文档。

所以这里把清单里**关键的 8 个名字**钉死：任何一个被改名/删除，这条就红，
逼着改代码的人同时更新文档（或反过来）。

⚠️ 这不是"文档全文校验"（那太脆），只是把**最容易被引用错**的那几个名字
列成显式清单 —— 清单短、改起来明确，才有人愿意维护。
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOC = PROJECT_ROOT / 'SCADA-深度审计-20261025.md'


def _doc_text() -> str:
    return DOC.read_text(encoding='utf-8')


def _source(*parts: str) -> str:
    return (PROJECT_ROOT.joinpath(*parts)).read_text(encoding='utf-8')


# ---------------------------------------------------------------------------
# 1. 健康检查项名（文档第 2–4 条）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('check_name', [
    'data_freshness',
    'device_freshness',
    'backup_readiness',
    'collector',
])
def test_health_check_names_exist(check_name):
    """清单里写的健康检查项名必须真的注册在代码里。"""
    src = _source('core', 'health_checker.py')
    assert f"'{check_name}'" in src, (
        f'运维清单提到健康检查项 `{check_name}`，但 health_checker.py 里没有 —— '
        '要么改名后忘了更新文档，要么这项被删了')


# ---------------------------------------------------------------------------
# 2. 统计字段名（文档第 7–8 条）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('field,source_path', [
    ('queue_pending_count', ('采集层', 'data_collector.py')),
    ('queue_dropped_persisted', ('采集层', 'data_collector.py')),
    ('broken_channels', ('报警层', 'alarm_manager.py')),
    ('consecutive_failures', ('报警层', 'alarm_manager.py')),
])
def test_stats_fields_exist(field, source_path):
    src = _source(*source_path)
    assert f"'{field}'" in src, (
        f'运维清单提到统计字段 `{field}`，但 {"/".join(source_path)} 里没有')


# ---------------------------------------------------------------------------
# 3. 文档里提到的工具与命令必须存在
# ---------------------------------------------------------------------------

def test_doc_referenced_tools_exist():
    assert (PROJECT_ROOT / 'tools' / 'alarm_kpi_report.py').exists()
    assert (PROJECT_ROOT / 'tools' / 'alarm_replay.py').exists()
    assert (PROJECT_ROOT / 'tests' / 'test_backup_restore_drill.py').exists()


def test_doc_mentions_latest_rounds():
    """文档必须覆盖到最近一轮 —— 否则它就不再是"当前状态"。"""
    text = _doc_text()
    # 章节标题写的是区间（"round 271–277"），表格里是 `| 277 |` —— 两种都认
    assert ('271–277' in text) or ('round 277' in text), (
        '审计文档没有更新到最近一轮（章节标题应包含 "round 271–277"）')
    assert '| 277 |' in text, '缺陷表里没有第 277 轮的行'
    assert '运维自检清单' in text, '缺少交接用的自检清单'


def test_doc_checklist_fields_are_backticked():
    """清单里的字段名要用反引号包起来 —— 否则读者分不清哪些是要去查的字段。

    允许带前缀（`output.broken_channels`），只要是**代码片段**形态即可。
    """
    import re

    text = _doc_text()
    checklist = text[text.find('## 十一、运维自检清单'):]
    for field in ('queue_pending_count', 'broken_channels', 'device_freshness'):
        assert re.search(rf'`[^`]*{field}[^`]*`', checklist), (
            f'{field} 在清单里没有用反引号标注（应写成 `...{field}...`）')
