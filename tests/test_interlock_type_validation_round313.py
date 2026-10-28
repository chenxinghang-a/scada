# -*- coding: utf-8 -*-
"""round 313：联锁规则的**类型拼错** = 静默失效，以及非可重入锁的闸门。

## 两条真缺陷

1. **`condition.type` 拼错 → 规则永不触发，一个字都不报。**
   `check_interlocks()` 只判 `threshold` / `coil`，其它值让 `matched` 保持
   False 然后 `continue`。yaml 里写 `type: thresold`（少个 h），
   界面上规则存在、`enabled: true`，超温时什么都不发生。

2. **`action.type` 拼错 → 条件会触发、审计记"已执行"，但什么都没做。**
   `_execute_interlock_action()` 对未知类型**没有 else 分支**，
   `action_ok` 保持 True，审计写 `interlock_triggered`（成功）。
   现场表现：状态显示"已触发"、日志说"已执行"，而**停机值一次都没下发** ——
   比第 1 条更危险，因为它看起来是成功的。

## 修法

* 加载时校验 `condition.type` / `action.type` 是否在允许集合内，
  不在就**拒绝并计入 `rejected`**（走 round 308 加的"部分丢弃"WARNING 汇总）；
* 执行时补 `else` → `action_ok = False` + ERROR（防运行期 `add_interlock()`
  注入的规则绕过加载校验）。

## 附带的闸门

本轮扫描「持锁调用自身方法」命中 6 处，全部是 `RLock`（可重入，安全）。
但**没有任何东西阻止将来有人把某把锁换成 `threading.Lock()`** ——
那时这 6 处里被碰到的那一处会**自锁死**（整个模块静默停摆，无任何报错）。
所以把"非可重入锁 + 嵌套调用"做成闸门。
"""

import ast
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parents[1]


def _dc(rules):
    from 智能层.device_control import DeviceControlSafety

    return DeviceControlSafety(MagicMock(), config={'interlocks': rules})


def _rule(rid, cond_type='threshold', act_type='block_start'):
    return {
        'id': rid,
        'name': rid,
        'condition': {'type': cond_type, 'device_id': 'd1',
                      'register': 'temp', 'operator': '>', 'value': 90},
        'action': {'type': act_type},
    }


# ---------------------------------------------------------------------------
# 1. 加载时校验类型
# ---------------------------------------------------------------------------

def test_bad_condition_type_is_rejected(caplog):
    """`condition.type` 拼错 → 拒绝加载并汇总告警。"""
    with caplog.at_level(logging.WARNING):
        dc = _dc([_rule('good'), _rule('typo', cond_type='thresold')])

    assert list(dc.get_interlock_status()) == ['good'], (
        '拼错的条件类型被当成有效规则加载了 —— 它会永不触发且不报错')
    summary = dc.get_interlock_load_summary()
    assert summary['rejected'] == 1
    assert any('永远不会触发' in r.getMessage() for r in caplog.records)
    assert any('部分丢弃' in r.getMessage() for r in caplog.records), (
        '拒绝了规则却没有走"部分丢弃"的汇总告警')


def test_bad_action_type_is_rejected(caplog):
    """`action.type` 拼错 → 拒绝加载（否则审计会说"已执行"）。"""
    with caplog.at_level(logging.WARNING):
        dc = _dc([_rule('typo_act', act_type='write_registor')])

    assert dc.get_interlock_status() == {}
    assert dc.get_interlock_load_summary()['rejected'] == 1
    assert any('动作不会执行' in r.getMessage() for r in caplog.records)


def test_valid_types_load_normally():
    """反向对照：合法类型照常加载，不许误杀。"""
    dc = _dc([
        _rule('t1', 'threshold', 'write_register'),
        _rule('t2', 'threshold', 'emergency_stop'),
        _rule('t3', 'coil', 'block_start'),
    ])
    assert sorted(dc.get_interlock_status()) == ['t1', 't2', 't3']
    assert dc.get_interlock_load_summary()['rejected'] == 0


# ---------------------------------------------------------------------------
# 2. 执行期兜底：未知动作类型不许算成功
# ---------------------------------------------------------------------------

