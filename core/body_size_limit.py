"""
API请求体大小限制
按端点细化配置请求体大小限制，防止大请求攻击。

使用方式:
    from core.body_size_limit import body_size_limit

    @app.route('/api/upload', methods=['POST'])
    @body_size_limit('50MB')
    def upload():
        ...
"""

import re
import logging
from functools import wraps
from flask import request, jsonify

logger = logging.getLogger(__name__)

# 大小单位映射
SIZE_UNITS = {
    'B': 1,
    'KB': 1024,
    'MB': 1024 * 1024,
    'GB': 1024 * 1024 * 1024,
}


def parse_size(size_str: str) -> int:
    """
    解析大小字符串为字节数

    Args:
        size_str: 大小字符串，如 '10MB', '1GB', '500KB'

    Returns:
        字节数
    """
    match = re.match(r'^(\d+(?:\.\d+)?)\s*([A-Za-z]+)$', size_str.strip())
    if not match:
        raise ValueError(f"无效的大小格式: {size_str}")

    value = float(match.group(1))
    unit = match.group(2).upper()

    if unit not in SIZE_UNITS:
        raise ValueError(f"未知的大小单位: {unit}")

    return int(value * SIZE_UNITS[unit])


def format_size(bytes_count: int) -> str:
    """格式化字节数为可读字符串"""
    if bytes_count < 1024:
        return f"{bytes_count}B"
    elif bytes_count < 1024 * 1024:
        return f"{bytes_count / 1024:.1f}KB"
    elif bytes_count < 1024 * 1024 * 1024:
        return f"{bytes_count / (1024 * 1024):.1f}MB"
    return f"{bytes_count / (1024 * 1024 * 1024):.1f}GB"


def body_size_limit(max_size: str = '1MB'):
    """
    请求体大小限制装饰器

    Args:
        max_size: 最大大小，如 '10MB', '1GB'
    """
    max_bytes = parse_size(max_size)

    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            content_length = request.content_length

            if content_length is not None and content_length > max_bytes:
                logger.warning(
                    "请求体超限: %s %s (%s > %s)",
                    request.method, request.path,
                    format_size(content_length), max_size
                )
                return jsonify({
                    'success': False,
                    'error': f'请求体大小超过限制（最大 {max_size}）',
                    'max_size': max_size,
                    'actual_size': format_size(content_length),
                }), 413

            return f(*args, **kwargs)

        return decorated
    return decorator


# 预定义常用限制
def limit_1mb(f):
    """1MB限制"""
    return body_size_limit('1MB')(f)

def limit_10mb(f):
    """10MB限制"""
    return body_size_limit('10MB')(f)

def limit_50mb(f):
    """50MB限制"""
    return body_size_limit('50MB')(f)

def limit_100mb(f):
    """100MB限制"""
    return body_size_limit('100MB')(f)
