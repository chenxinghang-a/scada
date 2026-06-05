"""
API错误码标准化
统一错误码体系，便于前端统一处理和国际化。

使用方式:
    from core.error_code_standardizer import ErrorCode, api_error_response
    return api_error_response(ErrorCode.DEVICE_NOT_FOUND, device_id='pump_001')
"""

import logging
from enum import Enum
from typing import Any, Dict, Optional
from flask import jsonify, request

logger = logging.getLogger(__name__)


class ErrorCode(Enum):
    """统一错误码"""
    # 通用错误 (1xxx)
    UNKNOWN_ERROR = 'ERR_1000'
    INVALID_REQUEST = 'ERR_1001'
    NOT_FOUND = 'ERR_1002'
    PERMISSION_DENIED = 'ERR_1003'
    RATE_LIMITED = 'ERR_1004'
    SERVICE_UNAVAILABLE = 'ERR_1005'
    TIMEOUT = 'ERR_1006'

    # 认证错误 (2xxx)
    AUTH_REQUIRED = 'ERR_2001'
    AUTH_EXPIRED = 'ERR_2002'
    AUTH_INVALID = 'ERR_2003'
    AUTH_LOCKED = 'ERR_2004'

    # 设备错误 (3xxx)
    DEVICE_NOT_FOUND = 'ERR_3001'
    DEVICE_OFFLINE = 'ERR_3002'
    DEVICE_BUSY = 'ERR_3003'
    DEVICE_TIMEOUT = 'ERR_3004'
    DEVICE_PROTOCOL_ERROR = 'ERR_3005'

    # 数据错误 (4xxx)
    DATA_VALIDATION = 'ERR_4001'
    DATA_NOT_FOUND = 'ERR_4002'
    DATA_CONFLICT = 'ERR_4003'
    DATA_TOO_LARGE = 'ERR_4004'

    # 系统错误 (5xxx)
    DATABASE_ERROR = 'ERR_5001'
    NETWORK_ERROR = 'ERR_5002'
    CONFIG_ERROR = 'ERR_5003'
    INTERNAL_ERROR = 'ERR_5004'


# 错误码描述映射
ERROR_DESCRIPTIONS: Dict[str, Dict[str, str]] = {
    'ERR_1000': {'zh': '未知错误', 'en': 'Unknown error'},
    'ERR_1001': {'zh': '请求参数无效', 'en': 'Invalid request'},
    'ERR_1002': {'zh': '资源不存在', 'en': 'Not found'},
    'ERR_1003': {'zh': '权限不足', 'en': 'Permission denied'},
    'ERR_1004': {'zh': '请求频率超限', 'en': 'Rate limited'},
    'ERR_1005': {'zh': '服务不可用', 'en': 'Service unavailable'},
    'ERR_1006': {'zh': '请求超时', 'en': 'Timeout'},
    'ERR_2001': {'zh': '需要认证', 'en': 'Authentication required'},
    'ERR_2002': {'zh': '认证已过期', 'en': 'Authentication expired'},
    'ERR_2003': {'zh': '认证无效', 'en': 'Invalid authentication'},
    'ERR_2004': {'zh': '账户已锁定', 'en': 'Account locked'},
    'ERR_3001': {'zh': '设备不存在', 'en': 'Device not found'},
    'ERR_3002': {'zh': '设备离线', 'en': 'Device offline'},
    'ERR_3003': {'zh': '设备忙', 'en': 'Device busy'},
    'ERR_3004': {'zh': '设备超时', 'en': 'Device timeout'},
    'ERR_3005': {'zh': '设备协议错误', 'en': 'Device protocol error'},
    'ERR_4001': {'zh': '数据验证失败', 'en': 'Data validation failed'},
    'ERR_4002': {'zh': '数据不存在', 'en': 'Data not found'},
    'ERR_4003': {'zh': '数据冲突', 'en': 'Data conflict'},
    'ERR_4004': {'zh': '数据量过大', 'en': 'Data too large'},
    'ERR_5001': {'zh': '数据库错误', 'en': 'Database error'},
    'ERR_5002': {'zh': '网络错误', 'en': 'Network error'},
    'ERR_5003': {'zh': '配置错误', 'en': 'Configuration error'},
    'ERR_5004': {'zh': '内部错误', 'en': 'Internal error'},
}

# 错误码对应的HTTP状态码
ERROR_HTTP_STATUS: Dict[str, int] = {
    'ERR_1000': 500,
    'ERR_1001': 400,
    'ERR_1002': 404,
    'ERR_1003': 403,
    'ERR_1004': 429,
    'ERR_1005': 503,
    'ERR_1006': 504,
    'ERR_2001': 401,
    'ERR_2002': 401,
    'ERR_2003': 401,
    'ERR_2004': 423,
    'ERR_3001': 404,
    'ERR_3002': 503,
    'ERR_3003': 409,
    'ERR_3004': 504,
    'ERR_3005': 502,
    'ERR_4001': 400,
    'ERR_4002': 404,
    'ERR_4003': 409,
    'ERR_4004': 413,
    'ERR_5001': 500,
    'ERR_5002': 502,
    'ERR_5003': 500,
    'ERR_5004': 500,
}


def get_error_description(error_code: str, lang: str = 'zh') -> str:
    """获取错误描述"""
    desc = ERROR_DESCRIPTIONS.get(error_code, {})
    return desc.get(lang, desc.get('zh', '未知错误'))


def api_error_response(
    error_code: ErrorCode,
    detail: str = None,
    lang: str = 'zh',
    **extra,
):
    """
    标准化错误响应

    Args:
        error_code: 错误码枚举
        detail: 详细信息
        lang: 语言（zh/en）
        **extra: 额外字段

    Returns:
        Flask响应
    """
    code = error_code.value
    description = get_error_description(code, lang)
    http_status = ERROR_HTTP_STATUS.get(code, 500)

    response = {
        'success': False,
        'error': {
            'code': code,
            'message': detail or description,
            'description': description,
        },
    }

    # 添加trace_id
    trace_id = getattr(request, 'trace_id', None) if request else None
    if trace_id:
        response['error']['trace_id'] = trace_id

    # 添加额外字段
    response.update(extra)

    return jsonify(response), http_status


def api_success_response(data: Any = None, message: str = None, **extra):
    """标准化成功响应"""
    response = {'success': True}

    if data is not None:
        response['data'] = data
    if message:
        response['message'] = message

    response.update(extra)
    return jsonify(response)
