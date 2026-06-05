"""
深度数据验证器
支持嵌套对象、数组、自定义规则的复杂数据校验。

使用方式:
    from core.deep_validator import DeepValidator, Rule
    validator = DeepValidator({
        'name': [Rule.required(), Rule.string(), Rule.max_length(100)],
        'address.city': [Rule.required(), Rule.string()],
        'tags': [Rule.array(), Rule.array_items(Rule.string())],
    })
    errors = validator.validate(data)
"""

import re
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class ValidationError:
    """验证错误"""

    def __init__(self, field: str, message: str, code: str = 'invalid'):
        self.field = field
        self.message = message
        self.code = code

    def to_dict(self) -> Dict[str, str]:
        return {
            'field': self.field,
            'message': self.message,
            'code': self.code,
        }


class Rule:
    """验证规则"""

    def __init__(self, validator: Callable, message: str, code: str = 'invalid'):
        self.validator = validator
        self.message = message
        self.code = code

    def validate(self, value: Any, field: str) -> Optional[ValidationError]:
        try:
            if not self.validator(value):
                return ValidationError(field, self.message, self.code)
        except Exception as e:
            return ValidationError(field, str(e), 'error')
        return None

    @staticmethod
    def required(message: str = '此字段为必填项') -> 'Rule':
        return Rule(lambda v: v is not None and v != '', message, 'required')

    @staticmethod
    def string(message: str = '必须为字符串') -> 'Rule':
        return Rule(lambda v: v is None or isinstance(v, str), message, 'type_error')

    @staticmethod
    def integer(message: str = '必须为整数') -> 'Rule':
        return Rule(lambda v: v is None or isinstance(v, int), message, 'type_error')

    @staticmethod
    def number(message: str = '必须为数字') -> 'Rule':
        return Rule(lambda v: v is None or isinstance(v, (int, float)), message, 'type_error')

    @staticmethod
    def boolean(message: str = '必须为布尔值') -> 'Rule':
        return Rule(lambda v: v is None or isinstance(v, bool), message, 'type_error')

    @staticmethod
    def array(message: str = '必须为数组') -> 'Rule':
        return Rule(lambda v: v is None or isinstance(v, list), message, 'type_error')

    @staticmethod
    def object(message: str = '必须为对象') -> 'Rule':
        return Rule(lambda v: v is None or isinstance(v, dict), message, 'type_error')

    @staticmethod
    def min_length(min_val: int, message: str = None) -> 'Rule':
        msg = message or f'长度不能少于{min_val}个字符'
        return Rule(lambda v: v is None or len(str(v)) >= min_val, msg, 'min_length')

    @staticmethod
    def max_length(max_val: int, message: str = None) -> 'Rule':
        msg = message or f'长度不能超过{max_val}个字符'
        return Rule(lambda v: v is None or len(str(v)) <= max_val, msg, 'max_length')

    @staticmethod
    def min_value(min_val: float, message: str = None) -> 'Rule':
        msg = message or f'值不能小于{min_val}'
        return Rule(lambda v: v is None or v >= min_val, msg, 'min_value')

    @staticmethod
    def max_value(max_val: float, message: str = None) -> 'Rule':
        msg = message or f'值不能大于{max_val}'
        return Rule(lambda v: v is None or v <= max_val, msg, 'max_value')

    @staticmethod
    def pattern(regex: str, message: str = '格式不正确') -> 'Rule':
        compiled = re.compile(regex)
        return Rule(lambda v: v is None or compiled.match(str(v)), message, 'pattern')

    @staticmethod
    def email(message: str = '邮箱格式不正确') -> 'Rule':
        pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return Rule.pattern(pattern, message)

    @staticmethod
    def url(message: str = 'URL格式不正确') -> 'Rule':
        pattern = r'^https?://[^\s/$.?#].[^\s]*$'
        return Rule.pattern(pattern, message)

    @staticmethod
    def one_of(values: list, message: str = None) -> 'Rule':
        msg = message or f'必须是以下值之一: {", ".join(map(str, values))}'
        return Rule(lambda v: v is None or v in values, msg, 'one_of')

    @staticmethod
    def custom(validator: Callable, message: str = '验证失败') -> 'Rule':
        return Rule(validator, message, 'custom')

    @staticmethod
    def array_items(item_rule: 'Rule', message: str = '数组元素验证失败') -> 'Rule':
        """验证数组中的每个元素"""

        def validate_items(v):
            if v is None:
                return True
            if not isinstance(v, list):
                return False
            return all(item_rule.validator(item) for item in v)

        return Rule(validate_items, message, 'array_items')


