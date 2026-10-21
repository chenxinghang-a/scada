"""
WebSocket 离线消息补发（round 239）

背景（**先说清楚严重性，别夸大**）
--------------------------------
`core/ws_offline_queue.py` 实现了一个完整的「断线期间缓存消息、重连后批量推送」
队列（`enqueue` / `dequeue` / `peek` / `get_queue_size` / `cleanup_expired`），
但全仓库**只有 `cleanup_expired()` 有调用方** —— 维护任务每 10 分钟清理一个
**永远为空**的队列。从来没有代码 enqueue，也没有代码 dequeue。

实际损失有多大？**没有"报警永久丢失"那么严重**，因为前端还有 HTTP 兜底：
- Dashboard 每 2~5 秒 `GET /api/alarms?limit=50` 覆盖报警列表；
- AlarmOutput 每次 `loadStatus()` 拉 `/broadcast/history?limit=30`。

所以真实缺口是**精度与延迟**，不是永久丢失：
1. 断线重连后，断线期间发生的报警要等下一次轮询（最多 5 秒）才出现在
   首页列表/顶部横幅上，而且**只有最近 50 条**；
2. 离线时间较长时（报警风暴），更早的报警不会进入首页列表；
3. 更根本的是：代码库里摆着一整套"断线补发"实现、还被维护任务定期清理，
   看代码的人会以为这个能力已经上线 —— **声明与实现不符**本身就是要修的东西。

修复后的行为（本文件锁死）：
- 报警/广播在广播的同时，对**当前离线的用户**按用户入队；
- 连接成功（认证后）立即补发该用户积压的消息，不必等轮询；
- 补发中途连接又断 → 剩余消息**放回队列**，不是丢掉；
- 判断粒度是用户而不是 sid（sid 每次连接都变，按 sid 归档等于永远补不到）。
"""

import os
import sys
import time

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import 展示层.websocket as ws  # noqa: E402
from core.ws_offline_queue import OfflineMessageQueue  # noqa: E402


@pytest.fixture
def fresh_state(monkeypatch):
    """每个用例一份干净的连接注册表 + 离线队列（不串味、不影响真实单例）"""
    monkeypatch.setattr(ws, '_connected_clients', set())
    monkeypatch.setattr(ws, '_sid_user', {})
    monkeypatch.setattr(ws, '_known_users', {})
    queue = OfflineMessageQueue(max_messages_per_client=100, max_age_seconds=3600)
    monkeypatch.setattr(ws, 'offline_queue', queue)
    return queue


def _login(sid: str, username: str):
    """模拟一次成功的 connect 登记（不含 socketio 事件绑定）"""
    with ws._clients_lock:
        ws._connected_clients.add(sid)
        ws._sid_user[sid] = username
        ws._known_users[username] = time.time()


def _logout(sid: str):
    with ws._clients_lock:
        ws._connected_clients.discard(sid)
        ws._sid_user.pop(sid, None)


# ============================================================
# 1. 离线用户判定
# ============================================================

class TestOfflineUsernames:
    def test_no_known_users_means_no_targets(self, fresh_state):
        assert ws._offline_usernames() == []

    def test_user_with_live_connection_is_not_offline(self, fresh_state):
        _login('sid1', 'alice')
        assert ws._offline_usernames() == []

    def test_user_without_connection_is_offline(self, fresh_state):
        _login('sid1', 'alice')
        _login('sid2', 'bob')
        _logout('sid1')
        assert ws._offline_usernames() == ['alice']

    def test_one_live_connection_is_enough(self, fresh_state):
        """同一用户两个客户端，断一个不算离线（按用户判定，不按 sid）。"""
        _login('sid1', 'alice')
        _login('sid2', 'alice')
        _logout('sid1')
        assert ws._offline_usernames() == []

    def test_reconnect_with_new_sid_keeps_user_known(self, fresh_state):
        """重连后 sid 变了，但用户名不变 —— 这正是不能用 sid 做 key 的原因。"""
        _login('sid-old', 'alice')
        _logout('sid-old')
        assert ws._offline_usernames() == ['alice']
        _login('sid-new', 'alice')
        assert ws._offline_usernames() == []

    def test_stale_user_stops_being_a_target(self, fresh_state):
        """长期不连的用户不再入队（否则账号删了也会被永久塞消息）。"""
        _login('sid1', 'ghost')
        _logout('sid1')

        # 把最后连接时间推到 TTL 之外
        ws._known_users['ghost'] = time.time() - ws.OFFLINE_TARGET_TTL - 1

        assert ws._offline_usernames() == []
        assert ws._queue_for_offline_users('alarm', {'alarm_id': 'A1'}) == 0

    def test_fresh_user_is_still_a_target(self, fresh_state):
        _login('sid1', 'alice')
        _logout('sid1')
        assert ws._offline_usernames() == ['alice']


