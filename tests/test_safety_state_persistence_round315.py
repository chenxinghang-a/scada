# -*- coding: utf-8 -*-
"""round 315：急停与联锁旁路是**纯内存**状态，进程重启后丢失。

## 缺陷（探针实测）

```
dc.trigger_emergency_stop('测试')      → estop_active = True，写操作被拒
dc.approve_bypass(...)                 → 联锁 il1 旁路生效（检修中）
—— 进程重启 ——
重启后 estop: active=False             ← 软件不再禁止写操作
重启后 check_write: allowed=True       ← 而现场急停按钮可能仍锁着
重启后 _interlock_bypass: set()        ← 旁路消失，联锁突然恢复
```

两条后果都指向安全：

1. **急停丢失**：重启后 `validate_write()` 放行写操作，而现场急停按钮可能
   还锁着 —— 操作员在界面上点"启动"，设备真的会动。
2. **旁路丢失**：维护窗口内重启（改配置、升级、崩溃重启）→ 联锁立刻恢复生效
   → 正在检修的设备被联锁动作（写停机值 / 急停），**人员可能被夹**。
   ISA-18.2 / IEC 61511 里旁路必须"超时自动恢复"，而不是"重启即恢复"。

## 修法

新增 `system_state`（KV）表 + `DeviceControlSafety._persist_* / _restore_persisted_state`：

* 急停：`trigger` / `reset` 时落库，`__init__` 恢复（**恢复后继续禁止写操作**）；
* 旁路：激活 / 自动恢复时落库，`__init__` 恢复 ——
  **必须校验 `expires_at`**：库里可能留着一条早该自动恢复的旁路
  （上次进程在 Timer 触发前被杀），不校验就等于"永久旁路"。

存的是**墙钟** `expires_at`（单调时钟不能跨进程），恢复时换算成剩余时长重建 Timer。
"""

import time
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from 存储层.database import Database
from 智能层.device_control import DeviceControlSafety

RULE = {
    'id': 'il1', 'name': '超温禁止启动', 'priority': 1, 'enabled': True,
    'condition': {'type': 'threshold', 'device_id': 'd1',
                  'register': 'temp', 'operator': '>', 'value': 90},
    'action': {'type': 'block_start'},
}


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'ctl.db'))


def _dc(db):
    dm = MagicMock()
    dm.devices = {'d1': {'protocol': 'modbus_tcp',
                         'registers': [{'name': 'temp', 'address': 0},
                                       {'name': 'fan', 'address': 10}]}}
    dc = DeviceControlSafety(db, dm, None, config={'interlocks': [dict(RULE)]})
    dc._write_limits = {'d1': {'temp': (0, 200), 'fan': (0, 1)}}
    return dc


# ---------------------------------------------------------------------------
# 1. KV 存储本身
# ---------------------------------------------------------------------------

def test_system_state_roundtrip(db):
    assert db.set_system_state('k1', {'a': 1, 'b': '中文'}) is True
    assert db.get_system_state('k1') == {'a': 1, 'b': '中文'}
    assert db.set_system_state('k1', None) is True
    assert db.get_system_state('k1') is None


def test_system_state_bad_json_is_tolerated(db):
    """库里被手工写坏 → 返回 None，不许抛（否则启动直接失败）。"""
    with db.get_connection() as conn:
        conn.cursor().execute(
            "INSERT OR REPLACE INTO system_state (key, value, updated_at) VALUES (?, ?, ?)",
            ('bad', 'not-json{{{', datetime.now()))
    assert db.get_system_state('bad') is None


# ---------------------------------------------------------------------------
# 2. 急停跨重启
# ---------------------------------------------------------------------------

def test_estop_survives_restart(db):
    """急停后重启 → 仍然处于急停，写操作仍被拒。"""
    dc1 = _dc(db)
    dc1.trigger_emergency_stop('现场急停按钮')
    assert dc1.get_estop_status()['active'] is True

    dc2 = _dc(db)      # 重启（__init__ 里恢复）

    st = dc2.get_estop_status()
    assert st['active'] is True, (
        '急停状态重启后丢失 —— 软件会放行写操作，而现场急停按钮可能还锁着')
    assert '急停按钮' in st['reason'], '急停原因没恢复（排障时看不出为什么在急停）'

    r = dc2.validate_write('d1', 0, 1, 'op')
    assert r['allowed'] is False, '重启后急停状态下仍然允许写操作'
    assert '紧急停机' in r['reason']


