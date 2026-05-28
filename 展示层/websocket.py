"""
WebSocket模块
实现实时数据推送
"""

import time
import logging
import threading
from flask import request
from flask_socketio import SocketIO, emit, join_room, leave_room
from datetime import datetime

logger = logging.getLogger(__name__)

# 全局SocketIO实例
socketio = None


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

    socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

    # 注册事件处理
    @socketio.on('connect')
    def handle_connect():
        """客户端连接"""
        logger.info(f"客户端连接: {request.sid}")
        emit('connected', {'message': '连接成功'})

    @socketio.on('disconnect')
    def handle_disconnect():
        """客户端断开"""
        logger.info(f"客户端断开: {request.sid}")

    @socketio.on('subscribe')
    def handle_subscribe(data):
        """订阅设备数据"""
        device_id = data.get('device_id')
        logger.info(f"客户端订阅设备: {device_id}")

        # 加入设备房间
        join_room(f'device_{device_id}')

        # 发送最新数据
        latest_data = database.get_latest_data(device_id)
        if latest_data:
            emit('data_update', latest_data)

    @socketio.on('unsubscribe')
    def handle_unsubscribe(data):
        """取消订阅"""
        device_id = data.get('device_id')
        logger.info(f"客户端取消订阅设备: {device_id}")

        # 离开设备房间
        leave_room(f'device_{device_id}')

    # 启动数据推送线程
    start_data_push_thread(database, data_collector)

    return socketio


def start_data_push_thread(database, data_collector):
    """启动数据推送线程（优化版：单次查询 + 状态缓存 + 异常保护）"""
    _stop_event = threading.Event()
    _last_status_time = 0
    _cached_status = None

    def push_data():
        nonlocal _last_status_time, _cached_status

        while not _stop_event.is_set():
            try:
                # 获取所有设备最新数据（单次查询，不是 N+1）
                devices = database.get_device_summary()
                if devices:
                    # 一次性获取所有设备的最新数据
                    all_latest = database.get_latest_data_all()
                    for device in devices:
                        device_id = device['device_id']
                        latest = all_latest.get(device_id) if all_latest else None
                        if latest:
                            try:
                                socketio.emit('data_update', latest, room=f'device_{device_id}')
                            except Exception as e:
                                logger.debug(f"推送设备 {device_id} 数据失败: {e}")

                # 系统状态每 10 秒更新一次（不是每 2 秒查 5 个 COUNT）
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
                        socketio.emit('system_status', _cached_status)
                    except Exception as e:
                        logger.debug(f"推送系统状态失败: {e}")

                time.sleep(2)

            except Exception as e:
                logger.error(f"数据推送异常: {e}")
                time.sleep(5)

    thread = threading.Thread(target=push_data, daemon=True)
    thread.start()
    logger.info("数据推送线程已启动")


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
