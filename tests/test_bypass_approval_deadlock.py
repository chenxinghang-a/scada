# -*- coding: utf-8 -*-
"""联锁旁路审批：第二次审批会死锁（round 230）。

缺陷
----
`approve_bypass()` 全程持 `self._bypass_lock`，而在**满足审批人数**时
在锁内调用 `self._activate_bypass(request)`；后者第一件事又是

    with self._bypass_lock:          # ← 再次获取同一把锁
        self._active_bypasses.add(...)

而 `self._bypass_lock = threading.Lock()` 是**不可重入**的
（不是 `RLock`）→ 第二次审批（也就是旁路真正生效的那一刻）**永久阻塞**。

后果比「一次操作卡住」严重得多：
* 审批请求的 worker 线程永久占着这把锁；
* 之后**任何**旁路操作（request / approve / reject / 列表查询）都会挂住
  → 整个联锁旁路功能在第一次成功审批后就彻底死掉，且没有任何报错，
  只有线程数慢慢涨、接口全部超时。
* 这是安全相关流程（IEC 61511 多人审批），卡死意味着现场无法合法旁路联锁。

修法：把 `_activate_bypass()` 移到 `with self._bypass_lock:` **外面**。
请求对象的状态在锁内已经改完（`status='approved'`），锁外激活不影响正确性；
临界区也因此更短。
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest


def _control(tmp_path):
    from 智能层.device_control import DeviceControlSafety

    dc = DeviceControlSafety(MagicMock(), device_manager=MagicMock())
    # 审计文件别写到仓库里
    dc._audit_file = tmp_path / 'audit.jsonl'
    return dc


def _run_with_timeout(fn, timeout: float = 3.0):
    """在子线程里跑 fn，超时返回 (False, None)。"""
    box = {}

    def target():
        box['value'] = fn()

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return False, None
    return True, box.get('value')


def test_second_approval_activates_bypass_without_deadlock(tmp_path):
    """核心用例：第二个人审批（= 旁路生效）必须能返回。"""
    dc = _control(tmp_path)
    rid = dc.request_bypass('rule_1', 'alice', '检修需要')

    ok, _ = dc.approve_bypass(rid, 'bob')
    assert ok, '第一位审批就该成功（1/2）'

    finished, result = _run_with_timeout(lambda: dc.approve_bypass(rid, 'carol'))

    assert finished, (
        '第二次审批卡死了 —— approve_bypass 持着 _bypass_lock 调 _activate_bypass，'
        '而 _activate_bypass 又去获取同一把不可重入的锁。'
        '实际影响：第一次成功审批后整个旁路功能永久挂住'
    )
    ok2, msg = result
    assert ok2 is True, msg
    assert 'rule_1' in dc._interlock_bypass, '旁路没有真正生效'


def test_bypass_lock_not_poisoned_after_activation(tmp_path):
    """旁路激活后，后续旁路操作必须照常可用（锁没被永久占用）。"""
    dc = _control(tmp_path)
    rid = dc.request_bypass('rule_1', 'alice', '检修需要')
    dc.approve_bypass(rid, 'bob')
    finished, _ = _run_with_timeout(lambda: dc.approve_bypass(rid, 'carol'))
    assert finished, '第二次审批卡死，后续操作必然全挂'

    # 再走一次完整流程：申请必须立刻返回
    finished2, rid2 = _run_with_timeout(lambda: dc.request_bypass('rule_2', 'alice', '另一个'))
    assert finished2, '锁被永久占用 —— 连新的旁路申请都卡住了'
    assert rid2


def test_separation_of_duties_still_enforced(tmp_path):
    """回归：修死锁不能顺手把「不能自己审批自己」弄丢。"""
    dc = _control(tmp_path)
    rid = dc.request_bypass('rule_1', 'alice', 'x')
    ok, msg = dc.approve_bypass(rid, 'alice')
    assert ok is False
    assert '自己' in msg


def test_double_approval_by_same_person_rejected(tmp_path):
    """回归：同一个人不能算两票。"""
    dc = _control(tmp_path)
    rid = dc.request_bypass('rule_1', 'alice', 'x')
    assert dc.approve_bypass(rid, 'bob')[0] is True
    ok, msg = dc.approve_bypass(rid, 'bob')
    assert ok is False
    assert '已经审批过' in msg


def test_approved_bypass_auto_restores_on_timeout(tmp_path):
    """旁路超时要能自动恢复（回归：别把定时器也挪没了）。"""
    dc = _control(tmp_path)
    rid = dc.request_bypass('rule_1', 'alice', 'x', timeout_minutes=30)
    dc.approve_bypass(rid, 'bob')
    dc.approve_bypass(rid, 'carol')
    assert 'rule_1' in dc._interlock_bypass

    # 直接调自动恢复（不真的等 30 分钟）
    dc._auto_restore_bypass('rule_1', rid)
    assert 'rule_1' not in dc._interlock_bypass
    assert 'rule_1' not in dc._active_bypasses
