"""
WebSocket模块
实现实时数据推送
"""

import time
import math
import logging
import threading
import yaml
from pathlib import Path
from flask import request
from flask_socketio import SocketIO, emit, join_room, leave_room
from datetime import datetime
from core.ws_compress import compress_message
from core.ws_offline_queue import offline_queue
import paths

logger = logging.getLogger(__name__)

# 全局SocketIO实例
socketio = None

# 已连接的 WebSocket 客户端 sid 集合。
#
# 放在**模块级**（而不是 init_socketio 内部的闭包局部变量）是为了让
# `get_connected_count()` 能被外部 import —— `展示层/api/api_health.py`
# 一直尝试 `from 展示层.websocket import get_connected_count`，
# 但该函数此前**根本不存在**，于是健康检查里的 `except Exception`
# 每次都吞掉 ImportError，`/api/health` 的 websocket 状态**永远是 'unknown'**，
# WebSocket 是否可用在监控里完全不可见。
_connected_clients: set[str] = set()

# sid → 已认证用户名。
#
# 为什么需要它：离线消息补发必须按**用户**归档，不能按 sid ——
# sid 是每次连接新分配的，客户端重连后 sid 完全不同，用 sid 做 key
# 等于永远取不到自己断线前缓存的消息（`offline_queue` 会一直空着）。
_sid_user: dict[str, str] = {}

# 曾经成功连接过的用户名 → 最后一次连接的时间戳 = 离线消息补发的目标集合。
#
# 只在 connect 成功后写入，因此「服务启动后从没人登录过」这段时间的报警
# 不会被补发（没有目标用户可言）—— 这是有意的，不要为了"看起来完整"
# 把所有人名都塞进来。
#
# 存时间戳而不是只存名字：账号被删/长期不用的用户如果不清理，会**永久**
# 被当作补发目标 —— 每条报警都往他的队列塞一份，白占内存与 CPU。
# 超过 `OFFLINE_TARGET_TTL` 没连过的用户不再入队（队列本身 1 小时就过期，
# 这个 TTL 只是防集合无限增长）。
_known_users: dict[str, float] = {}

# 离线补发的目标有效期（秒）
OFFLINE_TARGET_TTL = 24 * 3600

# 离线补发的一次性上限。`offline_queue` 每个客户端最多 100 条，
# 取 200 保证一次能取完，不会出现「补了一半剩下的要等下次重连」。
OFFLINE_REPLAY_LIMIT = 200

_clients_lock = threading.Lock()


def get_connected_count() -> int:
    """当前 WebSocket 连接数（线程安全）。供 /api/health 等监控端点使用。"""
    with _clients_lock:
        return len(_connected_clients)


def _emit_to_current_client(event, data):
    """把一条消息发给当前请求上下文里的那个客户端（socket.io 的 emit）。"""
    emit(event, data)


def _offline_usernames() -> list[str]:
    """当前「登录过但一个连接都不在线」的用户名列表。

    注意语义：判断粒度是**用户**，不是设备/标签页。同一用户开了两个客户端，
    只要还有一个连着，就不算离线、不入队 —— 另一个端断线期间的消息不会补发。
    按用户归档是刻意的：sid 每次连接都变，按 sid 归档等于永远补不到。
    """
    now = time.time()
    with _clients_lock:
        online = set(_sid_user.values())
        return [
            u for u, last_seen in _known_users.items()
            if u not in online and now - last_seen <= OFFLINE_TARGET_TTL
        ]


def _queue_for_offline_users(event: str, data, priority: int = 0) -> int:
    """把一条事件按用户归档给所有当前离线的用户，返回入队用户数。

    只用于**事件类**消息（报警 / 广播）。像 `data_update` 这种高频实时值
    不要走这里 —— 重连后直接拉最新值即可，回放历史实时值是纯噪声，
    而且 100 条的队列上限会被几秒钟的量灌满，把真正的报警挤掉。
    """
    queued = 0
    for username in _offline_usernames():
        try:
            if offline_queue.enqueue(username, event, data, priority=priority):
                queued += 1
        except Exception as e:
            logger.warning("离线消息入队失败 user=%s event=%s: %s", username, event, e)
    if queued:
        logger.info("断线用户离线入队: event=%s, 用户数=%d", event, queued)
    return queued


