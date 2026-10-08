"""
输入验证工具模块
提供统一的API参数校验函数
"""

# ============================================================================
# 接线状态：未接线（WIRED = False）
# ============================================================================
# 本模块在生产代码（run.py / 各业务层 / 其它模块）中**没有任何 import 引用**。
# 模块本身可用，但当前没有调用方 —— 它宣称的能力**当前并未生效**。
#
# 判定方式（可复现）：以 run.py / launcher.py / 各**独立入口模块**为根，
# 沿 import 图做可达性分析；本模块不在可达集内。
# 判据与守卫见 `tests/test_wiring_declaration.py`。
#
# 为什么保留而不删除：删掉即丢能力，且本模块可能仍有测试价值；
# 这里只把「没接线」**显式化、可追踪**，避免「代码在库里」被误读成「功能在跑」。
# 如果要真正启用它，请接线后**删掉本声明块**（守卫会据此发现状态变化）。
# ============================================================================
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
