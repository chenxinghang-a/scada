"""
API文档增强
为Swagger/OpenAPI文档添加参数示例、响应示例、错误码说明。

使用方式:
    from core.api_docs_enhancer import enhance_docs
    enhanced = enhance_docs(swagger_spec)
"""

import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# 常用参数示例
PARAM_EXAMPLES = {
    'device_id': {'example': 'pump_001', 'description': '设备唯一标识'},
    'register_name': {'example': 'temperature', 'description': '寄存器名称'},
    'page': {'example': 1, 'description': '页码（从1开始）'},
    'page_size': {'example': 20, 'description': '每页数量（最大100）'},
    'start_time': {'example': '2024-01-01T00:00:00', 'description': '开始时间（ISO 8601）'},
    'end_time': {'example': '2024-01-31T23:59:59', 'description': '结束时间（ISO 8601）'},
}

# 常用响应示例
RESPONSE_EXAMPLES = {
    'success': {
        'success': True,
        'data': {},
        'message': '操作成功',
    },
    'error': {
        'success': False,
        'error': '错误描述',
        'error_code': 'ERR_001',
    },
    'paginated': {
        'success': True,
        'data': [],
        'pagination': {
            'page': 1,
            'page_size': 20,
            'total': 100,
            'total_pages': 5,
        },
    },
    'unauthorized': {
        'success': False,
        'error': '未授权访问',
        'error_code': 'AUTH_001',
    },
    'rate_limited': {
        'success': False,
        'error': '请求过于频繁',
        'error_code': 'RATE_001',
        'retry_after': 60,
    },
}

# 错误码说明
ERROR_CODES = {
    'AUTH_001': '未授权访问，请先登录',
    'AUTH_002': 'Token已过期，请重新登录',
    'AUTH_003': '权限不足，无法执行此操作',
    'ERR_001': '请求参数错误',
    'ERR_002': '资源不存在',
    'ERR_003': '操作冲突',
    'ERR_004': '服务器内部错误',
    'RATE_001': '请求频率超限',
    'RATE_002': '用户请求频率超限',
    'DATA_001': '数据验证失败',
    'DATA_002': '数据格式错误',
}


def enhance_docs(spec: Dict[str, Any]) -> Dict[str, Any]:
    """增强Swagger文档"""
    enhanced = dict(spec)

    # 添加全局错误码说明
    if 'info' not in enhanced:
        enhanced['info'] = {}
    enhanced['info']['x-error-codes'] = ERROR_CODES

    # 增强每个路径
    if 'paths' in enhanced:
        for path, methods in enhanced['paths'].items():
            for method, operation in methods.items():
                if not isinstance(operation, dict):
                    continue
                _enhance_operation(operation, path)

    # 添加通用响应定义
    if 'components' not in enhanced:
        enhanced['components'] = {}
    if 'schemas' not in enhanced['components']:
        enhanced['components']['schemas'] = {}

    enhanced['components']['schemas']['ErrorResponse'] = {
        'type': 'object',
        'properties': {
            'success': {'type': 'boolean', 'example': False},
            'error': {'type': 'string', 'example': '错误描述'},
            'error_code': {'type': 'string', 'example': 'ERR_001'},
        },
    }

    return enhanced


def _enhance_operation(operation: Dict[str, Any], path: str):
    """增强单个操作"""
    # 添加参数示例
    if 'parameters' in operation:
        for param in operation['parameters']:
            name = param.get('name', '')
            if name in PARAM_EXAMPLES:
                param['example'] = PARAM_EXAMPLES[name]['example']
                if 'description' not in param:
                    param['description'] = PARAM_EXAMPLES[name]['description']

    # 添加响应示例
    if 'responses' in operation:
        for status_code, response in operation['responses'].items():
            if status_code == '200' and 'content' in response:
                for content_type, content in response['content'].items():
                    if 'example' not in content:
                        content['example'] = RESPONSE_EXAMPLES.get('success', {})
            elif status_code == '401':
                _add_error_response(response, 'unauthorized')
            elif status_code == '429':
                _add_error_response(response, 'rate_limited')
            elif status_code == '500':
                _add_error_response(response, 'error')

    # 添加标签
    if 'tags' not in operation:
        parts = path.split('/')
        if len(parts) > 2:
            operation['tags'] = [parts[2].replace('_', ' ').title()]


def _add_error_response(response: Dict[str, Any], example_key: str):
    """添加错误响应示例"""
    if 'content' not in response:
        response['content'] = {}
    if 'application/json' not in response['content']:
        response['content']['application/json'] = {}
    if 'example' not in response['content']['application/json']:
        response['content']['application/json']['example'] = RESPONSE_EXAMPLES.get(example_key, {})


def get_api_summary(spec: Dict[str, Any]) -> Dict[str, Any]:
    """获取API摘要统计"""
    paths = spec.get('paths', {})
    total_endpoints = 0
    methods_count: Dict[str, int] = {}
    tags: set = set()

    for path, methods in paths.items():
        for method, operation in methods.items():
            if method in ('get', 'post', 'put', 'delete', 'patch'):
                total_endpoints += 1
                methods_count[method] = methods_count.get(method, 0) + 1
                if isinstance(operation, dict):
                    for tag in operation.get('tags', []):
                        tags.add(tag)

    return {
        'total_endpoints': total_endpoints,
        'methods': methods_count,
        'tags': sorted(tags),
        'paths_count': len(paths),
    }
