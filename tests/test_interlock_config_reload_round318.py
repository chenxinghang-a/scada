# -*- coding: utf-8 -*-
"""round 318：`interlocks.yaml` 改了**根本不会生效** —— 必须重启进程。

## 缺陷

`DeviceControlSafety._load_preset_interlocks()` **只在 `__init__` 里调用一次**，
全仓库没有第二个调用点，也没有任何重载入口。而 `core/device_config_watch`
只监视 `devices.yaml`。

后果（方向"安全"但**会误导人**）：

* 改**高**阈值想减少误跳闸 → 实际没生效 → 继续误跳闸；
* 改**低**阈值想更严格 → 实际仍是旧阈值 → **保护不足而无人知**；
* 全程**没有任何提示**：运维改完文件、`git status` 干净，就以为生效了。

这是 round 283（`devices.yaml` 热重载）的**同型漏网**：当时只接了设备配置，
联锁配置（安全规则，比设备清单更需要热重载）没接。

## 修法

1. `DeviceControlSafety.reload_interlocks()`：**构造新表再原子替换**，
   返回 `added / removed / changed / kept`；
2. `removed` 与 `changed` 的规则，运行时状态必须清干净
   （`_interlock_states` / `_interlock_action_failures` / `_interlock_bypass`）——
   否则一条被删的联锁仍挂在旁路集合里，将来同 id 规则加回来时**自动处于旁路**；
3. `core/device_config_watch.watch_interlocks_once()`：复用 mtime 基线机制；
4. `core/maintenance` 注册 `interlock_config_watch`（10 秒），`run.py` 传 `device_control`。
"""

import logging
from unittest.mock import MagicMock

import pytest

from 智能层.device_control import DeviceControlSafety


def _rule(rid, thr=90.0, enabled=True, act='block_start'):
    return {
        'id': rid, 'name': rid, 'priority': 1, 'enabled': enabled,
        'condition': {'type': 'threshold', 'device_id': 'd1',
                      'register': 'temp', 'operator': '>', 'value': thr},
        'action': {'type': act},
    }


def _dc(rules):
    dm = MagicMock()
    dm.devices = {'d1': {'protocol': 'modbus_tcp',
                         'registers': [{'name': 'temp', 'address': 0}]}}
    return DeviceControlSafety(MagicMock(), dm, None, config={'interlocks': list(rules)})


# ---------------------------------------------------------------------------
# 1. 差集
# ---------------------------------------------------------------------------

def test_reload_reports_diff():
    dc = _dc([_rule('keep'), _rule('gone'), _rule('chg', thr=90.0)])

    dc.config['interlocks'] = [_rule('keep'), _rule('chg', thr=120.0), _rule('new')]
    diff = dc.reload_interlocks()

    assert diff['added'] == ['new']
    assert diff['removed'] == ['gone']
    assert diff['changed'] == ['chg'], '阈值改了却没被识别成"变更"'
    assert diff['kept'] == ['keep']
    assert sorted(dc._interlock_rules) == ['chg', 'keep', 'new']


def test_reload_applies_new_threshold():
    """改阈值后必须真的生效（这是整个缺陷的核心）。"""
    dc = _dc([_rule('r1', thr=90.0)])
    dc.check_interlocks('d1', 'temp', 100.0)
    assert dc._interlock_states['r1'] is True, '前提不成立：旧阈值下应已触发'

    dc.config['interlocks'] = [_rule('r1', thr=200.0)]
    dc.reload_interlocks()

    # 新阈值 200：100 不再成立
    dc.check_interlocks('d1', 'temp', 100.0)
    assert dc._interlock_states['r1'] is False, (
        '改完阈值仍按旧值判定 —— 运维调参完全没生效')


# ---------------------------------------------------------------------------
# 2. 失效规则的状态必须清干净
# ---------------------------------------------------------------------------

def test_removed_rule_state_is_cleared():
    dc = _dc([_rule('r1'), _rule('r2')])
    dc._interlock_states['r1'] = True
    dc._interlock_action_failures['r1'] = 3
    dc._interlock_bypass.add('r1')

    dc.config['interlocks'] = [_rule('r2')]
    dc.reload_interlocks()

    assert 'r1' not in dc._interlock_states
    assert 'r1' not in dc._interlock_action_failures
    assert 'r1' not in dc._interlock_bypass, (
        '被删的联锁仍挂在旁路集合里 —— 同 id 规则加回来时会自动处于旁路状态')


