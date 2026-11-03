# -*- coding: utf-8 -*-
"""round 358：删规则 / 禁用规则必须收尾它的活跃报警。

`_rebuild_rules_index()` 只索引 enabled 的规则。一条规则被删除或改成
`enabled: false` 之后，索引里就没有它了 —— **后续采样永远不会再检查这个点位** ——
于是 `_process_alarm_state()` 永远不会给它走 CLEAR 分支：

· `alarm_states` 里那条报警永久残留 → 看板永久显示一条「规则已经不存在的报警」；
· 库里对应记录永远停在 `cleared = 0` → 「未恢复报警数」永久偏大；
· 点「确认」能确认，但它永远不会「恢复」（没人再检查那个点位）；
· 升级跟踪继续跑，300/900/1800 秒后还为一条不存在的规则给三级主管打电话。

`_retire_inactive_rules()` 在 `reload_config` 里早就处理了这件事，
但两个 API 端点（DELETE / PUT）是**绕过领域方法**直接改 `rules` 字典的，
从来没调过它。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _manager(retire_return: int = 1):
    mgr = MagicMock()
    mgr.rules = {'r1': {'id': 'r1', 'enabled': True}}
    mgr._retire_inactive_rules.return_value = retire_return
    return mgr


def _patch_save(monkeypatch, ok=True):
    from 展示层.api import api_alarms as mod
    monkeypatch.setattr(mod, 'save_yaml_config', lambda *a, **k: ok)
    monkeypatch.setattr(mod, 'load_yaml_config', lambda *a, **k: {
        'alarm_rules': [{'id': 'r1', 'enabled': True, 'condition': {}}],
    })


def test_delete_rule_retires_active_alarms(client, auth_headers, app, monkeypatch):
    mgr = _manager(retire_return=2)
    app.alarm_manager = mgr
    _patch_save(monkeypatch)

    resp = client.delete('/api/alarm-rules/r1', headers=auth_headers)

    assert resp.status_code == 200
    body = resp.get_json()
    assert body['retired_alarms'] == 2
    mgr._retire_inactive_rules.assert_called_once_with({'r1'})


def test_update_rule_disabling_retires_active_alarms(client, auth_headers, app, monkeypatch):
    """`enabled: false` 与删除等价 —— 索引里同样没了。"""
    mgr = _manager(retire_return=1)
    mgr.rules = {'r1': {'id': 'r1', 'enabled': False}}
    app.alarm_manager = mgr
    _patch_save(monkeypatch)

    resp = client.put('/api/alarm-rules/r1', headers=auth_headers,
                      json={'enabled': False})

    assert resp.status_code == 200
    mgr._retire_inactive_rules.assert_called_once_with({'r1'})


def test_update_rule_keeping_enabled_does_not_retire(client, auth_headers, app, monkeypatch):
    """反向对照：只是改阈值（仍启用）时不能去收尾 —— 那会把正在报的报警清掉。"""
    mgr = _manager()
    mgr.rules = {'r1': {'id': 'r1', 'enabled': True}}
    app.alarm_manager = mgr
    _patch_save(monkeypatch)

    resp = client.put('/api/alarm-rules/r1', headers=auth_headers,
                      json={'threshold': 99})

    assert resp.status_code == 200
    mgr._retire_inactive_rules.assert_not_called()
