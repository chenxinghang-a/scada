"""Modbus 从站地址解析（round 334）。

缺陷
----
9 个读写方法（`read_holding_registers` / `read_input_registers` /
`read_coils` / `read_discrete_inputs` / `read_write_multiple_registers` /
`write_single_register` / `write_single_coil` / `write_multiple_registers` /
`write_multiple_coils`）原来都写：

    slave = slave_id or self.slave_id

**`slave_id=0` 是假值**，会被静默替换成 `self.slave_id`（默认 1）。
而 Modbus 地址 0 是**广播地址**：

  · 本意「广播给总线上所有从站」→ 实际只发给了 1 号（单播，会真的执行）；
  · 本意「发给 0 号」→ 也是发给 1 号。

两种情况都是**打错设备**，而且没有任何日志。工控现场一个从站号错位，
就是操作了另一台设备。

修法
----
抽 `_resolve_slave(slave_id, allow_broadcast=...)`：
  - 只有 `None` 才回退到设备默认值；
  - 显式传 0 就保留 0（写操作允许广播并记 warning，读操作拒绝 —— 广播读没有从站应答）；
  - bool / 非整数 / 超出 0-247 一律返回 None，由调用方明确失败（读返回 None、写返回 False）。

⚠️ 为什么返回 None 而不是抛异常：`slave` 的赋值在 `try` **之外**，
抛出去就是 500；而挪进 `try` 又会被捕获后走「返回陈旧缓存」——
那等于用**别的从站**的数据冒充，比失败更糟。
"""

from unittest.mock import MagicMock

import pytest

from 采集层.modbus_client import ModbusClient


def _client(slave_id=1):
    """已"连接"的客户端，底层换成记录调用的替身。"""
    c = ModbusClient({
        'id': 'dev_slave',
        'name': '从站地址测试',
        'protocol': 'modbus_tcp',
        'host': '127.0.0.1',
        'port': 502,
        'slave_id': slave_id,
        'registers': [{'address': 10, 'name': 'r10', 'scale': 1.0,
                       'offset': 0, 'access': 'rw'}],
    })
    c.client = MagicMock()
    c.connected = True
    c.reconnect = lambda: False

    ok = MagicMock()
    ok.isError.return_value = False
    ok.registers = [0]
    c.client.read_holding_registers.return_value = ok
    c.client.read_input_registers.return_value = ok
    c.client.write_register.return_value = ok
    c.client.write_coil.return_value = ok
    return c


def _slave_passed(mock_method) -> int | None:
    """取出底层调用里传的 slave 值。"""
    assert mock_method.called, '底层方法没被调用'
    kwargs = mock_method.call_args.kwargs
    if 'slave' in kwargs:
        return kwargs['slave']
    return mock_method.call_args.args[2] if len(mock_method.call_args.args) > 2 else None


class TestReadRejectsBroadcast:
    """读操作不能广播 —— 没有从站会应答，只会白等到超时。"""

    def test_read_holding_zero_is_rejected(self):
        c = _client(slave_id=1)
        assert c.read_holding_registers(10, 1, slave_id=0) is None
        c.client.read_holding_registers.assert_not_called()

    def test_read_input_zero_is_rejected(self):
        c = _client()
        assert c.read_input_registers(10, 1, slave_id=0) is None
        c.client.read_input_registers.assert_not_called()

    def test_read_coils_zero_is_rejected(self):
        c = _client()
        c.client.read_coils.return_value = MagicMock(
            isError=lambda: False, bits=[0])
        assert c.read_coils(10, 1, slave_id=0) is None
        c.client.read_coils.assert_not_called()


class TestWriteAllowsBroadcastButWarns:
    """写操作允许广播（协议合法），但必须真的发 0、不能变成默认值。"""

    def test_write_single_register_zero_keeps_zero(self):
        """**核心回归**：0 不能被替换成 self.slave_id（默认 1）。"""
        c = _client(slave_id=1)
        c.write_single_register(10, 5, slave_id=0)
        assert _slave_passed(c.client.write_register) == 0, (
            'slave_id=0 被静默替换成了默认从站 —— 广播变成了单播写 1 号')

    def test_write_single_coil_zero_keeps_zero(self):
        c = _client(slave_id=1)
        c.write_single_coil(10, True, slave_id=0)
        assert _slave_passed(c.client.write_coil) == 0


class TestNormalResolution:
    def test_none_uses_device_default(self):
        c = _client(slave_id=7)
        c.read_holding_registers(10, 1)
        assert _slave_passed(c.client.read_holding_registers) == 7

    def test_explicit_value_is_passed_through(self):
        c = _client(slave_id=1)
        c.read_holding_registers(10, 1, slave_id=5)
        assert _slave_passed(c.client.read_holding_registers) == 5

    def test_write_default_still_works(self):
        """反向对照：不传 slave_id 时写操作照常（别修过头）。"""
        c = _client(slave_id=3)
        assert c.write_single_register(10, 5) is True
        assert _slave_passed(c.client.write_register) == 3


class TestInvalidSlaveRejected:
    """bool / 非整数 / 越界 → 明确失败，不能悄悄用默认值。"""

    @pytest.mark.parametrize('bad', [True, False, 'abc', 1.5, -1, 248, 999])
    def test_read_with_invalid_slave_returns_none(self, bad):
        c = _client(slave_id=1)
        assert c.read_holding_registers(10, 1, slave_id=bad) is None
        c.client.read_holding_registers.assert_not_called()

    @pytest.mark.parametrize('bad', [True, 'abc', -1, 248])
    def test_write_with_invalid_slave_returns_false(self, bad):
        c = _client(slave_id=1)
        assert c.write_single_register(10, 5, slave_id=bad) is False
        c.client.write_register.assert_not_called()

    def test_bool_true_is_not_slave_1(self):
        """`slave_id=True` 不能因为 bool 是 int 子类就变成 1 号从站。"""
        c = _client(slave_id=1)
        assert c.read_holding_registers(10, 1, slave_id=True) is None
        c.client.read_holding_registers.assert_not_called()


class TestNoLegacyPatternRemains:
    """静态守卫：`slave_id or self.slave_id` 这种写法不许再出现。

    它是「0 被静默替换」的根源，而 0 在 Modbus 里是有语义的（广播）。
    任何 `x or default` 形式的「缺省值」判断，只要 0 是合法值就是 bug。
    """

    def test_no_truthiness_fallback_in_source(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[1]
               / '采集层' / 'modbus_client.py').read_text(encoding='utf-8')
        # 去掉注释与 docstring 里的引用，只看真正的赋值语句
        bad = [ln.strip() for ln in src.splitlines()
               if ln.strip().startswith('slave = slave_id or')]
        assert not bad, f'仍有真值回退写法: {bad}'
