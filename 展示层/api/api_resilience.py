"""
系统韧性API - 熔断器、动态限流、降级策略
提供系统韧性组件的状态查询和管理接口
"""

import logging
from flask import Blueprint, request, jsonify, current_app

from core.circuit_breaker import circuit_breaker_manager, CircuitBreakerError
from core.dynamic_rate_limiter import dynamic_rate_limiter, LoadLevel
from core.degradation_manager import degradation_manager, DegradationLevel
from core.fault_injection import fault_injector, FaultType, FaultSeverity
from core.chaos_engineering import chaos_engine
from core.service_response import success_response, error_response
from 用户层.auth import jwt_required, role_required

logger = logging.getLogger(__name__)

resilience_bp = Blueprint('api_resilience', __name__, url_prefix='/api/resilience')


# ================================================================
# 熔断器 API
# ================================================================

@resilience_bp.route('/circuit-breaker', methods=['GET'])
@jwt_required
def get_all_circuit_breakers():
    """获取所有熔断器状态

    round 251：原实现读 `circuit_breaker_manager.get_all_stats()` ——
    而 `core/circuit_breaker.py` 的通用熔断器**生产环境从未创建过实例**，
    这个接口于是永远回 `{'breakers': {}, 'total': 0}`。运维在看板上看到
    「0 个熔断器、一切正常」，而某台设备可能早已熔断打开。

    采集链真正的熔断保护是 `DataCollector` 里的内联实现
    （`_circuit_state` / `_failure_counts`，按设备、带冷却与半开试探）。
    现在以它为真源；通用管理器的数据作为 `generic` 字段附带返回
    （当前为空，保留是为了让"两套实现"这件事在接口层可见）。
    """
    try:
        generic = circuit_breaker_manager.get_all_stats()
    except Exception as e:      # noqa: BLE001
        logger.warning("读取通用熔断器管理器失败: %s", e)
        generic = {}

    devices: dict = {}
    source = 'generic_manager'
    degraded_reason = None
    try:
        dc = current_app.data_collector
        real = dc.get_circuit_breaker_states()
        # 形状校验：探测代码不能假设对方返回什么。测试夹具里的 MagicMock
        # 采集器、或者将来返回值不符预期的实现，都会让这里炸 ——
        # 而一个韧性接口自己 500 是最难看的失败。
        if not isinstance(real, dict):
            raise TypeError(
                f'get_circuit_breaker_states 返回 {type(real).__name__}，不是 dict')
        if not isinstance(real.get('devices'), dict):
            raise TypeError('熔断状态缺少 devices 字典')
        devices = real['devices']
        source = 'data_collector'
    except Exception as e:      # noqa: BLE001 —— 探测失败不该 500
        degraded_reason = str(e)
        logger.warning("读取采集器熔断状态失败: %s", e)

    payload = {
        'breakers': devices,
        'total': len(devices),
        'source': source,
        'generic': generic,
        'note': (
            'breakers 来自采集链的内联熔断器（真实生效的保护）；'
            'generic 是 core/circuit_breaker.py 的通用管理器，'
            '生产环境未使用，恒为空。'
        ),
    }
    if degraded_reason is not None:
        payload['degraded'] = {'status': 'unknown', 'reason': degraded_reason}
    return success_response(payload)


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
@role_required('admin')
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
@role_required('admin')
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
@role_required('admin')
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
@role_required('admin')
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
@role_required('admin')
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


# ================================================================
# 故障注入 API
# ================================================================

@resilience_bp.route('/fault-injection', methods=['GET'])
@jwt_required
def get_fault_injection_status():
    """获取故障注入器状态"""
    try:
        status = fault_injector.get_status()
        return success_response(status)
    except Exception as e:
        logger.error(f"获取故障注入状态失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@resilience_bp.route('/fault-injection/inject', methods=['POST'])
@jwt_required
@role_required('admin')
def inject_fault():
    """注入故障"""
    try:
        data = request.get_json()
        if not data:
            return error_response("请求体不能为空", 400)

        target = data.get('target')
        fault_type_name = data.get('fault_type')
        if not target or not fault_type_name:
            return error_response("target 和 fault_type 必填", 400)

        try:
            fault_type = FaultType(fault_type_name)
        except ValueError:
            valid = [t.value for t in FaultType]
            return error_response(f"无效故障类型: {fault_type_name}, 可选: {valid}", 400)

        severity_name = data.get('severity', 'medium')
        try:
            severity = FaultSeverity(severity_name)
        except ValueError:
            severity = FaultSeverity.MEDIUM

        duration = data.get('duration', 60)
        params = {k: v for k, v in data.items() if k not in ('target', 'fault_type', 'severity', 'duration')}

        injection = fault_injector.inject(target, fault_type, severity, duration, **params)
        if injection:
            return success_response({
                'message': f'故障已注入: {target}/{fault_type.value}',
                'injection': injection.to_dict(),
            })
        else:
            return error_response("故障注入已全局禁用", 403)
    except Exception as e:
        logger.error(f"注入故障失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@resilience_bp.route('/fault-injection/<target>', methods=['DELETE'])
@jwt_required
@role_required('admin')
def remove_fault(target):
    """移除故障注入"""
    try:
        removed = fault_injector.remove(target)
        if removed:
            return success_response({'message': f'故障注入已移除: {target}'})
        return error_response(f'未找到 {target} 的故障注入', 404)
    except Exception as e:
        logger.error(f"移除故障注入失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@resilience_bp.route('/fault-injection/clear', methods=['POST'])
@jwt_required
@role_required('admin')
def clear_all_faults():
    """清除所有故障注入"""
    try:
        fault_injector.clear_all()
        return success_response({'message': '所有故障注入已清除'})
    except Exception as e:
        logger.error(f"清除故障注入失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


# ================================================================
# 混沌工程 API
# ================================================================

@resilience_bp.route('/chaos', methods=['GET'])
@jwt_required
def get_chaos_status():
    """获取混沌工程引擎状态"""
    try:
        status = chaos_engine.get_status()
        return success_response(status)
    except Exception as e:
        logger.error(f"获取混沌工程状态失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@resilience_bp.route('/chaos/experiment', methods=['POST'])
@jwt_required
@role_required('admin')
def create_experiment():
    """创建混沌实验"""
    try:
        data = request.get_json()
        if not data:
            return error_response("请求体不能为空", 400)

        name = data.get('name')
        if not name:
            return error_response("name 必填", 400)

        experiment = chaos_engine.create_experiment(
            name=name,
            description=data.get('description', ''),
            steady_state=data.get('steady_state', ''),
            steps=data.get('steps', []),
            rollback_on_failure=data.get('rollback_on_failure', True),
            timeout=data.get('timeout', 300),
            tags=data.get('tags', []),
        )
        return success_response(experiment.to_dict())
    except Exception as e:
        logger.error(f"创建混沌实验失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@resilience_bp.route('/chaos/experiment/<name>/run', methods=['POST'])
@jwt_required
@role_required('admin')
def run_experiment(name):
    """运行混沌实验"""
    try:
        result = chaos_engine.run_experiment(name)
        if 'error' in result and result.get('state') is None:
            return error_response(result['error'], 404)
        return success_response(result)
    except Exception as e:
        logger.error(f"运行混沌实验失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)


@resilience_bp.route('/chaos/experiment/<name>/report', methods=['GET'])
@jwt_required
def get_experiment_report(name):
    """获取实验报告"""
    try:
        report = chaos_engine.generate_report(name)
        return success_response({'report': report})
    except Exception as e:
        logger.error(f"获取实验报告失败: {e}", exc_info=True)
        return error_response("服务器内部错误", 500)
