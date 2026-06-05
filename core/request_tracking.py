"""
请求追踪与响应时间中间件
为每个请求生成唯一trace_id，记录响应时间，慢请求告警。

等保2.0 GB/T 22239 要求：审计日志应包含请求标识，便于追溯。
"""

import time
import uuid
import logging
import threading
from flask import Flask, request, g

logger = logging.getLogger(__name__)

# 慢请求阈值（秒）
SLOW_REQUEST_THRESHOLD = 2.0

# 线程安全的请求计数器
_request_counter = threading.local()


def init_request_tracking(app: Flask):
    """
    初始化请求追踪中间件

    功能:
    1. 为每个请求生成唯一 trace_id
    2. 记录请求开始/结束时间
    3. 慢请求告警（>2秒）
    4. trace_id 写入响应头 X-Trace-ID
    5. 异常时 trace_id 写入错误响应

    Args:
        app: Flask应用实例
    """

    @app.before_request
    def _before_request():
        # 生成或提取 trace_id
        trace_id = request.headers.get('X-Trace-ID') or str(uuid.uuid4())[:16]
        g.trace_id = trace_id
        g.request_start_time = time.time()
        g.request_id = f"{request.method}:{request.path}"

    @app.after_request
    def _after_request(response):
        # 添加 trace_id 到响应头
        trace_id = getattr(g, 'trace_id', 'unknown')
        response.headers['X-Trace-ID'] = trace_id

        # 记录响应时间
        start_time = getattr(g, 'request_start_time', None)
        if start_time:
            duration = time.time() - start_time
            response.headers['X-Response-Time'] = f"{duration:.3f}s"

            # 慢请求告警
            if duration > SLOW_REQUEST_THRESHOLD:
                logger.warning(
                    "慢请求: %s %s 耗时%.3fs (trace_id=%s)",
                    request.method, request.path, duration, trace_id
                )

            # API请求日志（排除静态资源和健康检查）
            if request.path.startswith('/api/') and request.path != '/api/health/status':
                logger.info(
                    "API %s %s %d %.3fs (trace_id=%s)",
                    request.method, request.path,
                    response.status_code, duration, trace_id
                )

        return response

    @app.errorhandler(Exception)
    def _handle_exception(error):
        """全局异常处理中包含trace_id"""
        trace_id = getattr(g, 'trace_id', 'unknown')
        logger.error(
            "未捕获异常: %s (trace_id=%s)", error, trace_id,
            exc_info=True
        )
        from flask import jsonify
        response = jsonify({
            'success': False,
            'error': '服务器内部错误',
            'trace_id': trace_id,
        })
        response.status_code = 500
        response.headers['X-Trace-ID'] = trace_id
        return response

    logger.info("请求追踪中间件已初始化 (慢请求阈值=%ss)", SLOW_REQUEST_THRESHOLD)
