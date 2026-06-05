"""
API请求签名验证（多级策略）
HMAC签名防篡改，支持请求体+时间戳+nonce签名。

签名等级:
  - light: GET/HEAD/OPTIONS，只签 method+path+timestamp（不校验nonce）
  - full:  POST/PUT/PATCH/DELETE，签 method+path+body+timestamp+nonce

使用方式:
    from core.request_signature import signature_required, signature_light
    @app.route('/api/read')
    @signature_light
    def read_endpoint(): ...

    @app.route('/api/write', methods=['POST'])
    @signature_required
    def write_endpoint(): ...
"""

import hashlib
import hmac
import time
import logging
import secrets
from functools import wraps
from typing import Any, Dict, Optional
from flask import request, jsonify, current_app

logger = logging.getLogger(__name__)

# 签名配置
SIGNATURE_HEADER = 'X-Signature'
TIMESTAMP_HEADER = 'X-Timestamp'
NONCE_HEADER = 'X-Nonce'
MAX_TIMESTAMP_AGE = 300  # 5分钟

# nonce缓存（防重放）
_nonce_cache: Dict[str, float] = {}
_nonce_lock = __import__('threading').Lock()


def generate_signature(
    secret: str,
    method: str,
    path: str,
    body: str = '',
    timestamp: str = '',
    nonce: str = '',
) -> str:
    """
    生成请求签名

    Args:
        secret: 签名密钥
        method: HTTP方法
        path: 请求路径
        body: 请求体
        timestamp: 时间戳
        nonce: 随机数

    Returns:
        签名字符串
    """
    # 构建签名消息
    message_parts = [
        method.upper(),
        path,
        body,
        timestamp,
        nonce,
    ]
    message = '\n'.join(message_parts)

    # HMAC-SHA256签名
    signature = hmac.new(
        secret.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    return signature


def verify_signature(
    secret: str,
    method: str,
    path: str,
    body: str = '',
    timestamp: str = '',
    nonce: str = '',
    signature: str = '',
) -> bool:
    """
    验证请求签名

    Returns:
        签名是否有效
    """
    expected = generate_signature(secret, method, path, body, timestamp, nonce)
    return hmac.compare_digest(expected, signature)


def signature_required(f):
    """签名验证装饰器"""
    @wraps(f)
    def decorated(*args, **kwargs):
        # 获取签名头
        signature = request.headers.get(SIGNATURE_HEADER)
        timestamp = request.headers.get(TIMESTAMP_HEADER)
        nonce = request.headers.get(NONCE_HEADER)

        if not signature or not timestamp or not nonce:
            return jsonify({
                'success': False,
                'error': '缺少签名头',
                'error_code': 'MISSING_SIGNATURE',
            }), 401

        # 验证时间戳
        try:
            ts = float(timestamp)
            age = abs(time.time() - ts)
            if age > MAX_TIMESTAMP_AGE:
                return jsonify({
                    'success': False,
                    'error': '请求已过期',
                    'error_code': 'EXPIRED_REQUEST',
                }), 401
        except ValueError:
            return jsonify({
                'success': False,
                'error': '无效的时间戳',
                'error_code': 'INVALID_TIMESTAMP',
            }), 401

        # 验证nonce（防重放）
        with _nonce_lock:
            if nonce in _nonce_cache:
                return jsonify({
                    'success': False,
                    'error': '重复的请求',
                    'error_code': 'DUPLICATE_NONCE',
                }), 401
            _nonce_cache[nonce] = time.time()

            # 清理过期nonce
            expired = [k for k, v in _nonce_cache.items() if time.time() - v > MAX_TIMESTAMP_AGE]
            for k in expired:
                del _nonce_cache[k]

        # 获取请求体
        body = request.get_data(as_text=True) if request.method in ('POST', 'PUT', 'PATCH') else ''

        # 获取签名密钥
        secret = current_app.config.get('API_SIGNATURE_SECRET', '')
        if not secret:
            logger.warning("API_SIGNATURE_SECRET未配置，跳过签名验证")
            return f(*args, **kwargs)

        # 验证签名
        if not verify_signature(secret, request.method, request.path, body, timestamp, nonce, signature):
            return jsonify({
                'success': False,
                'error': '签名验证失败',
                'error_code': 'INVALID_SIGNATURE',
            }), 401

        return f(*args, **kwargs)

    return decorated


def get_signature_headers(
    secret: str,
    method: str,
    path: str,
    body: str = '',
) -> Dict[str, str]:
    """
    生成签名请求头

    Args:
        secret: 签名密钥
        method: HTTP方法
        path: 请求路径
        body: 请求体

    Returns:
        签名头字典
    """
    timestamp = str(int(time.time()))
    nonce = secrets.token_hex(16)
    signature = generate_signature(secret, method, path, body, timestamp, nonce)

    return {
        SIGNATURE_HEADER: signature,
        TIMESTAMP_HEADER: timestamp,
        NONCE_HEADER: nonce,
    }


def signature_light(f):
    """
    轻量签名装饰器（GET/HEAD/OPTIONS）
    只验证 method+path+timestamp，不校验nonce（读操作可重放）
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        signature = request.headers.get(SIGNATURE_HEADER)
        timestamp = request.headers.get(TIMESTAMP_HEADER)

        if not signature or not timestamp:
            return jsonify({
                'success': False,
                'error': '缺少签名头',
                'error_code': 'MISSING_SIGNATURE',
            }), 401

        # 验证时间戳
        try:
            ts = float(timestamp)
            age = abs(time.time() - ts)
            if age > MAX_TIMESTAMP_AGE * 2:  # 轻量签名允许更长过期
                return jsonify({
                    'success': False,
                    'error': '请求已过期',
                    'error_code': 'EXPIRED_REQUEST',
                }), 401
        except ValueError:
            return jsonify({
                'success': False,
                'error': '无效的时间戳',
                'error_code': 'INVALID_TIMESTAMP',
            }), 401

        # 获取签名密钥
        secret = current_app.config.get('API_SIGNATURE_SECRET', '')
        if not secret:
            return f(*args, **kwargs)

        # 轻量签名：method + path + timestamp
        expected = generate_signature(secret, request.method, request.path, timestamp=timestamp)
        if not hmac.compare_digest(expected, signature):
            return jsonify({
                'success': False,
                'error': '签名验证失败',
                'error_code': 'INVALID_SIGNATURE',
            }), 401

        return f(*args, **kwargs)
    return decorated


def cleanup_nonce_cache():
    """清理过期的nonce缓存"""
    with _nonce_lock:
        expired = [k for k, v in _nonce_cache.items() if time.time() - v > MAX_TIMESTAMP_AGE]
        for k in expired:
            del _nonce_cache[k]


def get_signature_stats() -> Dict[str, Any]:
    """获取签名验证统计"""
    with _nonce_lock:
        return {
            'nonce_cache_size': len(_nonce_cache),
            'max_timestamp_age': MAX_TIMESTAMP_AGE,
        }