def _replay_offline_messages(username: str, send=None) -> dict:
    """把 `username` 断线期间积压的消息补发出去。

    Args:
        username: 已认证用户名
        send: 发送函数 `(event, data) -> None`；默认发到当前请求上下文的客户端。
              测试注入假的 send，不需要真的 socketio。

    Returns:
        ``{'sent': 已发条数, 'requeued': 发送中断后放回队列的条数}``
    """
    result = {'sent': 0, 'requeued': 0}
    if not username:
        return result
    send = send or _emit_to_current_client

    try:
        messages = offline_queue.dequeue(username, limit=OFFLINE_REPLAY_LIMIT)
    except Exception as e:
        logger.error("读取离线消息失败 user=%s: %s", username, e)
        return result

    if not messages:
        return result

    for idx, msg in enumerate(messages):
        try:
            send(msg.get('event'), msg.get('data'))
            result['sent'] += 1
        except Exception as e:
            # 连接中途又断了：把这条和剩下的**放回队列**。
            # 它们已经被 dequeue 出来，直接丢就真没了 —— 而它们多半是报警。
            rest = messages[idx:]
            requeue_failed = 0
            for m in rest:
                try:
                    offline_queue.enqueue(username, m.get('event'), m.get('data'))
                except Exception as re_err:
                    # 重新入队失败 = 这条消息真的没了（多半是报警），必须留痕
                    requeue_failed += 1
                    logger.warning(
                        "离线消息重新入队失败 user=%s event=%s: %s",
                        username, m.get('event'), re_err,
                    )
            result['requeued'] = len(rest) - requeue_failed
            result['requeue_failed'] = requeue_failed
            logger.warning(
                "离线消息补发中断 user=%s 已发=%d 重新入队=%d: %s",
                username, result['sent'], result['requeued'], e,
            )
            return result

    logger.info("离线消息补发完成 user=%s 条数=%d", username, result['sent'])
    return result


def _load_cors_origins():
    """从配置文件加载CORS允许的源"""
    try:
        config_path = paths.resolve('配置/system.yaml')
        if config_path.exists():
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
            origins = config.get('web', {}).get('cors', {}).get('origins', [])
            if origins and '*' not in origins:
                return origins
    except Exception as e:
        logger.warning(f"加载CORS配置失败: {e}")
    # 默认只允许本地开发 + **桌面版**
    #
    # ⚠️ `'file://'` 不能漏：桌面版（Electron）页面是 file:// 加载的，
    #    Origin 就是字符串 'file://'。少了它 engine.io 回
    #    「file:// is not an accepted origin.」→ 实时通道（性能监控/数据大屏/
    #    报警输出）**全废**。这条**降级路径**尤其危险：配置一旦读不到，
    #    功能会静默消失，而日志里只有一句「加载CORS配置失败」。
    #    守卫：tests/test_websocket_cors_origins.py
    return ['file://', 'http://localhost:5000', 'http://127.0.0.1:5000']


def _payload(data):
    """把 Socket.IO 事件 payload 规整成 dict。

    `data` 直接来自客户端，类型完全不可控：None / 字符串 / 数字 / 列表都可能。
    原先各 handler 写的是 `(data or {}).get(...)` —— 那只防住了 None 和空值，
    `"abc".get` 照样抛 AttributeError。

    在 Socket.IO 事件处理器里，这个异常表现为**服务端刷堆栈、客户端收不到
    任何回应**，而且连接不会断 —— 比直接报错更难排查（客户端只会觉得"卡住了"）。
    """
    return data if isinstance(data, dict) else {}


