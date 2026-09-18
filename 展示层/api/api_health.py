"""
健康检查API
提供系统健康状态监控
"""

import logging
from flask import Blueprint, jsonify, current_app

from core.health_checker import HealthChecker, HealthStatus
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

        # 计算整体状态。
        #
        # 注意：此前只统计 ModuleRegistry（模块注册表的 error / disabled /
        # unavailable），而 **健康检查的结果完全不参与** —— 磁盘写满、内存打爆、
        # 数据库探测失败时各 check 已经是 unhealthy，顶层 `global_status`
        # 却照样回 `healthy`。
        #
        # 而本接口的注释写明「供负载均衡器探活」，顶层字段就是**结论本身**：
        # 探活方只看这个字段，不会去翻 checks 明细。所以「检查项挂了但结论健康」
        # 等于这套健康检查对外不存在。
        #
        # 现在两条线合并：模块注册表 OR 健康检查，取更严重的那个。
        unhealthy_modules = [
            name for name, info in modules_status.items()
            if info.get('status') in ('error', 'disabled', 'unavailable')
        ]

        checks = health_status.get('checks', {}) if isinstance(health_status, dict) else {}
        unhealthy_checks = [
            name for name, info in checks.items()
            if isinstance(info, dict) and info.get('status') == HealthStatus.UNHEALTHY
        ]
        degraded_checks = [
            name for name, info in checks.items()
            if isinstance(info, dict) and info.get('status') == HealthStatus.DEGRADED
        ]
        checks_global = health_status.get('global_status') if isinstance(health_status, dict) else None

        if (unhealthy_modules or unhealthy_checks
                or checks_global == HealthStatus.UNHEALTHY):
            global_status = 'unhealthy'
        elif degraded_checks or checks_global == HealthStatus.DEGRADED:
            global_status = 'degraded'
        else:
            global_status = 'healthy'

        return success_response({
            'global_status': global_status,
            'modules': modules_status,
            'checks': health_status,
            'unhealthy_modules': unhealthy_modules,
            'unhealthy_checks': unhealthy_checks
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
                "database": {"status": "ok", "latency_ms": 1.2},
                "websocket": {"status": "ok", "connected_clients": 5},
                "collector": {"status": "ok", "active_tasks": 10},
                "version": "1.3.1022",
                "api_cache": {...},
                "queues": {"report": {...}, "export": {...}},
                "circuit_breakers": {...}
            }
        }

    注意：探测失败的组件降级为 `{"status": "unknown"|"unavailable", "reason": "..."}`
    而不是抛异常 —— 单个组件探不到不该让整个诊断接口 500。但 `reason` 一定带上，
    否则运维看到 unknown 却无从判断是组件坏了还是探测代码本身出错。
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

        # 下面 6 个组件探测都是「探测失败就降级为 unknown/unavailable，不抛异常」。
        # 这个降级本身是对的（一个组件探不到不该让整个诊断接口 500），
        # 但**必须留下原因**：本接口存在的意义就是告诉运维「哪个组件坏了、为什么」，
        # 静默吞异常会让「组件真的坏了」和「组件正常但探测代码本身出错」
        # 长得一模一样，运维只能看到 unknown 却无从下手。
        #
        # 真实踩过：`get_connected_count` 曾因函数改名而不存在，
        # ImportError 被吞 → websocket 状态永远是 unknown，没人发现是代码坏了。
        # 所以这里统一：**日志记完整原因（含栈），响应带简短 reason**。

        # WebSocket状态
        try:
            from 展示层.websocket import get_connected_count
            result['websocket'] = {'status': 'ok', 'connected_clients': get_connected_count()}
        except Exception as e:
            logger.warning("健康详情：WebSocket 状态探测失败: %s", e, exc_info=True)
            result['websocket'] = {'status': 'unknown', 'reason': str(e)}

        # 采集器状态
        try:
            dc = current_app.data_collector
            result['collector'] = {
                'status': 'ok' if dc.running else 'stopped',
                'active_tasks': len(getattr(dc, 'tasks', {})),
            }
        except Exception as e:
            logger.warning("健康详情：采集器状态探测失败: %s", e, exc_info=True)
            result['collector'] = {'status': 'unknown', 'reason': str(e)}

        # 版本和运行时间（版本号来自 VERSION 文件，见 config.APP_VERSION）
        try:
            from config import APP_VERSION
            result['version'] = APP_VERSION
        except Exception as e:
            logger.warning("健康详情：读取版本号失败: %s", e, exc_info=True)
            result['version'] = 'unknown'

        # API缓存状态
        try:
            from core.api_cache import get_cache_stats
            result['api_cache'] = get_cache_stats()
        except Exception as e:
            logger.warning("健康详情：API 缓存状态探测失败: %s", e, exc_info=True)
            result['api_cache'] = {'status': 'unavailable', 'reason': str(e)}

        # 请求队列状态
        try:
            from core.request_queue import report_queue, export_queue
            result['queues'] = {
                'report': report_queue.get_stats(),
                'export': export_queue.get_stats(),
            }
        except Exception as e:
            logger.warning("健康详情：请求队列状态探测失败: %s", e, exc_info=True)
            result['queues'] = {'status': 'unavailable', 'reason': str(e)}

        # 熔断器状态
        try:
            from core.circuit_breaker import circuit_breaker_manager
            result['circuit_breakers'] = circuit_breaker_manager.get_all_stats()
        except Exception as e:
            logger.warning("健康详情：熔断器状态探测失败: %s", e, exc_info=True)
            result['circuit_breakers'] = {'status': 'unavailable', 'reason': str(e)}

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


@health_bp.route('/tasks/<task_id>', methods=['GET'])
@jwt_required
def get_task_status(task_id):
    """
    查询异步任务状态

    Args:
        task_id: 任务ID

    Returns:
        {
            "success": true,
            "data": {
                "id": "abc123",
                "status": "pending|running|completed|failed",
                "created_at": 1234567890.0,
                "result": {...}
            }
        }
    """
    try:
        from core.request_queue import report_queue, export_queue

        # 在所有队列中查找任务
        for queue in [report_queue, export_queue]:
            status = queue.get_status(task_id)
            if status:
                return success_response(status)

        return error_response(f"任务 '{task_id}' 不存在", 404)
    except Exception as e:
        logger.error(f"查询任务状态失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)
