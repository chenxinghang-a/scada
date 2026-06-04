"""
健康检查API
提供系统健康状态监控
"""

import logging
from flask import Blueprint, jsonify, current_app

from core.health_checker import HealthChecker
from core.module_registry import ModuleRegistry
from core.service_response import success_response, error_response
from 用户层.auth import jwt_required

logger = logging.getLogger(__name__)

health_bp = Blueprint('api_health', __name__, url_prefix='/api/health')


@health_bp.route('/status', methods=['GET'])
def get_health_status():
    """
    获取系统健康状态（无需认证，供负载均衡器探活）

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
        logger.error(f"获取健康状态失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@health_bp.route('/status/detail', methods=['GET'])
@jwt_required
def get_health_detail():
    """
    获取详细组件健康状态（数据库/WebSocket/采集器等）

    Returns:
        {
            "success": true,
            "data": {
                "database": {"status": "ok", "latency_ms": 1.2, "connections": 3},
                "websocket": {"status": "ok", "connected_clients": 5},
                "collector": {"status": "ok", "active_tasks": 10, "queue_size": 0},
                "alarm": {"status": "ok", "active_alarms": 2},
                "uptime_seconds": 3600,
                "version": "3.1.0"
            }
        }
    """
    try:
        result = {}

        # 数据库状态
        try:
            db = current_app.database
            import time
            start = time.time()
            with db.get_connection(readonly=True) as conn:
                conn.execute("SELECT 1")
            latency = round((time.time() - start) * 1000, 2)
            result['database'] = {'status': 'ok', 'latency_ms': latency}
        except Exception as e:
            result['database'] = {'status': 'error', 'error': str(e)}

        # WebSocket状态
        try:
            from 展示层.websocket import get_connected_count
            result['websocket'] = {'status': 'ok', 'connected_clients': get_connected_count()}
        except Exception:
            result['websocket'] = {'status': 'unknown'}

        # 采集器状态
        try:
            dc = current_app.data_collector
            result['collector'] = {
                'status': 'ok' if dc.running else 'stopped',
                'active_tasks': len(getattr(dc, 'tasks', {})),
            }
        except Exception:
            result['collector'] = {'status': 'unknown'}

        # 版本和运行时间
        result['version'] = '3.1.0'

        return success_response(result)
    except Exception as e:
        logger.error(f"获取详细健康状态失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@health_bp.route('/modules', methods=['GET'])
@jwt_required
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
        logger.error(f"获取模块状态失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@health_bp.route('/modules/<module_name>', methods=['GET'])
@jwt_required
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
        logger.error(f"获取模块状态失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@health_bp.route('/checks', methods=['GET'])
@jwt_required
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
        logger.error(f"获取健康检查结果失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@health_bp.route('/checks/<check_name>', methods=['GET'])
@jwt_required
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
        logger.error(f"运行健康检查失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@health_bp.route('/available', methods=['GET'])
@jwt_required
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
        logger.error(f"获取可用模块失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@health_bp.route('/unavailable', methods=['GET'])
@jwt_required
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
        logger.error(f"获取不可用模块失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)
