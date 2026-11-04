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
from collections import OrderedDict
from functools import wraps
from typing import Any, Dict, Optional
from flask import request, jsonify, current_app
from core.safe_numeric import to_finite_float

logger = logging.getLogger(__name__)

# 签名配置
SIGNATURE_HEADER = 'X-Signature'
TIMESTAMP_HEADER = 'X-Timestamp'
NONCE_HEADER = 'X-Nonce'
MAX_TIMESTAMP_AGE = 300  # 5分钟

#: nonce 缓存**硬上限**（round 369）。
#: 原来这个字典无界，且验签**之前**就写入：未授权攻击者不需要知道密钥，
#: 只要伪造合法时间戳 + 随机 Nonce 高频请求，就能在几秒内把进程内存撑爆
#: （OOM DoS），或者抢注合法客户端马上要用的 Nonce。
MAX_NONCE_CACHE = 50000
_nonce_cache: "OrderedDict[str, float]" = OrderedDict()
_nonce_lock = __import__('threading').Lock()


def _is_production() -> bool:
    """是否生产环境（无法判定时按生产处理，fail-closed）。"""
    try:
        from config import IS_PRODUCTION
        return bool(IS_PRODUCTION)
    except Exception:      # noqa: BLE001
        return True


def _signature_secret() -> str:
    """读取签名密钥（应用配置优先，其次环境变量）。"""
    secret = current_app.config.get('API_SIGNATURE_SECRET', '') or ''
    if not secret:
        import os
        secret = os.environ.get('API_SIGNATURE_SECRET', '') or ''
    return secret


def _register_nonce(nonce: str) -> bool:
    """登记 nonce（**验签通过后**才调用）。返回 False 表示重放。"""
    now = time.monotonic()
    with _nonce_lock:
        if nonce in _nonce_cache:
            return False
        _nonce_cache[nonce] = now
        _nonce_cache.move_to_end(nonce)

        # 插入顺序即时间顺序：从头清理过期的
        while _nonce_cache:
            oldest, ts = next(iter(_nonce_cache.items()))
            if now - ts > MAX_TIMESTAMP_AGE:
                _nonce_cache.popitem(last=False)
            else:
                break
        # 硬上限：极端流量下宁可淘汰最旧的，也不能让内存无界增长
        while len(_nonce_cache) > MAX_NONCE_CACHE:
            _nonce_cache.popitem(last=False)
    return True


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
            # ⚠️ 必须 `to_finite_float`：`X-Timestamp: NaN` 会让
            # `float()` 成功返回 nan，而 `age = abs(now - nan)` 仍是 nan，
            # `nan > MAX_TIMESTAMP_AGE` 恒为 **False** —— 过期检查直接被绕过，
            # 重放保护形同虚设。（`inf` 会被 `> MAX` 挡住，`nan` 不会。）
            ts = to_finite_float(timestamp, 'timestamp')
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

        # 获取请求体
        body = request.get_data(as_text=True) if request.method in ('POST', 'PUT', 'PATCH') else ''

        # 获取签名密钥 —— **必须先拿到密钥再谈验证**。
        # 原来缺密钥时直接 `return f(...)` 放行：全库从未向 app.config 注入过
        # API_SIGNATURE_SECRET，于是这个"安全机制"在生产环境下 100% 是旁路。
        secret = _signature_secret()
        if not secret:
            if _is_production():
                logger.error(
                    "API_SIGNATURE_SECRET 未配置，签名保护不可用 —— 生产环境拒绝请求（fail-closed）")
                return jsonify({
                    'success': False,
                    'error': '服务端未配置请求签名密钥，已拒绝请求',
                    'error_code': 'SIGNATURE_NOT_CONFIGURED',
                }), 503
            logger.warning("API_SIGNATURE_SECRET未配置（非生产环境），跳过签名验证")
            return f(*args, **kwargs)

        # 先验签
        if not verify_signature(secret, request.method, request.path, body, timestamp, nonce, signature):
            return jsonify({
                'success': False,
                'error': '签名验证失败',
                'error_code': 'INVALID_SIGNATURE',
            }), 401

        # 验签通过后才登记 nonce（防重放）。
        # 顺序反了等于"未授权请求也能写缓存"：攻击者不知道密钥也能撑爆内存。
        if not _register_nonce(nonce):
            return jsonify({
                'success': False,
                'error': '重复的请求',
                'error_code': 'DUPLICATE_NONCE',
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
            ts = to_finite_float(timestamp, 'timestamp')  # NaN 会绕过过期检查，见上
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

        # 获取签名密钥（缺失时生产环境 fail-closed，见 signature_required）
        secret = _signature_secret()
        if not secret:
            if _is_production():
                logger.error(
                    "API_SIGNATURE_SECRET 未配置，签名保护不可用 —— 生产环境拒绝请求（fail-closed）")
                return jsonify({
                    'success': False,
                    'error': '服务端未配置请求签名密钥，已拒绝请求',
                    'error_code': 'SIGNATURE_NOT_CONFIGURED',
                }), 503
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
    """清理过期的nonce缓存（时间基准与登记时一致：单调时钟）"""
    now = time.monotonic()
    with _nonce_lock:
        expired = [k for k, v in _nonce_cache.items()
                   if now - v > MAX_TIMESTAMP_AGE]
        for k in expired:
            _nonce_cache.pop(k, None)


def get_signature_stats() -> Dict[str, Any]:
    """获取签名验证统计"""
    with _nonce_lock:
        return {
            'nonce_cache_size': len(_nonce_cache),
            'nonce_cache_max': MAX_NONCE_CACHE,
            'max_timestamp_age': MAX_TIMESTAMP_AGE,
        }
