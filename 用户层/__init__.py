"""
用户层模块
提供用户认证、权限管理功能
"""

from typing import Any
from .auth import AuthManager, jwt_required, role_required

__all__ = ['AuthManager', 'jwt_required', 'role_required']