# ============================================================
# 2. 报警/广播对离线用户入队
# ============================================================

class TestQueueForOfflineUsers:
    def test_queues_only_for_offline_users(self, fresh_state):
        _login('sid1', 'alice')      # 在线
        _login('sid2', 'bob')
        _logout('sid2')              # bob 离线

        n = ws._queue_for_offline_users('alarm', {'alarm_id': 'A1'})

        assert n == 1
        assert fresh_state.get_queue_size('bob') == 1
        assert fresh_state.get_queue_size('alice') == 0

    def test_nobody_offline_queues_nothing(self, fresh_state):
        _login('sid1', 'alice')
        assert ws._queue_for_offline_users('alarm', {'alarm_id': 'A1'}) == 0
        assert fresh_state.get_stats()['total_enqueued'] == 0

    def test_alarm_priority_beats_broadcast(self, fresh_state):
        """队列满时先挤掉广播，不能把报警挤掉。"""
        _login('sid1', 'bob')
        _logout('sid1')

        for i in range(100):
            ws._queue_for_offline_users('broadcast', {'n': i}, priority=0)
        assert fresh_state.get_queue_size('bob') == 100

        ws._queue_for_offline_users('alarm', {'alarm_id': 'CRITICAL'}, priority=10)

        remaining = fresh_state.peek('bob')
        assert len(remaining) == 100
        assert any(m['data'].get('alarm_id') == 'CRITICAL' for m in remaining), \
            '报警被广播挤掉了 —— 优先级没生效'

    def test_emit_alarm_queues_for_offline_user(self, fresh_state, monkeypatch):
        """端到端：emit_alarm 广播后必须给离线用户入队。"""
        sent = []

        class _FakeSocketIO:
            def emit(self, event, data, **kw):
                sent.append((event, data))

        monkeypatch.setattr(ws, 'socketio', _FakeSocketIO())

        _login('sid1', 'bob')
        _logout('sid1')

        ws.emit_alarm({'alarm_id': 'A9', 'level': 'critical'})

        assert sent == [('alarm', {'alarm_id': 'A9', 'level': 'critical'})]
        assert fresh_state.get_queue_size('bob') == 1

    def test_emit_broadcast_queues_for_offline_user(self, fresh_state, monkeypatch):
        class _FakeSocketIO:
            def emit(self, event, data, **kw):
                pass

        monkeypatch.setattr(ws, 'socketio', _FakeSocketIO())
        _login('sid1', 'bob')
        _logout('sid1')

        ws.emit_broadcast({'msg': 'hello'})
        assert fresh_state.get_queue_size('bob') == 1

    def test_nothing_queued_when_socketio_is_down(self, fresh_state, monkeypatch):
        """socketio 没初始化时整条实时通道都废了，入队没有意义。"""
        monkeypatch.setattr(ws, 'socketio', None)
        _login('sid1', 'bob')
        _logout('sid1')

        ws.emit_alarm({'alarm_id': 'A1'})
        assert fresh_state.get_stats()['total_enqueued'] == 0


# ============================================================
# 3. 重连补发
# ============================================================

