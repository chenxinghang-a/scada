# -*- coding: utf-8 -*-
"""round 304：报警规则表的生命周期（诚实清单第 1、2 项）。

三个缺陷形状：
· 重载「只增不删」—— 文件里删掉的规则继续生效，还会被写回文件；
· 外部事件寄生在 `self.rules` —— 运行时数据被落盘成配置、污染统计、进判定索引；
· 索引 `clear()` 后逐条重建 —— 中间有「索引为空」窗口，撞上的采样静默漏检。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import yaml


def _mgr():
    from 报警层.alarm_manager import AlarmManager
    return AlarmManager(MagicMock())


def _write_rules(path, rules):
    path.write_text(yaml.safe_dump({'alarm_rules': rules}, allow_unicode=True),
                    encoding='utf-8')


def _rule(rid, name, threshold=10):
    return {'id': rid, 'name': name, 'device_id': 'd1', 'register_name': 't',
            'condition': 'gt', 'threshold': threshold, 'level': 'warning'}


# ---------------------------------------------------------------------------
# 1. 重载必须让「删除」也传播
# ---------------------------------------------------------------------------

def test_reload_removes_deleted_rules(tmp_path):
    cfg = tmp_path / 'alarms.yaml'
    _write_rules(cfg, [_rule('r1', 'A'), _rule('r2', 'B', 20)])

    mgr = _mgr()
    mgr.config_path = str(cfg)
    try:
        mgr.load_config()
        assert set(mgr.rules) == {'r1', 'r2'}

        # 运维从文件里删掉 r2
        _write_rules(cfg, [_rule('r1', 'A')])
        mgr.load_config()

        assert 'r2' not in mgr.rules, (
            '配置里删掉的规则仍留在内存 —— 而且下一次 add_rule 会把它写回文件，重启也活')
        indexed = [rid for rid, _ in mgr._rules_index.get(('d1', 't'), [])]
        assert indexed == ['r1'], f'索引里还留着已删除的规则：{indexed}'
    finally:
        mgr.stop()


def test_reload_keeps_rules_when_file_unchanged(tmp_path):
    """反向对照：文件没变时规则必须原样保留。"""
    cfg = tmp_path / 'alarms.yaml'
    _write_rules(cfg, [_rule('r1', 'A'), _rule('r2', 'B', 20)])

    mgr = _mgr()
    mgr.config_path = str(cfg)
    try:
        mgr.load_config()
        mgr.load_config()
        assert set(mgr.rules) == {'r1', 'r2'}
    finally:
        mgr.stop()


# ---------------------------------------------------------------------------
# 2. 外部事件不许寄生在配置规则表里
# ---------------------------------------------------------------------------

def test_external_alarm_does_not_pollute_rules(tmp_path):
    cfg = tmp_path / 'alarms.yaml'
    _write_rules(cfg, [])

    mgr = _mgr()
    mgr.config_path = str(cfg)
    try:
        mgr.load_config()
        mgr.raise_external_alarm('edge_x', '管道压力超限', 'warning')

        assert 'edge_x' not in mgr.rules, '外部事件被塞进了配置规则表'
        active = mgr.get_active_alarms()
        assert any(a['alarm_id'] == 'edge_x' for a in active), (
            '活跃报警列表里看不到这条外部事件 —— 名称/级别丢了')

        mgr._save_config()
        saved = yaml.safe_load(cfg.read_text(encoding='utf-8')) or {}
        ids = [r.get('id') for r in saved.get('alarm_rules', [])]
        assert 'edge_x' not in ids, (
            f'外部事件被落盘成配置规则（重启后变成 condition: external 的僵尸规则）：{ids}')
    finally:
        mgr.stop()


def test_external_alarm_not_in_decision_index():
    """外部事件不该进判定索引 —— 否则同名点位上报时会把它判成「已恢复」。"""
    mgr = _mgr()
    try:
        mgr.raise_external_alarm('edge_z', '事件', 'warning')
        mgr._rebuild_rules_index()
        hits = mgr._rules_index.get(('external', 'event'), [])
        assert not hits, f'外部事件进了判定索引：{hits}'
    finally:
        mgr.stop()


def test_clear_external_alarm_reclaims_description():
    """`_external_alarms` 不能只增不减（按消息哈希派生的部署会无界增长）。"""
    mgr = _mgr()
    try:
        mgr.raise_external_alarm('edge_y', '事件', 'warning')
        assert 'edge_y' in mgr._external_alarms

        mgr.clear_external_alarm('edge_y')

        assert 'edge_y' not in mgr._external_alarms, '外部事件描述没有被回收'
    finally:
        mgr.stop()


# ---------------------------------------------------------------------------
# 3. 索引重建必须是整体替换，不能有「空窗」
# ---------------------------------------------------------------------------

def test_index_rebuild_replaces_instead_of_clearing():
    """`clear()` 后逐条 append 会留下「索引为空」的窗口，撞上的采样静默漏检。"""
    mgr = _mgr()
    try:
        mgr._rules_index = {'sentinel': [('x', {})]}
        old = mgr._rules_index

        mgr._rebuild_rules_index()

        assert mgr._rules_index is not old, (
            '索引还是同一个 dict —— 说明是 clear() 后重建，中间存在空窗')
        assert 'sentinel' not in mgr._rules_index, '旧内容没被替换掉'
    finally:
        mgr.stop()