def init_socketio(app, database, data_collector):
    """
    初始化WebSocket

    Args:
        app: Flask应用实例
        database: 数据库实例
        data_collector: 数据采集器实例

    Returns:
        SocketIO: SocketIO实例
    """
    global socketio

    cors_origins = _load_cors_origins()

    # PyInstaller 打包环境下 eventlet/gevent 不可用，直接用 threading
    import sys
    is_frozen = getattr(sys, 'frozen', False)

    async_mode = 'threading'
    if not is_frozen:
        try:
            import eventlet
            async_mode = 'eventlet'
        except ImportError:
            try:
                import gevent
                async_mode = 'gevent'
            except ImportError:
                async_mode = 'threading'

    try:
        socketio = SocketIO(app, cors_allowed_origins=cors_origins, async_mode=async_mode)
        logger.info(f"SocketIO 初始化成功，异步模式: {async_mode}")
    except Exception as e:
        logger.error(f"SocketIO 初始化失败: {e}，尝试使用 threading 模式")
        try:
            socketio = SocketIO(app, cors_allowed_origins=cors_origins, async_mode='threading')
            logger.info("SocketIO 使用 threading 模式初始化成功")
        except Exception as e2:
            logger.error(f"SocketIO 初始化彻底失败: {e2}")
            socketio = None
            return None

    # 连接数限制配置
    MAX_CONNECTIONS = 100
    # 说明：`_connected_clients` / `_clients_lock` / `_client_count()` 已提升到
    # **模块级**（见文件顶部），供 `get_connected_count()` 对外暴露。
    # 这里**不能**再定义同名局部变量 —— 那会遮蔽模块级对象，
    # 导致模块级的计数永远为空、而健康检查读到 0。
    # socketio 是 threading 模式，connect/disconnect 在各自的工作线程里跑，
    # 对 set 的读-改-写必须加锁，否则并发连接时会出现「检查时未满、写入时已满」，
    # 实际连接数突破 MAX_CONNECTIONS；纯 len() 的读在 CPython 下虽不会崩，
    # 但和写入交错仍会拿到过期计数，日志与限流判断都不可信。
    _client_count = get_connected_count

    # 注册事件处理
    @socketio.on('connect')
    def handle_connect():
        """客户端连接（需要JWT认证 + 连接数限制）"""
        token = request.args.get('token')
        if not token:
            logger.warning(f"WebSocket连接被拒绝: 未提供token, sid={request.sid}")
            return False  # 拒绝连接

        auth_manager = app.auth_manager
        user = auth_manager.verify_token(token)
        if not user:
            logger.warning(f"WebSocket连接被拒绝: token无效, sid={request.sid}")
            return False  # 拒绝连接

        # 认证通过后才占名额：容量检查与登记放在同一临界区内，
        # 保证「检查 → 写入」原子，不会超卖连接数。
        username = user['username']
        with _clients_lock:
            if len(_connected_clients) >= MAX_CONNECTIONS:
                logger.warning(f"WebSocket连接被拒绝: 超过最大连接数 {MAX_CONNECTIONS}, sid={request.sid}")
                return False
            _connected_clients.add(request.sid)
            _sid_user[request.sid] = username
            _known_users[username] = time.time()
            current = len(_connected_clients)

        logger.info(f"客户端连接: {request.sid}, 用户: {username}, 当前连接数: {current}")
        # Prometheus：当前连接数（round 253）。此前该 gauge 从未被设置，
        # 看板上恒 0 —— 有连接也显示 0，等于这个指标不存在。
        try:
            from core.metrics import WS_CONNECTIONS
            WS_CONNECTIONS.set(current)
        except Exception as e:      # noqa: BLE001
            logger.debug(f"WebSocket 连接数指标打点失败: {e}")
        emit('connected', {'message': '连接成功', 'user': username})

        # 补发断线期间积压的报警/广播。
        #
        # 此前 `core/ws_offline_queue.py` 整个模块只被维护任务调用
        # `cleanup_expired()` —— 从来没人 enqueue、也没人 dequeue，队列永远是空的。
        #
        # 注意别夸大严重性：前端还有 HTTP 兜底（Dashboard 每 2~5 秒拉
        # `/api/alarms?limit=50`，AlarmOutput 拉 `/broadcast/history?limit=30`），
        # 所以断线期间的报警**不是永久丢失**。真实缺口是「精度与延迟」：
        # 重连后要等下一次轮询才看到，且只有最近 50 条；更根本的是代码库里
        # 摆着一整套没接线的实现、还被定期清理，看代码的人会以为它已上线。
        _replay_offline_messages(username)

    @socketio.on('disconnect')
    def handle_disconnect():
        """客户端断开"""
        with _clients_lock:
            _connected_clients.discard(request.sid)
            _sid_user.pop(request.sid, None)
            current = len(_connected_clients)
        logger.info(f"客户端断开: {request.sid}, 当前连接数: {current}")
        try:
            from core.metrics import WS_CONNECTIONS
            WS_CONNECTIONS.set(current)
        except Exception as e:      # noqa: BLE001
            logger.debug(f"WebSocket 连接数指标打点失败: {e}")

    @socketio.on('heartbeat')
    def handle_heartbeat(data):
        """心跳保活（客户端定期发送，服务端回复确认）"""
        emit('heartbeat_ack', {
            'timestamp': datetime.now().isoformat(),
            'client_timestamp': _payload(data).get('timestamp')
        })

    @socketio.on('subscribe')
    def handle_subscribe(data):
        """订阅设备数据"""
        # `data` 可能是 None（客户端发了空包），也可能是字符串/数字/列表。
        # 原先写 `(data or {}).get` 只防住了 None —— `"abc".get` 会抛
        # AttributeError，在事件处理器里表现为服务端刷堆栈、客户端收不到回应。
        device_id = _payload(data).get('device_id')
        if not device_id:
            logger.warning("订阅请求缺少 device_id，已忽略")
            emit('error', {'message': '订阅需要 device_id'})
            return
        logger.info(f"客户端订阅设备: {device_id}")

        # 加入设备房间
        join_room(f'device_{device_id}')

        # 发送最新数据（字典格式：{register_name: {device_id, register_name, value, ...}}）
        latest_data = database.get_latest_data(device_id)
        if latest_data:
            emit('data_update', latest_data)

    @socketio.on('unsubscribe')
    def handle_unsubscribe(data):
        """取消订阅"""
        device_id = _payload(data).get('device_id')
        if not device_id:
            logger.warning("取消订阅请求缺少 device_id，已忽略")
            emit('error', {'message': '取消订阅需要 device_id'})
            return
        logger.info(f"客户端取消订阅设备: {device_id}")

        # 离开设备房间
        leave_room(f'device_{device_id}')

    # 启动数据推送线程
    start_data_push_thread(database, data_collector)

    return socketio


