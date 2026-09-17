"""
WebSocket模块
实现实时数据推送
"""

import time
import logging
import threading
import yaml
from pathlib import Path
from flask import request
from flask_socketio import SocketIO, emit, join_room, leave_room
from datetime import datetime
from core.ws_compress import compress_message
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
_clients_lock = threading.Lock()


def get_connected_count() -> int:
    """当前 WebSocket 连接数（线程安全）。供 /api/health 等监控端点使用。"""
    with _clients_lock:
        return len(_connected_clients)


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
    # 默认只允许本地开发
    return ['http://localhost:5000', 'http://127.0.0.1:5000']


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
        with _clients_lock:
            if len(_connected_clients) >= MAX_CONNECTIONS:
                logger.warning(f"WebSocket连接被拒绝: 超过最大连接数 {MAX_CONNECTIONS}, sid={request.sid}")
                return False
            _connected_clients.add(request.sid)
            current = len(_connected_clients)

        logger.info(f"客户端连接: {request.sid}, 用户: {user['username']}, 当前连接数: {current}")
        emit('connected', {'message': '连接成功', 'user': user['username']})

    @socketio.on('disconnect')
    def handle_disconnect():
        """客户端断开"""
        with _clients_lock:
            _connected_clients.discard(request.sid)
            current = len(_connected_clients)
        logger.info(f"客户端断开: {request.sid}, 当前连接数: {current}")

    @socketio.on('heartbeat')
    def handle_heartbeat(data):
        """心跳保活（客户端定期发送，服务端回复确认）"""
        emit('heartbeat_ack', {
            'timestamp': datetime.now().isoformat(),
            'client_timestamp': data.get('timestamp') if data else None
        })

    @socketio.on('subscribe')
    def handle_subscribe(data):
        """订阅设备数据"""
        # `data` 可能是 None（客户端发了空包），此时 `data.get` 会抛 AttributeError，
        # 在事件处理器里表现为服务端刷堆栈、客户端收不到任何回应。
        device_id = (data or {}).get('device_id')
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
        device_id = (data or {}).get('device_id')
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


def start_data_push_thread(database, data_collector):
    """启动数据推送线程（优化版：单次查询 + 状态缓存 + 异常保护）"""
    global _push_stop_event
    # 如果已有线程在运行，先停止
    if _push_stop_event is not None:
        _push_stop_event.set()
    _push_stop_event = threading.Event()
    _last_status_time = 0
    _cached_status = None

    def push_data():
        nonlocal _last_status_time, _cached_status
        # 自适应频率：根据连接客户端数调整推送间隔
        BASE_INTERVAL = 2.0  # 基础间隔2秒
        MAX_INTERVAL = 10.0  # 最大间隔10秒（无客户端时）

        while not _push_stop_event.is_set():
            try:
                # 根据连接客户端数调整推送频率
                client_count = len(socketio.server.manager.rooms.get('/', {}))
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

                _push_stop_event.wait(push_interval)

            except Exception as e:
                logger.error(f"数据推送异常: {e}")
                _push_stop_event.wait(5)

    thread = threading.Thread(target=push_data, daemon=True)
    thread.start()
    logger.info("数据推送线程已启动")


def stop_data_push_thread():
    """停止数据推送线程"""
    global _push_stop_event
    if _push_stop_event is not None:
        _push_stop_event.set()
        _push_stop_event = None
        logger.info("数据推送线程已停止")


def emit_alarm(alarm_data):
    """发送报警通知（广播给所有客户端）"""
    if socketio:
        try:
            socketio.emit('alarm', alarm_data)
        except Exception as e:
            logger.debug(f"报警推送失败: {e}")


def emit_broadcast(broadcast_data):
    """发送广播事件通知（广播给所有客户端）"""
    if socketio:
        try:
            socketio.emit('broadcast', broadcast_data)
        except Exception as e:
            logger.debug(f"广播推送失败: {e}")


def emit_device_status(device_id, status):
    """发送设备状态更新"""
    if socketio:
        try:
            socketio.emit('device_status', {
                'device_id': device_id,
                'status': status
            }, room=f'device_{device_id}')
        except Exception as e:
            logger.debug(f"设备状态推送失败: {e}")
