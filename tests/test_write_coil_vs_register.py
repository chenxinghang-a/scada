# -*- coding: utf-8 -*-
"""写线圈不能写成寄存器；回读必须读同一种对象（round 234）。

缺陷
----
`POST /api/control/<id>/write-coil` 走安全模块时调用
`device_control.write_with_verification(device_id, address, 0/1, operator)`，
而该方法**没有 kind 参数**，内部按 `hasattr(client, 'write_single_register')`
优先判断 —— 所有 Modbus 客户端**同时**具备
`write_single_register` 和 `write_single_coil`，于是：

* **「写线圈」实际写进了同一地址的保持寄存器**。
  寄存器 0 和线圈 0 在现场是两个完全不同的对象（一个数据字 vs 一个开关位）：
  操作员以为按下了启动位，实际往数据寄存器里写了个 1。

* **回读验证读的也是保持寄存器**（不管写的是线圈还是寄存器），
  等于校验了另一个物理对象 —— 因为刚写的就是那个寄存器，所以
  「回读一致」轻松通过，**给出完全错误的信心**。

* 回读异常被 `except` 吞掉，`readback_ok` 保持 True → 通信断了也报「写入成功」。

* 寄存器比对用 `abs(diff) > 1` 的容差 → 写 100 读到 99 也算通过。

修法
----
1. `write_with_verification(..., kind='register'|'coil')`，由调用方明确指定；
2. 写入按 kind 走对应方法；客户端不支持时**明确报错**，不再静默失败；
3. 回读读**同一种对象**；
4. 拿不到回读值（不支持/异常）→ `verified=None`（**未验证**），不再当通过；
5. 寄存器按**精确值**比对；返回值新增 `verified` 字段。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class _FakeClient:
    """可观测的假客户端：记录调用了哪个方法、读回来什么值。"""

    def __init__(self, reg_value=0, coil_value=False, fail_read=False):
        self.calls: list[tuple] = []
        self.connected = True
        self._reg_value = reg_value
        self._coil_value = coil_value
        self._fail_read = fail_read

    # ---- 写 ----
    def write_single_register(self, address, value, slave_id=None):
        self.calls.append(('write_register', address, value))
        return True

    def write_single_coil(self, address, value, slave_id=None):
        self.calls.append(('write_coil', address, value))
        return True

    # ---- 读 ----
    def read_holding_registers(self, address, count=1, slave_id=None):
        self.calls.append(('read_registers', address))
        if self._fail_read:
            raise OSError('模拟通信中断')
        return [self._reg_value]

    def read_coils(self, address, count=1, slave_id=None):
        self.calls.append(('read_coils', address))
        if self._fail_read:
            raise OSError('模拟通信中断')
        return [self._coil_value]


def _dc(client):
    from 智能层.device_control import DeviceControlSafety

    dm = MagicMock()
    dm.simulation_mode = True
    dm.devices = {}
    dm.get_all_devices.return_value = {}
    dm.get_client.return_value = client
    dc = DeviceControlSafety(MagicMock(), device_manager=dm)
    dc._audit_file = MagicMock()          # 不往仓库里写审计文件
    return dc


def _methods(client):
    return [c[0] for c in client.calls]


# ---------------------------------------------------------------------------
# 1. 写线圈必须调 write_single_coil，不能落到寄存器
# ---------------------------------------------------------------------------

def test_coil_write_uses_coil_api_not_register():
    client = _FakeClient(coil_value=True)
    dc = _dc(client)

    res = dc.write_with_verification('dev1', 0, 1, 'op', kind='coil')

    assert res['success'] is True, res
    assert 'write_coil' in _methods(client), (
        f'写线圈没有调用 write_single_coil，实际调用: {client.calls}'
    )
    assert 'write_register' not in _methods(client), (
        f'写线圈居然调用了 write_single_register —— 写进了数据寄存器: {client.calls}'
    )


def test_register_write_uses_register_api():
    client = _FakeClient(reg_value=100)
    dc = _dc(client)

    res = dc.write_with_verification('dev1', 5, 100, 'op')      # 默认 kind='register'

    assert res['success'] is True
    assert 'write_register' in _methods(client)
    assert 'write_coil' not in _methods(client)


# ---------------------------------------------------------------------------
# 2. 回读必须读同一种对象
# ---------------------------------------------------------------------------

def test_coil_readback_reads_coils():
    client = _FakeClient(coil_value=True)
    dc = _dc(client)
    dc.write_with_verification('dev1', 3, 1, 'op', kind='coil')

    assert 'read_coils' in _methods(client), (
        f'写线圈后没有读线圈，实际: {client.calls}'
    )
    assert 'read_registers' not in _methods(client), (
        '写线圈后去读保持寄存器 —— 校验的是另一个物理对象，结论无意义'
    )


def test_register_readback_reads_registers():
    client = _FakeClient(reg_value=42)
    dc = _dc(client)
    dc.write_with_verification('dev1', 3, 42, 'op')

    assert 'read_registers' in _methods(client)
    assert 'read_coils' not in _methods(client)


# ---------------------------------------------------------------------------
# 3. 回读结果语义：True / False / None
# ---------------------------------------------------------------------------

def test_verified_true_on_match():
    dc = _dc(_FakeClient(reg_value=100))
    res = dc.write_with_verification('dev1', 1, 100, 'op')
    assert res['verified'] is True
    assert res['warnings'] == []


def test_verified_false_on_mismatch():
    dc = _dc(_FakeClient(reg_value=90))
    res = dc.write_with_verification('dev1', 1, 100, 'op')
    assert res['verified'] is False
    assert '回读验证失败' in res['message']
    assert res['warnings'], '验证失败必须带 warning'


def test_register_readback_is_exact_not_tolerance():
    """写 100 读到 99 必须算验证失败（原来容差 ±1 会放过）。"""
    dc = _dc(_FakeClient(reg_value=99))
    res = dc.write_with_verification('dev1', 1, 100, 'op')
    assert res['verified'] is False, (
        '写 100 读到 99 被判为验证通过 —— 寄存器写入应当精确比对'
    )


def test_readback_failure_is_unverified_not_success():
    """回读异常（通信断了）不能算「验证通过」。"""
    dc = _dc(_FakeClient(fail_read=True))
    res = dc.write_with_verification('dev1', 1, 100, 'op')

    assert res['success'] is True, '写入本身是成功的'
    assert res['verified'] is None, (
        f"回读失败却给出 verified={res['verified']!r} —— 未验证不能当通过"
    )
    assert '无法回读验证' in res['message']
    assert res['warnings']


# ---------------------------------------------------------------------------
# 4. API 层：写线圈端点必须把 kind 传下去
# ---------------------------------------------------------------------------

def test_api_write_coil_passes_kind(client, auth_headers):
    dc = MagicMock()
    dc.write_with_verification.return_value = {'success': True, 'message': 'ok'}
    client.application.device_control = dc

    resp = client.post(
        '/api/devices/dev1/write-coil',
        json={'address': 7, 'value': True},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)[:200]

    kwargs = dc.write_with_verification.call_args.kwargs
    assert kwargs.get('kind') == 'coil', (
        f"写线圈端点没有传 kind='coil'（实际 {kwargs}）—— "
        '会落到寄存器写入分支，操作员按的是数据字不是开关位'
    )


def test_api_write_register_does_not_force_coil(client, auth_headers):
    dc = MagicMock()
    dc.write_with_verification.return_value = {'success': True, 'message': 'ok'}
    client.application.device_control = dc

    client.post('/api/devices/dev1/write-register',
                json={'address': 7, 'value': 42}, headers=auth_headers)

    kwargs = dc.write_with_verification.call_args.kwargs
    assert kwargs.get('kind', 'register') == 'register'
