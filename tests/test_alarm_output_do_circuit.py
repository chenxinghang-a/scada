# -*- coding: utf-8 -*-
"""声光输出的 DO 通道：网关断线不许把整个报警链路拖死（round 286）。

缺陷形态
--------
`AlarmOutput` 硬件模式下：

    def _get_modbus_client(self):
        if self._modbus_client is None:
            self._modbus_client = ModbusTcpClient(host, port=port)   # 默认超时
            if not self._modbus_client.connect():
                self._modbus_client = None      # ← 下次再重连一遍
                return None

    def _write_all_do(self, ...):
        with self._lock:                        # ← 状态锁
            self._write_do(...)                 # ← 4 次网络写

    def _flash_loop(self, ...):
        while self._flash_running:
            with self._lock:                    # ← 每 250ms 一次
                self._write_do(do_addr, state)

三个问题叠在一起：

1. **没有超时上限**：默认 Modbus 超时 3~20 秒，网关断电时每次 `connect()`
   都可能卡这么久；
2. **失败不熔断**：连接失败就把 client 置 None，下一次调用**重新连一遍** ——
   而闪烁线程每 250ms 就要写一次 DO；
3. **I/O 在状态锁内做**：`self._lock` 同时保护 `current_state`，
   于是 `get_status()`（API）、`trigger_alarm()`（报警主链路）、
   `acknowledge()`（消音）全被堵在锁上。

现场表现：声光网关一断线，报警链路和报警 API 一起卡死，操作员点确认没反应，
而采集数据处理线程还在同步调 `check_alarm()` —— 数据队列跟着积压。

判据
----
**I/O 失败必须快速失败并停下来**：短超时 + 连续失败熔断 + 状态锁不跨 I/O 持有。
声光坏掉是"这个通道不可用"，不是"整个报警系统停机"。

本文件钉住的不变量：
1. DO 写失败 N 次后熔断，熔断期间**不碰 socket、不重连**；
2. 熔断到期后恢复尝试，成功即关闭；
3. 状态锁 `_lock` **绝不**在网络 I/O 期间被持有（`get_status()` 随时能返回）；
4. 连接失败时 client 被关闭（不泄漏 socket）；
5. 建连时显式传入短超时；
6. `get_status()` 能看出 DO 通道坏了（否则界面显示的是"意图"不是"事实"）。
"""

from __future__ import annotations

import threading
import time

import pytest


def _output(**overrides):
    from 报警层.alarm_output import AlarmOutput

    cfg = {
        'enabled': True,
        'simulation': False,          # 走硬件分支
        'modbus': {'host': '127.0.0.1', 'port': 15020, 'slave_id': 1},
        'do_mapping': {'red_light': 0, 'yellow_light': 1,
                       'green_light': 2, 'buzzer': 3},
    }
    cfg.update(overrides)
    return AlarmOutput(cfg)


class _FakeResult:
    def __init__(self, error: bool = False):
        self._error = error

    def isError(self):
        return self._error


class _FakeClient:
    """可编排的假 Modbus 客户端（记录调用次数）。"""

    def __init__(self, fail: bool = False, on_write=None):
        self.fail = fail
        self.write_calls = 0
        self.connect_calls = 0
        self.closed = 0
        self._on_write = on_write

    def connect(self):
        self.connect_calls += 1
        return not self.fail

    def close(self):
        self.closed += 1

    def write_coil(self, address, value, slave=1):
        self.write_calls += 1
        if self._on_write is not None:
            self._on_write(address, value)
        if self.fail:
            raise OSError('网关不可达')
        return _FakeResult(error=False)


@pytest.fixture
def out():
    o = _output()
    yield o
    try:
        o._flash_running = False
        o._buzzer_running = False
    except Exception:       # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 熔断
# ---------------------------------------------------------------------------

def test_consecutive_failures_open_circuit(out):
    from 报警层.alarm_output import CIRCUIT_FAIL_THRESHOLD

    client = _FakeClient(fail=True)
    out._modbus_client = client

    for _ in range(CIRCUIT_FAIL_THRESHOLD):
        assert out._write_do(0, True) is False

    assert out.get_status()['do_channel']['circuit_open'] is True, (
        '连续失败达到阈值后没有熔断 —— 闪烁线程会每 250ms 重连一次网关')


def test_open_circuit_stops_touching_the_socket(out):
    """熔断期间**一次 socket 操作都不许有**（这是熔断存在的唯一理由）。"""
    from 报警层.alarm_output import CIRCUIT_FAIL_THRESHOLD

    client = _FakeClient(fail=True)
    out._modbus_client = client
    for _ in range(CIRCUIT_FAIL_THRESHOLD):
        out._write_do(0, True)

    before = (client.write_calls, client.connect_calls)
    for _ in range(20):
        assert out._write_do(0, True) is False
    after = (client.write_calls, client.connect_calls)

    assert after == before, (
        f'熔断期间仍在碰 socket：写 {after[0] - before[0]} 次、'
        f'连接 {after[1] - before[1]} 次')


def test_circuit_expires_and_recovers(out, monkeypatch):
    import 报警层.alarm_output as mod

    client = _FakeClient(fail=True)
    out._modbus_client = client
    for _ in range(mod.CIRCUIT_FAIL_THRESHOLD):
        out._write_do(0, True)
    assert out.get_status()['do_channel']['circuit_open'] is True

    # 把"现在"推到熔断到期之后
    real_monotonic = time.monotonic
    monkeypatch.setattr(mod.time, 'monotonic',
                        lambda: real_monotonic() + mod.CIRCUIT_OPEN_SECONDS + 1)

    client.fail = False
    assert out._write_do(0, True) is True, '熔断到期后没有恢复尝试'
    assert out.get_status()['do_channel']['circuit_open'] is False
    assert out.get_status()['do_channel']['fail_streak'] == 0


