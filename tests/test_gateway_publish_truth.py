# -*- coding: utf-8 -*-
"""网关的三条发布通道必须"真的发出去了"才算成功（round 292）。

缺陷形态（同一族：**通道失败静默**）
----------------------------------
`BaseGateway` 有三条出站通道，写法各不相同，但没有一条能让调用方知道成败：

* `publish_telemetry()`：检查了 `publish()` 的返回码，失败只 `errors += 1`，
  然后 **`return`（None）** —— 调用方无从判断；
* `publish_status()`：**连返回码都不看**，`mqtt_client` 没初始化时直接 `return`，
  连一次计数都没有；
* `publish_alarm()`：同上。报警发不出去 = 声光/看板上什么都没有，而日志里
  最多一行 broker 侧的 debug。

后果：网关看起来"在跑"（轮询线程照转、`connected_devices` 照标 True），
而 MQTT 上一条数据都没有 —— 判断依据只有"另一端有没有收到"，
本地没有任何信号。

判据（承接 283-291）
------------------
1. 通道返回值 = 真的发出去了吗：`rc != MQTT_ERR_SUCCESS`、异常、客户端未初始化
   三者都算失败，且必须**返回 False**；
2. 失败必须留痕：`errors` 计数 + `last_error` + `last_error_time`；
3. 不改变成功路径的语义（成功返回 True，计数照旧由回调负责）。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import paho.mqtt.client as mqtt
import pytest

from gateway.base_gateway import BaseGateway
from gateway.thing_model import DeviceTelemetry


class _Gateway(BaseGateway):
    """最小可实例化网关（只为测发布通道，不碰真实协议）。"""

    def connect(self) -> bool:
        return True

    def disconnect(self):
        pass

    def read_device_data(self, device_id):
        return None

    def convert_to_telemetry(self, device_id, raw_data):
        raise NotImplementedError


def _gw(rc: int = mqtt.MQTT_ERR_SUCCESS, raises: bool = False):
    gw = _Gateway({'gateway_id': 'gw_test'})
    client = MagicMock()
    if raises:
        client.publish.side_effect = RuntimeError('broker 挂了')
    else:
        client.publish.return_value = MagicMock(rc=rc)
    gw.mqtt_client = client
    return gw, client


def _telemetry():
    return DeviceTelemetry(
        DeviceID='dev1', Timestamp=1750000000.0, Protocol='ModbusTCP',
        Metrics={'temperature': {'value': 25.0, 'unit': '°C', 'quality': 192}})


# ---------------------------------------------------------------------------
# 1. 遥测通道
# ---------------------------------------------------------------------------

def test_telemetry_publish_returns_true_on_success():
    gw, client = _gw()

    ok = gw.publish_telemetry(_telemetry())

    assert ok is True
    assert client.publish.called
    assert gw.stats['errors'] == 0


def test_telemetry_publish_returns_false_on_bad_rc():
    """排队失败（rc != 0）→ 必须返回 False 并记账（反向：原实现返回 None）。"""
    gw, _ = _gw(rc=mqtt.MQTT_ERR_NO_CONN)

    ok = gw.publish_telemetry(_telemetry())

    assert ok is False, '发布失败却返回了"成功/None" —— 调用方无从判断'
    assert gw.stats['errors'] == 1
    assert gw.stats['last_error']
    assert gw.stats['last_error_time'] is not None


def test_telemetry_publish_returns_false_on_exception():
    gw, _ = _gw(raises=True)

    ok = gw.publish_telemetry(_telemetry())

    assert ok is False
    assert gw.stats['errors'] == 1
    assert 'broker 挂了' in gw.stats['last_error']


# ---------------------------------------------------------------------------
# 2. 状态 / 报警通道：原实现完全不看返回码
# ---------------------------------------------------------------------------

def test_status_publish_returns_false_on_bad_rc():
    gw, _ = _gw(rc=mqtt.MQTT_ERR_NO_CONN)

    ok = gw.publish_status('dev1', True, 'running')

    assert ok is False, '状态发布失败未被识别 —— 看板会认为网关一切正常'
    assert gw.stats['errors'] == 1


def test_alarm_publish_returns_false_on_bad_rc():
    gw, _ = _gw(rc=mqtt.MQTT_ERR_NO_CONN)

    ok = gw.publish_alarm('dev1', 'critical', 'temperature', '温度过高')

    assert ok is False, '报警发布失败未被识别 —— 报警发不出去等于没报'
    assert gw.stats['errors'] == 1


def test_status_and_alarm_succeed_when_rc_ok():
    """反向对照：rc=0 时三条通道都必须返回 True（防止一刀切全判失败）。"""
    gw, _ = _gw()

    assert gw.publish_status('dev1', True, 'running') is True
    assert gw.publish_alarm('dev1', 'critical', 'temperature', '温度过高') is True
    assert gw.stats['errors'] == 0


# ---------------------------------------------------------------------------
# 3. 客户端未初始化：不许静默
# ---------------------------------------------------------------------------

def test_publish_without_client_is_a_counted_failure():
    gw = _Gateway({'gateway_id': 'gw_noclient'})
    gw.mqtt_client = None

    ok = gw.publish_telemetry(_telemetry())

    assert ok is False
    assert gw.stats['errors'] == 1, (
        '客户端未初始化时静默返回 —— 连计数都没有，运维查不到任何痕迹')
    assert '未初始化' in gw.stats['last_error']

# ---------------------------------------------------------------------------
# 4. 构造本身：本轮顺带修掉的崩溃
# ---------------------------------------------------------------------------

def test_gateway_construction_does_not_raise():
    """构造网关不得抛异常。

    round 292 实测：`__init__` 里缺 `global _signal_registered` —— 函数体后面对它
    赋值，会让这个名字在整个 `__init__` 内变成**局部变量**，于是
    `if not _signal_registered` 读的是未初始化局部变量 →
    **任何网关一实例化就抛 UnboundLocalError**。
    而在此之前的测试只覆盖各协议的 `decode_*` 函数，**没有一条实例化过
    BaseGateway** —— `gateway/` 这 4400 行在运行时根本起不来，测试全绿。
    """
    gw = _Gateway({'gateway_id': 'gw_ctor'})

    assert gw.running is False
    assert gw.stats['errors'] == 0


def test_signal_handler_is_registered_after_first_construction():
    """反向对照：第一次构造后必须真的注册上（`_signal_registered` 变 True）。

    只断言"不抛异常"是不够的：把整段注册逻辑删掉也能不抛。
    """
    import gateway.base_gateway as bg

    _Gateway({'gateway_id': 'gw_sig'})

    assert bg._signal_registered is True, '信号处理器没有注册（或全局标志没置位）'
