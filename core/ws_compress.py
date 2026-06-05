"""
WebSocket 消息压缩
对大载荷（>1KB）自动 gzip 压缩，减少带宽占用。

协议约定:
  - 压缩消息: { "__compressed": true, "data": "<base64 gzip>" }
  - 未压缩消息: 原始 JSON

使用方式:
    compressed = compress_message(payload)
    socketio.emit('data', compressed, room=sid)
"""

import json
import gzip
import base64
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 压缩阈值（字节）：小于该值不压缩
COMPRESS_THRESHOLD = 1024  # 1KB


def compress_message(payload: Any) -> Any:
    """
    压缩 WebSocket 消息

    Args:
        payload: 要发送的数据（dict/list/str）

    Returns:
        压缩后的消息（如果 > 阈值）或原始消息
    """
    try:
        if isinstance(payload, str):
            raw = payload.encode('utf-8')
        else:
            raw = json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8')

        if len(raw) < COMPRESS_THRESHOLD:
            return payload

        compressed = gzip.compress(raw, compresslevel=6)
        if len(compressed) >= len(raw):
            # 压缩后更大，不压缩
            return payload

        return {
            '__compressed': True,
            'data': base64.b64encode(compressed).decode('ascii'),
        }
    except Exception as e:
        logger.debug("消息压缩失败: %s", e)
        return payload


def decompress_message(payload: Any) -> Any:
    """
    解压 WebSocket 消息（客户端发送时）

    Args:
        payload: 接收的消息

    Returns:
        解压后的数据
    """
    try:
        if isinstance(payload, dict) and payload.get('__compressed'):
            compressed = base64.b64decode(payload['data'])
            raw = gzip.decompress(compressed)
            return json.loads(raw.decode('utf-8'))
        return payload
    except Exception as e:
        logger.debug("消息解压失败: %s", e)
        return payload
