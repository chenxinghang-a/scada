"""连续写多寄存器：不支持时退化为逐字写，且必须如实反映成败。

## 为什么补

`tools/audit_untested.py` 列出 `_write_registers_compat` 在三份管理器里
**都零引用**。它的职责是：客户端没有 `write_multiple_registers`（或只抛
`NotImplementedError`）时，退化成逐个 `write_single_register` 写。

三个地方各写了一份同名实现：

    采集层/real_device_manager.py
    采集层/simulated_device_manager.py
    智能层/device_control.py

典型的「平行实现」——改一份漏两份的高发区（前面 SF-07 就栽在这上面）。

## 钉什么

  · 有能力时**直接用**，不走退化路径
  · 抛 NotImplementedError 时退化，且地址**递增**
  · 退化路径里只要有一个字失败，整体就是 False（不能"写了 3 个算成功"）
  · 返回值必须是 bool，不能是 None（调用方 `if not ok:` 会把 None 当失败，
    但 `ok and ...` 链里 None 会让整条链变成 None）
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from 采集层.real_device_manager import RealDeviceManager
from 采集层.simulated_device_manager import SimulatedDeviceManager
from 智能层.device_control import DeviceControlSafety


def _fn_holders():
    """三份实现的 `_write_registers_compat`（都是 staticmethod）。"""
    return [
        pytest.param(RealDeviceManager._write_registers_compat, id='real'),
        pytest.param(SimulatedDeviceManager._write_registers_compat, id='sim'),
        pytest.param(DeviceControlSafety._write_registers_compat, id='control'),
    ]


@pytest.mark.parametrize('fn', _fn_holders())
class TestMultiWriteCompat:
    def test_uses_multi_when_available(self, fn):
        client = MagicMock()
        client.write_multiple_registers.return_value = True
        assert fn(client, 100, [1, 2, 3]) is True
        client.write_multiple_registers.assert_called_once()
        client.write_single_register.assert_not_called()

    def test_multi_returning_false_is_false(self, fn):
        client = MagicMock()
        client.write_multiple_registers.return_value = False
        assert fn(client, 100, [1, 2, 3]) is False

    def test_falls_back_on_not_implemented(self, fn):
        client = MagicMock()
        client.write_multiple_registers.side_effect = NotImplementedError
        client.write_single_register.return_value = True

        assert fn(client, 200, [7, 8]) is True
        calls = [c.args for c in client.write_single_register.call_args_list]
        assert calls == [(200, 7), (201, 8)], f'退化路径必须地址递增，实际 {calls}'

    def test_fallback_partial_failure_is_false(self, fn):
        """两字里第二个写失败 → 整体必须 False，不能"写了一个算成功"。"""
        client = MagicMock()
        client.write_multiple_registers.side_effect = NotImplementedError
        client.write_single_register.side_effect = [True, False]

        assert fn(client, 300, [1, 2]) is False

    def test_fallback_exception_is_not_swallowed_as_success(self, fn):
        """写的时候抛异常 → 不该被吞成 True。"""
        client = MagicMock()
        client.write_multiple_registers.side_effect = NotImplementedError
        client.write_single_register.side_effect = [True, RuntimeError('boom')]

        with pytest.raises(RuntimeError):
            fn(client, 400, [1, 2])

    def test_returns_real_bool(self, fn):
        client = MagicMock()
        client.write_multiple_registers.return_value = True
        got = fn(client, 500, [1])
        assert isinstance(got, bool)