class DeepValidator:
    """深度数据验证器"""

    def __init__(self, schema: Dict[str, List[Rule]]):
        """
        Args:
            schema: 验证规则，支持点号分隔的嵌套路径
                    {'name': [Rule.required()], 'address.city': [Rule.required()]}
        """
        self.schema = schema

    def validate(self, data: Dict[str, Any]) -> List[Dict[str, str]]:
        """
        验证数据

        Returns:
            错误列表，空列表表示验证通过
        """
        errors = []

        for field_path, rules in self.schema.items():
            value = self._get_nested_value(data, field_path)

            for rule in rules:
                error = rule.validate(value, field_path)
                if error:
                    errors.append(error.to_dict())
                    break  # 同一字段只报第一个错误

        return errors

    def _get_nested_value(self, data: Any, path: str) -> Any:
        """获取嵌套值"""
        parts = path.split('.')
        current = data

        for part in parts:
            if current is None:
                return None
            if isinstance(current, dict):
                current = current.get(part)
            elif isinstance(current, list):
                try:
                    current = current[int(part)]
                except (ValueError, IndexError):
                    return None
            else:
                return None

        return current

    def validate_strict(self, data: Dict[str, Any]) -> List[Dict[str, str]]:
        """严格验证（检查未知字段）"""
        errors = self.validate(data)

        # 检查是否有未知字段
        known_fields = set()
        for field_path in self.schema:
            known_fields.add(field_path.split('.')[0])

        if isinstance(data, dict):
            for key in data:
                if key not in known_fields:
                    errors.append({
                        'field': key,
                        'message': f'未知字段: {key}',
                        'code': 'unknown_field',
                    })

        return errors


def validate_device_config(config: Dict[str, Any]) -> List[Dict[str, str]]:
    """验证设备配置"""
    validator = DeepValidator({
        'name': [Rule.required(), Rule.string(), Rule.max_length(100)],
        'protocol': [Rule.required(), Rule.one_of(['modbus_tcp', 'modbus_rtu', 'opcua', 'mqtt'])],
        'host': [Rule.string()],
        'port': [Rule.integer(), Rule.min_value(1), Rule.max_value(65535)],
        'slave_id': [Rule.integer(), Rule.min_value(1), Rule.max_value(247)],
        'registers': [Rule.array()],
        'registers.*.name': [Rule.required(), Rule.string()],
        'registers.*.address': [Rule.required(), Rule.integer(), Rule.min_value(0)],
        'registers.*.type': [Rule.required(), Rule.one_of(['bool', 'int16', 'uint16', 'int32', 'uint32', 'float32', 'float64'])],
    })
    return validator.validate(config)


def validate_alarm_rule(rule: Dict[str, Any]) -> List[Dict[str, str]]:
    """验证报警规则"""
    validator = DeepValidator({
        'name': [Rule.required(), Rule.string(), Rule.max_length(100)],
        'device_id': [Rule.required(), Rule.string()],
        'register_name': [Rule.required(), Rule.string()],
        'condition': [Rule.required(), Rule.one_of(['>', '<', '>=', '<=', '==', '!='])],
        'threshold': [Rule.required(), Rule.number()],
        'level': [Rule.required(), Rule.one_of(['info', 'warning', 'error', 'critical'])],
    })
    return validator.validate(rule)
