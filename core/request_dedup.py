"""
请求去重中间件
防止短时间内重复提交同一请求（如双击按钮、网络重试）。

使用方式:
    在 routes.py 中: init_request_dedup(app)
"""

import time
import hashlib
import threading
import logging
from flask import Flask, request, g

logger = logging.getLogger(__name__)

# 去重窗口（秒）
DEDUP_WINDOW = 2.0

# 已处理请求的指纹缓存（指纹 → 过期时间）
_fingerprints: dict[str, float] = {}
_fingerprints_lock = threading.Lock()


def _make_fingerprint() -> str:
    """生成请求指纹：方法+路径+用户+body哈希"""
    parts = [
        request.method,
        request.path,
        request.headers.get('Authorization', '')[:50],
    ]
    if request.is_json:
        import json
        body = request.get_json(silent=True)
        if body:
            parts.append(json.dumps(body, sort_keys=True, default=str)[:500])

    raw = '|'.join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cleanup_expired():
    """清理过期的指纹缓存"""
    now = time.time()
    with _fingerprints_lock:
        expired = [k for k, v in _fingerprints.items() if v < now]
        for k in expired:
            del _fingerprints[k]


def init_request_dedup(app: Flask):
    """
    初始化请求去重中间件

    对 POST/PUT/PATCH/DELETE 请求进行去重：
    - 相同指纹在 DEDUP_WINDOW 内的重复请求返回 409
    - 每10秒清理一次过期缓存
    """
    _last_cleanup = [time.time()]

    @app.before_request
    def _check_duplicate():
        # 只对写操作去重
        if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
            return None

        # 跳过不需要去重的路径
        skip_paths = ('/api/health', '/api/csrf-token', '/api/system/client-errors')
        if any(request.path.startswith(p) for p in skip_paths):
            return None

        fingerprint = _make_fingerprint()
        now = time.time()

        with _fingerprints_lock:
            expiry = _fingerprints.get(fingerprint)
            if expiry and expiry > now:
                logger.debug("重复请求被拒绝: %s %s (fingerprint=%s)", request.method, request.path, fingerprint[:8])
                from flask import jsonify
                return jsonify({
                    'success': False,
                    'error': '请求重复提交，请勿重复操作',
                    'retry_after': round(expiry - now, 1),
                }), 409

            _fingerprints[fingerprint] = now + DEDUP_WINDOW

        # 定期清理
        if now - _last_cleanup[0] > 10:
            _last_cleanup[0] = now
            _cleanup_expired()

        return None

    logger.info("请求去重中间件已初始化 (窗口=%ss)", DEDUP_WINDOW)
