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
import zlib
import base64
import logging
from typing import Any

logger = logging.getLogger(__name__)

# 压缩阈值（字节）：小于该值不压缩
COMPRESS_THRESHOLD = 1024  # 1KB

# 压缩包（base64 解码后）允许的最大字节数
MAX_COMPRESSED_SIZE = 1024 * 1024  # 1MB

# 解压输出的最大字节数。
# gzip 的压缩比可以做到 1000:1 以上，不限制输出大小的话，一个几十 KB 的
# 「压缩炸弹」就能在解压时吃光内存（OOM）。这里给解压结果设硬上限。
MAX_DECOMPRESSED_SIZE = 8 * 1024 * 1024  # 8MB


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


def _decompress_limited(compressed: bytes, max_size: int) -> bytes:
    """带输出上限的 gzip 解压。

    ``gzip.decompress()`` 会把整个解压结果一次性放进内存，遇到压缩炸弹
    （极小压缩包 → 极大明文）会直接 OOM。这里改用 ``zlib.decompressobj``
    分块解压，累计输出一旦超过 max_size 立刻抛错中止。

    Raises:
        ValueError: 输出超过 max_size，或数据不是合法的 gzip 流
    """
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)  # 16: 按 gzip 容器解析
    chunks: list[bytes] = []
    total = 0
    buf = compressed

    while buf:
        # max_length 只允许再产出「剩余额度 + 1」字节，多出 1 字节即判定超限
        out = decompressor.decompress(buf, max_size - total + 1)
        total += len(out)
        if total > max_size:
            raise ValueError(f"解压结果超过上限 {max_size} 字节，疑似压缩炸弹，已拒绝")
        chunks.append(out)
        if decompressor.unconsumed_tail:
            buf = decompressor.unconsumed_tail
        else:
            break

    # 只接受单个 gzip 成员：多成员/尾部垃圾会让「解压结果」与
    # 发送方实际想要的内容不一致，按失败处理（fail-closed）。
    if decompressor.unused_data:
        raise ValueError("gzip 流含多余数据（多成员或尾部垃圾），已拒绝")

    tail = decompressor.flush()
    if total + len(tail) > max_size:
        raise ValueError(f"解压结果超过上限 {max_size} 字节，疑似压缩炸弹，已拒绝")
    chunks.append(tail)
    return b''.join(chunks)


def decompress_message(payload: Any, max_size: int = MAX_DECOMPRESSED_SIZE) -> Any:
    """
    解压 WebSocket 消息（客户端发送时）

    Args:
        payload: 接收的消息
        max_size: 解压输出的最大字节数，超过则拒绝（默认 8MB）

    Returns:
        解压后的数据；数据非法或超过上限时原样返回 payload
    """
    try:
        if isinstance(payload, dict) and payload.get('__compressed'):
            data = payload.get('data')
            if not isinstance(data, str):
                logger.warning("压缩消息的 data 字段不是字符串，拒绝解压")
                return payload

            # 校验1：base64 文本长度（4/3 膨胀）——先挡掉明显超大的载荷
            if len(data) > (MAX_COMPRESSED_SIZE // 3) * 4 + 16:
                logger.warning("压缩消息体积超过上限 %d 字节，拒绝解压", MAX_COMPRESSED_SIZE)
                return payload

            compressed = base64.b64decode(data, validate=True)

            # 校验2：压缩包本身的大小
            if len(compressed) > MAX_COMPRESSED_SIZE:
                logger.warning("压缩包体积超过上限 %d 字节，拒绝解压", MAX_COMPRESSED_SIZE)
                return payload

            # 校验3：gzip 尾部 ISIZE 字段声明的原始长度（mod 2^32）
            # 声明值本身就超限时可以直接拒绝，不必真的去解压。
            if len(compressed) >= 4:
                declared = int.from_bytes(compressed[-4:], 'little')
                if declared > max_size:
                    logger.warning(
                        "压缩包声明解压长度 %d 字节，超过上限 %d，拒绝解压",
                        declared, max_size)
                    return payload

            # 校验4：解压过程中限制累计输出字节数
            raw = _decompress_limited(compressed, max_size)
            return json.loads(raw.decode('utf-8'))
        return payload
    except Exception as e:
        logger.debug("消息解压失败: %s", e)
        return payload
