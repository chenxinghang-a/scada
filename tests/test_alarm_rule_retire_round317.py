# -*- coding: utf-8 -*-
"""round 317：规则被删除/禁用后，它留下的活跃报警变成**永久幽灵**。

## 缺陷（探针实测）

```
配置：r1(d1/temp, >80)、r2(d2/press, >90)、r3(d3/flow, >50)
触发全部三条 → 活跃: [r1, r2, r3]

热重载：删掉 r1、把 r2 改成 enabled: false、r3 保留
热重载后 rules: [r2, r3]              ← 配置层面确实生效了
热重载后活跃:   [r1, r2, r3]          ← **r1/r2 还在**
库里未恢复:     [r1, r2, r3]          ← 记录永远 cleared=0
```

根因：`_rebuild_rules_index()` 只索引 `enabled` 的规则。规则被删/禁用后：

* 索引里没有它 → **后续采样永远不会再检查这个点位**；
* `_process_alarm_state()` 永远不会给它走 CLEAR 分支 → `alarm_states` 里
  那条**永久残留**；库里的 `cleared` 永远是 0。

现场表现：看板永久显示一条**规则已经不存在的报警**；点"确认"能确认，
但它永远不会"恢复"（没人再检查那个点位）；`get_active_alarms()` 与
"未恢复报警数"永久偏大。

与 round 274 修的「已删设备的幽灵报警」是同一个病，维度从 device 换成 rule。

## 修法

`load_config()` 里算出 `inactive = 删除的 ∪ 禁用的`，调
`_retire_inactive_rules()` 收尾：从 `alarm_states` 移除、库里标记
`cleared = 1`、从升级跟踪移除（否则 300/900/1800 秒后还会为一个
不存在的规则给三级主管打电话）。
"""

import logging
from datetime import datetime
from unittest.mock import MagicMock

import pytest
import yaml

from 存储层.database import Database
from 报警层.alarm_manager import AlarmManager


def _rule(rid, dev, reg, thr=80.0, enabled=True):
    return {'id': rid, 'name': f'规则{rid}', 'level': 'critical',
            'condition': 'greater_than', 'threshold': thr, 'delay': 0,
            'deadband': 0, 'enabled': enabled, 'area': 'all',
            'device_id': dev, 'register_name': reg}


@pytest.fixture
def env(tmp_path):
    db = Database(str(tmp_path / 'alarm.db'))
    cfg = tmp_path / 'alarms.yaml'

    def write(rules):
        cfg.write_text(yaml.safe_dump({'alarm_rules': rules}, allow_unicode=True),
                       encoding='utf-8')

    def build():
        m = AlarmManager(db, config_path=str(cfg))
        m.alarm_output = MagicMock()
        m.alarm_output.enabled = True
        m.broadcast_system = MagicMock()
        m.broadcast_system.enabled = True
        m._websocket_emit = MagicMock()
        return m

    return db, write, build


def _fire(m, dev, reg, value=95.0):
    m.check_alarm(dev, reg, value, datetime(2026, 10, 29, 4, 0, 0))


def _stop(m):
    try:
        m.stop()
    except Exception:       # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 1. 删除规则
# ---------------------------------------------------------------------------

def test_deleted_rule_retires_its_active_alarm(env, caplog):
    """删掉规则 → 它的活跃报警必须收尾（状态 / 库 / 活跃列表三处）。"""
    db, write, build = env
    write([_rule('r1', 'd1', 'temp'), _rule('r3', 'd3', 'flow', 50.0)])
    m = build()
    try:
        _fire(m, 'd1', 'temp')
        _fire(m, 'd3', 'flow')
        assert sorted(a['alarm_id'] for a in m.get_active_alarms()) == ['r1', 'r3']

        write([_rule('r3', 'd3', 'flow', 50.0)])       # 删掉 r1
        with caplog.at_level(logging.WARNING):
            m.load_config()

        assert 'r1' not in [k[0] for k in m.alarm_states], (
            '规则删了但活跃报警还在 —— 它会永久挂在看板上，且永远不会恢复')
        assert sorted(a['alarm_id'] for a in m.get_active_alarms()) == ['r3']
        assert 'r1' not in [r['alarm_id'] for r in db.get_uncleared_alarms()], (
            '库里那条永远 cleared=0 —— "未恢复报警数"永久偏大')
        assert any('收尾' in r.getMessage() for r in caplog.records), (
            '收尾没有留痕，运维不知道那条报警为什么突然消失了')
    finally:
        _stop(m)


