"""WebSocket 健康检查可见性回归测试。

背景（2026-09 审计遗留项）
--------------------------
`展示层/api/api_health.py` 一直这样写：

    try:
        from 展示层.websocket import get_connected_count
        result['websocket'] = {'status': 'ok', 'connected_clients': get_connected_count()}
    except Exception:
        result['websocket'] = {'status': 'unknown'}

但 `get_connected_count` **在 websocket.py 里根本不存在** →
每次都抛 ImportError → 被 `except Exception` 吞掉 →
`/api/health` 的 websocket 状态**永远是 'unknown'**。

后果：WebSocket 是实时数据推送的唯一通道，而监控里**完全看不到它是否可用** ——
连接数为 0、或连接全挂了，健康检查都只回一个 'unknown'。

本测试锁死「该函数存在、可 import、且返回真实连接数」。
"""
import threading

import pytest


def test_get_connected_count_is_importable():
    """核心回归：健康检查依赖的 import 必须成立。

    修复前这里是 ImportError，被 api_health 的 except 吞掉 → 永远 'unknown'。
    """
    from 展示层.websocket import get_connected_count
    assert callable(get_connected_count)


def test_get_connected_count_returns_int():
    from 展示层.websocket import get_connected_count
    n = get_connected_count()
    assert isinstance(n, int)
    assert n >= 0


def test_count_reflects_module_level_registry():
    """计数必须读的是**模块级**注册表。

    若 `init_socketio` 内部再定义同名局部变量，会遮蔽模块级对象，
    导致这里的写入读不到 —— 这正是重构时最容易踩的坑。
    """
    import 展示层.websocket as ws

    before = ws.get_connected_count()
    fake_sid = 'pytest-probe-sid'
    with ws._clients_lock:
        ws._connected_clients.add(fake_sid)
    try:
        assert ws.get_connected_count() == before + 1, (
            '模块级注册表与 get_connected_count() 读的不是同一份数据'
        )
    finally:
        with ws._clients_lock:
            ws._connected_clients.discard(fake_sid)

    assert ws.get_connected_count() == before


def test_init_socketio_does_not_shadow_module_registry():
    """静态守卫：`init_socketio` 内不得再定义 `_connected_clients` / `_clients_lock`。

    这类遮蔽在运行期只表现为「计数永远是 0」，不报错、极难排查，
    所以用源码扫描把它挡在提交前。
    """
    import inspect
    import 展示层.websocket as ws

    src = inspect.getsource(ws.init_socketio)
    for forbidden in ('_connected_clients = set()', '_clients_lock = threading.Lock()'):
        assert forbidden not in src, (
            f'init_socketio 内出现 `{forbidden}`，会遮蔽模块级对象，'
            '导致 get_connected_count() 永远返回 0'
        )


def test_count_is_thread_safe():
    """并发读写下计数不应崩、也不应算出负数"""
    import 展示层.websocket as ws

    errors = []

    def churn():
        try:
            for i in range(200):
                sid = f'probe-{threading.get_ident()}-{i}'
                with ws._clients_lock:
                    ws._connected_clients.add(sid)
                ws.get_connected_count()
                with ws._clients_lock:
                    ws._connected_clients.discard(sid)
        except Exception as e:      # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=churn) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f'并发访问抛异常: {errors}'
    assert ws.get_connected_count() >= 0
