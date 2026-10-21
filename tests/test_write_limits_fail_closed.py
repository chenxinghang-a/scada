# -*- coding: utf-8 -*-
"""未配置写入量程的设备：真实模式下必须 fail-closed（round 236）。

缺陷
----
`validate_write()` 第 3 步是 `if limits:` —— **没配量程就整段跳过**：
既不做范围校验，也不给任何提示。

而随仓库发布的 `配置/devices.yaml` 里 `write_limits` 出现次数是 **0**。
也就是说开箱状态下：

    操作员把 100 写成 10000（多打一个 0）→ 校验通过 → 直接下发到现场设备。

联锁那边的口径是「真实设备模式没配规则 → fail-closed 拒绝一切写」，
理由完全一样：**没有量程 = 一条永不触发的保护**。两处口径必须一致，
否则「保护没配」这件事只在联锁上被拦住、在量程上被放行。

修法
----
* 真实设备模式 + 该设备没有任何 `write_limits` → **拒绝写入**，
  并在 reason 里说清去哪里补配置；
* 模拟模式 → 放行（演示要能跑），但在 `warnings` 里明确写出
  「本次写入未做范围校验」，不再静默。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _dc(simulation: bool, devices: dict | None = None, limits: dict | None = None):
    from 智能层.device_control import DeviceControlSafety

    dm = MagicMock()
    dm.simulation_mode = simulation
    dm.devices = devices if devices is not None else {'dev1': {'id': 'dev1'}}
    dm.get_all_devices.return_value = dm.devices
    dm.get_client.return_value = MagicMock(connected=True)

    dc = DeviceControlSafety(MagicMock(), device_manager=dm)
    dc._audit_file = MagicMock()
    if limits is not None:
        dc._write_limits = limits
    # 真实模式要求「至少有一条联锁规则」，否则会先被联锁的 fail-closed 拦住
    # （那条规则与本文件要测的量程无关，这里给一条不触发的占位规则）
    dc._interlock_rules = {'dummy': {'name': '占位', 'action': {'type': 'block_start'},
                                     'condition': {'device_id': 'other'}}}
    return dc


# ---------------------------------------------------------------------------
# 1. 真实模式 + 没配量程 → 拒绝
# ---------------------------------------------------------------------------

def test_real_mode_without_limits_refuses_write():
    dc = _dc(simulation=False, limits={})        # 该设备没有任何量程

    res = dc.validate_write('dev1', 100, 10000, 'op')

    assert res['allowed'] is False, (
        '真实设备模式下、设备没配量程却放行了写入 —— 等于任意值都能下发'
    )
    assert 'write_limits' in res['reason'], f'提示里要说清补哪里: {res["reason"]}'
    assert 'dev1' in res['reason']


def test_real_mode_without_limits_blocks_write_with_verification():
    """端到端：拒绝要发生在写入之前，客户端一次都不该被调用。"""
    dc = _dc(simulation=False, limits={})
    client = dc.device_manager.get_client.return_value

    res = dc.write_with_verification('dev1', 100, 10000, 'op')

    assert res['success'] is False
    client.write_single_register.assert_not_called()
    client.write_single_coil.assert_not_called()


# ---------------------------------------------------------------------------
# 2. 模拟模式 + 没配量程 → 放行但留痕
# ---------------------------------------------------------------------------

def test_simulation_without_limits_allows_but_warns():
    dc = _dc(simulation=True, limits={})

    res = dc.validate_write('dev1', 100, 10000, 'op')

    assert res['allowed'] is True, '模拟模式没有物理风险，不应拒绝（演示要能跑）'
    assert any('write_limits' in w for w in res['warnings']), (
        f'放行了却没有任何提示 —— 又回到「静默无校验」: {res["warnings"]}'
    )


# ---------------------------------------------------------------------------
# 3. 配了量程时行为不变（回归）
# ---------------------------------------------------------------------------

def test_real_mode_with_limits_still_enforces_range():
    dc = _dc(simulation=False, limits={'dev1': {'temperature': (0.0, 100.0)}})
    dc._resolve_register_name = lambda device_id, address: 'temperature'

    ok = dc.validate_write('dev1', 100, 50, 'op')
    assert ok['allowed'] is True

    bad = dc.validate_write('dev1', 100, 150, 'op')
    assert bad['allowed'] is False
    assert '超出安全范围' in bad['reason']


def test_real_mode_register_not_in_whitelist_still_refused():
    dc = _dc(simulation=False, limits={'dev1': {'temperature': (0.0, 100.0)}})
    dc._resolve_register_name = lambda device_id, address: 'pressure'

    res = dc.validate_write('dev1', 100, 50, 'op')
    assert res['allowed'] is False
    assert '白名单' in res['reason']


def test_real_mode_unresolvable_register_still_refused():
    """有量程但地址反查不到寄存器名 → 无法校验 → 拒绝（原有 fail-closed）。"""
    dc = _dc(simulation=False, limits={'dev1': {'temperature': (0.0, 100.0)}})
    dc._resolve_register_name = lambda device_id, address: None

    res = dc.validate_write('dev1', 100, 50, 'op')
    assert res['allowed'] is False
    assert '无法解析' in res['reason']