# ---------------------------------------------------------------------------
# 2. 禁用规则
# ---------------------------------------------------------------------------

def test_disabled_rule_also_retires(env):
    """`enabled: false` 与删除等价 —— 都不再进索引，都会变幽灵。"""
    db, write, build = env
    write([_rule('r2', 'd2', 'press', 90.0), _rule('r3', 'd3', 'flow', 50.0)])
    m = build()
    try:
        _fire(m, 'd2', 'press')
        _fire(m, 'd3', 'flow')

        write([_rule('r2', 'd2', 'press', 90.0, enabled=False),
               _rule('r3', 'd3', 'flow', 50.0)])
        m.load_config()

        assert 'r2' not in [k[0] for k in m.alarm_states], (
            '规则被禁用了但活跃报警还在 —— 没人再检查那个点位，它永远不恢复')
        assert 'r2' not in [r['alarm_id'] for r in db.get_uncleared_alarms()]
    finally:
        _stop(m)


# ---------------------------------------------------------------------------
# 3. 反向对照
# ---------------------------------------------------------------------------

def test_kept_rule_is_not_touched(env):
    """保留的规则不受影响（不许误收尾）。"""
    db, write, build = env
    write([_rule('r1', 'd1', 'temp'), _rule('r3', 'd3', 'flow', 50.0)])
    m = build()
    try:
        _fire(m, 'd1', 'temp')
        _fire(m, 'd3', 'flow')

        write([_rule('r3', 'd3', 'flow', 50.0)])
        m.load_config()

        assert 'r3' in [k[0] for k in m.alarm_states], '保留的规则被误收尾了'
        assert 'r3' in [a['alarm_id'] for a in m.get_active_alarms()]
    finally:
        _stop(m)


def test_threshold_change_does_not_retire(env):
    """改阈值**不算**失效：规则仍 enabled、仍在索引里，下一个采样周期按新阈值判定。

    这条是防"收尾太激进"——如果连改阈值都收尾，一次正常调参就会让
    所有在跑的报警突然消失，操作员会以为"问题自己好了"。
    """
    db, write, build = env
    write([_rule('r1', 'd1', 'temp', 80.0)])
    m = build()
    try:
        _fire(m, 'd1', 'temp')
        assert 'r1' in [k[0] for k in m.alarm_states]

        write([_rule('r1', 'd1', 'temp', 200.0)])      # 只改阈值
        m.load_config()

        assert 'r1' in [k[0] for k in m.alarm_states], (
            '改阈值被当成"规则失效"收尾了 —— 调参会静默清掉在跑的报警')
        # 下一次采样按新阈值判定 → 95 < 200，条件不成立 → 正常 CLEAR
        _fire(m, 'd1', 'temp', 95.0)
        assert 'r1' not in [k[0] for k in m.alarm_states], (
            '新阈值下条件已不成立，却没有走 CLEAR')
    finally:
        _stop(m)


def test_retire_removes_escalation_tracking(env):
    """收尾必须同时撤掉升级跟踪（否则还会为不存在的规则打电话）。

    ⚠️ `_escalation_manager` 只有在 `alarms.yaml` 里配了 escalation 段时才创建
    （`load_config` 里 `if self._escalation_manager is None`）。本组用例的
    极简配置没有那个段，所以这里直接注入一个 MagicMock 来验证"有没有调
    `remove_alarm`"——比构造完整的多级升级配置更聚焦。
    """
    db, write, build = env
    write([_rule('r1', 'd1', 'temp')])
    m = build()
    try:
        m._escalation_manager = MagicMock()
        _fire(m, 'd1', 'temp')
        assert m._escalation_manager.record_alarm.called, (
            '前提不成立：触发时没有登记升级跟踪')

        write([])                                       # 全部删掉
        m.load_config()

        m._escalation_manager.remove_alarm.assert_any_call('r1', 'd1', 'temp')
    finally:
        _stop(m)


def test_no_active_alarm_no_error(env):
    """反向对照：规则删了但没有活跃报警 → 收尾 0 条，不报错。"""
    db, write, build = env
    write([_rule('r1', 'd1', 'temp')])
    m = build()
    try:
        write([])          # 从未触发过就删掉
        m.load_config()
        assert m.alarm_states == {}
    finally:
        _stop(m)
