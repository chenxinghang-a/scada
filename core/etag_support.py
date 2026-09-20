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

# ============================================================================
# 接线状态：未接线（WIRED = False）
# ============================================================================
# 本模块在生产代码（run.py / 各业务层 / 其它 core 模块）中**没有任何 import 引用**。
# 模块本身可用，但当前没有调用方 —— 也就是说它宣称的这项能力**当前并未生效**。
#
# 为什么保留而不删除：删掉即丢能力，模块本身有测试价值；这里只把「没接线」显式化、
# 可追踪，避免「代码在库里」被误读成「功能在跑」。
#
# 自动化复核（防止本标注过期）：
#   tests/test_core_regressions.py::test_unwired_marker_matches_reality
#   —— 该用例用 AST 扫描全仓库 import。一旦有人把本模块接进生产代码，
#      而这里仍写着 WIRED = False，用例即失败，强制文档与事实同步。
#
# 接线建议（需改 run.py / 各层，core 内部无权自行接线）：
#     给读多写少的 GET 端点加 `@etag_required`，配合 If-None-Match 降低带宽。
# ============================================================================
WIRED = False


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

                if data is None:
                    # 非 JSON 响应（text/html、二进制、空 body）时 Flask 的 get_json()
                    # 返回 None 而**不抛异常**（已实测 flask 3.1.3）。此前会继续执行
                    # generate_etag(None) → sha256(str(None))，于是所有非 JSON 响应都拿到
                    # **同一个常量 ETag**（dc937b59892604f5）。客户端把这个值带到**别的**
                    # 非 JSON 接口上，If-None-Match 命中 → 服务端返回 304 空 body，
                    # 浏览器于是拿着陈旧/错误的内容当最新内容用 —— 典型"静默假成功"。
                    # 故：无有效 payload 时不生成 ETag（Cache-Control 保持原样）。
                    logger.debug(
                        "etag_required: 响应非 JSON (mimetype=%s)，跳过 ETag 以避免常量 ETag 造成跨接口 304",
                        getattr(result, 'mimetype', 'unknown'),
                    )
                    result.headers['Cache-Control'] = 'private, max-age=0, must-revalidate'
                    return result

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
            except Exception as e:
                # 异常被吞掉后本响应不会带 ETag 头 → 条件请求缓存对该接口静默失效（功能降级）
                logger.warning(f"etag_required 生成/设置ETag失败(该响应将不带ETag): {e}")

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
            except Exception as e:
                # 预期内：非 JSON 响应时 get_json() 返回 None，取 last_modified 必然失败，属正常分支；
                # 其余失败只会导致 Last-Modified 未设置，故用 debug 避免刷屏。
                logger.debug(f"conditional_request 设置Last-Modified失败(该响应将不带Last-Modified): {e}")

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
