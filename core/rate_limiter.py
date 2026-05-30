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
    """
    limiter = Limiter(
        app=app,
        key_func=get_remote_address,
        default_limits=["200 per minute", "50 per second"],
        storage_uri="memory://",
        strategy="fixed-window",
    )

    logger.info("API速率限制已启用: 200/min, 50/s default")
    return limiter
