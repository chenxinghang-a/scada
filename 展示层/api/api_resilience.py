"""
系统韧性API - 熔断器、动态限流、降级策略
提供系统韧性组件的状态查询和管理接口
"""

import logging
from flask import Blueprint, request, jsonify

from core.circuit_breaker import circuit_breaker_manager, CircuitBreakerError
from core.dynamic_rate_limiter import dynamic_rate_limiter, LoadLevel
from core.degradation_manager import degradation_manager, DegradationLevel
from core.service_response import success_response, error_response
from 用户层.auth import jwt_required

logger = logging.getLogger(__name__)

resilience_bp = Blueprint('api_resilience', __name__, url_prefix='/api/resilience')


# ================================================================
# 熔断器 API
# ================================================================

@resilience_bp.route('/circuit-breaker', methods=['GET'])
@jwt_required
def get_all_circuit_breakers():
    """获取所有熔断器状态"""
    try:
        stats = circuit_breaker_manager.get_all_stats()
        return success_response({
            'breakers': stats,
            'total': len(stats),
        })
    except Exception as e:
        logger.error(f"获取熔断器状态失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@resilience_bp.route('/circuit-breaker/<name>', methods=['GET'])
@jwt_required
def get_circuit_breaker(name):
    """获取指定熔断器状态"""
    try:
        breaker = circuit_breaker_manager.get(name)
        if not breaker:
            return error_response(f"熔断器 {name} 不存在", 404)
        return success_response(breaker.get_stats())
    except Exception as e:
        logger.error(f"获取熔断器 {name} 失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@resilience_bp.route('/circuit-breaker/<name>/reset', methods=['POST'])
@jwt_required
def reset_circuit_breaker(name):
    """重置指定熔断器"""
    try:
        breaker = circuit_breaker_manager.get(name)
        if not breaker:
            return error_response(f"熔断器 {name} 不存在", 404)
        breaker.reset()
        logger.info(f"熔断器 {name} 已手动重置")
        return success_response({'message': f'熔断器 {name} 已重置'})
    except Exception as e:
        logger.error(f"重置熔断器 {name} 失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@resilience_bp.route('/circuit-breaker/reset-all', methods=['POST'])
@jwt_required
def reset_all_circuit_breakers():
    """重置所有熔断器"""
    try:
        circuit_breaker_manager.reset_all()
        logger.info("所有熔断器已手动重置")
        return success_response({'message': '所有熔断器已重置'})
    except Exception as e:
        logger.error(f"重置所有熔断器失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


# ================================================================
# 动态限流器 API
# ================================================================

@resilience_bp.route('/rate-limiter', methods=['GET'])
@jwt_required
def get_rate_limiter_status():
    """获取动态限流器状态"""
    try:
        status = dynamic_rate_limiter.get_status()
        return success_response(status)
    except Exception as e:
        logger.error(f"获取限流器状态失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@resilience_bp.route('/rate-limiter/profiles', methods=['GET'])
@jwt_required
def get_rate_limiter_profiles():
    """获取所有限流配置文件"""
    try:
        profiles = dynamic_rate_limiter.get_all_profiles()
        return success_response(profiles)
    except Exception as e:
        logger.error(f"获取限流配置失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@resilience_bp.route('/rate-limiter/profile', methods=['PUT'])
@jwt_required
def update_rate_limiter_profile():
    """覆盖某等级的限流参数"""
    try:
        data = request.get_json()
        if not data:
            return error_response("请求体不能为空", 400)

        level = data.get('level')
        rpm = data.get('rpm')
        rps = data.get('rps')

        valid_levels = [LoadLevel.LOW, LoadLevel.MEDIUM, LoadLevel.HIGH, LoadLevel.CRITICAL]
        if level not in valid_levels:
            return error_response(f"无效等级: {level}, 可选: {valid_levels}", 400)
        if not isinstance(rpm, int) or rpm < 1:
            return error_response("rpm 必须为正整数", 400)
        if not isinstance(rps, int) or rps < 1:
            return error_response("rps 必须为正整数", 400)

        dynamic_rate_limiter.override_profile(level, rpm, rps)
        return success_response({
            'message': f'限流配置已更新: {level} → rpm={rpm}, rps={rps}',
            'profile': dynamic_rate_limiter.get_current_profile(),
        })
    except Exception as e:
        logger.error(f"更新限流配置失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


# ================================================================
# 降级策略 API
# ================================================================

@resilience_bp.route('/degradation', methods=['GET'])
@jwt_required
def get_degradation_status():
    """获取降级管理器状态"""
    try:
        status = degradation_manager.get_status()
        return success_response(status)
    except Exception as e:
        logger.error(f"获取降级状态失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@resilience_bp.route('/degradation/levels', methods=['GET'])
@jwt_required
def get_degradation_levels():
    """获取所有降级等级信息"""
    try:
        levels = degradation_manager.get_all_levels_info()
        return success_response(levels)
    except Exception as e:
        logger.error(f"获取降级等级信息失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@resilience_bp.route('/degradation/degrade', methods=['POST'])
@jwt_required
def manual_degrade():
    """手动降级"""
    try:
        data = request.get_json()
        if not data:
            return error_response("请求体不能为空", 400)

        level = data.get('level')
        reason = data.get('reason', '手动操作')

        valid_levels = [l.value for l in DegradationLevel]
        if level not in valid_levels:
            return error_response(f"无效等级: {level}, 可选: {valid_levels}", 400)

        degradation_manager.manual_degrade(level, reason)
        return success_response({
            'message': f'已降级至 {DegradationLevel(level).name}',
            'level': level,
            'reason': reason,
        })
    except Exception as e:
        logger.error(f"手动降级失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@resilience_bp.route('/degradation/restore', methods=['POST'])
@jwt_required
def manual_restore():
    """恢复自动管理"""
    try:
        degradation_manager.manual_restore()
        return success_response({'message': '已恢复自动降级管理'})
    except Exception as e:
        logger.error(f"恢复降级管理失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@resilience_bp.route('/degradation/feature/<feature_name>', methods=['GET'])
@jwt_required
def check_feature_enabled(feature_name):
    """检查某功能是否可用"""
    try:
        enabled = degradation_manager.is_feature_enabled(feature_name)
        return success_response({
            'feature': feature_name,
            'enabled': enabled,
            'level': degradation_manager.level_name,
        })
    except Exception as e:
        logger.error(f"检查功能可用性失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)
