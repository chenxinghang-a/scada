# -*- coding: utf-8 -*-
"""接线验证：`FlaskConfig` 的三个无主旋钮 + 四个死配置类（round 231）。

背景
----
`tools/scan_config_readers.py` 长期报着这些「装饰品」：

* `FlaskConfig.DEBUG / REQUEST_TIMEOUT / MAX_CONCURRENT_REQUESTS` —— 定义了没人读；
* `AlarmOutputConfig` / `BroadcastConfig` / `MQTTConfig` / `TDengineConfig`
  —— **整类**没人 import，真值分别硬编码在 run.py / mqtt_client.py 里。

接线的要求是「**设了真的生效**」，所以这里逐条验行为，而不是只看代码里出现过名字。

⚠️ 两条容易接出事故的地方，本文件都钉住了：
1. `AlarmOutputConfig.SIMULATION` / `BroadcastConfig.SIMULATION` 原来默认 `True`，
   直接 `or` 进 `simulation` 会让**真实设备模式也被强制模拟**（现场灯塔不亮）。
   所以它们的语义改成「强制模拟」，默认 `False`，并且强制时 run.py 必须打 WARNING。
2. `TDengineConfig.PASSWORD` 原来默认空串，而 `TDengineClient` 的默认密码是
   `taosdata` —— 直接接线会把原本能连上的实例连挂。已把默认值对齐。
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from config import (  # noqa: E402
    AlarmOutputConfig, BroadcastConfig, FlaskConfig, MQTTConfig, TDengineConfig,
)


def _make_app():
    """用真实 create_app 建应用（只 mock 认证管理器）。

    ⚠️ 不要 mock `create_limiter`：被 mock 后所有端点返回空 200，行为断言会假绿。
    """
    with patch('展示层.routes.AuthManager') as MockAuth:
        MockAuth.return_value = MagicMock()
        from 展示层.routes import create_app
        return create_app(
            database=MagicMock(),
            device_manager=MagicMock(),
            alarm_manager=MagicMock(),
            data_collector=MagicMock(),
        )


# ---------------------------------------------------------------------------
# 1. MAX_CONCURRENT_REQUESTS：超过上限必须 503，而不是无限堆积
# ---------------------------------------------------------------------------

def test_concurrency_limit_returns_503():
    app = _make_app()
    slots = app.extensions['request_slots']
    limit = FlaskConfig.MAX_CONCURRENT_REQUESTS

    # 把槽位全部占满（模拟「已有 limit 个慢请求在跑」）
    taken = 0
    while slots.acquire(blocking=False):
        taken += 1
    assert taken == limit, f'槽位数 {taken} != 配置 {limit}'

    try:
        resp = app.test_client().get('/api/health/status')
        assert resp.status_code == 503, (
            f'并发满了却返回 {resp.status_code} —— 上限没生效，慢请求会无限堆积'
        )
        body = resp.get_json() or {}
        assert body.get('error_code') == 'TOO_MANY_CONCURRENT_REQUESTS'
        assert resp.headers.get('Retry-After')
    finally:
        for _ in range(taken):
            slots.release()


def test_slot_released_after_request():
    """一次正常请求结束后槽位必须归还（否则跑满 limit 次就永久 503）。"""
    app = _make_app()
    slots = app.extensions['request_slots']
    limit = FlaskConfig.MAX_CONCURRENT_REQUESTS

    resp = app.test_client().get('/api/health/status')
    assert resp.status_code != 503

    # 还能再拿满 limit 个，说明上一个请求归还了
    taken = 0
    while slots.acquire(blocking=False):
        taken += 1
    try:
        assert taken == limit, (
            f'只能再取到 {taken}/{limit} 个槽位 —— 请求结束后没有 release，'
            '跑满 limit 次之后整个服务会永久 503'
        )
    finally:
        for _ in range(taken):
            slots.release()


def test_slot_not_double_released_on_503():
    """被 503 短路时不能再 release（否则 BoundedSemaphore 会抛 ValueError，
    或者槽位越用越多，等于没有上限）。"""
    app = _make_app()
    slots = app.extensions['request_slots']
    limit = FlaskConfig.MAX_CONCURRENT_REQUESTS

    taken = 0
    while slots.acquire(blocking=False):
        taken += 1
    try:
        client = app.test_client()
        for _ in range(3):
            assert client.get('/api/health/status').status_code == 503
        # 连发 3 个被拒的请求后，槽位占用数不应变化
        extra = 0
        while slots.acquire(blocking=False):
            extra += 1
        assert extra == 0, f'被拒的请求多释放了槽位（多出 {extra} 个）'
        for _ in range(extra):
            slots.release()
    finally:
        for _ in range(taken):
            slots.release()


# ---------------------------------------------------------------------------
# 2. REQUEST_TIMEOUT：超过阈值必须留痕（响应头 + 日志）
# ---------------------------------------------------------------------------

def test_slow_request_is_marked(monkeypatch):
    monkeypatch.setattr(FlaskConfig, 'REQUEST_TIMEOUT', 0)   # 任何请求都算慢
    app = _make_app()

    resp = app.test_client().get('/api/health/status')
    assert 'X-Slow-Request' in resp.headers, (
        '请求耗时超过 SCADA_REQUEST_TIMEOUT 却没有标记 —— 这个旋钮仍是装饰品'
    )


def test_fast_request_is_not_marked():
    app = _make_app()
    resp = app.test_client().get('/api/health/status')
    assert 'X-Slow-Request' not in resp.headers


# ---------------------------------------------------------------------------
# 3. DEBUG：必须真的传给 app.run / socketio.run
# ---------------------------------------------------------------------------

def test_run_py_passes_debug_flag():
    src = (REPO / 'run.py').read_text(encoding='utf-8')
    assert 'debug=FlaskConfig.DEBUG' in src, (
        'run.py 又把 debug 写死成 False 了 —— FLASK_DEBUG 环境变量不生效'
    )
    assert src.count('debug=FlaskConfig.DEBUG') == 2, 'app.run 与 socketio.run 都要传'


# ---------------------------------------------------------------------------
# 4. 四个「整类死配置」的接线
# ---------------------------------------------------------------------------

def test_run_py_wires_alarm_output_and_broadcast_configs():
    src = (REPO / 'run.py').read_text(encoding='utf-8')
    for ref in (
        'AlarmOutputConfig.MODBUS_HOST', 'AlarmOutputConfig.MODBUS_PORT',
        'AlarmOutputConfig.MODBUS_SLAVE_ID', 'AlarmOutputConfig.DO_MAPPING',
        'AlarmOutputConfig.ENABLED',
        'BroadcastConfig.AREAS', 'BroadcastConfig.PRESET_TEMPLATES',
        'BroadcastConfig.ENABLED', 'BroadcastConfig.MQTT_BROKER',
    ):
        assert ref in src, f'run.py 没有引用 {ref} —— 这个配置项又变回装饰品了'


def test_simulation_flags_default_to_false():
    """两个 SIMULATION 的默认值必须是 False（= 跟随系统模式）。

    如果默认 True，`simulation_mode or Config.SIMULATION` 会让**真实设备模式**
    也走模拟输出 —— 现场灯塔/广播不动作，且这是安全相关路径。
    """
    assert AlarmOutputConfig.SIMULATION is False
    assert BroadcastConfig.SIMULATION is False


def test_forced_simulation_warns(monkeypatch, caplog):
    """显式强制模拟时必须打 WARNING（不能静默让硬件不动作）。"""
    import logging

    monkeypatch.setattr(AlarmOutputConfig, 'SIMULATION', True)
    src = (REPO / 'run.py').read_text(encoding='utf-8')
    # 源码层面钉住这条告警存在（run.py 的 main() 无法在单测里整体跑起来）
    assert 'AlarmOutputConfig.SIMULATION=True' in src
    assert 'BroadcastConfig.SIMULATION=True' in src
    assert '现场灯塔' in src and '现场功放' in src


def test_tdengine_password_default_matches_client():
    """`TDengineConfig.PASSWORD` 的生效值必须与客户端默认一致（占位符要当未设置）。

    ⚠️ 两个坑，第一版都踩了：
    1. 直接比较 `TDengineConfig.PASSWORD` 会被 `.env` 覆盖影响 —— 但 `.env` 里
       写的是 `CHANGE_ME`（占位符），本来就该当作「未设置」；
    2. 原来 config 默认空串、客户端默认 `taosdata`，接线后会用空密码连库。
    """
    from timeseries.tdengine_client import TDengineClient
    import inspect

    client_default = inspect.signature(TDengineClient.__init__).parameters['password'].default
    assert TDengineConfig.PASSWORD == client_default, (
        f'生效密码 {TDengineConfig.PASSWORD!r} != 客户端默认 {client_default!r} —— '
        '接线后会用错密码连库（`.env` 里的 CHANGE_ME 是占位符，不能当密码用）'
    )


def test_tdengine_password_treats_placeholder_as_unset():
    src = (REPO / 'config.py').read_text(encoding='utf-8')
    helper = src.split('def _tdengine_password')[1].split('class ')[0]
    assert "'CHANGE_ME'" in helper, (
        'TDengine 密码不再把 CHANGE_ME 当占位符 —— 会把占位符当密码发出去'
    )
    assert 'taosdata' in helper


def test_run_py_wires_tdengine_config():
    src = (REPO / 'run.py').read_text(encoding='utf-8')
    for ref in ('TDengineConfig.HOST', 'TDengineConfig.PORT',
                'TDengineConfig.USER', 'TDengineConfig.PASSWORD',
                'TDengineConfig.DATABASE'):
        assert ref in src, f'run.py 没有引用 {ref}'


def test_mqtt_client_uses_mqtt_config_defaults():
    """MQTT 客户端的默认 broker / client_id 前缀 / QoS 必须来自 MQTTConfig。"""
    pytest.importorskip('paho.mqtt')
    from 采集层.mqtt_client import MQTTClient

    c = MQTTClient(broker_host='x', broker_port=1, client_id='dev9')
    try:
        assert c.client_id == f'{MQTTConfig.CLIENT_ID_PREFIX}dev9', (
            f'client_id={c.client_id!r} —— 没走 MQTTConfig.CLIENT_ID_PREFIX'
        )
    finally:
        try:
            c.disconnect()
        except Exception:
            pass


def test_mqtt_defaults_come_from_config(monkeypatch):
    """改 MQTTConfig 的默认 broker，客户端必须跟着变（证明真的读它）。"""
    pytest.importorskip('paho.mqtt')
    from 采集层.mqtt_client import MQTTClient

    monkeypatch.setattr(MQTTConfig, 'BROKER_HOST', 'mqtt.example.internal')
    monkeypatch.setattr(MQTTConfig, 'BROKER_PORT', 1884)

    c = MQTTClient(client_id='dev10')
    try:
        assert c.broker_host == 'mqtt.example.internal', (
            f'broker_host={c.broker_host!r} —— 仍写死 localhost，MQTTConfig 没生效'
        )
        assert c.broker_port == 1884
    finally:
        try:
            c.disconnect()
        except Exception:
            pass
