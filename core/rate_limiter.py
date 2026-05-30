"""
API速率限制 - 防止暴力攻击和DDoS
等保2.0要求：对异常访问行为进行限制
"""
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import logging

logger = logging.getLogger(__name__)


def create_limiter(app):
    """创建速率限制器

    默认限制: 200次/分钟, 50次/秒
    敏感接口单独限流（登录、注册、密码修改、设备控制）
    """
    limiter = Limiter(
        app=app,
        key_func=get_remote_address,
        default_limits=["200 per minute", "50 per second"],
        storage_uri="memory://",
        strategy="fixed-window",
    )

    # 登录接口更严格的限制（防暴力破解）
    @limiter.limit("5 per minute", endpoint="api_auth.login")
    def login_limit():
        pass

    # 注册接口限制
    @limiter.limit("3 per minute", endpoint="api_auth.register")
    def register_limit():
        pass

    # 密码修改限制
    @limiter.limit("10 per minute", endpoint="api_auth.change_password")
    def change_password_limit():
        pass

    # 设备控制限制（防止误操作）
    @limiter.limit("30 per minute", endpoint="api_control.write_register")
    @limiter.limit("30 per minute", endpoint="api_control.write_coil")
    @limiter.limit("10 per minute", endpoint="api_control.trigger_estop")
    def control_limit():
        pass

    logger.info("API速率限制已启用: 200/min, 50/s default, 5/min login")
    return limiter