# 模块级停止事件，供外部调用 stop_data_push_thread()
_push_stop_event = None


def _count_clients(rooms: dict | None) -> int:
    """数「当前连接的不同客户端数」。

    socket.io 的 `manager.rooms` 形状是 `{room: {sid: ...}}`：
    键是**房间名**（含默认房间 `''`），值是该房间里的会话 id 集合。
    原实现直接 `len(rooms)` —— 数的是**房间数**，不是客户端数：
    20 个客户端全订阅同一台设备时 `rooms` 只有 2 个键（`''` 和 `device_x`），
    自适应频率据此以为「客户端很少」，一直按最高频推送。

    这里对**会话 id 去重**：同一个连接既在默认房间又在设备房间，只算一次。
    """
    if not rooms:
        return 0
    sids: set[str] = set()
    for members in rooms.values():
        if not members:
            continue
        try:
            sids.update(members)
        except TypeError:      # 非预期结构（不同 socket.io 版本）→ 不猜，按 0 处理
            return 0
    return len(sids)


# 上一轮的设备连接状态：device_id -> (connected, status, stopped)
#
# 模块级是为了「跨轮次记住基线」；测试直接重置这个 dict 即可。
_device_status_cache: dict[str, tuple] = {}


def _diff_device_status(brief_statuses, cache: dict) -> list[tuple[str, dict]]:
    """对比上一轮的设备连接状态，返回发生变化的那几台。

    Args:
        brief_statuses: `device_manager.get_all_status(brief=True)` 的结果
            （每条含 `device_id` / `connected` / `status` / `stopped`）
        cache: 上一轮基线，**会被就地更新**

    Returns:
        ``[(device_id, {'connected':…, 'status':…, 'stopped':…}), …]``

    设计取舍：
    - **首轮只建基线不推**。启动瞬间 100 台设备全部「从无到有」，
      全推一遍等于把一次 HTTP 轮询复制到 WebSocket 上，纯噪声。
    - **只推变化**。设备状态几小时不变是常态，逐轮全推会让
      「实时推送」退化成高频轮询。
    - **消失的设备清基线**。否则设备删了再加回来（同 id）时，
      缓存里还留着旧值，判不出变化 → 再也不推。
    """
    changes: list[tuple[str, dict]] = []
    seen: set[str] = set()

    for st in brief_statuses or ():
        try:
            device_id = st.get('device_id')
        except AttributeError:
            continue
        if not device_id:
            continue
        seen.add(device_id)

        cur = (
            bool(st.get('connected')),
            st.get('status'),
            bool(st.get('stopped')),
        )
        prev = cache.get(device_id)
        if prev == cur:
            continue
        cache[device_id] = cur
        if prev is None:
            continue        # 首轮：只建基线

        changes.append((device_id, {
            'connected': cur[0],
            'status': cur[1],
            'stopped': cur[2],
        }))

    for device_id in list(cache):
        if device_id not in seen:
            del cache[device_id]

    return changes


