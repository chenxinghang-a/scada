"""
健康检查API
提供系统健康状态监控
"""

import logging
from flask import Blueprint, jsonify, current_app

from core.health_checker import HealthChecker
from core.module_registry import ModuleRegistry
from core.service_response import success_response, error_response

logger = logging.getLogger(__name__)

health_bp = Blueprint('api_health', __name__, url_prefix='/api/health')


@health_bp.route('/status', methods=['GET'])
def get_health_status():
    """
    获取系统健康状态
    
    Returns:
        {
            "success": true,
            "data": {
                "global_status": "healthy|degraded|unhealthy",
                "modules": {...},
                "checks": {...}
            }
        }
    """
    try:
        # 获取模块状态
        modules_status = ModuleRegistry.get_status()
        
        # 获取健康检查状态
        health_status = HealthChecker.get_status()
        
        # 计算整体状态
        unhealthy_modules = [
            name for name, info in modules_status.items()
            if info.get('status') in ('error', 'disabled', 'unavailable')
        ]
        
        if unhealthy_modules:
            global_status = 'degraded'
        else:
            global_status = 'healthy'
        
        return success_response({
            'global_status': global_status,
            'modules': modules_status,
            'checks': health_status,
            'unhealthy_modules': unhealthy_modules
        })
    except Exception as e:
        logger.error(f"获取健康状态失败: {e}")
        return error_response(str(e), 500)


@health_bp.route('/modules', methods=['GET'])
def get_modules_status():
    """
    获取所有模块状态
    
    Returns:
        {
            "success": true,
            "data": {
                "module_name": {
                    "status": "initialized|error|disabled|unavailable",
                    "has_instance": true|false,
                    "error": null|"error message"
                }
            }
        }
    """
    try:
        modules_status = ModuleRegistry.get_status()
        return success_response(modules_status)
    except Exception as e:
        logger.error(f"获取模块状态失败: {e}")
        return error_response(str(e), 500)


@health_bp.route('/modules/<module_name>', methods=['GET'])
def get_module_status(module_name):
    """
    获取指定模块状态
    
    Args:
        module_name: 模块名称
        
    Returns:
        {
            "success": true,
            "data": {
                "status": "initialized|error|disabled|unavailable",
                "has_instance": true|false,
                "error": null|"error message"
            }
        }
    """
    try:
        module_status = ModuleRegistry.get_status(module_name)
        
        if module_status.get('status') == 'not_found':
            return error_response(f"模块 '{module_name}' 未注册", 404)
        
        return success_response(module_status)
    except Exception as e:
        logger.error(f"获取模块状态失败: {e}")
        return error_response(str(e), 500)


@health_bp.route('/checks', methods=['GET'])
def get_health_checks():
    """
    获取所有健康检查结果
    
    Returns:
        {
            "success": true,
            "data": {
                "global_status": "healthy|degraded|unhealthy",
                "checks": {...}
            }
        }
    """
    try:
        health_status = HealthChecker.check()
        return success_response(health_status)
    except Exception as e:
        logger.error(f"获取健康检查结果失败: {e}")
        return error_response(str(e), 500)


@health_bp.route('/checks/<check_name>', methods=['GET'])
def run_health_check(check_name):
    """
    运行指定健康检查
    
    Args:
        check_name: 检查名称
        
    Returns:
        {
            "success": true,
            "data": {
                "status": "healthy|degraded|unhealthy",
                "message": "...",
                "details": {...}
            }
        }
    """
    try:
        result = HealthChecker.check(check_name)
        
        if result.get('status') == 'unknown':
            return error_response(f"健康检查 '{check_name}' 未注册", 404)
        
        return success_response(result)
    except Exception as e:
        logger.error(f"运行健康检查失败: {e}")
        return error_response(str(e), 500)


@health_bp.route('/available', methods=['GET'])
def get_available_modules():
    """
    获取所有可用模块
    
    Returns:
        {
            "success": true,
            "data": ["module1", "module2", ...]
        }
    """
    try:
        available = ModuleRegistry.get_available_modules()
        return success_response(available)
    except Exception as e:
        logger.error(f"获取可用模块失败: {e}")
        return error_response(str(e), 500)


@health_bp.route('/unavailable', methods=['GET'])
def get_unavailable_modules():
    """
    获取所有不可用模块
    
    Returns:
        {
            "success": true,
            "data": ["module1", "module2", ...]
        }
    """
    try:
        unavailable = ModuleRegistry.get_unavailable_modules()
        return success_response(unavailable)
    except Exception as e:
        logger.error(f"获取不可用模块失败: {e}")
        return error_response(str(e), 500)
