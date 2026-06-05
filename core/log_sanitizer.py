"""
日志敏感数据脱敏器
自动过滤密码/token/密钥等敏感信息，防止日志泄露。

使用方式:
    from core.log_sanitizer import SanitizingFilter
    logger.addFilter(SanitizingFilter())
"""

import re
import logging
from typing import Dict, List, Pattern


class SanitizingFilter(logging.Filter):
    """日志敏感数据过滤器"""

    # 敏感字段名模式
    SENSITIVE_KEYS = {
        'password', 'passwd', 'pwd', 'secret', 'token', 'api_key', 'apikey',
        'access_token', 'refresh_token', 'auth_token', 'session_id',
        'csrf_token', 'private_key', 'credential', 'authorization',
        'x-api-key', 'x-auth-token', 'bearer',
    }

    # 敏感值正则模式
    SENSITIVE_PATTERNS: List[Pattern] = [
        # Bearer token
        re.compile(r'(Bearer\s+)[A-Za-z0-9\-._~+/]+=*', re.IGNORECASE),
        # API Key格式
        re.compile(r'(api[_-]?key[\'"\s:=]+)[A-Za-z0-9\-._~+/]{16,}', re.IGNORECASE),
        # JWT token
        re.compile(r'(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})'),
        # 通用密码赋值
        re.compile(r'(password|passwd|pwd|secret|token)\s*[=:]\s*[\'"][^\'"]{4,}[\'"]', re.IGNORECASE),
        # 长hex字符串（可能是密钥）
        re.compile(r'\b[a-fA-F0-9]{32,}\b'),
    ]

    # 脱敏替换
    MASK = '***'
    MASK_LONG = '****...****'

    def __init__(self, name: str = ''):
        super().__init__(name)
        self._compiled_keys = {k.lower() for k in self.SENSITIVE_KEYS}

    def filter(self, record: logging.LogRecord) -> bool:
        """过滤并脱敏日志记录"""
        # 脱敏消息
        if record.msg and isinstance(record.msg, str):
            record.msg = self._sanitize(record.msg)

        # 脱敏参数
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._sanitize_value(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._sanitize_value(a) for a in record.args)

        return True

    def _sanitize(self, text: str) -> str:
        """脱敏文本"""
        result = text

        # 替换正则匹配的敏感模式
        for pattern in self.SENSITIVE_PATTERNS:
            result = pattern.sub(self._mask_match, result)

        return result

    def _mask_match(self, match: re.Match) -> str:
        """正则替换回调"""
        groups = match.groups()
        if groups:
            # 保留前缀，替换值
            return groups[0] + self.MASK_LONG
        return self.MASK_LONG

    def _sanitize_value(self, value: any) -> any:
        """脱敏单个值"""
        if isinstance(value, str) and len(value) > 8:
            # 检查是否像密钥/token
            if any(c.isupper() for c in value) and any(c.isdigit() for c in value) and len(value) > 16:
                return self.MASK_LONG
        return value


class SanitizingFormatter(logging.Formatter):
    """带脱敏的格式化器"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._filter = SanitizingFilter()

    def format(self, record: logging.LogRecord) -> str:
        # 先脱敏
        self._filter.filter(record)
        return super().format(record)


def setup_log_sanitization():
    """全局设置日志脱敏"""
    root_logger = logging.getLogger()
    sanitizer = SanitizingFilter()

    # 添加到所有handler
    for handler in root_logger.handlers:
        handler.addFilter(sanitizer)

    return sanitizer
