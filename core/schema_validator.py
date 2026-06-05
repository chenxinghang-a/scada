"""
JSON Schema验证增强
API输入验证，支持嵌套对象和自定义规则。

使用方式:
    from core.schema_validator import SchemaValidator
    validator = SchemaValidator(schema)
    result = validator.validate(data)
"""

import re
import logging
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)


class SchemaValidationError:
    """验证错误"""
    def __init__(self, path: str, message: str, value: Any = None):
        self.path = path
        self.message = message
        self.value = value

    def to_dict(self) -> Dict[str, Any]:
        return {
            'path': self.path,
            'message': self.message,
            'value': str(self.value)[:100] if self.value is not None else None,
        }


class SchemaValidator:
    """Schema验证器"""

    def __init__(self, schema: Dict[str, Any]):
        self.schema = schema

    def validate(self, data: Any) -> Tuple[bool, List[SchemaValidationError]]:
        """
        验证数据

        Args:
            data: 要验证的数据

        Returns:
            (是否有效, 错误列表)
        """
        errors = []
        self._validate_node(data, self.schema, '$', errors)
        return len(errors) == 0, errors

    def _validate_node(self, data: Any, schema: Dict, path: str, errors: List):
        """验证节点"""
        # 类型检查
        expected_type = schema.get('type')
        if expected_type:
            if not self._check_type(data, expected_type):
                errors.append(SchemaValidationError(
                    path, f"期望类型 {expected_type}，实际类型 {type(data).__name__}", data
                ))
                return

        # 空值检查
        if data is None:
            if schema.get('required', False):
                errors.append(SchemaValidationError(path, "必填字段不能为空"))
            return

        # 枚举检查
        enum_values = schema.get('enum')
        if enum_values and data not in enum_values:
            errors.append(SchemaValidationError(
                path, f"值必须是 {enum_values} 之一", data
            ))

        # 数值范围
        if isinstance(data, (int, float)):
            minimum = schema.get('minimum')
            maximum = schema.get('maximum')
            if minimum is not None and data < minimum:
                errors.append(SchemaValidationError(
                    path, f"值不能小于 {minimum}", data
                ))
            if maximum is not None and data > maximum:
                errors.append(SchemaValidationError(
                    path, f"值不能大于 {maximum}", data
                ))

        # 字符串规则
        if isinstance(data, str):
            min_length = schema.get('minLength')
            max_length = schema.get('maxLength')
            pattern = schema.get('pattern')
            format_type = schema.get('format')

            if min_length is not None and len(data) < min_length:
                errors.append(SchemaValidationError(
                    path, f"长度不能少于 {min_length} 个字符", data
                ))
            if max_length is not None and len(data) > max_length:
                errors.append(SchemaValidationError(
                    path, f"长度不能超过 {max_length} 个字符", data
                ))
            if pattern and not re.match(pattern, data):
                errors.append(SchemaValidationError(
                    path, f"格式不匹配: {pattern}", data
                ))
            if format_type:
                if not self._check_format(data, format_type):
                    errors.append(SchemaValidationError(
                        path, f"格式不正确: {format_type}", data
                    ))

        # 数组验证
        if isinstance(data, list):
            items_schema = schema.get('items')
            if items_schema:
                min_items = schema.get('minItems')
                max_items = schema.get('maxItems')
                if min_items is not None and len(data) < min_items:
                    errors.append(SchemaValidationError(
                        path, f"数组长度不能少于 {min_items}", data
                    ))
                if max_items is not None and len(data) > max_items:
                    errors.append(SchemaValidationError(
                        path, f"数组长度不能超过 {max_items}", data
                    ))
                for i, item in enumerate(data):
                    self._validate_node(item, items_schema, f'{path}[{i}]', errors)

        # 对象验证
        if isinstance(data, dict):
            properties = schema.get('properties', {})
            required = schema.get('required', [])
            additional = schema.get('additionalProperties', True)

            # 必填字段
            for field in required:
                if field not in data:
                    errors.append(SchemaValidationError(
                        f'{path}.{field}', "必填字段缺失"
                    ))

            # 字段验证
            for key, value in data.items():
                field_path = f'{path}.{key}'
                if key in properties:
                    self._validate_node(value, properties[key], field_path, errors)
                elif not additional:
                    errors.append(SchemaValidationError(
                        field_path, "不允许的额外字段", key
                    ))

    def _check_type(self, data: Any, expected: str) -> bool:
        """检查类型"""
        if data is None:
            return expected == 'null'
        type_map = {
            'string': str,
            'integer': int,
            'number': (int, float),
            'boolean': bool,
            'array': list,
            'object': dict,
        }
        expected_type = type_map.get(expected)
        if expected_type is None:
            return True
        return isinstance(data, expected_type)

    def _check_format(self, value: str, format_type: str) -> bool:
        """检查格式"""
        formats = {
            'email': r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$',
            'uri': r'^https?://',
            'ipv4': r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$',
            'date': r'^\d{4}-\d{2}-\d{2}$',
            'date-time': r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}',
            'uuid': r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
        }
        pattern = formats.get(format_type)
        if pattern:
            return bool(re.match(pattern, value, re.IGNORECASE))
        return True


# 预定义Schema
DEVICE_SCHEMA = {
    'type': 'object',
    'required': ['name', 'protocol'],
    'properties': {
        'name': {'type': 'string', 'minLength': 1, 'maxLength': 100},
        'protocol': {'type': 'string', 'enum': ['modbus_tcp', 'modbus_rtu', 'opcua', 'mqtt']},
        'host': {'type': 'string'},
        'port': {'type': 'integer', 'minimum': 1, 'maximum': 65535},
        'slave_id': {'type': 'integer', 'minimum': 1, 'maximum': 247},
        'description': {'type': 'string', 'maxLength': 500},
    },
    'additionalProperties': False,
}

ALARM_RULE_SCHEMA = {
    'type': 'object',
    'required': ['name', 'device_id', 'register_name', 'condition', 'threshold'],
    'properties': {
        'name': {'type': 'string', 'minLength': 1, 'maxLength': 100},
        'device_id': {'type': 'string'},
        'register_name': {'type': 'string'},
        'condition': {'type': 'string', 'enum': ['>', '<', '>=', '<=', '==', '!=']},
        'threshold': {'type': 'number'},
        'level': {'type': 'string', 'enum': ['info', 'warning', 'error', 'critical']},
        'message': {'type': 'string', 'maxLength': 500},
    },
}
