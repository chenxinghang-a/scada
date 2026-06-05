"""
API 响应缓存
为频繁访问的端点提供 TTL 内存缓存，减少数据库查询压力。

使用方式:
    from core.api_cache import cached_response, invalidate_cache

    @app.route('/api/devices')
    @cached_response(ttl=5)
    def get_devices():
        ...
"""

import time
import hashlib
import threading
import logging
from functools import wraps
from typing import Any, Optional
from flask import request, jsonify

logger = logging.getLogger(__name__)

# 缓存存储: key → (expire_time, response_data, status_code)
_cache: dict[str, tuple[float, Any, int]] = {}
_cache_lock = threading.Lock()

# 默认 TTL（秒）
DEFAULT_TTL = 5

# 缓存统计
_stats = {
    'hits': 0,
    'misses': 0,
    'evictions': 0,
}


def _make_cache_key(prefix: str) -> str:
    """生成缓存键：prefix + 方法 + 路径 + 查询参数 + 用户"""
    parts = [
        prefix,
        request.method,
        request.path,
        request.query_string.decode('utf-8', errors='replace'),
        # 不同用户看到不同数据
        getattr(request, 'current_user', {}).get('username', 'anon'),
    ]
    raw = '|'.join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def cached_response(ttl: int = DEFAULT_TTL, prefix: str = ''):
    """
    API 响应缓存装饰器

    Args:
        ttl: 缓存有效期（秒）
        prefix: 缓存键前缀（用于按端点失效）
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            # 只缓存 GET 请求
            if request.method != 'GET':
                return f(*args, **kwargs)

            cache_key = _make_cache_key(prefix or f.__name__)
            now = time.time()

            # 尝试命中缓存
            with _cache_lock:
                entry = _cache.get(cache_key)
                if entry and entry[0] > now:
                    _stats['hits'] += 1
                    resp_data, status_code = entry[1], entry[2]
                    resp = jsonify(resp_data)
                    resp.status_code = status_code
                    resp.headers['X-Cache'] = 'HIT'
                    resp.headers['X-Cache-TTL'] = str(int(entry[0] - now))
                    return resp

            _stats['misses'] += 1

            # 执行原始函数
            result = f(*args, **kwargs)

            # 处理 Flask 响应对象
            if hasattr(result, 'get_json'):
                try:
                    data = result.get_json()
                    status_code = result.status_code
                except Exception:
                    return result
            elif isinstance(result, tuple):
                data, status_code = result[0], result[1] if len(result) > 1 else 200
                if hasattr(data, 'get_json'):
                    data = data.get_json()
            else:
                return result

            # 存入缓存
            if status_code == 200 and data is not None:
                with _cache_lock:
                    _cache[cache_key] = (now + ttl, data, status_code)

            resp = jsonify(data) if isinstance(data, (dict, list)) else data
            if hasattr(resp, 'headers'):
                resp.headers['X-Cache'] = 'MISS'
            return resp

        return decorated
    return decorator


def invalidate_cache(prefix: str = None):
    """
    失效缓存

    Args:
        prefix: 要失效的前缀（None 则清空全部）
    """
    with _cache_lock:
        if prefix is None:
            count = len(_cache)
            _cache.clear()
            logger.debug("缓存已全部清空 (%d 条)", count)
        else:
            keys_to_remove = [k for k in _cache if k.startswith(prefix)]
            for k in keys_to_remove:
                del _cache[k]
            logger.debug("缓存已失效: prefix=%s (%d 条)", prefix, len(keys_to_remove))


def cleanup_expired():
    """清理过期缓存条目"""
    now = time.time()
    with _cache_lock:
        expired = [k for k, v in _cache.items() if v[0] <= now]
        for k in expired:
            del _cache[k]
        _stats['evictions'] += len(expired)
    return len(expired)


def get_cache_stats() -> dict:
    """获取缓存统计"""
    with _cache_lock:
        total = _stats['hits'] + _stats['misses']
        hit_rate = _stats['hits'] / total * 100 if total > 0 else 0
        return {
            'size': len(_cache),
            'hits': _stats['hits'],
            'misses': _stats['misses'],
            'evictions': _stats['evictions'],
            'hit_rate': round(hit_rate, 1),
        }
