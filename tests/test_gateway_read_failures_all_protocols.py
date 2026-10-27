# -*- coding: utf-8 -*-
"""读取失败记账必须覆盖**所有**网关协议，且有守卫防新网关漏接（round 295）。

背景
----
round 295 前半（wb 交接时已完成）只接了 `modbus_gateway` 与 `s7_gateway`。
本仓还有 `dnp3` / `iec104` / `opcua` 三个网关，它们的 `read_device_data`
同样有"读不到就跳过/返回 None"的路径，但**一处都没记账** ——

    client = self._clients.get(device_id)
    if not client or not client.connected:
        self.logger.error(f"设备 {device_id} 未连接")
        return None                       # ← stats 里什么都不涨

    data = client.read_data()
    if not data:
        return None                       # ← 连日志都没有

后果与 modbus 那条一样：`/stats` 上 `read_failures=0`、`errors=0`，
网关看起来完全健康，而实际上这台设备一个点都没读到。

判据
----
**"读不到"是必须计数的事件**，与协议无关。哪个网关漏了，
运维就会在那一类设备上失去唯一的观测点。

本文件钉两层：
1. 三个协议各一条行为用例（未连接 / 无节点配置 → `read_failures` 涨）；
2. **结构性守卫**：扫 `gateway/*_gateway.py` 的 AST，凡是
   `read_device_data*` 方法体内出现 `return None`，就必须同时出现
   `_note_read_failure(` —— 否则新加的网关会再次静默。

⚠️ 守卫用 AST 而不是字符串匹配：字符串切片会被"函数后面另一个同名调用"
骗过（wb 在 293 踩过这个坑，我在 295 也记一笔）。
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

GATEWAY_DIR = Path(__file__).resolve().parents[1] / 'gateway'


# ---------------------------------------------------------------------------
# 行为：三个协议都要记账
# ---------------------------------------------------------------------------

def _base_stats(gw):
    return gw.stats['read_failures']


@pytest.mark.parametrize('module,cls_name', [
    ('gateway.dnp3_gateway', 'DNP3Gateway'),
    ('gateway.iec104_gateway', 'IEC104Gateway'),
])
def test_disconnected_device_counts_read_failure(module, cls_name):
    """设备未连接时读取返回 None —— 必须计数。"""
    mod = __import__(module, fromlist=[cls_name])
    cls = getattr(mod, cls_name)
    gw = cls({'gateway_id': 'gw_x', 'devices': [], 'poll_interval': 0.01})

    assert _base_stats(gw) == 0, '前提不成立：初始 read_failures 不是 0'
    assert gw.read_device_data('nope') is None

    assert _base_stats(gw) == 1, (
        f'{cls_name}：未连接时读取失败没有记账 —— '
        f'/stats 上这台设备"看起来健康"')
    assert '未连接' in (gw.stats['last_read_error'] or '')
    assert gw.stats['last_read_error_time'] is not None


@pytest.mark.parametrize('module,cls_name', [
    ('gateway.dnp3_gateway', 'DNP3Gateway'),
    ('gateway.iec104_gateway', 'IEC104Gateway'),
])
def test_empty_read_counts_read_failure(module, cls_name):
    """连上了但 `client.read_data()` 返回空 —— 同样必须计数。"""
    mod = __import__(module, fromlist=[cls_name])
    cls = getattr(mod, cls_name)
    gw = cls({'gateway_id': 'gw_x', 'devices': [], 'poll_interval': 0.01})

    client = MagicMock()
    client.connected = True
    client.read_data.return_value = {}
    gw._clients['d1'] = client

    assert gw.read_device_data('d1') is None
    assert _base_stats(gw) == 1, f'{cls_name}：空读取没有记账'
    assert '返回空' in (gw.stats['last_read_error'] or '')


def test_opcua_without_node_config_counts_read_failure():
    """OPC UA 设备配了但没节点配置 —— 一个点都读不到，必须记账。"""
    from gateway.opcua_gateway import OPCUAGateway

    gw = OPCUAGateway({'gateway_id': 'gw_ua', 'devices': [], 'poll_interval': 0.01})
    gw._clients['d1'] = MagicMock()          # 有 client，但 _node_configs 为空

    assert gw.read_device_data('d1') is None
    assert _base_stats(gw) == 1, 'OPC UA 无节点配置时没有记账'


# ---------------------------------------------------------------------------
# 守卫：新网关不许再漏
# ---------------------------------------------------------------------------

def _read_methods(tree: ast.AST):
    """找出所有名字以 read_device_data 开头的方法定义。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith('read_device_data'):
                yield node