def test_changed_rule_state_is_reset_and_bypass_dropped():
    """改了条件 → 旧阈值下的"已触发"和旧审批的旁路都不能沿用。"""
    dc = _dc([_rule('r1', thr=90.0)])
    dc._interlock_states['r1'] = True
    dc._interlock_bypass.add('r1')

    dc.config['interlocks'] = [_rule('r1', thr=120.0)]
    dc.reload_interlocks()

    assert dc._interlock_states.get('r1') is False, (
        '新阈值下仍沿用旧阈值的"已触发"状态')
    assert 'r1' not in dc._interlock_bypass, (
        '旧规则（90℃）的旁路审批被自动套到新规则（120℃）上')


def test_kept_rule_state_is_preserved():
    """反向对照：没变的规则状态必须保留（不许一刀切重置）。"""
    dc = _dc([_rule('keep'), _rule('chg', thr=90.0)])
    dc._interlock_states['keep'] = True

    dc.config['interlocks'] = [_rule('keep'), _rule('chg', thr=120.0)]
    dc.reload_interlocks()

    assert dc._interlock_states.get('keep') is True, '没变的规则被误重置了'


# ---------------------------------------------------------------------------
# 3. mtime 监视
# ---------------------------------------------------------------------------

@pytest.fixture
def watch_env(tmp_path, monkeypatch):
    import core.device_config_watch as mod
    path = tmp_path / 'interlocks.yaml'
    path.write_text('interlocks: []\n', encoding='utf-8')
    monkeypatch.setattr(mod, 'DEFAULT_INTERLOCK_REL', str(path))
    return mod, path


def test_watch_first_call_only_baselines(watch_env):
    mod, _path = watch_env
    dc = MagicMock()
    state = {}
    r = mod.watch_interlocks_once(dc, state=state)
    assert r['reloaded'] is False
    assert '基线' in r['reason']
    dc.reload_interlocks.assert_not_called()


def test_watch_reloads_on_mtime_change(watch_env):
    mod, path = watch_env
    dc = MagicMock()
    dc.reload_interlocks.return_value = {'added': ['r1'], 'removed': [],
                                         'changed': [], 'kept': []}
    state = {}
    mod.watch_interlocks_once(dc, state=state)          # 建基线

    import os
    import time
    time.sleep(0.01)
    path.write_text('interlocks:\n- id: r1\n', encoding='utf-8')
    os.utime(path, None)

    r = mod.watch_interlocks_once(dc, state=state)
    assert r['reloaded'] is True
    dc.reload_interlocks.assert_called_once()


def test_watch_advances_baseline_even_on_error(watch_env):
    """重载抛异常时基线也要推进 —— 否则每 10 秒刷一次同样的错误日志。"""
    mod, path = watch_env
    dc = MagicMock()
    dc.reload_interlocks.side_effect = RuntimeError('boom')
    state = {}
    mod.watch_interlocks_once(dc, state=state)

    import os
    import time
    time.sleep(0.01)
    path.write_text('interlocks:\n- id: x\n', encoding='utf-8')
    os.utime(path, None)

    r1 = mod.watch_interlocks_once(dc, state=state)
    assert r1['reloaded'] is False and '异常' in r1['reason']
    calls = dc.reload_interlocks.call_count

    r2 = mod.watch_interlocks_once(dc, state=state)      # 同一 mtime 再来一次
    assert r2['reloaded'] is False
    assert dc.reload_interlocks.call_count == calls, (
        '同一个坏文件被反复重试 —— 每 10 秒刷一次错误日志')


def test_watch_without_reload_method_is_not_fatal(watch_env):
    """对象没有 `reload_interlocks` → 返回说明，不许抛。"""
    mod, path = watch_env
    dc = object()          # 没有 reload_interlocks
    state = {}
    mod.watch_interlocks_once(dc, state=state)

    import os
    import time
    time.sleep(0.01)
    path.write_text('interlocks: []\n# x\n', encoding='utf-8')
    os.utime(path, None)

    r = mod.watch_interlocks_once(dc, state=state)
    assert r['changed'] is True
    assert r['reloaded'] is False
    assert 'reload_interlocks' in r['reason']
