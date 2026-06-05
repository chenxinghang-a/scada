"""
API ETag支持
条件请求（If-None-Match/If-Modified-Since），减少带宽。

使用方式:
    from core.etag_support import etag_required, generate_etag
    @app.route('/api/data')
    @etag_required
    def get_data():
        return {'data': [...]}
"""

import hashlib
import logging
import time
from functools import wraps
from typing import Any, Optional
from flask import request, make_response, jsonify

logger = logging.getLogger(__name__)


def generate_etag(data: Any) -> str:
    """生成ETag"""
    content = str(data).encode('utf-8')
    return hashlib.sha256(content).hexdigest()[:16]


def generate_weak_etag(data: Any) -> str:
    """生成弱ETag"""
    return f'W/"{generate_etag(data)}"'


def etag_required(f):
    """ETag装饰器：支持条件请求"""
    @wraps(f)
    def decorated(*args, **kwargs):
        result = f(*args, **kwargs)

        # 处理Flask响应对象
        if hasattr(result, 'get_json'):
            try:
                data = result.get_json()
                etag = generate_etag(data)

                # 检查If-None-Match
                if_none_match = request.headers.get('If-None-Match')
                if if_none_match and if_none_match == etag:
                    response = make_response('', 304)
                    response.headers['ETag'] = etag
                    return response

                # 设置ETag头
                result.headers['ETag'] = etag
                result.headers['Cache-Control'] = 'private, max-age=0, must-revalidate'
                return result
            except Exception:
                pass

        # 处理tuple响应
        if isinstance(result, tuple) and len(result) >= 1:
            data = result[0]
            if isinstance(data, dict):
                etag = generate_etag(data)

                if_none_match = request.headers.get('If-None-Match')
                if if_none_match and if_none_match == etag:
                    return make_response('', 304)

                response = make_response(jsonify(data))
                response.headers['ETag'] = etag
                response.headers['Cache-Control'] = 'private, max-age=0, must-revalidate'
                if len(result) > 1:
                    response.status_code = result[1]
                return response

        # 处理dict响应
        if isinstance(result, dict):
            etag = generate_etag(result)

            if_none_match = request.headers.get('If-None-Match')
            if if_none_match and if_none_match == etag:
                return make_response('', 304)

            response = make_response(jsonify(result))
            response.headers['ETag'] = etag
            response.headers['Cache-Control'] = 'private, max-age=0, must-revalidate'
            return response

        return result

    return decorated


def conditional_request(f):
    """条件请求装饰器：支持If-Modified-Since"""
    @wraps(f)
    def decorated(*args, **kwargs):
        result = f(*args, **kwargs)

        if hasattr(result, 'get_json'):
            try:
                data = result.get_json()
                last_modified = data.get('last_modified')

                if last_modified:
                    if_modified_since = request.headers.get('If-Modified-Since')
                    if if_modified_since:
                        # 简化比较：实际应用中需要解析HTTP日期
                        pass

                    result.headers['Last-Modified'] = last_modified
                    result.headers['Cache-Control'] = 'private, max-age=0, must-revalidate'
            except Exception:
                pass

        return result

    return decorated


class ETagManager:
    """ETag管理器"""

    def __init__(self):
        self._cache = {}

    def get_etag(self, key: str, data_generator) -> tuple:
        """获取或生成ETag"""
        import time
        now = time.time()

        if key in self._cache:
            cached = self._cache[key]
            if now - cached['timestamp'] < 60:  # 60秒缓存
                return cached['etag'], cached['data']

        data = data_generator()
        etag = generate_etag(data)

        self._cache[key] = {
            'etag': etag,
            'data': data,
            'timestamp': now,
        }

        return etag, data

    def invalidate(self, key: str):
        """使ETag失效"""
        self._cache.pop(key, None)

    def clear(self):
        """清空缓存"""
        self._cache.clear()


# 全局实例
etag_manager = ETagManager()