def _count_note_calls(node: ast.AST) -> int:
    n = 0
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Attribute) and f.attr == '_note_read_failure':
                n += 1
    return n


def _count_returns_none(node: ast.AST) -> int:
    """方法体内 `return None` 的个数。

    ⚠️ `return None` 在 AST 里是 `Return(value=Constant(None))`，
    **不是** `Return(value=None)`（那是裸 `return`）。
    第一版守卫只判了后者，于是对真实代码恒为假 —— 一条恒绿的空守卫。
    是下面那条反向对照把它抓出来的。
    """
    n = 0
    for sub in ast.walk(node):
        if isinstance(sub, ast.Return):
            v = sub.value
            if v is None:
                n += 1
            elif isinstance(v, ast.Constant) and v.value is None:
                n += 1
    return n


def _violations(tree: ast.AST) -> list[str]:
    """每个失败分支（`return None`）都必须配一次记账。

    只判"方法里有没有记账"是不够的 —— 一个方法有 3 处 `return None`、
    只接了 1 处，守卫照样通过（第一版就是这样，变异 A 直接存活）。
    所以按**数量**比对：`_note_read_failure` 调用数必须 ≥ `return None` 个数。
    """
    bad = []
    for fn in _read_methods(tree):
        returns = _count_returns_none(fn)
        notes = _count_note_calls(fn)
        if returns and notes < returns:
            bad.append(f'{fn.name}（return None {returns} 处 / 记账 {notes} 处）')
    return bad


def test_every_gateway_read_path_records_failures():
    """凡是 `read_device_data*` 里有 `return None` 的网关，必须**逐分支**记账。"""
    offenders = []
    for path in sorted(GATEWAY_DIR.glob('*_gateway.py')):
        if path.name == 'base_gateway.py':
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for bad in _violations(tree):
            offenders.append(f'{path.name}::{bad}')

    assert not offenders, (
        '以下读取方法的失败分支（return None）没有逐条记账 —— '
        '这些协议的设备"读不到"在 /stats 上完全看不出来：\n  '
        + '\n  '.join(offenders))


def test_guard_catches_method_without_any_call(tmp_path):
    """反向对照①：一处记账都没有 → 必须被抓。"""
    src = (
        'class G:\n'
        '    def read_device_data(self, d):\n'
        '        if not d:\n'
        '            return None\n'
        '        return {}\n'
    )
    assert _violations(ast.parse(src)) == ['read_device_data（return None 1 处 / 记账 0 处）'], (
        '守卫抓不到完全没记账的读取方法')


def test_guard_catches_partially_wired_method():
    """反向对照②：**接了 1 处、漏了 1 处**也必须被抓。

    这是真实发生过的形态（变异 A）：方法里已经有一处 `_note_read_failure`，
    只判"有没有调用"的守卫会放行，漏掉的那条失败分支就永远静默。
    """
    src = (
        'class G:\n'
        '    def read_device_data(self, d):\n'
        '        if not d:\n'
        '            self._note_read_failure(d, "*", "未连接")\n'
        '            return None\n'
        '        if not d.read():\n'
        '            return None\n'
        '        return {}\n'
    )
    bad = _violations(ast.parse(src))
    assert bad and 'return None 2 处 / 记账 1 处' in bad[0], (
        f'守卫对"只接了一半"免疫 —— 漏掉的分支会继续静默（实际: {bad}）')


def test_guard_passes_when_fully_wired():
    """全接上之后必须放行（防假阳性）。"""
    src = (
        'class G:\n'
        '    def read_device_data(self, d):\n'
        '        if not d:\n'
        '            self._note_read_failure(d, "*", "未连接")\n'
        '            return None\n'
        '        if not d.read():\n'
        '            self._note_read_failure(d, "*", "空")\n'
        '            return None\n'
        '        return {}\n'
    )
    assert _violations(ast.parse(src)) == [], '全接上后守卫仍报错（假阳性）'
