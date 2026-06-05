"""
按用户限流器
为不同用户角色设置不同的API调用频率限制。

使用方式:
    from core.user_rate_limiter import user_rate_limit

    @app.route('/api/export/data')
    @user_rate_limit(calls=10, period=60)  # 每分钟10次
    def export_data():
        ...
"""

import time
import logging
import threading
from functools import wraps
from typing import Optional
from flask import request, jsonify, g

logger = logging.getLogger(__name__)

# 用户限流配置: {username: {path: [timestamps]}}
_user_limits: dict[str, dict[str, list[float]]] = {}
_user_limits_lock = threading.Lock()

# 角色默认限流配置
ROLE_DEFAULTS = {
    'admin': {'calls': 100, 'period': 60},      # 管理员: 100次/分钟
    'engineer': {'calls': 60, 'period': 60},     # 工程师: 60次/分钟
    'operator': {'calls': 30, 'period': 60},     # 操作员: 30次/分钟
    'viewer': {'calls': 20, 'period': 60},       # 查看者: 20次/分钟
    'default': {'calls': 10, 'period': 60},      # 默认: 10次/分钟
}


def user_rate_limit(calls: int = 0, period: int = 60, role_overrides: Optional[dict] = None):
    """
    按用户限流装饰器

    Args:
        calls: 每周期允许的调用次数（0则使用角色默认值）
        period: 时间窗口（秒）
        role_overrides: 角色覆盖配置 {role: {calls, period}}
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            # 获取当前用户
            username = 'anonymous'
            role = 'default'
            if hasattr(request, 'current_user') and request.current_user:
                username = request.current_user.get('username', 'anonymous')
                role = request.current_user.get('role', 'default')

            # 确定限流参数
            if role_overrides and role in role_overrides:
                limit_calls = role_overrides[role].get('calls', calls or 10)
                limit_period = role_overrides[role].get('period', period)
            elif calls > 0:
                limit_calls = calls
                limit_period = period
            else:
                defaults = ROLE_DEFAULTS.get(role, ROLE_DEFAULTS['default'])
                limit_calls = defaults['calls']
                limit_period = defaults['period']

            # 检查限流
            path = request.path
            now = time.time()

            with _user_limits_lock:
                if username not in _user_limits:
                    _user_limits[username] = {}
                if path not in _user_limits[username]:
                    _user_limits[username][path] = []

                timestamps = _user_limits[username][path]

                # 清理过期的时间戳
                cutoff = now - limit_period
                while timestamps and timestamps[0] < cutoff:
                    timestamps.pop(0)

                # 检查是否超限
                if len(timestamps) >= limit_calls:
                    retry_after = int(timestamps[0] + limit_period - now) + 1
                    logger.warning(
                        "用户限流触发: user=%s role=%s path=%s (%d/%d in %ds)",
                        username, role, path, len(timestamps), limit_calls, limit_period
                    )
                    resp = jsonify({
                        'success': False,
                        'error': '请求过于频繁，请稍后重试',
                        'retry_after': retry_after,
                    })
                    resp.status_code = 429
                    resp.headers['Retry-After'] = str(retry_after)
                    resp.headers['X-RateLimit-Limit'] = str(limit_calls)
                    resp.headers['X-RateLimit-Remaining'] = '0'
                    resp.headers['X-RateLimit-Reset'] = str(int(timestamps[0] + limit_period))
                    return resp

                # 记录本次请求
                timestamps.append(now)

            # 执行原始函数
            resp = f(*args, **kwargs)

            # 添加限流头
            if hasattr(resp, 'headers'):
                with _user_limits_lock:
                    remaining = limit_calls - len(_user_limits.get(username, {}).get(path, []))
                resp.headers['X-RateLimit-Limit'] = str(limit_calls)
                resp.headers['X-RateLimit-Remaining'] = str(max(0, remaining))
                resp.headers['X-RateLimit-Reset'] = str(int(now + limit_period))

            return resp

        return decorated
    return decorator


def get_user_rate_stats(username: str = None) -> dict:
    """获取用户限流统计"""
    with _user_limits_lock:
        if username:
            return {
                'username': username,
                'paths': {
                    path: len(timestamps)
                    for path, timestamps in _user_limits.get(username, {}).items()
                },
            }
        return {
            'users': len(_user_limits),
            'total_entries': sum(
                sum(len(ts) for ts in paths.values())
                for paths in _user_limits.values()
            ),
        }


def cleanup_expired_entries():
    """清理过期的限流条目"""
    now = time.time()
    with _user_limits_lock:
        for username in list(_user_limits.keys()):
            for path in list(_user_limits[username].keys()):
                timestamps = _user_limits[username][path]
                while timestamps and timestamps[0] < now - 120:  # 清理2分钟前的
                    timestamps.pop(0)
                if not timestamps:
                    del _user_limits[username][path]
            if not _user_limits[username]:
                del _user_limits[username]
