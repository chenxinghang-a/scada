"""
CSRF防护模块
实现Double Submit Cookie模式的CSRF防护
GB/T 37980 合规
"""
import hmac
import hashlib
import time
import secrets
import logging
from functools import wraps
from flask import request, jsonify, current_app

logger = logging.getLogger(__name__)


class CSRFProtection:
    """CSRF防护 - Double Submit Cookie模式"""

    def __init__(self, app=None):
        self._secret = None
        if app:
            self.init_app(app)

    def init_app(self, app):
        """初始化Flask应用"""
        from config import SecurityConfig
        self._secret = SecurityConfig.CSRF_SECRET.encode('utf-8') if SecurityConfig.CSRF_ENABLED else None

        if not SecurityConfig.CSRF_ENABLED:
            logger.info("CSRF防护已禁用")
            return

        # 注册before_request检查
        @app.before_request
        def check_csrf():
            # 只检查状态变更请求
            if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
                # API请求检查CSRF token
                if request.path.startswith('/api/'):
                    # 登录/注册等公开端点豁免
                    exempt_paths = ['/api/auth/login', '/api/auth/register', '/api/auth/refresh']
                    if request.path in exempt_paths:
                        return

                    # JWT认证的API不需要CSRF（Bearer token本身就是防护）
                    auth_header = request.headers.get('Authorization', '')
                    if auth_header.startswith('Bearer '):
                        return

                    # 检查CSRF token
                    csrf_token = request.headers.get('X-CSRF-Token')
                    if not csrf_token:
                        return jsonify({'error': '缺少CSRF令牌'}), 403

                    # 验证token
                    if not self._validate_token(csrf_token):
                        return jsonify({'error': 'CSRF令牌无效'}), 403

    def _validate_token(self, token: str) -> bool:
        """验证CSRF token"""
        if not self._secret:
            return True

        try:
            # token格式: timestamp.hmac
            parts = token.split('.', 1)
            if len(parts) != 2:
                return False

            timestamp_str, hmac_value = parts
            timestamp = int(timestamp_str)

            # 检查过期（1小时）
            if time.time() - timestamp > 3600:
                return False

            # 验证HMAC
            expected = hmac.new(
                self._secret,
                timestamp_str.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()

            return hmac.compare_digest(hmac_value, expected)
        except (ValueError, TypeError) as e:
            logger.debug(f"CSRF token验证异常: {e}")
            return False

    def generate_token(self) -> str:
        """生成CSRF token"""
        if not self._secret:
            return ''

        timestamp = str(int(time.time()))
        hmac_value = hmac.new(
            self._secret,
            timestamp.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return f"{timestamp}.{hmac_value}"


# 全局实例
csrf = CSRFProtection()