class TestReplay:
    def test_replays_queued_messages(self, fresh_state):
        fresh_state.enqueue('alice', 'alarm', {'alarm_id': 'A1'}, priority=10)
        fresh_state.enqueue('alice', 'alarm', {'alarm_id': 'A2'}, priority=10)

        delivered = []
        result = ws._replay_offline_messages('alice', send=lambda e, d: delivered.append((e, d)))

        assert result == {'sent': 2, 'requeued': 0}
        assert [d['alarm_id'] for _, d in delivered] == ['A1', 'A2']
        assert fresh_state.get_queue_size('alice') == 0

    def test_replay_orders_by_priority(self, fresh_state):
        fresh_state.enqueue('alice', 'broadcast', {'n': 1}, priority=0)
        fresh_state.enqueue('alice', 'alarm', {'n': 2}, priority=10)

        delivered = []
        ws._replay_offline_messages('alice', send=lambda e, d: delivered.append(e))

        assert delivered[0] == 'alarm', '报警必须排在广播前面'

    def test_no_messages_is_noop(self, fresh_state):
        delivered = []
        result = ws._replay_offline_messages('alice', send=lambda e, d: delivered.append(e))
        assert result == {'sent': 0, 'requeued': 0}
        assert delivered == []

    def test_empty_username_is_noop(self, fresh_state):
        assert ws._replay_offline_messages('', send=lambda e, d: None) == {'sent': 0, 'requeued': 0}

    def test_partial_failure_requeues_rest(self, fresh_state):
        """补发到一半连接又断：剩下的必须放回队列，不能丢。

        这是本功能最要命的一条 —— 它们多半是报警，丢了就等于没补。
        """
        for i in range(5):
            fresh_state.enqueue('alice', 'alarm', {'alarm_id': f'A{i}'}, priority=10)

        calls = []

        def flaky_send(event, data):
            calls.append(data['alarm_id'])
            if len(calls) == 3:
                raise ConnectionError('连接又断了')

        result = ws._replay_offline_messages('alice', send=flaky_send)

        assert result['sent'] == 2
        assert result['requeued'] == 3
        remaining = [m['data']['alarm_id'] for m in fresh_state.peek('alice')]
        assert remaining == ['A2', 'A3', 'A4'], f'未发成功的消息没回到队列: {remaining}'

    def test_replay_survives_broken_queue(self, fresh_state, monkeypatch):
        """队列本身抛异常时不能把 connect 流程带崩（否则客户端连不上）。"""
        class _Boom:
            def dequeue(self, *a, **kw):
                raise RuntimeError('queue broken')

        monkeypatch.setattr(ws, 'offline_queue', _Boom())
        assert ws._replay_offline_messages('alice', send=lambda e, d: None) == \
            {'sent': 0, 'requeued': 0}

    def test_full_cycle_alarm_while_offline_then_reconnect(self, fresh_state, monkeypatch):
        """完整链路：在线 → 断线 → 期间来报警 → 重连补发。"""
        class _FakeSocketIO:
            def emit(self, event, data, **kw):
                pass

        monkeypatch.setattr(ws, 'socketio', _FakeSocketIO())

        _login('sid-1', 'alice')
        _logout('sid-1')
        ws.emit_alarm({'alarm_id': 'WHILE-OFFLINE'})

        # 重连（新 sid）
        _login('sid-2', 'alice')
        delivered = []
        ws._replay_offline_messages('alice', send=lambda e, d: delivered.append(d))

        assert [d['alarm_id'] for d in delivered] == ['WHILE-OFFLINE']
        assert fresh_state.get_queue_size('alice') == 0


# ============================================================
# 4. 静态守卫：不能退回「零接线」
# ============================================================

def test_offline_queue_is_actually_wired():
    """`ws_offline_queue` 必须有真实的 enqueue 与 dequeue 调用点。

    修复前整个模块只有 `cleanup_expired()` 一个调用方 —— 队列永远是空的，
    功能形同虚设。这条守卫防止谁把接线删了却没意识到功能整条消失。
    """
    import pathlib
    import re

    src_path = pathlib.Path(PROJECT_ROOT) / '展示层' / 'websocket.py'
    source = src_path.read_text(encoding='utf-8')

    assert re.search(r'offline_queue\.enqueue\(', source), \
        'websocket.py 里没有 offline_queue.enqueue() —— 断线期间的报警不会入队'
    assert re.search(r'offline_queue\.dequeue\(', source), \
        'websocket.py 里没有 offline_queue.dequeue() —— 重连后不会补发'


def test_replay_is_called_on_connect():
    """补发必须挂在 connect 处理里，否则队列会一直积压到过期。"""
    import pathlib
    import re

    source = (pathlib.Path(PROJECT_ROOT) / '展示层' / 'websocket.py').read_text(encoding='utf-8')

    assert re.search(r'_replay_offline_messages\(username\)', source), \
        'handle_connect 里没有调用 _replay_offline_messages(username)'
