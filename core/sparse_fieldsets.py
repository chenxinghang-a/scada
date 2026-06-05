"""
API响应字段过滤（Sparse Fieldsets）
客户端指定需要的字段，减少响应大小。

使用方式:
    GET /api/devices?fields=id,name,status
    GET /api/data/latest?fields=device_id,value,timestamp

    from core.sparse_fieldsets import filter_fields, sparse_fields_decorator
"""

import logging
from functools import wraps
from typing import Any, Dict, List, Optional, Set
from flask import request

logger = logging.getLogger(__name__)


def filter_fields(data: Any, fields: Optional[str] = None) -> Any:
    """
    过滤数据字段

    Args:
        data: 原始数据（dict或list of dict）
        fields: 逗号分隔的字段名（None表示返回全部）

    Returns:
        过滤后的数据
    """
    if not fields:
        return data

    field_set = set(f.strip() for f in fields.split(',') if f.strip())
    if not field_set:
        return data

    if isinstance(data, dict):
        return {k: v for k, v in data.items() if k in field_set}

    if isinstance(data, list):
        return [filter_fields(item, fields) for item in data]

    return data


def filter_nested_fields(data: Dict, fields: str) -> Dict:
    """
    过滤嵌套字段（支持点号语法）

    例如: fields=name,address.city,address.country
    结果: {name: '...', address: {city: '...', country: '...'}}
    """
    field_set = set(f.strip() for f in fields.split(',') if f.strip())
    if not field_set:
        return data

    result: Dict[str, Any] = {}

    for field in field_set:
        parts = field.split('.')
        if len(parts) == 1:
            # 顶层字段
            if field in data:
                result[field] = data[field]
        else:
            # 嵌套字段
            parent = parts[0]
            if parent in data and isinstance(data[parent], dict):
                if parent not in result:
                    result[parent] = {}
                child = '.'.join(parts[1:])
                child_value = data[parent].get(child)
                if child_value is not None:
                    result[parent][child] = child_value

    return result


def sparse_fields_decorator(f):
    """
    装饰器：自动应用sparse fieldsets

    用法:
        @app.route('/api/devices')
        @sparse_fields_decorator
        def get_devices():
            return {'data': devices}
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        result = f(*args, **kwargs)

        # 获取fields参数
        fields = request.args.get('fields')
        if not fields:
            return result

        # 处理Flask响应对象
        if hasattr(result, 'get_json'):
            try:
                data = result.get_json()
                if data and isinstance(data, dict):
                    if 'data' in data:
                        data['data'] = filter_fields(data['data'], fields)
                    elif 'items' in data:
                        data['items'] = filter_fields(data['items'], fields)
                    else:
                        data = filter_fields(data, fields)

                    from flask import jsonify
                    new_response = jsonify(data)
                    new_response.status_code = result.status_code
                    return new_response
            except Exception:
                pass

        # 处理tuple响应
        if isinstance(result, tuple) and len(result) >= 1:
            data = result[0]
            if isinstance(data, dict):
                if 'data' in data:
                    data['data'] = filter_fields(data['data'], fields)
                else:
                    data = filter_fields(data, fields)
                return (data,) + result[1:]

        # 处理dict响应
        if isinstance(result, dict):
            if 'data' in result:
                result['data'] = filter_fields(result['data'], fields)
            else:
                result = filter_fields(result, fields)

        return result

    return decorated


def get_requested_fields() -> Optional[Set[str]]:
    """获取客户端请求的字段集合"""
    fields = request.args.get('fields')
    if not fields:
        return None
    return set(f.strip() for f in fields.split(',') if f.strip())


def apply_fields_to_query(base_fields: List[str], requested: Optional[Set[str]] = None) -> List[str]:
    """
    将请求的字段应用到数据库查询列

    Args:
        base_fields: 基础字段列表
        requested: 客户端请求的字段集合

    Returns:
        过滤后的字段列表
    """
    if not requested:
        return base_fields

    # 确保id字段总是包含
    requested.add('id')
    return [f for f in base_fields if f in requested]
