"""
输入验证工具模块
提供统一的API参数校验函数
"""
import re
from typing import Any, Optional, List
from flask import request


def validate_required(data: dict, fields: List[str]) -> Optional[str]:
    """验证必填字段，返回错误消息或None"""
    missing = [f for f in fields if f not in data or data[f] is None or data[f] == '']
    if missing:
        return f"缺少必填字段: {', '.join(missing)}"
    return None


def validate_string(value: Any, field_name: str, min_len: int = 1, max_len: int = 255, pattern: str = None) -> Optional[str]:
    """验证字符串字段"""
    if not isinstance(value, str):
        return f"{field_name} 必须是字符串"
    if len(value) < min_len:
        return f"{field_name} 长度不能少于 {min_len} 个字符"
    if len(value) > max_len:
        return f"{field_name} 长度不能超过 {max_len} 个字符"
    if pattern and not re.match(pattern, value):
        return f"{field_name} 格式不正确"
    return None


def validate_int(value: Any, field_name: str, min_val: int = None, max_val: int = None) -> Optional[str]:
    """验证整数字段"""
    try:
        val = int(value)
    except (ValueError, TypeError):
        return f"{field_name} 必须是整数"
    if min_val is not None and val < min_val:
        return f"{field_name} 不能小于 {min_val}"
    if max_val is not None and val > max_val:
        return f"{field_name} 不能大于 {max_val}"
    return None


def validate_float(value: Any, field_name: str, min_val: float = None, max_val: float = None) -> Optional[str]:
    """验证浮点数字段"""
    try:
        val = float(value)
    except (ValueError, TypeError):
        return f"{field_name} 必须是数字"
    if min_val is not None and val < min_val:
        return f"{field_name} 不能小于 {min_val}"
    if max_val is not None and val > max_val:
        return f"{field_name} 不能大于 {max_val}"
    return None


def validate_device_id(device_id: str) -> Optional[str]:
    """验证设备ID格式"""
    if not device_id:
        return "设备ID不能为空"
    if len(device_id) > 100:
        return "设备ID长度不能超过100"
    if not re.match(r'^[a-zA-Z0-9_-]+$', device_id):
        return "设备ID只能包含字母、数字、下划线和连字符"
    return None


def validate_ip_address(ip: str) -> Optional[str]:
    """验证IP地址格式"""
    if not ip:
        return "IP地址不能为空"
    pattern = r'^(\d{1,3}\.){3}\d{1,3}$'
    if not re.match(pattern, ip):
        return "IP地址格式不正确"
    parts = ip.split('.')
    for part in parts:
        if int(part) > 255:
            return "IP地址每段不能超过255"
    return None


def validate_port(port: Any) -> Optional[str]:
    """验证端口号"""
    return validate_int(port, "端口", min_val=1, max_val=65535)
