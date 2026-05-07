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
    """
    启动数据推送线程
    
    注意: async_mode='threading'时用time.sleep()而非socketio.sleep()
    socketio.sleep()在gevent/eventlet模式下才正确工作
    """
    def push_data():
        """推送数据到客户端"""
        while True:
            try:
                # 获取所有设备最新数据
                devices = database.get_device_summary()
                
                for device in devices:
                    device_id = device['device_id']
                    latest_data = database.get_latest_data(device_id)
                    
                    if latest_data:
                        socketio.emit('data_update', latest_data, room=f'device_{device_id}')
                
                # 推送系统状态
                system_status = {
                    'timestamp': datetime.now().isoformat(),
                    'collector_stats': data_collector.get_stats(),
                    'database_stats': database.get_database_stats()
                }
                socketio.emit('system_status', system_status)
                
                # threading模式用time.sleep
                time.sleep(2)
                
            except Exception as e:
                logger.error(f"数据推送异常: {e}")
                time.sleep(5)
    
    thread = threading.Thread(target=push_data, daemon=True)
    thread.start()
    logger.info("数据推送线程已启动")


def emit_alarm(alarm_data):
    """发送报警通知"""
    if socketio:
        socketio.emit('alarm', alarm_data, broadcast=True)


def emit_broadcast(broadcast_data):
    """发送广播事件通知"""
    if socketio:
        socketio.emit('broadcast', broadcast_data, broadcast=True)


def emit_device_status(device_id, status):
    """发送设备状态更新"""
    if socketio:
        socketio.emit('device_status', {
            'device_id': device_id,
            'status': status
        }, room=f'device_{device_id}')
