"""
统一服务响应
提供标准化的API响应格式
"""

from typing import Any, Dict, Optional
from flask import jsonify


class ServiceResponse:
    """
    统一服务响应
    
    所有API都应使用此类返回响应，确保格式一致
    """
    
    def __init__(self, success: bool, data: Any = None, error: str = None, 
                 code: int = 200, message: str = None):
        """
        初始化响应
        
        Args:
            success: 是否成功
            data: 响应数据
            error: 错误信息
            code: HTTP状态码
            message: 提示信息
        """
        self.success = success
        self.data = data
        self.error = error
        self.code = code
        self.message = message
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        result = {'success': self.success}
        
        if self.data is not None:
            result['data'] = self.data
        
        if self.error:
            result['error'] = self.error
        
        if self.message:
            result['message'] = self.message
        
        return result
    
    def to_json(self):
        """转换为Flask JSON响应"""
        return jsonify(self.to_dict()), self.code
    
    @classmethod
    def ok(cls, data: Any = None, message: str = None) -> 'ServiceResponse':
        """
        成功响应
        
        Args:
            data: 响应数据
            message: 提示信息
            
        Returns:
            ServiceResponse实例
        """
        return cls(success=True, data=data, message=message)
    
    @classmethod
    def error(cls, error: str, code: int = 400, data: Any = None) -> 'ServiceResponse':
        """
        错误响应
        
        Args:
            error: 错误信息
            code: HTTP状态码
            data: 附加数据
            
        Returns:
            ServiceResponse实例
        """
        return cls(success=False, error=error, code=code, data=data)
    
    @classmethod
    def not_found(cls, resource: str = "资源") -> 'ServiceResponse':
        """
        未找到响应
        
        Args:
            resource: 资源名称
            
        Returns:
            ServiceResponse实例
        """
        return cls(success=False, error=f"{resource}未找到", code=404)
    
    @classmethod
    def unauthorized(cls, message: str = "未授权") -> 'ServiceResponse':
        """
        未授权响应
        
        Args:
            message: 错误信息
            
        Returns:
            ServiceResponse实例
        """
        return cls(success=False, error=message, code=401)
    
    @classmethod
    def forbidden(cls, message: str = "权限不足") -> 'ServiceResponse':
        """
        禁止访问响应
        
        Args:
            message: 错误信息
            
        Returns:
            ServiceResponse实例
        """
        return cls(success=False, error=message, code=403)
    
    @classmethod
    def module_unavailable(cls, module_name: str) -> 'ServiceResponse':
        """
        模块不可用响应
        
        Args:
            module_name: 模块名称
            
        Returns:
            ServiceResponse实例
        """
        return cls(
            success=False,
            error=f"模块 '{module_name}' 未启用或不可用",
            code=503,
            data={'module': module_name, 'status': 'unavailable'}
        )
    
    @classmethod
    def validation_error(cls, errors: list) -> 'ServiceResponse':
        """
        验证错误响应
        
        Args:
            errors: 错误列表
            
        Returns:
            ServiceResponse实例
        """
        return cls(success=False, error="数据验证失败", code=422, data={'errors': errors})


def success_response(data: Any = None, message: str = None):
    """
    成功响应快捷函数
    
    Args:
        data: 响应数据
        message: 提示信息
        
    Returns:
        Flask JSON响应
    """
    return ServiceResponse.ok(data, message).to_json()


def error_response(error: str, code: int = 400):
    """
    错误响应快捷函数
    
    Args:
        error: 错误信息
        code: HTTP状态码
        
    Returns:
        Flask JSON响应
    """
    return ServiceResponse.error(error, code).to_json()


def module_unavailable_response(module_name: str):
    """
    模块不可用响应快捷函数
    
    Args:
        module_name: 模块名称
        
    Returns:
        Flask JSON响应
    """
    return ServiceResponse.module_unavailable(module_name).to_json()
