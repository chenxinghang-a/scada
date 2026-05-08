"""
设备控制API
寄存器写入/线圈写入/紧急停机/安全联锁/操作审计
"""

import logging
from flask import Blueprint, jsonify, request, current_app

from 用户层.auth import jwt_required, role_required
from ._common import get_auth_manager
from core.service_response import module_unavailable_response

logger = logging.getLogger(__name__)

control_bp = Blueprint('api_control', __name__, url_prefix='/api')

_require_auth = jwt_required
_require_engineer = role_required('admin', 'engineer')


# ==================== 设备控制API ====================

@control_bp.route('/devices/<device_id>/write-register', methods=['POST'])
@_require_engineer
def write_register(device_id):
    """写入寄存器（带安全校验）"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '请提供写入参数'}), 400

    address = data.get('address')
    value = data.get('value')
    if address is None or value is None:
        return jsonify({'error': '缺少address或value参数'}), 400

    operator = request.current_user['username']

    # 工厂级安全校验
    device_control = getattr(current_app, 'device_control', None)
    if device_control:
        result = device_control.write_with_verification(
            device_id, int(address), int(value), operator)
        if not result['success']:
            return jsonify(result), 403
        return jsonify(result)

    # 降级：无安全模块时直接写入
    client = current_app.device_manager.get_client(device_id)
    if not client:
        return jsonify({'error': f'设备 {device_id} 不存在'}), 404
    if not client.connected:
        return jsonify({'error': f'设备 {device_id} 未连接'}), 400

    success = client.write_single_register(int(address), int(value))
    if success:
        get_auth_manager().log_operation(
            operator, 'write_register',
            f'设备 {device_id} 写入寄存器 address={address} value={value}')
        return jsonify({'success': True, 'message': f'写入成功: 地址={address}, 值={value}'})
    return jsonify({'success': False, 'message': '写入失败，请检查设备连接和寄存器地址'}), 400


@control_bp.route('/devices/<device_id>/write-coil', methods=['POST'])
@_require_engineer
def write_coil(device_id):
    """写入线圈（带安全校验）"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '请提供写入参数'}), 400

    address = data.get('address')
    value = data.get('value')
    if address is None or value is None:
        return jsonify({'error': '缺少address或value参数'}), 400

    operator = request.current_user['username']

    # 工厂级安全校验
    device_control = getattr(current_app, 'device_control', None)
    if device_control:
        result = device_control.write_with_verification(
            device_id, int(address), 1 if value else 0, operator)
        if not result['success']:
            return jsonify(result), 403
        return jsonify(result)

    # 降级：无安全模块时直接写入
    client = current_app.device_manager.get_client(device_id)
    if not client:
        return jsonify({'error': f'设备 {device_id} 不存在'}), 404
    if not client.connected:
        return jsonify({'error': f'设备 {device_id} 未连接'}), 400

    success = client.write_single_coil(int(address), bool(value))
    if success:
        get_auth_manager().log_operation(
            operator, 'write_coil',
            f'设备 {device_id} 写入线圈 address={address} value={value}')
        return jsonify({'success': True, 'message': f'写入成功: 地址={address}, 值={value}'})
    return jsonify({'success': False, 'message': '写入失败，请检查设备连接和线圈地址'}), 400


@control_bp.route('/control/logs', methods=['GET'])
@_require_auth
def get_control_logs():
    """获取控制操作日志（含安全审计）"""
    limit = request.args.get('limit', 50, type=int)
    logs = get_auth_manager().get_operation_logs(limit=limit)
    control_logs = [log for log in logs if log.get('action') in ('write_register', 'write_coil')]

    # 合并安全审计日志
    device_control = getattr(current_app, 'device_control', None)
    if device_control:
        audit_logs = device_control.get_audit_log(limit=limit)
        control_logs.extend(audit_logs)

    # 按时间排序
    control_logs.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    return jsonify({'logs': control_logs[:limit]})


# ==================== 工厂级设备控制安全API ====================

@control_bp.route('/control/estop', methods=['POST'])
@_require_auth
def trigger_estop():
    """紧急停机（最高优先级，任何角色均可触发）"""
    data = request.get_json() or {}
    reason = data.get('reason', '操作员手动触发紧急停机')
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return jsonify({'error': '设备控制安全模块未启用'}), 503
    result = device_control.trigger_emergency_stop(reason)
    get_auth_manager().log_operation(
        request.current_user['username'], 'emergency_stop', f'紧急停机: {reason}')
    return jsonify(result)


@control_bp.route('/control/estop/reset', methods=['POST'])
@_require_engineer
def reset_estop():
    """解除紧急停机（需工程师权限）"""
    operator = request.current_user['username']
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return jsonify({'error': '设备控制安全模块未启用'}), 503
    result = device_control.reset_emergency_stop(operator)
    get_auth_manager().log_operation(operator, 'estop_reset', '解除紧急停机')
    return jsonify(result)


@control_bp.route('/control/estop/status', methods=['GET'])
@_require_auth
def get_estop_status():
    """获取紧急停机状态"""
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return module_unavailable_response('device_control')
    return jsonify(device_control.get_estop_status())


@control_bp.route('/control/interlocks', methods=['GET'])
@_require_auth
def get_interlocks():
    """获取所有安全联锁状态"""
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return module_unavailable_response('device_control')
    return jsonify(device_control.get_interlock_status())


@control_bp.route('/control/interlocks/<rule_id>/bypass', methods=['POST'])
@_require_engineer
def bypass_interlock(rule_id):
    """旁路联锁（维护用，需工程师权限+原因）"""
    data = request.get_json() or {}
    reason = data.get('reason', '维护')
    operator = request.current_user['username']
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return jsonify({'error': '设备控制安全模块未启用'}), 503
    success = device_control.bypass_interlock(rule_id, operator, reason)
    return jsonify({'success': success, 'message': f'联锁 {rule_id} 已旁路' if success else '旁路失败'})


@control_bp.route('/control/interlocks/<rule_id>/restore', methods=['POST'])
@_require_engineer
def restore_interlock(rule_id):
    """恢复联锁"""
    operator = request.current_user['username']
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return jsonify({'error': '设备控制安全模块未启用'}), 503
    success = device_control.restore_interlock(rule_id, operator)
    return jsonify({'success': success, 'message': f'联锁 {rule_id} 已恢复' if success else '恢复失败'})


@control_bp.route('/control/health', methods=['GET'])
@_require_auth
def get_device_health():
    """获取所有设备健康状态"""
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return module_unavailable_response('device_control')
    return jsonify(device_control.get_device_health_summary())


@control_bp.route('/control/audit', methods=['GET'])
@_require_auth
def get_audit_log():
    """获取操作审计日志"""
    limit = request.args.get('limit', 100, type=int)
    action_filter = request.args.get('action')
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return module_unavailable_response('device_control')
    return jsonify({'logs': device_control.get_audit_log(limit, action_filter)})


@control_bp.route('/control/status', methods=['GET'])
@_require_auth
def get_control_safety_status():
    """获取设备控制安全系统完整状态"""
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return module_unavailable_response('device_control')
    return jsonify({'enabled': True, **device_control.get_full_status()})