def test_reset_estop_clears_persistence(db):
    """反向对照：解除急停后再重启，不该又变回急停。"""
    dc1 = _dc(db)
    dc1.trigger_emergency_stop('测试')
    dc1.reset_emergency_stop('eng1')

    dc2 = _dc(db)
    assert dc2.get_estop_status()['active'] is False, (
        '解除急停没有清库 —— 每次重启都会"复活"成急停，设备永远起不来')


def test_no_estop_no_restore(db):
    """反向对照：没触发过急停 → 重启后正常。"""
    dc = _dc(db)
    assert dc.get_estop_status()['active'] is False
    assert dc.validate_write('d1', 10, 1, 'op')['allowed'] is True


# ---------------------------------------------------------------------------
# 3. 旁路跨重启
# ---------------------------------------------------------------------------

def _activate_bypass(dc, minutes=30):
    rid = dc.request_bypass('il1', 'op1', '维护', minutes)
    dc.approve_bypass(rid, 'op2')
    dc.approve_bypass(rid, 'op3')
    assert 'il1' in dc._interlock_bypass, '前提不成立：旁路没生效'
    return rid


def test_bypass_survives_restart(db):
    """维护窗口内重启 → 旁路仍然有效（否则联锁突然恢复，检修人员被夹）。"""
    dc1 = _dc(db)
    _activate_bypass(dc1)

    dc2 = _dc(db)
    assert 'il1' in dc2._interlock_bypass, (
        '旁路重启后失效 —— 正在检修的设备会被联锁动作')
    assert 'il1' in dc2._active_bypasses


def test_expired_bypass_is_not_restored(db, caplog):
    """**关键反向对照**：库里留着一条早该过期的旁路 → 不许恢复。

    场景：旁路的 Timer 还没触发，进程被杀。重启后如果不校验 `expires_at`，
    这条旁路就变成**永久旁路** —— 超温联锁永远不生效。
    """
    dc1 = _dc(db)
    rid = _activate_bypass(dc1, minutes=1)

    # 把库里的到期时间改成"已经过去 1 小时"
    payload = db.get_system_state(DeviceControlSafety.STATE_KEY_BYPASS)
    assert 'il1' in payload
    payload['il1']['expires_at'] = time.time() - 3600
    db.set_system_state(DeviceControlSafety.STATE_KEY_BYPASS, payload)

    # ⚠️ 断言必须看**日志**，不能只看 `_interlock_bypass` 的最终状态：
    # 第一版只断言 `'il1' not in dc2._interlock_bypass`，结果变异（去掉过期校验）
    # **存活**了 —— 因为变异后 `remaining = expires_at - now = -3600`，
    # `threading.Timer(-3600, ...)` **立即触发** `_auto_restore_bypass`，
    # 把刚恢复的旁路又移除了，最终状态恰好一致。这是"巧合掩盖"。
    # 日志能区分「根本没恢复」与「恢复后又被立即移除」——
    # 后者虽然最终状态对，但**中间有一瞬间联锁是旁路着的**，那才是危险的。
    import logging
    with caplog.at_level(logging.WARNING):
        dc2 = _dc(db)

    restored_logs = [r.getMessage() for r in caplog.records
                     if '旁路从数据库恢复' in r.getMessage()]
    assert not restored_logs, (
        f'已过期的旁路被恢复了（虽然随后被 Timer 立即移除，'
        f'但恢复动作本身就不该发生）: {restored_logs}')

    assert 'il1' not in dc2._interlock_bypass, (
        '已过期的旁路被恢复了 —— 这条联锁变成永久旁路，超温也不会拦')
    # 过期的条目应从库里清掉
    assert not (db.get_system_state(DeviceControlSafety.STATE_KEY_BYPASS) or {}).get('il1')


def test_bypass_auto_restore_clears_persistence(db):
    """旁路超时自动恢复后，库里也要清掉（否则下次启动又"恢复"一遍）。"""
    dc1 = _dc(db)
    rid = _activate_bypass(dc1)
    dc1._auto_restore_bypass('il1', rid)

    assert 'il1' not in dc1._interlock_bypass
    assert not (db.get_system_state(DeviceControlSafety.STATE_KEY_BYPASS) or {})


def test_no_bypass_no_restore(db):
    """反向对照：没申请过旁路 → 重启后 `_interlock_bypass` 为空。"""
    dc = _dc(db)
    assert dc._interlock_bypass == set()
    assert dc._active_bypasses == set()
