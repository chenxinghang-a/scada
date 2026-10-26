# -*- coding: utf-8 -*-
"""语音广播：发布失败不许报「广播已发送」（round 287）。

缺陷形态
--------
`BroadcastSystem.speak()` 无条件返回成功：

    if not self.simulation:
        self._mqtt_publish(msg)          # ← 返回值被丢弃

    msg.status = 'sent'                  # ← 不管发没发出去
    ...
    return {'success': True, 'message': '广播已发送'}

而 `_mqtt_publish()` 本身返回 None，失败只留一行 error 日志。后果连锁：

1. **现场音柱没响，界面说"广播已发送"** —— 操作员以为已经通知到人了；
2. `AlarmManager._call_output_method()` 把 `{'success': True}` 当成功 →
   `_record_output_success()` 把失败计数清零 → round 267 加的
   「通知通道连续失败要报警」**对广播通道永远不生效**；
3. `get_status()` 只有 `mqtt_connected` —— "连上了但发布一直失败"看不出来。

判据
----
与 round 286（声光 DO）同一条：**通道的真实结果必须传出去，且坏了要能停下来**。
`speak()` 的 success 是"真的发出去了吗"，不是"我尝试过了"。

本文件钉住：
1. 硬件模式下发布失败 → `speak()` 返回 `success: False`，历史里 `status='failed'`；
2. 发布返回错误码 / 未连接 / paho 未安装，都算失败；
3. 连续失败熔断，熔断期间**不连接、不发布**；到期恢复；
4. 模拟模式行为不变；
5. **端到端**：`AlarmManager` 现在真的会把广播记为失败通道。
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest


def _bs(simulation=False, **mqtt):
    from 报警层.broadcast_system import BroadcastSystem

    cfg = {'enabled': True, 'simulation': simulation,
           'mqtt': {'broker': '127.0.0.1', 'port': 1883, **mqtt}}
    return BroadcastSystem(cfg)


class _Info:
    def __init__(self, rc=0):
        self.rc = rc


class _FakeMqtt:
    def __init__(self, rc=0, raise_exc=None):
        self.rc = rc
        self.raise_exc = raise_exc
        self.publish_calls = 0
        self.topics = []

    def publish(self, topic, payload, qos=0):
        self.publish_calls += 1
        self.topics.append(topic)
        if self.raise_exc:
            raise self.raise_exc
        return _Info(self.rc)


def _wire(bs, connected=True, rc=0, raise_exc=None):
    """把一个假的、已连接的 MQTT client 装进去（不碰真网络）。"""
    client = _FakeMqtt(rc=rc, raise_exc=raise_exc)
    bs._mqtt_client = client
    bs._mqtt_connected = connected
    return client


# ---------------------------------------------------------------------------
# speak() 必须如实上报
# ---------------------------------------------------------------------------

def test_speak_reports_failure_when_not_connected():
    bs = _bs()
    _wire(bs, connected=False)

    result = bs.speak('火警疏散', level='critical')

    assert result['success'] is False, (
        'MQTT 未连接却回「广播已发送」—— 现场音柱没响，操作员以为通知到了')
    assert '未发出' in result['message']
    assert result['data']['status'] == 'failed'


def test_speak_reports_success_when_published():
    bs = _bs()
    client = _wire(bs, connected=True, rc=0)

    result = bs.speak('测试广播', level='info')

    assert result['success'] is True
    assert result['data']['status'] == 'sent'
    assert client.publish_calls == 1
    assert client.topics == ['pa/all'], f'主题不对: {client.topics}'


def test_publish_error_code_is_failure():
    """`publish()` 返回 rc != 0 表示"本地就没交出去"，必须算失败。"""
    bs = _bs()
    _wire(bs, connected=True, rc=4)      # MQTT_ERR_NO_CONN 之类

    result = bs.speak('测试广播')

    assert result['success'] is False
    assert bs.get_status()['mqtt']['fail_streak'] == 1


def test_publish_exception_is_failure():
    bs = _bs()
    _wire(bs, connected=True, raise_exc=OSError('broken pipe'))

    result = bs.speak('测试广播')

    assert result['success'] is False
    assert bs.get_status()['mqtt']['fail_streak'] == 1


def test_missing_paho_is_failure(monkeypatch):
    import 报警层.broadcast_system as mod

    monkeypatch.setattr(mod, 'MQTT_AVAILABLE', False)
    bs = _bs()

    result = bs.speak('测试广播')

    assert result['success'] is False, 'paho 没装却回「广播已发送」'


def test_history_records_failed_status():
    bs = _bs()
    _wire(bs, connected=False)

    bs.speak('msg1')
    history = bs.get_history()

    assert history[-1]['status'] == 'failed'
    assert history[-1]['text'] == 'msg1'


def test_callback_still_fires_on_failure():
    """失败也要回调（前端要显示"广播失败"），但 status 必须是 failed。"""
    bs = _bs()
    _wire(bs, connected=False)
    cb = MagicMock()
    bs.add_callback(cb)

    bs.speak('msg')

    cb.assert_called_once()
    assert cb.call_args[0][0]['status'] == 'failed'


def test_simulation_mode_unchanged():
    """模拟模式不碰 MQTT，行为必须与以前一致（回归）。"""
    bs = _bs(simulation=True)

    result = bs.speak('模拟广播')

    assert result['success'] is True
    assert result['data']['status'] == 'sent'


# ---------------------------------------------------------------------------
# 熔断
# ---------------------------------------------------------------------------

def test_consecutive_failures_open_circuit():
    from 报警层.broadcast_system import BROADCAST_CIRCUIT_FAIL_THRESHOLD

    bs = _bs()
    # ⚠️ 必须"已连接但 publish 返回错误码"，否则失败发生在**更早**的分支
    # （`MQTT 未连接`），熔断检查根本不在那条路径上 —— 用例会对变异免疫。
    _wire(bs, connected=True, rc=4)

    for _ in range(BROADCAST_CIRCUIT_FAIL_THRESHOLD):
        bs.speak('x')

    assert bs.get_status()['mqtt']['circuit_open'] is True, (
        '连续失败达到阈值后没有熔断 —— 每条报警都会卡在一次 Broker 发布上')


def test_open_circuit_stops_publishing():
    """熔断期间**一次 publish 都不许有**（这是熔断存在的唯一理由）。"""
    from 报警层.broadcast_system import BROADCAST_CIRCUIT_FAIL_THRESHOLD

    bs = _bs()
    client = _wire(bs, connected=True, rc=4)      # 连接正常，但发布一直失败
    for _ in range(BROADCAST_CIRCUIT_FAIL_THRESHOLD):
        bs.speak('x')
    before = client.publish_calls
    assert before == BROADCAST_CIRCUIT_FAIL_THRESHOLD

    for _ in range(10):
        bs.speak('x')

    assert client.publish_calls == before, (
        f'熔断期间又发布了 {client.publish_calls - before} 次 —— '
        f'熔断没有生效')


def test_open_circuit_does_not_reconnect(monkeypatch):
    """熔断期间也不许重新连 Broker（否则每条报警都要等一次连接超时）。"""
    import 报警层.broadcast_system as mod

    bs = _bs()
    _wire(bs, connected=True, rc=4)
    for _ in range(mod.BROADCAST_CIRCUIT_FAIL_THRESHOLD):
        bs.speak('x')
    assert bs.get_status()['mqtt']['circuit_open'] is True

    # 把 client 摘掉：不熔断的话下一次 speak 会去 `_connect_mqtt()`
    bs._mqtt_client = None
    bs._mqtt_connected = False
    connect_calls = []
    monkeypatch.setattr(bs, '_connect_mqtt', lambda: connect_calls.append(1))

    for _ in range(5):
        bs.speak('x')

    assert connect_calls == [], (
        f'熔断期间仍然尝试连接 Broker {len(connect_calls)} 次')


def test_circuit_expires_and_recovers(monkeypatch):
    import 报警层.broadcast_system as mod

    bs = _bs()
    _wire(bs, connected=False)
    for _ in range(mod.BROADCAST_CIRCUIT_FAIL_THRESHOLD):
        bs.speak('x')
    assert bs.get_status()['mqtt']['circuit_open'] is True

    real_monotonic = time.monotonic
    monkeypatch.setattr(mod.time, 'monotonic',
                        lambda: real_monotonic() + mod.BROADCAST_CIRCUIT_OPEN_SECONDS + 1)

    client = _wire(bs, connected=True, rc=0)
    result = bs.speak('恢复测试')

    assert result['success'] is True, '熔断到期后没有恢复尝试'
    assert client.publish_calls == 1
    assert bs.get_status()['mqtt']['circuit_open'] is False
    assert bs.get_status()['mqtt']['fail_streak'] == 0


def test_success_resets_streak():
    bs = _bs()
    _wire(bs, connected=False)
    bs.speak('x')
    assert bs.get_status()['mqtt']['fail_streak'] == 1

    _wire(bs, connected=True, rc=0)
    bs.speak('x')

    assert bs.get_status()['mqtt']['fail_streak'] == 0


# ---------------------------------------------------------------------------
# 端到端：AlarmManager 现在能把广播记为失败通道
# ---------------------------------------------------------------------------

def test_alarm_manager_counts_broadcast_failure():
    """round 267 的「通道连续失败要报警」此前对广播**永远不生效**。"""
    from 报警层.alarm_manager import AlarmManager

    db = MagicMock()
    mgr = AlarmManager(db)
    bs = _bs()                       # 硬件模式、未连接 → 发布必然失败
    _wire(bs, connected=False)
    mgr.broadcast_system = bs

    try:
        ok = mgr._invoke_broadcast('critical', '测试', 'dev1', 'all')
    finally:
        try:
            mgr.stop()
        except Exception:       # noqa: BLE001
            pass

    assert ok is False, (
        '广播发布失败，AlarmManager 却认为通道成功 —— 失败计数会被清零，'
        '「广播坏了」永远报不出来')
    assert mgr._output_errors.get('broadcast', 0) == 1
