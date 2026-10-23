# -*- coding: utf-8 -*-
"""HA 心跳超时必须按**单调时钟**判定（round 259）。

缺陷形态
--------
`core/ha_manager.py` 的心跳失效检测是：

    now - self._last_peer_heartbeat > self.heartbeat_timeout

原来是 `time.time()` 算的。墙钟被回拨一次（NTP 校时、现场机无电池开机后校时、
手工改时间），差值变负 → **对端明明已经宕机，`peer_alive` 却一直为真**：

* 备节点永远不接管（`_heartbeat_loop` 里 `peer_timed_out` 永不成立）；
* 主备状态接口显示「对端活着」，运维不会去查；
* 反过来（向前跳）健康对端被误判超时 → **双主**，两边同时采集/写库。

判据：**时长用 monotonic，时刻用 wall clock**。
心跳包里带的时间戳（`'timestamp': time.time()`）是**线上时刻**，继续用墙钟
（要参与 HMAC 签名、跨机比对），不能改。
"""

from __future__ import annotations

import re
import time

#: 真实墙钟函数（被 monkeypatch 之前的原版）—— 用来模拟"回拨 8 小时"
_REAL_TIME = time.time
from pathlib import Path

HA_SRC = Path(__file__).resolve().parents[1] / "core" / "ha_manager.py"


def _make_ha(**kw):
    from core.ha_manager import HAManager

    params = dict(node_id='node-a', peer_address='127.0.0.1:9',
                  # 配了对端就必须给共享密钥（构造期 fail-closed，防双主）
                  shared_secret='t' * 32,
                  heartbeat_interval=0.05, heartbeat_timeout=0.05)
    params.update(kw)
    return HAManager(**params)


def test_peer_goes_stale_after_backward_clock_jump(monkeypatch):
    """墙钟回拨 8 小时后，超过超时窗口的对端仍必须判为失效。"""
    ha = _make_ha()
    ha._peer_seen = True
    ha._last_peer_heartbeat = time.monotonic()

    monkeypatch.setattr(time, 'time', lambda: _REAL_TIME() - 8 * 3600)   # 墙钟回拨
    time.sleep(0.08)                                          # 真实超时 80ms > 50ms

    assert ha.get_status()['peer_alive'] is False, (
        '对端早已超时却仍报「活着」—— 说明心跳窗口用的是墙钟，'
        '一次校时之后备机永远不接管（或反向误判导致双主）')


def test_peer_alive_while_within_window(monkeypatch):
    """反向对照：窗口内必须判为活着（防止把 `peer_alive` 写成恒 False）。"""
    ha = _make_ha(heartbeat_timeout=10.0)
    ha._peer_seen = True
    ha._last_peer_heartbeat = time.monotonic()

    monkeypatch.setattr(time, 'time', lambda: _REAL_TIME() - 8 * 3600)
    assert ha.get_status()['peer_alive'] is True


def test_received_heartbeat_stores_monotonic(monkeypatch):
    """收到心跳后记下的时刻必须是单调时钟。

    这条是**行为判别**：上面那两条用固定值打桩 `_last_peer_heartbeat`，
    如果实现把墙钟/单调时钟混着比（差值恰好是巨大正数），断言会碰巧通过。
    这里走真实接收路径 `_handle_heartbeat`，直接量它存下来的是什么时钟。
    """
    ha = _make_ha()
    msg = {'type': 'heartbeat', 'node_id': 'b', 'role': 'primary',
           'priority': 1, 'timestamp': _REAL_TIME(), 'state': 'active',
           'signature': 'stub'}
    monkeypatch.setattr(ha, '_verify_hmac', lambda *a, **k: True)

    ha._handle_heartbeat(msg, ('127.0.0.1', 1234))

    stored = ha._last_peer_heartbeat
    assert abs(time.monotonic() - stored) < 60, (
        f'收到心跳后记下的时刻 {stored!r} 不是单调时钟（与 monotonic 差 '
        f'{abs(time.monotonic() - stored):.0f}s）—— 超时判定会算错')


def test_never_seen_peer_is_not_alive():
    """从未收到过心跳 → 不算活着（不依赖 `> 0` 这种哨兵）。"""
    ha = _make_ha()
    assert ha.get_status()['peer_alive'] is False


def test_heartbeat_loop_uses_monotonic():
    """静态守卫：心跳超时判定里不许再出现 `time.time()`。"""
    code = re.sub(r"#.*$", "", HA_SRC.read_text(encoding='utf-8'), flags=re.M)
    hits = re.findall(r"time\.time\(\)\s*-\s*self\._last_peer_heartbeat", code)
    assert not hits, (
        '心跳超时又用墙钟算了 —— 校时回拨会让对端「永远活着」')
    assert 'time.monotonic() - self._last_peer_heartbeat' in code


def test_wire_timestamp_stays_wall_clock():
    """反面：心跳包里的 `timestamp` 是线上时刻，必须继续用墙钟（要参与签名）。"""
    src = HA_SRC.read_text(encoding='utf-8')
    assert "'timestamp': time.time()" in src, (
        '心跳包的 timestamp 被改成单调时钟了 —— 它要参与 HMAC 签名、跨机比对，'
        '必须是墙钟')
