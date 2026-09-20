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

# 缓存存储: key → (expire_time, response_data, status_code, prefix)
#
# 第 4 项 prefix 是**原始端点前缀**（`_make_cache_key` 的输入），必须随条目一起存：
# 缓存键是 sha256 摘要，无法从键反推端点名，旧实现用
# `k.startswith(prefix)` 拿原始端点名去比对 sha256 键 —— 永远不成立，
# 按端点失效（invalidate_cache('devices')）实际上什么都没删，
# 修改数据后接口继续返回陈旧缓存，且日志显示"已失效 0 条"，看起来很正常。
_cache: dict[str, tuple[float, Any, int, str]] = {}
_cache_lock = threading.Lock()

# 默认 TTL（秒）
DEFAULT_TTL = 5

# 缓存统计
_stats = {
    'hits': 0,
    'misses': 0,
    'evictions': 0,
    # 按前缀失效的命中/落空次数：落空次数长期增长而命中为 0，
    # 说明调用方用的前缀和 @cached_response(prefix=...) 对不上。
    'invalidations': 0,
    'invalidate_misses': 0,
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

            endpoint = prefix or f.__name__
            cache_key = _make_cache_key(endpoint)
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
                except Exception as e:
                    # 解析不出 JSON → 直接返回原响应（不缓存）。必须留痕：
                    # 否则"这个端点一直不缓存"看起来只是缓存策略，实际是解析失败。
                    logger.warning(
                        "响应体解析失败，本次不缓存(将原样返回): %s: %s",
                        getattr(f, '__name__', '?'), e)
                    return result
            elif isinstance(result, tuple):
                data, status_code = result[0], result[1] if len(result) > 1 else 200
                if hasattr(data, 'get_json'):
                    data = data.get_json()
            else:
                return result

            # 存入缓存（连同端点前缀一起存，供 invalidate_cache 按前缀匹配）
            if status_code == 200 and data is not None:
                with _cache_lock:
                    _cache[cache_key] = (now + ttl, data, status_code, endpoint)

            resp = jsonify(data) if isinstance(data, (dict, list)) else data
            if hasattr(resp, 'headers'):
                resp.headers['X-Cache'] = 'MISS'
            return resp

        return decorated
    return decorator


def invalidate_cache(prefix: str = None) -> int:
    """
    失效缓存

    Args:
        prefix: 要失效的端点前缀（None 则清空全部）。匹配规则：条目的
            `@cached_response(prefix=...)`（未显式指定时为函数名）等于 prefix
            或以 `prefix` 开头。

    Returns:
        实际删除的条目数。

    说明：缓存键是 sha256 摘要，必须靠条目内保存的端点名做前缀匹配，
    不能对键本身做 startswith。
    """
    removed = 0
    with _cache_lock:
        if prefix is None:
            removed = len(_cache)
            _cache.clear()
            logger.debug("缓存已全部清空 (%d 条)", removed)
        else:
            keys_to_remove = [
                k for k, v in _cache.items()
                if len(v) > 3 and isinstance(v[3], str) and v[3].startswith(prefix)
            ]
            for k in keys_to_remove:
                del _cache[k]
            removed = len(keys_to_remove)
            _stats['invalidations'] += removed
            if removed == 0:
                # 落空要留痕：否则"改了数据但接口还是旧值"会被归因到别处
                _stats['invalidate_misses'] += 1
                logger.warning(
                    "按前缀失效未命中任何缓存条目: prefix=%r (当前 %d 条缓存，"
                    "请确认与 @cached_response(prefix=...) 一致)",
                    prefix, len(_cache))
            else:
                logger.debug("缓存已失效: prefix=%s (%d 条)", prefix, removed)
    return removed


def cleanup_expired():
    """清理过期缓存条目"""
    now = time.time()
    with _cache_lock:
        expired = [k for k, v in _cache.items() if v[0] <= now]
        for k in expired:
            del _cache[k]
        _stats['evictions'] += len(expired)
    if expired:
        logger.debug("清理过期缓存条目: %d 条", len(expired))
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
            # 按前缀失效的有效性：invalidate_misses 持续增长说明调用方
            # 传的 prefix 跟缓存条目上的 endpoint 名对不上（失效没生效）
            'invalidated': _stats['invalidations'],
            'invalidate_misses': _stats['invalidate_misses'],
        }
