# -*- coding: utf-8 -*-
"""写入点位类型必须严格匹配，不允许「线圈写不了就退回写寄存器」（round 235）。

背景
----
round 234 修了 `write_with_verification` 把线圈写成寄存器的问题。同一形状的
**跨类型兜底**还有两处：

* `采集层/real_device_manager._write_control()`：
  `kind == 'coil'` 且客户端没有 `write_single_coil` 时，
  **退回调用 `write_single_register`**；
* `智能层/device_control.batch_control()`：同样的 coil → register 兜底。

寄存器 0 和线圈 0 在现场是**两个不同对象**（一个数据字 vs 一个开关位）。
「写不进去就换个地方写」不是降级，是**写错对象**，而调用方只会看到成功。

实测：只有 Modbus / 模拟客户端实现了这两个方法，而且**两个都实现** ——
跨类型兜底在现有实现里根本打不到，只会成为将来的坑。所以直接删掉：
客户端不支持该类型 → 明确失败（写日志 + 返回失败原因）。

本文件两条腿：
* 行为：reg-only 客户端 + coil 点位 → 必须失败，且**不能**调用寄存器写；
* 静态：源码里不允许出现「coil 分支内调用 write_single_register」（反之亦然）。
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parent.parent


class _RegOnlyClient:
    """只有寄存器写法的客户端（模拟 OPC UA / REST 这类单一对象空间的适配器）。"""

    def __init__(self):
        self.calls: list[tuple] = []
        self.connected = True

    def write_single_register(self, address, value, slave_id=None):
        self.calls.append(('register', address, value))
        return True


class _CoilOnlyClient:
    def __init__(self):
        self.calls: list[tuple] = []
        self.connected = True

    def write_single_coil(self, address, value, slave_id=None):
        self.calls.append(('coil', address, value))
        return True


# ---------------------------------------------------------------------------
# 1. real_device_manager._write_control
# ---------------------------------------------------------------------------

def test_coil_point_refuses_register_fallback():
    """coil 点位 + 只有寄存器写法的客户端 → 失败，且**不能**写寄存器。"""
    from 采集层.real_device_manager import RealDeviceManager

    client = _RegOnlyClient()
    ok = RealDeviceManager._write_control(client, 'coil', 0, 1)

    assert ok is False, '线圈点位在只有寄存器写法的客户端上居然"成功"了'
    assert client.calls == [], (
        f'跨类型兜底把线圈写进了寄存器: {client.calls}'
    )


def test_register_point_refuses_coil_fallback():
    from 采集层.real_device_manager import RealDeviceManager

    client = _CoilOnlyClient()
    ok = RealDeviceManager._write_control(client, 'register', 0, 42)

    assert ok is False
    assert client.calls == [], f'跨类型兜底把寄存器写进了线圈: {client.calls}'


def test_coil_point_uses_coil_on_full_client():
    """回归：两个方法都有的客户端，coil 点位必须走线圈。"""
    from 采集层.real_device_manager import RealDeviceManager

    class _Both(_RegOnlyClient):
        def write_single_coil(self, address, value, slave_id=None):
            self.calls.append(('coil', address, value))
            return True

    client = _Both()
    assert RealDeviceManager._write_control(client, 'coil', 5, True) is True
    assert client.calls == [('coil', 5, True)]


# ---------------------------------------------------------------------------
# 2. batch_control
# ---------------------------------------------------------------------------

def test_batch_control_reports_unsupported_instead_of_writing_elsewhere():
    from 智能层.device_control import DeviceControlSafety

    client = _RegOnlyClient()
    dm = MagicMock()
    dm.simulation_mode = True
    dm.devices = {'dev1': {'id': 'dev1'}}
    dm.get_all_devices.return_value = {'dev1': {'id': 'dev1'}}
    dm.get_client.return_value = client

    dc = DeviceControlSafety(MagicMock(), device_manager=dm)
    dc._audit_file = MagicMock()
    dc._get_controllable_devices = lambda: ['dev1']
    dc._resolve_control_point = lambda device_id, action: {
        'type': 'coil', 'address': 0, 'value': 1,
    }

    res = dc.batch_control('start', 'op')

    assert res['results']['dev1']['success'] is False
    assert '不支持线圈' in res['results']['dev1']['message']
    assert client.calls == [], f'批量启动把线圈写进了寄存器: {client.calls}'


# ---------------------------------------------------------------------------
# 3. 静态闸门：coil 分支里不许出现寄存器写（反之亦然）
# ---------------------------------------------------------------------------

PROD_DIRS = ('采集层', '存储层', '展示层', '报警层', '智能层', '用户层', 'core')


def _iter_prod_files():
    for d in PROD_DIRS:
        root = REPO / d
        if not root.exists():
            continue
        for p in root.rglob('*.py'):
            if '__pycache__' in p.parts:
                continue
            yield p


def _called_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def scan_cross_kind_fallback(src: str, rel: str = '<mem>') -> list[str]:
    """在「提到 coil 的分支」里找 `write_single_register` 调用（反之亦然）。"""
    tree = ast.parse(src)
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        cond = ast.unparse(node.test).lower()
        # 判断这个 if 分支在讲哪种点位
        if 'coil' in cond and 'register' not in cond:
            bad = 'write_single_register'
        elif 'register' in cond and 'coil' not in cond:
            bad = 'write_single_coil'
        else:
            continue
        for inner in node.body:          # 只看本分支体（不看 else）
            for sub in ast.walk(inner):
                if _called_name(sub) == bad:
                    hits.append(
                        f'{rel}:{getattr(sub, "lineno", 0)} 在 `{ast.unparse(node.test)}` '
                        f'分支里调用了 {bad}() —— 跨类型写，等于写错对象'
                    )
    return hits


def test_no_cross_kind_write_fallback():
    hits: list[str] = []
    for path in _iter_prod_files():
        rel = path.relative_to(REPO).as_posix()
        try:
            hits.extend(scan_cross_kind_fallback(
                path.read_text(encoding='utf-8', errors='replace'), rel))
        except SyntaxError:
            continue

    assert not hits, (
        '以下位置在「线圈/寄存器」分支里写了**另一种**对象：\n'
        '线圈和寄存器在现场是两个不同对象，退回写等于写错地方，'
        '而调用方只会看到成功。客户端不支持就明确失败。\n\n'
        + '\n'.join('  ' + h for h in hits)
    )


def test_guard_catches_the_real_shape():
    """元守卫：闸门必须能认出 round 234/235 那个形状。"""
    bad = '''
def write_control(client, kind, address, value):
    if kind == 'coil':
        if hasattr(client, 'write_single_coil'):
            return client.write_single_coil(address, value)
        if hasattr(client, 'write_single_register'):
            return client.write_single_register(address, int(value))
    return False
'''
    hits = scan_cross_kind_fallback(bad, 'demo.py')
    assert hits, '闸门认不出「coil 分支里退回写寄存器」'
    assert 'write_single_register' in hits[0]


def test_guard_allows_correct_shape():
    ok = '''
def write_control(client, kind, address, value):
    if kind == 'coil':
        return client.write_single_coil(address, bool(value))
    if kind == 'register':
        return client.write_single_register(address, int(value))
    return False
'''
    assert scan_cross_kind_fallback(ok, 'demo.py') == []
