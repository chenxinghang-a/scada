# -*- coding: utf-8 -*-
"""WebSocket 数据推送线程的两个缺陷回归（round 220）。

缺陷 1：重启推送线程会**多留一条永不退出的线程**
----------------------------------------------
`start_data_push_thread()` 重启时先把旧事件 `set()`，然后**把模块全局换成新事件**。
而线程体里写的是 `while not _push_stop_event.is_set()` —— 读的是**全局名字**（迟绑定），
于是旧线程下一轮看到的是**新**事件（未 set），永远不知道自己该停。

后果：每重启一次多一条推送线程 → 同一个库被重复查、同一条数据被 emit 多次；
而 `stop_data_push_thread()` 把全局置 None 后，旧线程还会在 while 条件处抛
`AttributeError`（带堆栈猝死，日志里看不出根因）。

修法：把事件捕获成**局部变量**交给线程，线程只认自己那一份。

缺陷 2：自适应推送频率数的是「房间数」不是「客户端数」
----------------------------------------------------
`socketio.server.manager.rooms['/']` 的形状是 `{room: {sid: ...}}`。
原实现 `len(rooms)`：20 个客户端全订阅同一台设备时只有 2 个键
（默认房间 `''` + `device_x`），于是被判成「客户端很少」，一直按最高频推送。
"""

import time
from unittest.mock import MagicMock

import pytest

import 展示层.websocket as ws


# ---------------------------------------------------------------------------
# 缺陷 2：客户端计数
# ---------------------------------------------------------------------------

def test_count_clients_empty():
    assert ws._count_clients({}) == 0
    assert ws._count_clients(None) == 0


def test_count_clients_dedups_sids_across_rooms():
    """同一连接同时在默认房间和设备房间，只能算一个客户端。"""
    rooms = {
        '': {'sid-a', 'sid-b', 'sid-c'},
        'device_d1': {'sid-a', 'sid-b'},
        'device_d2': {'sid-a'},
    }
    assert ws._count_clients(rooms) == 3, (
        "同一 sid 被重复计数 —— 会把「3 个客户端」算成 6"
    )


def test_count_clients_not_room_count():
    """关键差异：20 个客户端订阅同一台设备时，房间只有 2 个，客户端是 20 个。"""
    rooms = {'': {f'sid-{i}' for i in range(20)}, 'device_d1': {f'sid-{i}' for i in range(20)}}
    assert ws._count_clients(rooms) == 20, (
        f"数成了房间数（{len(rooms)}）而不是客户端数 —— 自适应降频永远不触发"
    )


def test_count_clients_tolerates_unknown_shape():
    """socket.io 版本不同、结构异常时不抛异常（推送线程不该因此挂掉）。"""
    assert ws._count_clients({'': 123}) == 0


# ---------------------------------------------------------------------------
# 缺陷 1：重启后旧线程必须真的退出
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_socketio(monkeypatch):
    """避免线程里访问真实 socketio（未初始化时是 None）。"""
    fake = MagicMock()
    fake.server.manager.rooms = {'/': {}}
    monkeypatch.setattr(ws, 'socketio', fake)
    yield fake
    ws.stop_data_push_thread()


def _start(db, dc):
    """启动推送线程并返回**这一次**起的线程对象。

    不要用 `threading.enumerate()` 差集去猜：差集里可能混进别的线程
    （日志、采集、定时器），猜错就会 join 到一条不是自己起的线程上 ——
    CI 上实测报过 `RuntimeError: cannot join thread before it is started`。
    """
    t = ws.start_data_push_thread(db, dc)
    assert t is not None, 'start_data_push_thread 没有返回线程对象'
    return t


def _fake_backends():
    db = MagicMock()
    db.get_device_summary.return_value = []
    db.get_database_stats.return_value = {}
    dc = MagicMock()
    dc.get_stats.return_value = {}
    return db, dc


def test_restart_stops_previous_thread(fake_socketio):
    """重启推送线程后，**旧线程必须退出**（否则线程数无限增长）。"""
    db, dc = _fake_backends()

    t1 = _start(db, dc)
    t2 = _start(db, dc)
    assert t1 is not t2, '两次启动拿到同一个线程对象'

    t1.join(3)
    assert not t1.is_alive(), (
        '重启后旧推送线程仍在运行 —— 每重启一次多一条线程，DB 查询翻倍、'
        '同一条数据被 emit 多次（根因：线程读的是模块全局事件，迟绑定）'
    )
    assert t2.is_alive(), '新线程没起来'


def test_stop_clears_global_and_thread_exits(fake_socketio):
    db, dc = _fake_backends()

    t = _start(db, dc)

    ws.stop_data_push_thread()

    t.join(3)
    assert not t.is_alive(), 'stop 之后线程仍存活'
    assert ws._push_stop_event is None, 'stop 之后没有清空全局事件引用'