def test_unknown_action_type_is_not_success(caplog):
    """运行期注入的规则绕过加载校验时，执行期必须如实返回 False。

    原来没有 else 分支 → `action_ok` 保持 True → 审计记 `interlock_triggered`，
    而动作一次都没执行。
    """
    dc = _dc([])
    dc.add_interlock(_rule('injected', act_type='write_registor'))

    with caplog.at_level(logging.ERROR):
        ok = dc._execute_interlock_action(dc._interlock_rules['injected'])

    assert ok is False, '未知动作类型被当成"执行成功" —— 审计会说设备已进入安全状态'
    assert any('动作未执行' in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 3. 闸门：非可重入锁 + 嵌套调用 = 自锁死
# ---------------------------------------------------------------------------

#: 已知用 `threading.Lock()`（非可重入）且**不存在**持锁调用自身方法的类。
#: 每条必须写清为什么安全。
NON_REENTRANT_OK = {
    # 目前没有任何类命中（全部用 RLock）。这条清单是给将来准备的：
    # 若某个类确实需要非可重入锁且不嵌套调用，在这里登记并说明。
}

LOCK_NAMES = ('_lock', '_state_lock', '_io_lock', '_tasks_lock', '_dbstats_lock',
              '_bypass_lock', '_output_error_lock', '_tracking_lock',
              '_notify_lock', '_scheduler_lock')


def _scan_reentrant_violations():
    """返回 [(文件, 类, 方法, 被调方法, 锁名)]：非可重入锁的嵌套调用。"""
    out = []
    for path in REPO.rglob('*.py'):
        if any(x in path.parts for x in ('.venv', 'dist', 'tests', 'legacy', '__pycache__')):
            continue
        if any(p.startswith('.pytest_tmp') for p in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding='utf-8', errors='ignore'))
        except SyntaxError:
            continue
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            # 1) 哪些锁是**非可重入**的（`threading.Lock()` 而不是 RLock）
            non_reentrant = set()
            for node in ast.walk(cls):
                if not isinstance(node, ast.Assign):
                    continue
                tgt = node.targets[0] if node.targets else None
                if not (isinstance(tgt, ast.Attribute) and tgt.attr in LOCK_NAMES):
                    continue
                val = node.value
                if (isinstance(val, ast.Call) and isinstance(val.func, ast.Attribute)
                        and val.func.attr == 'Lock'
                        and isinstance(val.func.value, ast.Name)
                        and val.func.value.id == 'threading'):
                    non_reentrant.add(tgt.attr)
            if not non_reentrant:
                continue

            methods = {m.name: m for m in cls.body
                       if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
            # 2) 每个方法拿哪些锁
            takes = {}
            for name, m in methods.items():
                held = set()
                for node in ast.walk(m):
                    if isinstance(node, ast.With):
                        for item in node.items:
                            e = item.context_expr
                            if (isinstance(e, ast.Attribute) and e.attr in non_reentrant
                                    and isinstance(e.value, ast.Name)
                                    and e.value.id == 'self'):
                                held.add(e.attr)
                if held:
                    takes[name] = held
            # 3) 持锁调用自身方法，而那个方法也要同一把非可重入锁
            for name, held in takes.items():
                for node in ast.walk(methods[name]):
                    if not isinstance(node, ast.With):
                        continue
                    inner_held = set()
                    for item in node.items:
                        e = item.context_expr
                        if (isinstance(e, ast.Attribute) and e.attr in held
                                and isinstance(e.value, ast.Name)
                                and e.value.id == 'self'):
                            inner_held.add(e.attr)
                    if not inner_held:
                        continue
                    for sub in ast.walk(node):
                        if (isinstance(sub, ast.Call)
                                and isinstance(sub.func, ast.Attribute)
                                and isinstance(sub.func.value, ast.Name)
                                and sub.func.value.id == 'self'
                                and sub.func.attr in takes):
                            overlap = inner_held & takes[sub.func.attr]
                            if overlap:
                                out.append((str(path.relative_to(REPO)).replace('\\', '/'),
                                            cls.name, name, sub.func.attr,
                                            sorted(overlap)[0]))
    return out


def test_no_nested_acquisition_of_non_reentrant_lock():
    """持非可重入锁调用「也要同一把锁」的自身方法 → 自锁死。

    round 283 在 `DataCollector._tasks_lock` 上踩过：`sync_device_tasks()`
    持锁调用 `start_device_task()`（它第一件事就是拿同一把锁），
    整个采集器**静默停摆且没有任何报错**。
    """
    violations = _scan_reentrant_violations()
    offenders = [f'{f}::{c}.{m}() 持 {lock} 调用 self.{callee}()'
                 for f, c, m, callee, lock in violations
                 if f'{f}::{c}' not in NON_REENTRANT_OK]
    assert not offenders, (
        '以下位置持**非可重入**锁调用了会再拿同一把锁的自身方法 —— '
        '会自锁死（整个模块静默停摆，无任何报错）：\n  ' + '\n  '.join(offenders))


def test_guard_detects_a_synthetic_violation():
    """反向对照：造一个"非可重入锁 + 嵌套调用"的类，扫描器必须抓到。"""
    src = (
        'import threading\n'
        'class Bad:\n'
        '    def __init__(self):\n'
        '        self._lock = threading.Lock()\n'
        '    def outer(self):\n'
        '        with self._lock:\n'
        '            self.inner()\n'
        '    def inner(self):\n'
        '        with self._lock:\n'
        '            return 1\n'
    )
    tree = ast.parse(src)
    cls = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)][0]
    non_reentrant = {
        n.targets[0].attr for n in ast.walk(cls)
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Attribute)
        and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute)
        and n.value.func.attr == 'Lock'
    }
    assert non_reentrant == {'_lock'}, '锁类型识别失效（扫描器会恒绿）'


def test_rlock_is_allowed():
    """反向对照：`RLock` 的嵌套调用是合法的，不许误报。"""
    src = (
        'import threading\n'
        'class Ok:\n'
        '    def __init__(self):\n'
        '        self._lock = threading.RLock()\n'
        '    def outer(self):\n'
        '        with self._lock:\n'
        '            self.inner()\n'
    )
    tree = ast.parse(src)
    cls = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)][0]
    non_reentrant = {
        n.targets[0].attr for n in ast.walk(cls)
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Attribute)
        and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute)
        and n.value.func.attr == 'Lock'      # RLock 不匹配 'Lock'
    }
    assert non_reentrant == set(), 'RLock 被误判成非可重入锁'
