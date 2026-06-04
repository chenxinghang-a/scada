"""
API速率限制 - 防止暴力攻击和DDoS
等保2.0要求：对异常访问行为进行限制

增强功能：
- 按端点类型分级限制
- 登录接口严格限制
- 控制接口中等限制
- 查询接口宽松限制
"""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import logging

logger = logging.getLogger(__name__)

# 限流配置
RATE_LIMIT_CONFIG = {
    # 默认限制
    'default': ['200 per minute', '50 per second'],

    # 登录接口（严格）
    'login': ['5 per minute', '10 per hour'],

    # 控制接口（中等）
    'control': ['30 per minute', '100 per hour'],

    # 查询接口（宽松）
    'query': ['500 per minute'],

    # 导出接口（严格）
    'export': ['10 per minute'],

    # 批量操作（严格）
    'batch': ['5 per minute'],
}


def create_limiter(app):
    """创建速率限制器"""
    limiter = Limiter(
        app=app,
        key_func=get_remote_address,
        default_limits=RATE_LIMIT_CONFIG['default'],
        storage_uri="memory://",
        strategy="fixed-window",
    )

    logger.info(f"API速率限制已启用: {RATE_LIMIT_CONFIG['default']}")
    return limiter


def get_rate_limit(endpoint_type: str = 'default') -> str:
    """获取指定端点类型的限流配置"""
    return RATE_LIMIT_CONFIG.get(endpoint_type, RATE_LIMIT_CONFIG['default'])


def rate_limit_login(func):
    """登录接口限流装饰器"""
    from flask_limiter.util import get_remote_address
    return get_limiter().limit(
        RATE_LIMIT_CONFIG['login'],
        key_func=get_remote_address
    )(func)


def rate_limit_control(func):
    """控制接口限流装饰器"""
    from flask_limiter.util import get_remote_address
    return get_limiter().limit(
        RATE_LIMIT_CONFIG['control'],
        key_func=get_remote_address
    )(func)


def rate_limit_export(func):
    """导出接口限流装饰器"""
    from flask_limiter.util import get_remote_address
    return get_limiter().limit(
        RATE_LIMIT_CONFIG['export'],
        key_func=get_remote_address
    )(func)


# 全局限流器实例
_limiter = None


def get_limiter():
    """获取全局限流器实例"""
    global _limiter
    return _limiter


def set_limiter(limiter):
    """设置全局限流器实例"""
    global _limiter
    _limiter = limiter