def test_success_resets_streak(out):
    client = _FakeClient(fail=False)
    out._modbus_client = client

    out._write_do(0, True)
    out._write_do(0, False)

    assert out.get_status()['do_channel']['fail_streak'] == 0


def test_connect_failure_closes_client(out, monkeypatch):
    """连不上的 client 必须 close —— 否则每次重试漏一个 socket。"""
    import pymodbus.client as pc

    created = []

    class _BadClient(_FakeClient):
        def __init__(self, *a, **kw):
            super().__init__(fail=True)
            created.append(self)

    monkeypatch.setattr(pc, 'ModbusTcpClient', _BadClient)
    out._modbus_client = None

    assert out._write_do(0, True) is False
    assert created and created[0].closed >= 1, '连接失败的 client 没有被关闭'


def test_client_is_created_with_short_timeout(out, monkeypatch):
    """建连必须显式传短超时 —— 默认超时下网关断电会卡 3~20 秒。"""
    import pymodbus.client as pc
    from 报警层.alarm_output import DEFAULT_MODBUS_TIMEOUT_S

    seen = {}

    class _Rec(_FakeClient):
        def __init__(self, host, port=None, timeout=None):
            super().__init__(fail=False)
            seen['host'] = host
            seen['port'] = port
            seen['timeout'] = timeout

    monkeypatch.setattr(pc, 'ModbusTcpClient', _Rec)
    out._modbus_client = None

    assert out._write_do(0, True) is True
    assert seen['timeout'] == DEFAULT_MODBUS_TIMEOUT_S, (
        f"建连没传短超时（timeout={seen['timeout']}）—— 网关断电会长时间阻塞")
    assert seen['timeout'] <= 2.0


# ---------------------------------------------------------------------------
# 状态锁不跨 I/O 持有
# ---------------------------------------------------------------------------

def test_state_lock_is_not_held_during_io(out):
    """`write_coil` 执行期间必须能拿到 `_lock`（否则 API / 报警主链路被堵死）。"""
    got = {}

    def _probe(address, value):
        acquired = out._lock.acquire(timeout=1.0)
        got['acquired'] = acquired
        if acquired:
            out._lock.release()

    out._modbus_client = _FakeClient(on_write=_probe)

    out._write_all_do(red=True, yellow=False, green=False, buzzer=True)

    assert got.get('acquired') is True, (
        '_write_all_do 在做网络 I/O 时还持着状态锁 —— '
        '网关一断线，get_status()/trigger_alarm()/acknowledge() 全部被堵死')


def test_get_status_responsive_while_io_blocked(out):
    """I/O 卡住时 `get_status()` 必须仍然能返回（前端不能跟着超时）。"""
    release = threading.Event()

    def _slow(address, value):
        release.wait(5)          # 模拟"网关不可达，写操作卡住"

    out._modbus_client = _FakeClient(on_write=_slow)

    t = threading.Thread(target=out._write_all_do,
                         kwargs={'red': True, 'yellow': False,
                                 'green': False, 'buzzer': True},
                         daemon=True)
    t.start()
    time.sleep(0.1)              # 确保写操作已经在进行中

    started = time.monotonic()
    status = out.get_status()
    elapsed = time.monotonic() - started
    release.set()
    t.join(timeout=5)

    assert status['enabled'] is True
    assert elapsed < 1.0, f'get_status() 被 I/O 卡了 {elapsed:.2f}s'


def test_flash_loop_does_not_hold_state_lock(out):
    """闪烁线程同理：它的每一次 DO 写都不许持状态锁。"""
    got = []

    def _probe(address, value):
        acquired = out._lock.acquire(timeout=0.5)
        got.append(acquired)
        if acquired:
            out._lock.release()

    out._modbus_client = _FakeClient(on_write=_probe)
    # ⚠️ `_flash_loop` 是**死循环**（`while self._flash_running`），
    # 必须放到线程里跑、再从外部把标志置 False —— 直接调用会永久卡住。
    out._flash_running = True
    out._flash_state = False
    t = threading.Thread(target=out._flash_loop, args=('red', 0.01), daemon=True)
    t.start()
    time.sleep(0.15)
    out._flash_running = False
    t.join(timeout=3)

    assert got, '前提不成立：闪烁循环一次都没写 DO'
    assert all(got), '闪烁线程持着状态锁做 I/O'


# ---------------------------------------------------------------------------
# 可观测
# ---------------------------------------------------------------------------

def test_status_exposes_do_channel_health(out):
    client = _FakeClient(fail=True)
    out._modbus_client = client
    out._write_do(0, True)

    ch = out.get_status()['do_channel']
    assert ch['fail_streak'] == 1
    assert ch['circuit_open'] is False
    assert ch['fail_threshold'] >= 2
    assert 'circuit_remaining_seconds' in ch


def test_trigger_alarm_reports_failure_with_open_circuit(out):
    """熔断打开时 `trigger_alarm` 仍要如实返回 False（round 251 的契约不变）。"""
    from 报警层.alarm_output import CIRCUIT_FAIL_THRESHOLD

    client = _FakeClient(fail=True)
    out._modbus_client = client
    for _ in range(CIRCUIT_FAIL_THRESHOLD):
        out._write_do(0, True)

    assert out.trigger_alarm('critical', '测试') is False
