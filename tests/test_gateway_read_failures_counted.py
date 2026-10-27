# -*- coding: utf-8 -*-
"""单点读取失败必须计入 stats（round 295）。

缺陷形态（同一族：**失败静默**，这次是"读数"这条通道）
----------------------------------------------------
轮询型网关是一个寄存器一个寄存器地读，失败的**逐个跳过**：

    value = self._read_register(...)
    if value is not None:
        result[name] = value
    else:
        self.logger.warning(f"设备 {device_id} 寄存器 {name} 读取失败")   # ← 仅此而已
    except Exception as e:
        self.logger.error(f"... 读取异常: {e}")                            # ← 也只打日志

于是：一台设备的 10 个点里 9 个读不到时，
* `stats['errors']` 仍然是 **0** —— `/stats` 上看起来完全健康；
* 已发布的遥测里只剩那 1 个点，而且质量码是 `GOOD`；
* 唯一痕迹是日志里 9 行 warning，谁也不会去数。

判据：**"读不到"和"没这个点"是两件事**，前者必须计数并可回溯到具体点位。

测试策略：直接构造 Modbus 网关，注入假客户端，让其中一个寄存器返回错误响应、
另一个正常返回，然后检查遥测结果 + stats。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gateway.modbus_gateway import ModbusGateway


def _gw():
    gw = ModbusGateway({
        'gateway_id': 'gw_read',
        'devices': [{'device_id': 'd1', 'slave_id': 1}],
        'poll_interval': 0.01,
    })
    # 两个寄存器：good 正常，bad 返回错误
    gw._register_configs['d1'] = [
        {'name': 'good', 'address': 0, 'count': 1, 'type': 'uint16'},
        {'name': 'bad', 'address': 1, 'count': 1, 'type': 'uint16'},
    ]
    gw._byte_orders['d1'] = None
    client = MagicMock()
    gw._clients['d1'] = client
    gw.connected_devices['d1'] = True
    return gw, client


def _resp(registers=None, error=False):
    r = MagicMock()
    r.isError.return_value = error
    r.registers = registers or []
    return r


# ---------------------------------------------------------------------------
# 1. 失败必须计数
# ---------------------------------------------------------------------------

def test_single_register_failure_is_counted():
    gw, client = _gw()
    client.read_holding_registers.side_effect = [_resp([42]), _resp(error=True)]

    data = gw.read_device_data('d1')

    assert data == {'good': 42.0}, '前提不成立：应该只有一个点读成功'
    assert gw.stats['read_failures'] == 1, (
        '单点读取失败了，但 stats 里没有任何记录 —— '
        '/stats 会显示 errors=0，运维看不出"有一半的点读不到"')
    assert 'd1/bad' in (gw.stats['last_read_error'] or '')
    assert gw.stats['last_read_error_time'] is not None


def test_read_exception_is_counted_too():
    """底层抛异常时也必须计一次失败。

    ⚠️ 注意异常是在 `_read_register()` 内部被吞掉的（它 catch 后 `return None`），
    所以外层看到的是"读取返回空"——原因文本因此是通用文案，不是异常原文。
    本用例钉的是**必须计数**这件事，不是文案；顺手记一笔：
    `_read_register` 里的 `except Exception` 把失败原因吃掉了，
    若以后需要"按原因分类（超时/CRC/非法地址）"，得让它把原因带出来。
    """
    gw, client = _gw()
    client.read_holding_registers.side_effect = [_resp([1]), RuntimeError('超时')]

    gw.read_device_data('d1')

    assert gw.stats['read_failures'] == 1, '底层异常导致该点没读到，但计数仍是 0'
    assert 'd1/bad' in (gw.stats['last_read_error'] or '')


def test_all_registers_failing_is_counted_per_point():
    """全部读挂时要有 N 条失败记录，而不是一条"设备读取失败"。"""
    gw, client = _gw()
    gw._register_configs['d1'] = [
        {'name': f'r{i}', 'address': i, 'count': 1, 'type': 'uint16'} for i in range(3)
    ]
    client.read_holding_registers.return_value = _resp(error=True)

    data = gw.read_device_data('d1')

    assert data is None
    assert gw.stats['read_failures'] == 3


def test_no_failure_no_counter():
    """反向对照：全部读成功时计数必须保持 0（防一刀切累加）。"""
    gw, client = _gw()
    client.read_holding_registers.side_effect = [_resp([1]), _resp([2])]

    data = gw.read_device_data('d1')

    assert data == {'good': 1.0, 'bad': 2.0}
    assert gw.stats['read_failures'] == 0
    assert gw.stats['last_read_error'] is None


def test_s7_failure_is_counted():
    """S7 走的是同一套记账（两个网关的失败分支都必须计数）。"""
    from gateway.s7_gateway import S7Gateway

    gw = S7Gateway({'gateway_id': 'gw_s7', 'devices': [{'device_id': 'd1'}]})
    gw._register_configs['d1'] = [
        {'name': 'good', 'db': 1, 'offset': 0, 'type': 'int16'},
        {'name': 'bad', 'db': 1, 'offset': 2, 'type': 'int16'},
    ]
    gw._clients['d1'] = MagicMock()
    gw.connected_devices['d1'] = True
    gw._read_register = MagicMock(side_effect=[12.0, None])

    data = gw.read_device_data('d1')

    assert data == {'good': 12.0}
    assert gw.stats['read_failures'] == 1
    assert 'd1/bad' in (gw.stats['last_read_error'] or '')
