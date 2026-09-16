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
import threading

logger = logging.getLogger(__name__)

# 限流配置
RATE_LIMIT_CONFIG = {
    # 默认限制
    'default': ['200 per minute', '50 per second'],

    # 登录接口（严格）
    'login': ['5 per minute', '10 per hour'],

    # 改密接口（严格，防令牌被盗后暴力改密）
    'change_password': ['5 per minute', '20 per hour'],

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
    """创建速率限制器并登记为全局实例"""
    limiter = Limiter(
        app=app,
        key_func=get_remote_address,
        default_limits=RATE_LIMIT_CONFIG['default'],
        storage_uri="memory://",
        strategy="fixed-window",
    )

    # 必须登记，否则 get_limiter() 恒为 None，
    # rate_limit_login / control / export 这些装饰器一被使用就 AttributeError。
    set_limiter(limiter)

    logger.info(f"API速率限制已启用: {RATE_LIMIT_CONFIG['default']}")
    return limiter


def _as_limit_string(limits) -> str:
    """把限流配置规整为 flask-limiter 能接受的字符串。

    RATE_LIMIT_CONFIG 里存的是列表（便于阅读和复用），但 Limiter.limit()
    只接受字符串，多条限制用 ';' 分隔。历史上这里直接传列表，导致
    unhashable type: 'list'，这也是那几个限流装饰器从未真正生效的原因之一。
    """
    if isinstance(limits, str):
        return limits
    return ';'.join(str(x) for x in limits)


# 需要分级限流的端点（endpoint 名 → 配置键）
_ENDPOINT_LIMITS = {
    'api_auth.login': 'login',
    'api_auth.force_change_password': 'change_password',
    'api_auth.change_password': 'change_password',
    'api_auth.register': 'batch',
}


def apply_endpoint_limits(app, limiter):
    """把分级限流绑定到具体视图函数。

    flask-limiter 的装饰器在 import 期就需要 Limiter 实例存在，而本项目的 Limiter
    是在 create_app 里才创建的（单元测试直接用裸 Flask app，不经过 create_app）。
    因此在蓝图注册完成后按 endpoint 名逐个包裹视图函数：
    既让真实服务生效，又不会给单测引入未初始化的限流器。

    Returns:
        list[str]: 实际绑定成功的 endpoint 名
    """
    applied = []
    for endpoint, cfg_key in _ENDPOINT_LIMITS.items():
        view = app.view_functions.get(endpoint)
        if view is None:
            logger.warning("分级限流：未找到端点 %s，跳过", endpoint)
            continue
        # flask-limiter 的 limit() 只接受字符串；列表会触发 unhashable type: 'list'。
        # 多条限制用 ';' 连接，例如 '5 per minute;10 per hour'。
        limits = _as_limit_string(RATE_LIMIT_CONFIG[cfg_key])
        app.view_functions[endpoint] = limiter.limit(
            limits, key_func=get_remote_address
        )(view)
        applied.append(endpoint)

    logger.info("分级限流已绑定 %d 个端点: %s", len(applied), ', '.join(applied))
    return applied


def get_rate_limit(endpoint_type: str = 'default') -> str:
    """获取指定端点类型的限流配置"""
    return RATE_LIMIT_CONFIG.get(endpoint_type, RATE_LIMIT_CONFIG['default'])


def rate_limit_login(func):
    """登录接口限流装饰器"""
    from flask_limiter.util import get_remote_address
    return get_limiter().limit(
        _as_limit_string(RATE_LIMIT_CONFIG['login']),
        key_func=get_remote_address
    )(func)


def rate_limit_control(func):
    """控制接口限流装饰器"""
    from flask_limiter.util import get_remote_address
    return get_limiter().limit(
        _as_limit_string(RATE_LIMIT_CONFIG['control']),
        key_func=get_remote_address
    )(func)


def rate_limit_export(func):
    """导出接口限流装饰器"""
    from flask_limiter.util import get_remote_address
    return get_limiter().limit(
        _as_limit_string(RATE_LIMIT_CONFIG['export']),
        key_func=get_remote_address
    )(func)


# 全局限流器实例
# 访问必须持锁：create_limiter() 在 Flask 启动路径上写，get_limiter() 可能被
# 任意请求线程（含限流装饰器首次调用）读，无锁时存在「读到 None → AttributeError」
# 与多实例并发创建的窗口。
_limiter = None
_limiter_lock = threading.Lock()


def get_limiter():
    """获取全局限流器实例（线程安全）"""
    with _limiter_lock:
        return _limiter


def set_limiter(limiter):
    """设置全局限流器实例（线程安全，幂等：已设置则不覆盖）"""
    global _limiter
    with _limiter_lock:
        if _limiter is None:
            _limiter = limiter
        elif _limiter is not limiter:
            logger.warning("限流器实例被重复设置，保留既有实例，忽略本次设置")
    return _limiter
