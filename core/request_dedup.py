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

# 缓存按 Flask app 隔离，避免测试实例/多应用相互污染。
# 每个进程内仍只负责当前 app；多进程部署应使用共享存储实现跨进程去重。
_STATE_KEY = 'request_dedup'


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


def _cleanup_expired(fingerprints: dict[str, float], lock: threading.Lock):
    """清理过期的指纹缓存"""
    now = time.time()
    with lock:
        expired = [k for k, v in fingerprints.items() if v < now]
        for k in expired:
            del fingerprints[k]


def init_request_dedup(app: Flask):
    """
    初始化请求去重中间件

    对 POST/PUT/PATCH/DELETE 请求进行去重：
    - 相同指纹在 DEDUP_WINDOW 内的重复请求返回 409
    - 每10秒清理一次过期缓存
    """
    state = app.extensions.setdefault(
        _STATE_KEY,
        {'fingerprints': {}, 'lock': threading.Lock(), 'last_cleanup': time.time()},
    )
    fingerprints: dict[str, float] = state['fingerprints']
    fingerprints_lock: threading.Lock = state['lock']

    @app.before_request
    def _check_duplicate():
        # 只对写操作去重
        if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
            return None

        # 跳过不需要去重的路径
        skip_paths = (
            '/api/health',
            '/api/csrf-token',
            '/api/system/client-errors',
            # 登录请求不是重复写入；同一凭据的快速重试必须交给认证/限流层处理。
            '/api/auth/login',
        )
        if any(request.path.startswith(p) for p in skip_paths):
            return None

        fingerprint = _make_fingerprint()
        now = time.time()

        with fingerprints_lock:
            expiry = fingerprints.get(fingerprint)
            if expiry and expiry > now:
                logger.debug("重复请求被拒绝: %s %s (fingerprint=%s)", request.method, request.path, fingerprint[:8])
                from flask import jsonify
                return jsonify({
                    'success': False,
                    'error': '请求重复提交，请勿重复操作',
                    'retry_after': round(expiry - now, 1),
                }), 409

            fingerprints[fingerprint] = now + DEDUP_WINDOW

        # 定期清理
        if now - state['last_cleanup'] > 10:
            state['last_cleanup'] = now
            _cleanup_expired(fingerprints, fingerprints_lock)

        return None

    logger.info("请求去重中间件已初始化 (窗口=%ss)", DEDUP_WINDOW)
