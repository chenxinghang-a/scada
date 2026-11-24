# -*- coding: utf-8 -*-
"""round 396：急停与普通写入的毫秒级竞态（内存闸门）。

## 缺陷

`write_with_verification` 在设备锁内、真正下发前会再读一次急停状态
（`_estop_active` / 持久化状态）。但 `trigger_emergency_stop` **不拿设备锁**，
于是存在一个窗口：

    写入通过校验 → （急停生效）→ 写入下发 → 设备被这条"合法"写入启动

## 修法（方案 C）

* 维护内存态 `threading.Event`（`_estop_signal`）；
* `trigger_emergency_stop` **第一件事**就是 `.set()`（不拿锁，急停不排队）；
* `write_with_verification` 在**进入时**与**真正下发前**各做一次无锁
  `is_set()` —— 已置位直接拒绝，不进入 `client.write_*`；
* `reset_emergency_stop` 在完成硬件复位之后才 `.clear()`；
* 它与"读持久化状态"是**与**关系（重启后由恢复流程重新 `.set()`）。
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock


class _EchoClient:
    """写什么读回什么；`write_calls` 用于断言"有没有真的下发"。"""

    def __init__(self):
        self.connected = True
        self.last_read_source = 'fresh'
        self.coils: dict[int, bool] = {}
        self.write_calls: list[tuple] = []

    def write_single_coil(self, address, value):
        self.write_calls.append(('coil', int(address), bool(value)))
        self.coils[int(address)] = bool(value)
        return True

    def write_single_register(self, address, value):
        self.write_calls.append(('register', int(address), int(value)))
        return True

    def read_coils(self, address, count=1):
        return [self.coils.get(int(address), False)]

    def read_holding_registers(self, address, count=1):
        return [0] * count


class _Manager:
    def __init__(self, client=None):
        self.devices = {'press_01': {
            'protocol': 'modbus_tcp',
            'device_category': 'mechanical',
            'control': {'start': {'type': 'coil', 'address': 1, 'value': True}},
        }}
        self.simulation_mode = False
        self.client = client or _EchoClient()
        self.stopped: set[str] = set()
        self.stop_calls: list[str] = []
        self.start_calls: list[str] = []
        self.estop_override_calls: list[bool] = []
        self.on_override = None
        self.on_start = None

    def get_all_devices(self):
        return self.devices

    def get_client(self, _device_id):
        return self.client

    def get_device_status(self, device_id):
        return {'device_id': device_id, 'device_category': 'mechanical',
                'stopped': device_id in self.stopped}

    def stop_device(self, device_id, write=True):
        self.stop_calls.append(device_id)
        self.stopped.add(device_id)
        return True

    def start_device(self, device_id, write=True):
        self.start_calls.append(device_id)
        if self.on_start is not None:
            self.on_start(device_id)
        self.stopped.discard(device_id)
        return True

    def set_estop_override(self, active):
        self.estop_override_calls.append(bool(active))
        if self.on_override is not None:
            self.on_override(active)


def _dc(manager=None):
    from 智能层.device_control import DeviceControlSafety
    manager = manager or _Manager()
    # 空联锁 + 控制点位写入（control_point=True），避免与 Q6 的联锁闸门混淆。
    dc = DeviceControlSafety(MagicMock(), manager, None, config={'interlocks': []})
    return dc, manager


def _write(dc):
    return dc.write_with_verification(
        'press_01', 1, True, 'op', kind='coil', control_point=True)


# ---------------------------------------------------------------------------
# 1. 进入即拒绝（无需锁、无需读持久化状态）
# ---------------------------------------------------------------------------

def test_entry_rejects_when_signal_set():
    dc, dm = _dc()
    dc._estop_signal.set()
    # 注意：故意**不**置 `_estop_active`，证明拒绝来自内存闸门本身。
    assert dc._estop_active is False

    res = _write(dc)

    assert res['success'] is False
    assert res['reason_code'] == 'ESTOP_ACTIVE'
    assert res['blocked_by'] == 'estop_signal'
    assert dm.client.write_calls == [], '急停信号已置位，仍然向现场下发了写入'


# ---------------------------------------------------------------------------
# 2. 校验通过、尚未下发 —— 急停此刻生效，写入必须被拦下（核心窗口）
# ---------------------------------------------------------------------------

def test_estop_between_validation_and_dispatch_blocks_write(monkeypatch):
    dc, dm = _dc()
    orig_audit = dc._audit
    windows: list[bool] = []

    def hook(action, operator, detail):
        # `write_attempt` 是"校验已通过、即将写 client"的分界点。
        # 在这里置信号 = 精确模拟窗口内急停生效。
        if action == 'write_attempt':
            dc._estop_signal.set()
            windows.append(True)
        return orig_audit(action, operator, detail)

    monkeypatch.setattr(dc, '_audit', hook)

    res = _write(dc)

    assert windows, '用例前提不成立：没有走到 write_attempt 审计'
    assert res['success'] is False
    assert res['reason_code'] == 'ESTOP_ACTIVE'
    assert '紧急停机' in res['message']
    assert dm.client.write_calls == [], (
        '急停在校验后、下发前生效，写入仍然落地了')


# ---------------------------------------------------------------------------
# 3. 急停动作本身不被"慢写入/设备锁"阻塞
# ---------------------------------------------------------------------------

def test_trigger_does_not_wait_for_device_lock():
    dc, dm = _dc()
    # 模拟另一笔写入正持有设备锁（或卡在慢速总线上）。
    # owner 必须是 'op'：`validate_write` 的互斥检查会先放行，写入才能真的
    # 走到"阻塞在设备锁上"这一步（否则它会在校验阶段就以"他人操作中"返回）。
    assert dc.acquire_device_lock('press_01', 'op') is True

    result: dict = {}

    def _writer():
        # 这笔写入会先通过校验，然后阻塞在设备锁上
        result['res'] = _write(dc)

    writer = threading.Thread(target=_writer, daemon=True)
    writer.start()
    time.sleep(0.15)

    started = time.monotonic()
    estop = dc.trigger_emergency_stop('race test')
    elapsed = time.monotonic() - started

    assert dc._estop_signal.is_set() is True, '急停没有第一时间置内存信号'
    assert elapsed < 1.0, (
        f'急停等了 {elapsed:.2f}s —— 它不该排在设备锁/慢速总线后面')
    assert estop['success'] is True

    dc.release_device_lock('press_01')
    writer.join(timeout=5)
    assert not writer.is_alive()

    # 在途写入拿到锁后必须被拒（内存信号/急停状态任一即可），且没有下发
    assert result['res']['success'] is False
    assert result['res'].get('reason_code') == 'ESTOP_ACTIVE'
    assert dm.client.write_calls == []


def test_signal_is_set_before_persisting_and_stopping():
    """顺序：先 `.set()`，再持久化，再向 mechanical 下发停机。"""
    dc, dm = _dc()
    seen: dict[str, bool] = {}

    # `set_estop_override` 在 `_persist_estop()` 之后、stop_device 之前调用
    dm.on_override = lambda _active: seen.setdefault(
        'signal_at_override', dc._estop_signal.is_set())

    dc.trigger_emergency_stop('order test')

    assert seen['signal_at_override'] is True, (
        '持久化/下发停机时内存信号仍未置位 —— 窗口内写入会被放行')
    assert dm.stop_calls == ['press_01']


# ---------------------------------------------------------------------------
# 4. 解除：硬件复位之后才打开闸门
# ---------------------------------------------------------------------------

def test_reset_clears_signal_only_after_hardware_restart():
    dc, dm = _dc()
    dc.trigger_emergency_stop('manual')
    assert dc._estop_devices == ['press_01'], '前提不成立：急停应记下它停掉的设备'
    assert dc._estop_signal.is_set() is True

    seen: dict[str, bool] = {}
    dm.on_start = lambda _device_id: seen.setdefault(
        'signal_during_start', dc._estop_signal.is_set())

    res = dc.reset_emergency_stop('engineer')

    assert res['success'] is True
    assert seen.get('signal_during_start') is True, (
        '硬件复位还没做完就打开了闸门 —— 复位期间的写入会落到现场')
    assert dc._estop_signal.is_set() is False
    assert dc._estop_active is False


def test_reset_blocked_by_interlock_keeps_gate_closed():
    """联锁未解除时 `reset` 提前返回，闸门必须保持关闭。"""
    from 智能层.device_control import DeviceControlSafety

    dc, dm = _dc()
    dc.trigger_emergency_stop('manual')
    with dc._lock:
        dc._interlock_rules = {'il_x': {
            'id': 'il_x', 'name': '超温', 'enabled': True,
            'condition': {'type': 'threshold', 'device_id': 'press_01',
                          'register': 'temp', 'operator': '>', 'value': 1},
            'action': {'type': 'block_start', 'device_id': 'press_01'},
        }}
        dc._interlock_states = {'il_x': True}

    res = dc.reset_emergency_stop('engineer')

    assert res['success'] is False
    assert dc._estop_signal.is_set() is True, '解除失败却打开了内存急停闸门'


# ---------------------------------------------------------------------------
# 5. 跨重启：恢复持久化急停时必须同步置内存信号
# ---------------------------------------------------------------------------

def test_restore_persisted_estop_sets_signal():
    from 智能层.device_control import DeviceControlSafety

    db = MagicMock()

    def _get_state(key):
        if key == DeviceControlSafety.STATE_KEY_ESTOP:
            return {'active': True, 'time': None,
                    'reason': '重启前急停', 'affected_devices': []}
        return None

    db.get_system_state.side_effect = _get_state
    dc = DeviceControlSafety(db, _Manager(), None, config={'interlocks': []})

    assert dc._estop_active is True
    assert dc._estop_signal.is_set() is True, (
        '重启后持久化状态是急停，内存闸门却没置位 —— 写入入口的无锁检查会放行')