def _push_device_status_changes(data_collector) -> int:
    """把本轮变化的设备连接状态推给客户端，返回推送条数。

    `emit_device_status()` 此前**零调用** —— 前端
    `socket.on('device_status')` 一直在监听一个永不到来的事件，
    设备在线/离线只能靠 2~5 秒的 HTTP 轮询兜底。

    数据源用 `get_all_status(brief=True)` 而不是 `get_device_status()`：
    后者会**尝试自动重连**并写状态缓存，在推送线程里每轮调 100 次等于
    把「查询状态」变成「反复发起连接」，副作用太大。
    """
    device_manager = getattr(data_collector, 'device_manager', None)
    if device_manager is None:
        return 0
    try:
        brief = device_manager.get_all_status(brief=True)
    except Exception as e:
        logger.debug(f"读取设备状态列表失败: {e}")
        return 0

    changes = _diff_device_status(brief, _device_status_cache)
    for device_id, payload in changes:
        emit_device_status(device_id, payload)
    return len(changes)


def _base_push_interval() -> float:
    """数据推送的基础间隔（秒），读运行时配置 `websocket_push_interval_ms`。

    这是 `RuntimeConfigManager` 里**唯一**接了线的键：改它立即生效，不用重启。
    其余键（`device_poll_interval_sec` / `max_concurrent_requests` 等）
    只是写内存、不影响任何模块 —— 见 `core/ops_tools.WIRED_KEYS` 与
    `get_wiring_status()`。

    返回值夹在 0.5~10 秒：
    - 小于 0.5 秒等于忙等，会把 SQLite 查询打成连发；
    - 大于 10 秒时「实时推送」名存实亡，且无客户端时的 10 秒降频档也被绕过。
    非有限值 / 非法类型一律退回默认 2 秒，不让一个手滑的配置把推送线程搞死。
    """
    try:
        from core.ops_tools import runtime_config_manager
        ms = float(runtime_config_manager.get('websocket_push_interval_ms', 2000))
    except Exception as e:
        logger.debug(f"读取 websocket_push_interval_ms 失败，用默认 2 秒: {e}")
        return 2.0
    if not math.isfinite(ms) or ms <= 0:
        return 2.0
    return min(max(ms / 1000.0, 0.5), 10.0)


