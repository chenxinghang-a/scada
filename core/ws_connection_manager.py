"""
WebSocket连接管理器增强
追踪连接状态、消息统计、客户端信息。

使用方式:
    from core.ws_connection_manager import ws_manager
    stats = ws_manager.get_stats()
"""

# ============================================================================
# 接线状态：未接线（WIRED = False）
# ============================================================================
# 本模块在生产代码（run.py / 各业务层 / 其它 core 模块）中**没有任何 import 引用**。
# 模块本身可用，但当前没有调用方 —— 也就是说它宣称的这项能力**当前并未生效**。
#
# 为什么保留而不删除：删掉即丢能力，模块本身有测试价值；这里只把「没接线」显式化、
# 可追踪，避免「代码在库里」被误读成「功能在跑」。
#
# 自动化复核（防止本标注过期）：
#   tests/test_core_regressions.py::test_unwired_marker_matches_reality
#   —— 该用例用 AST 扫描全仓库 import。一旦有人把本模块接进生产代码，
#      而这里仍写着 WIRED = False，用例即失败，强制文档与事实同步。
#
# 接线建议（需改 run.py / 各层，core 内部无权自行接线）：
#     在 WebSocket 连接建立/断开回调里登记连接，并把 get_stats() 暴露给运维页面。
# ============================================================================
WIRED = False


import time
import logging
import threading
from typing import Dict, Any, Optional, Set
from collections import defaultdict

logger = logging.getLogger(__name__)


class WSClient:
    """WebSocket客户端信息"""

    def __init__(self, sid: str, ip: str = '', user_agent: str = ''):
        self.sid = sid
        self.ip = ip
        self.user_agent = user_agent
        self.connected_at = time.time()
        self.last_active = time.time()
        self.messages_sent = 0
        self.messages_received = 0
        self.bytes_sent = 0
        self.bytes_received = 0
        self.rooms: Set[str] = set()

    def to_dict(self) -> Dict[str, Any]:
        return {
            'sid': self.sid,
            'ip': self.ip,
            'user_agent': self.user_agent[:100],
            'connected_at': self.connected_at,
            'last_active': self.last_active,
            'uptime': time.time() - self.connected_at,
            'messages_sent': self.messages_sent,
            'messages_received': self.messages_received,
            'bytes_sent': self.bytes_sent,
            'bytes_received': self.bytes_received,
            'rooms': list(self.rooms),
        }


class WSConnectionManager:
    """WebSocket连接管理器"""

    def __init__(self):
        self._clients: Dict[str, WSClient] = {}
        self._lock = threading.Lock()
        self._peak_connections = 0
        self._total_connections = 0
        self._total_messages = 0
        self._message_types: Dict[str, int] = defaultdict(int)

    def on_connect(self, sid: str, ip: str = '', user_agent: str = ''):
        """客户端连接"""
        with self._lock:
            client = WSClient(sid, ip, user_agent)
            self._clients[sid] = client
            self._total_connections += 1
            self._peak_connections = max(self._peak_connections, len(self._clients))
            logger.info("WebSocket连接: %s (ip=%s, 当前=%d)", sid[:8], ip, len(self._clients))

    def on_disconnect(self, sid: str):
        """客户端断开"""
        with self._lock:
            client = self._clients.pop(sid, None)
            if client:
                uptime = time.time() - client.connected_at
                logger.info(
                    "WebSocket断开: %s (在线%.0fs, 发送%d条, 接收%d条)",
                    sid[:8], uptime, client.messages_sent, client.messages_received
                )

    def on_message_sent(self, sid: str, event: str, size: int = 0):
        """消息发送"""
        with self._lock:
            client = self._clients.get(sid)
            if client:
                client.messages_sent += 1
                client.bytes_sent += size
                client.last_active = time.time()
            self._total_messages += 1
            self._message_types[event] += 1

    def on_message_received(self, sid: str, event: str, size: int = 0):
        """消息接收"""
        with self._lock:
            client = self._clients.get(sid)
            if client:
                client.messages_received += 1
                client.bytes_received += size
                client.last_active = time.time()

    def join_room(self, sid: str, room: str):
        """加入房间"""
        with self._lock:
            client = self._clients.get(sid)
            if client:
                client.rooms.add(room)

    def leave_room(self, sid: str, room: str):
        """离开房间"""
        with self._lock:
            client = self._clients.get(sid)
            if client:
                client.rooms.discard(room)

    def get_client(self, sid: str) -> Optional[Dict[str, Any]]:
        """获取客户端信息"""
        with self._lock:
            client = self._clients.get(sid)
            return client.to_dict() if client else None

    def get_all_clients(self) -> list:
        """获取所有客户端"""
        with self._lock:
            return [c.to_dict() for c in self._clients.values()]

    def get_stats(self) -> Dict[str, Any]:
        """获取统计"""
        with self._lock:
            clients = list(self._clients.values())
            active = len(clients)
            total_bytes_sent = sum(c.bytes_sent for c in clients)
            total_bytes_recv = sum(c.bytes_received for c in clients)

            return {
                'active_connections': active,
                'peak_connections': self._peak_connections,
                'total_connections': self._total_connections,
                'total_messages': self._total_messages,
                'total_bytes_sent': total_bytes_sent,
                'total_bytes_received': total_bytes_recv,
                'message_types': dict(self._message_types),
                'clients': [c.to_dict() for c in clients],
            }

    def get_room_stats(self) -> Dict[str, Any]:
        """获取房间统计"""
        with self._lock:
            rooms: Dict[str, int] = defaultdict(int)
            for client in self._clients.values():
                for room in client.rooms:
                    rooms[room] += 1
            return dict(rooms)

    def get_idle_clients(self, idle_seconds: float = 300) -> list:
        """获取空闲客户端"""
        with self._lock:
            now = time.time()
            return [
                c.to_dict() for c in self._clients.values()
                if now - c.last_active > idle_seconds
            ]


# 全局实例
ws_manager = WSConnectionManager()
