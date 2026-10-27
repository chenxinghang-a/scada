# -*- coding: utf-8 -*-
"""部分点读不到时，遥测必须**降级质量码**并写明丢了哪些点（round 296）。

缺陷形态
--------
`read_device_data` 只返回**读成功**的点：

    for name in registers:
        if value is not None:
            result[name] = value
        else:
            self._note_read_failure(...)      # round 295 只加了计数

`convert_to_telemetry` 再给每个点无条件标 `quality=GOOD(192)`。
于是"10 个点里 9 个读不到"这一轮，发出去的遥测看起来**和完全正常一模一样**，
只是少了 9 个指标 —— 下游无法区分：

* "这台设备本来就只有 1 个指标"
* "这台设备有 10 个指标，这轮 9 个没读到"

round 295 的 `read_failures` 计数只解决"有没有失败"，不解决"这条数据可不可信"。

判据
----
**读数不可全信时，质量码必须降级**（OPC UA：UNCERTAIN = 值可用但不可全信）。
而且降级不能靠"塞 `value=None`"实现 —— `ThingModelValidator` 会判
"value 必须是数字"，**整条遥测被下游丢弃**，比缺几个点更糟。
所以：已有点全部降为 UNCERTAIN，丢失点位与原因写进 `Metadata['read_failures']`。

本文件钉住：
1. 部分失败 → 所有已有点 quality=UNCERTAIN(104)，Metadata 里能查到丢了哪些点；
2. 全成功 → quality 保持 GOOD(192)，无 Metadata 降级信息（反向对照）；
3. **标记不跨轮累积**：上一轮失败、这一轮全成功 → 必须恢复 GOOD
   （否则"一次抖动，永久不可信"）；
4. 降级后的遥测仍能通过 `ThingModelValidator`（不能被下游丢弃）；
5. 订阅端**真的消费** quality（否则降级了也没人知道）。
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from gateway.modbus_gateway import ModbusGateway

GOOD = 192
UNCERTAIN = 104


def _gw():
    gw = ModbusGateway({
        'gateway_id': 'gw_q',
        'devices': [{'device_id': 'd1', 'slave_id': 1}],
        'poll_interval': 0.01,
    })
    gw._register_configs['d1'] = [
        {'name': 'good', 'address': 0, 'count': 1, 'type': 'uint16'},
        {'name': 'bad', 'address': 1, 'count': 1, 'type': 'uint16'},
    ]
    gw._byte_orders['d1'] = None
    gw._clients['d1'] = MagicMock()
    gw.connected_devices['d1'] = True
    return gw


def _resp(registers=None, error=False):
    r = MagicMock()
    r.isError.return_value = error
    r.registers = registers or []
    return r


def _round(gw, bad_error=True):
    """模拟一轮轮询：清标记 → 读 → 转换 → 降级。

    ⚠️ 调用签名是 `read_holding_registers(address, count, slave=...)`
    （见 `_read_register`），不是 (slave, address, count)。
    """
    gw.begin_device_read('d1')

    def _read(address, count=1, slave=1, **kw):
        return _resp([42], error=False) if address == 0 else _resp(error=bad_error)

    gw._clients['d1'].read_holding_registers.side_effect = _read
    raw = gw.read_device_data('d1')
    if raw is None:
        return None
    telemetry = gw.convert_to_telemetry('d1', raw)
    gw.apply_read_quality('d1', telemetry)
    return telemetry


# ---------------------------------------------------------------------------
# 网关侧
# ---------------------------------------------------------------------------

def test_partial_read_degrades_quality():
    gw = _gw()
    telemetry = _round(gw)

    assert telemetry is not None, '前提不成立：部分成功时应返回非空结果'
    assert set(telemetry.Metrics) == {'good'}
    assert telemetry.Metrics['good']['quality'] == UNCERTAIN, (
        '有寄存器读不到时，剩下的点仍标 GOOD —— 下游无法区分'
        '「设备只有 1 个指标」和「9 个点没读到」')
    assert telemetry.Metadata.get('read_quality') == 'partial'
    assert 'bad' in (telemetry.Metadata.get('read_failures') or {}), (
        f"没有写明丢了哪个点: {telemetry.Metadata.get('read_failures')}")


def test_full_success_keeps_good_quality():
    """反向对照：全部读成功时不许降级。"""
    gw = _gw()
    gw._register_configs['d1'] = [
        {'name': 'good', 'address': 0, 'count': 1, 'type': 'uint16'},
    ]
    gw.begin_device_read('d1')
    gw._clients['d1'].read_holding_registers.side_effect = None
    gw._clients['d1'].read_holding_registers.return_value = _resp([42])
    raw = gw.read_device_data('d1')
    telemetry = gw.convert_to_telemetry('d1', raw)
    degraded = gw.apply_read_quality('d1', telemetry)

    assert degraded is False
    assert telemetry.Metrics['good']['quality'] == GOOD
    assert 'read_failures' not in (telemetry.Metadata or {})


def test_marks_do_not_leak_into_next_round():
    """上一轮失败、这一轮全成功 → 必须恢复 GOOD。

    否则一次抖动会让这台设备**永久**被标成不可信。
    """
    gw = _gw()
    first = _round(gw)
    assert first.Metrics['good']['quality'] == UNCERTAIN

    # 第二轮：只留一个寄存器，且读成功
    gw._register_configs['d1'] = [
        {'name': 'good', 'address': 0, 'count': 1, 'type': 'uint16'},
    ]
    gw.begin_device_read('d1')
    gw._clients['d1'].read_holding_registers.side_effect = None
    gw._clients['d1'].read_holding_registers.return_value = _resp([43])
    raw = gw.read_device_data('d1')
    telemetry = gw.convert_to_telemetry('d1', raw)
    gw.apply_read_quality('d1', telemetry)

    assert telemetry.Metrics['good']['quality'] == GOOD, (
        '失败标记跨轮累积 —— 一次抖动之后这台设备永久被降级')


def test_degraded_telemetry_still_passes_validation():
    """降级后的遥测不能被下游丢弃（`value` 必须仍是数字）。"""
    from gateway.thing_model import ThingModelValidator

    gw = _gw()
    telemetry = _round(gw)
    ok, errors = ThingModelValidator.validate_telemetry(
        json.loads(telemetry.to_json()))

    assert ok, f'降级后的遥测被校验拒绝（会被订阅端整条丢弃）: {errors}'


# ---------------------------------------------------------------------------
# 订阅端必须真的消费 quality
# ---------------------------------------------------------------------------

def _subscriber():
    from gateway.mqtt_subscriber import MQTTSubscriber
    return MQTTSubscriber(broker_host='127.0.0.1')


def _payload(device_id='d1', quality=GOOD, failures=None):
    metrics = {'temp': {'value': 42.0, 'unit': '°C', 'quality': quality}}
    meta = {}
    if failures:
        meta = {'read_quality': 'partial', 'read_failures': failures}
    return json.dumps({
        'DeviceID': device_id,
        'Timestamp': 1700000000.0,
        'Protocol': 'ModbusTCP',
        'Metrics': metrics,
        'GatewayID': 'gw_q',
        'Metadata': meta,
    })


def test_subscriber_counts_degraded_telemetry():
    sub = _subscriber()
    sub._handle_telemetry('scada/d1/telemetry',
                          _payload(quality=UNCERTAIN, failures={'bad': '读取失败'}))

    stats = sub.get_stats()
    assert stats['degraded_telemetry'] == 1, (
        '订阅端不消费 quality —— 网关降级了也没人知道')
    assert stats['degraded_metrics'] == 1
    assert stats['last_degraded_devices'] == ['d1']


def test_subscriber_ignores_good_telemetry():
    """反向对照：全 GOOD 不许计成降级。"""
    sub = _subscriber()
    sub._handle_telemetry('scada/d1/telemetry', _payload(quality=GOOD))

    stats = sub.get_stats()
    assert stats['degraded_telemetry'] == 0
    assert stats['last_degraded_devices'] == []


def test_subscriber_degraded_device_list_is_bounded():
    """降级设备清单不许无界增长。"""
    sub = _subscriber()
    for i in range(40):
        sub._handle_telemetry(f'scada/d{i}/telemetry',
                              _payload(device_id=f'd{i}', quality=UNCERTAIN))

    assert len(sub.get_stats()['last_degraded_devices']) <= 20