def start_data_push_thread(database, data_collector):
    """启动数据推送线程（优化版：单次查询 + 状态缓存 + 异常保护）"""
    global _push_stop_event
    # 如果已有线程在运行，先停止
    if _push_stop_event is not None:
        _push_stop_event.set()

    # ⚠️ 必须把事件**捕获成本地变量**再交给线程，不能让线程每轮去读模块全局。
    # 原实现 `while not _push_stop_event.is_set()` 读的是全局名字（迟绑定）：
    # 重启时全局被换成**新**事件（未 set），旧线程于是永远看不到自己被停 ——
    # 每重启一次就多一条推送线程，DB 查询翻倍、同一条数据被 emit 多次；
    # 而 `stop_data_push_thread()` 把全局置 None 后，旧线程还会在 while 条件处
    # 抛 AttributeError（线程带堆栈猝死）。
    stop_event = threading.Event()
    _push_stop_event = stop_event

    _last_status_time = 0
    _cached_status = None

    def push_data():
        nonlocal _last_status_time, _cached_status
        # 自适应频率：根据连接客户端数调整推送间隔
        MAX_INTERVAL = 10.0  # 最大间隔10秒（无客户端时）

        while not stop_event.is_set():
            try:
                # 基础间隔每轮读一次运行时配置 —— 运维改了立即生效，不用重启。
                BASE_INTERVAL = _base_push_interval()

                # 根据连接客户端数调整推送频率
                client_count = _count_clients(socketio.server.manager.rooms.get('/', {}))
                if client_count == 0:
                    push_interval = MAX_INTERVAL  # 无客户端时降低频率
                elif client_count > 20:
                    push_interval = min(BASE_INTERVAL * 2, 5.0)  # 多客户端时适当降频
                else:
                    push_interval = BASE_INTERVAL

                devices = database.get_device_summary()
                if devices:
                    all_latest = database.get_latest_data_all()
                    for device in devices:
                        device_id = device['device_id']
                        latest = all_latest.get(device_id) if all_latest else None
                        if latest:
                            try:
                                socketio.emit('data_update', compress_message(latest), room=f'device_{device_id}')
                            except Exception as e:
                                logger.debug(f"推送设备 {device_id} 数据失败: {e}")

                # 设备连接/运行状态变化 → `device_status`。
                # 只在有客户端时做：没人连的时候算出来也没人收。
                if client_count > 0:
                    try:
                        _push_device_status_changes(data_collector)
                    except Exception as e:
                        logger.debug(f"设备状态推送失败: {e}")

                now = time.time()
                if now - _last_status_time >= 10:
                    _cached_status = {
                        'timestamp': datetime.now().isoformat(),
                        'collector_stats': data_collector.get_stats(),
                        'database_stats': database.get_database_stats()
                    }
                    _last_status_time = now

                if _cached_status:
                    try:
                        socketio.emit('system_status', compress_message(_cached_status))
                    except Exception as e:
                        logger.debug(f"推送系统状态失败: {e}")

                stop_event.wait(push_interval)

            except Exception as e:
                logger.error(f"数据推送异常: {e}")
                stop_event.wait(5)

    thread = threading.Thread(target=push_data, daemon=True)
    thread.start()
    logger.info("数据推送线程已启动")
    # 返回线程对象：调用方/测试要能**精确**拿到这一次起的线程。
    # 否则只能靠 `threading.enumerate()` 差集去猜，而差集里可能混进别的线程
    # （日志/采集/定时器），猜错就会 join 到一条不是自己起的线程上
    # —— CI 上实测出现过 `RuntimeError: cannot join thread before it is started`。
    return thread


def stop_data_push_thread():
    """停止数据推送线程"""
    global _push_stop_event
    if _push_stop_event is not None:
        _push_stop_event.set()
        _push_stop_event = None
        logger.info("数据推送线程已停止")


def emit_alarm(alarm_data):
    """发送报警通知（广播给所有客户端；离线的按用户入队，重连后补发）"""
    if socketio:
        try:
            socketio.emit('alarm', alarm_data)
        except Exception as e:
            logger.debug(f"报警推送失败: {e}")
        # 报警优先级最高：队列满时挤掉的是广播类消息，不是报警
        _queue_for_offline_users('alarm', alarm_data, priority=10)


def emit_broadcast(broadcast_data):
    """发送广播事件通知（广播给所有客户端；离线的按用户入队）"""
    if socketio:
        try:
            socketio.emit('broadcast', broadcast_data)
        except Exception as e:
            logger.debug(f"广播推送失败: {e}")
        _queue_for_offline_users('broadcast', broadcast_data, priority=0)


def emit_device_status(device_id, status):
    """发送设备连接/运行状态更新（按设备房间定向推送）。

    `status` 形状：``{'connected': bool, 'status': 'online'|'offline', 'stopped': bool}``
    —— 前端 Dashboard 的 handler 读的是 `data.status.connected` /
    `data.status.status`（**外层 `status` 再套一层 `status`**，看着别扭，
    但改形状要同时改前端，本轮不动）。

    由推送线程的 `_push_device_status_changes()` 调用，只在状态**变化**时发。
    """
    if socketio:
        try:
            socketio.emit('device_status', {
                'device_id': device_id,
                'status': status
            }, room=f'device_{device_id}')
        except Exception as e:
            logger.debug(f"设备状态推送失败: {e}")
