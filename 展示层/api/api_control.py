"""
设备控制API
寄存器写入/线圈写入/紧急停机/安全联锁/操作审计
"""

import logging
from typing import Optional
from flask import Blueprint, jsonify, request, current_app

from 用户层.auth import jwt_required, role_required
from ._common import get_auth_manager
from core.service_response import module_unavailable_response

logger = logging.getLogger(__name__)

control_bp = Blueprint('api_control', __name__, url_prefix='/api')

_require_auth = jwt_required
_require_engineer = role_required('admin', 'engineer')


def _safe_int(val, name='value'):
    """安全整数转换"""
    try:
        return int(val)
    except (ValueError, TypeError):
        raise ValueError(f'Invalid {name}: must be integer')


def _safe_float(val, name='value'):
    """安全浮点转换"""
    try:
        return float(val)
    except (ValueError, TypeError):
        raise ValueError(f'Invalid {name}: must be number')


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

    try:
        address = _safe_int(address, 'address')
        value = _safe_int(value, 'value')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    # 工厂级安全校验
    device_control = getattr(current_app, 'device_control', None)
    if device_control:
        result = device_control.write_with_verification(
            device_id, address, value, operator)
        if not result['success']:
            return jsonify(result), 403
        return jsonify(result)

    # 降级：无安全模块时直接写入
    client = current_app.device_manager.get_client(device_id)
    if not client:
        return jsonify({'error': f'设备 {device_id} 不存在'}), 404
    if not client.connected:
        return jsonify({'error': f'设备 {device_id} 未连接'}), 400

    success = client.write_single_register(address, value)
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

    try:
        address = _safe_int(address, 'address')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    # 工厂级安全校验
    device_control = getattr(current_app, 'device_control', None)
    if device_control:
        result = device_control.write_with_verification(
            device_id, address, 1 if value else 0, operator)
        if not result['success']:
            return jsonify(result), 403
        return jsonify(result)

    # 降级：无安全模块时直接写入
    client = current_app.device_manager.get_client(device_id)
    if not client:
        return jsonify({'error': f'设备 {device_id} 不存在'}), 404
    if not client.connected:
        return jsonify({'error': f'设备 {device_id} 未连接'}), 400

    success = client.write_single_coil(address, bool(value))
    if success:
        get_auth_manager().log_operation(
            operator, 'write_coil',
            f'设备 {device_id} 写入线圈 address={address} value={value}')
        return jsonify({'success': True, 'message': f'写入成功: 地址={address}, 值={value}'})
    return jsonify({'success': False, 'message': '写入失败，请检查设备连接和线圈地址'}), 400


@control_bp.route('/devices/<device_id>/adjust', methods=['POST'])
@_require_engineer
def adjust_device(device_id):
    """调节设备参数（写入指定寄存器值，如温度设定值、速度等）"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '请提供调节参数'}), 400

    register_name = data.get('register_name')
    value = data.get('value')
    if register_name is None or value is None:
        return jsonify({'error': '缺少 register_name 或 value 参数'}), 400

    try:
        value = _safe_float(value, 'value')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    operator = request.current_user['username']
    result = current_app.device_manager.adjust_device(device_id, register_name, value)

    if result['success']:
        get_auth_manager().log_operation(
            operator, 'adjust_device',
            f'设备 {device_id} 调节 {register_name} = {value}')

    return jsonify(result)


@control_bp.route('/devices/<device_id>/write-endpoint', methods=['POST'])
@_require_engineer
def write_endpoint(device_id):
    """写入REST端点（支持POST/PUT方法）"""
    data = request.get_json() or {}
    endpoint_name = data.get('endpoint')
    value = data.get('value')
    method = data.get('method', 'PUT').upper()

    if not endpoint_name:
        return jsonify({'error': '请指定端点名称'}), 400

    if method not in ('POST', 'PUT'):
        return jsonify({'error': '不支持的HTTP方法，仅支持POST/PUT'}), 400

    operator = request.current_user['username']

    device_manager = current_app.device_manager
    client = device_manager.get_client(device_id)
    if not client:
        return jsonify({'error': f'设备 {device_id} 不存在'}), 404

    # 查找端点配置
    device_config = device_manager.devices.get(device_id, {})
    endpoints = device_config.get('endpoints', [])
    endpoint_config = next((ep for ep in endpoints if ep.get('name') == endpoint_name), None)

    if not endpoint_config:
        return jsonify({'error': f'端点 {endpoint_name} 不存在'}), 404

    # 执行写入
    success = False
    if method == 'PUT' and hasattr(client, 'put_endpoint'):
        result = client.put_endpoint(endpoint_config, {'value': value})
        success = (result or {}).get('success', False)
    elif method == 'POST' and hasattr(client, 'post_endpoint'):
        result = client.post_endpoint(endpoint_config, {'value': value})
        success = (result or {}).get('success', False)
    elif hasattr(client, 'write_endpoint'):
        success = client.write_endpoint(endpoint_config, value, method=method)

    if success:
        get_auth_manager().log_operation(
            operator, f'write_endpoint_{method.lower()}',
            f'设备 {device_id} {method}端点 {endpoint_name}={value}')
        return jsonify({'success': True, 'message': f'{method}写入成功: {endpoint_name}={value}'})
    return jsonify({'success': False, 'message': f'{method}写入失败，请检查设备连接'}), 400


@control_bp.route('/control/logs', methods=['GET'])
@_require_auth
def get_control_logs():
    """获取控制操作日志（含安全审计）"""
    limit = request.args.get('limit', 50, type=int)
    logs = get_auth_manager().get_operation_logs(limit=limit)
    control_logs = [log for log in logs if log.get('action') in (
        'write_register', 'write_coil', 'stop_device', 'start_device',
        'batch_stop', 'batch_start', 'batch_reset',
        'emergency_stop', 'estop_reset',
    )]

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


@control_bp.route('/control/interlocks/bypass-request', methods=['POST'])
@_require_engineer
def create_bypass_request():
    """创建联锁旁路请求（需多人审批）"""
    data = request.get_json() or {}
    interlock_id = data.get('interlock_id')
    reason = data.get('reason', '')
    timeout_minutes = data.get('timeout_minutes', 30)

    if not interlock_id:
        return jsonify({'error': '缺少 interlock_id 参数'}), 400

    operator = request.current_user['username']
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return jsonify({'error': '设备控制安全模块未启用'}), 503

    request_id = device_control.request_bypass(interlock_id, operator, reason, timeout_minutes)
    return jsonify({
        'success': True,
        'request_id': request_id,
        'message': f'旁路请求已创建，等待2人审批，{timeout_minutes}分钟内有效'
    })


@control_bp.route('/control/interlocks/bypass-approve', methods=['POST'])
@_require_engineer
def approve_bypass_request():
    """审批联锁旁路请求"""
    data = request.get_json() or {}
    request_id = data.get('request_id')

    if not request_id:
        return jsonify({'error': '缺少 request_id 参数'}), 400

    approver = request.current_user['username']
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return jsonify({'error': '设备控制安全模块未启用'}), 503

    success, message = device_control.approve_bypass(request_id, approver)
    return jsonify({'success': success, 'message': message}), (200 if success else 400)


@control_bp.route('/control/interlocks/bypass-reject', methods=['POST'])
@_require_engineer
def reject_bypass_request():
    """拒绝联锁旁路请求"""
    data = request.get_json() or {}
    request_id = data.get('request_id')
    reason = data.get('reason', '')

    if not request_id:
        return jsonify({'error': '缺少 request_id 参数'}), 400

    rejector = request.current_user['username']
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return jsonify({'error': '设备控制安全模块未启用'}), 503

    success, message = device_control.reject_bypass(request_id, rejector, reason)
    return jsonify({'success': success, 'message': message}), (200 if success else 400)


@control_bp.route('/control/interlocks/bypass-pending', methods=['GET'])
@_require_auth
def get_pending_bypasses():
    """获取待审批的旁路请求"""
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return jsonify({'error': '设备控制安全模块未启用'}), 503

    pending = device_control.get_pending_bypasses()
    return jsonify({'pending': pending, 'count': len(pending)})


@control_bp.route('/control/health', methods=['GET'])
@_require_auth
def get_device_health():
    """获取所有设备健康状态"""
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return module_unavailable_response('device_control')
    return jsonify(device_control.get_device_health_summary())


@control_bp.route('/control/batch', methods=['POST'])
@_require_engineer
def batch_control():
    """批量控制设备（启动/停止/复位），支持按设备ID列表过滤"""
    data = request.get_json() or {}
    action = data.get('action')
    device_ids = data.get('device_ids')  # 可选：指定设备ID列表

    if action not in ('start', 'stop', 'reset'):
        return jsonify({'error': '无效的操作类型，支持: start, stop, reset'}), 400

    operator = request.current_user['username']

    device_control = getattr(current_app, 'device_control', None)
    if device_control:
        result = device_control.batch_control(action, operator, device_ids=device_ids)
        target_desc = f'{len(device_ids)}台设备' if device_ids else '所有设备'
        get_auth_manager().log_operation(
            operator, f'batch_{action}', f'批量{action}{target_desc}')
        return jsonify(result)

    # 降级：无安全模块时直接操作设备
    device_manager = current_app.device_manager
    results = {}

    # 确定要操作的设备列表
    target_devices = device_manager.devices.items()
    if device_ids:
        target_devices = [(did, cfg) for did, cfg in target_devices if did in device_ids]

    for device_id, config in target_devices:
        if not config.get('enabled', True):
            continue
        try:
            client = device_manager.get_client(device_id)
            if not client:
                results[device_id] = {'success': False, 'message': '设备客户端不存在'}
                continue

            # 确保设备已连接
            if not getattr(client, 'connected', False):
                try:
                    client.connect()
                except Exception:
                    pass

            success = False
            if action == 'start':
                if hasattr(client, 'write_single_coil'):
                    success = client.write_single_coil(0, True)
                elif hasattr(client, 'write_single_register'):
                    success = client.write_single_register(100, 1)
            elif action == 'stop':
                if hasattr(client, 'write_single_coil'):
                    success = client.write_single_coil(0, False)
                elif hasattr(client, 'write_single_register'):
                    success = client.write_single_register(100, 0)
            elif action == 'reset':
                if hasattr(client, 'write_single_register'):
                    success = client.write_single_register(100, 0)
                elif hasattr(client, 'write_single_coil'):
                    success = client.write_single_coil(0, False)

            results[device_id] = {
                'success': success,
                'message': f'{action}操作{"成功" if success else "失败"}'
            }
        except Exception as e:
            logger.error(f"批量控制设备 {device_id} 失败: {e}", exc_info=True)
            results[device_id] = {'success': False, 'message': f'设备 {device_id} 操作失败'}

    success_count = sum(1 for r in results.values() if r.get('success'))
    total = len(results)

    target_desc = f'{len(device_ids)}台设备' if device_ids else '所有设备'
    get_auth_manager().log_operation(
        operator, f'batch_{action}', f'批量{action}{target_desc}: {success_count}/{total} 成功')
    return jsonify({
        'success': success_count > 0,
        'message': f'批量{action}完成: {success_count}/{total} 成功',
        'results': results,
        'success_count': success_count,
        'total': total
    })


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


# ==================== 配方/批量过程控制API ====================

@control_bp.route('/control/recipe/start', methods=['POST'])
@_require_engineer
def start_recipe():
    """启动配方"""
    data = request.get_json() or {}
    recipe_name = data.get('recipe_name')
    device_id = data.get('device_id')

    if not recipe_name:
        return jsonify({'error': '缺少 recipe_name 参数'}), 400

    from 采集层.recipe_simulator import RecipeSimulator
    if recipe_name not in RecipeSimulator.RECIPES:
        return jsonify({
            'error': f'配方 "{recipe_name}" 不存在',
            'available': list(RecipeSimulator.RECIPES.keys())
        }), 404

    # 查找目标设备的行为模拟器
    behavior_sim = _get_behavior_simulator(device_id)
    if not behavior_sim:
        return jsonify({'error': '未找到可用的设备行为模拟器'}), 404

    success = behavior_sim.set_recipe(recipe_name)
    if success:
        operator = request.current_user['username']
        get_auth_manager().log_operation(
            operator, 'start_recipe',
            f'设备 {behavior_sim.device_name} 启动配方: {recipe_name}')
        return jsonify({
            'success': True,
            'message': f'配方 "{recipe_name}" 已启动',
            'device': behavior_sim.device_name,
            'status': behavior_sim.get_recipe_status()
        })
    return jsonify({'success': False, 'message': '配方启动失败'}), 400


@control_bp.route('/control/recipe/stop', methods=['POST'])
@_require_engineer
def stop_recipe():
    """停止配方"""
    data = request.get_json() or {}
    device_id = data.get('device_id')

    behavior_sim = _get_behavior_simulator(device_id)
    if not behavior_sim:
        return jsonify({'error': '未找到可用的设备行为模拟器'}), 404

    status = behavior_sim.get_recipe_status()
    if not status:
        return jsonify({'error': '该设备没有正在运行的配方'}), 400

    behavior_sim.stop_recipe()
    operator = request.current_user['username']
    get_auth_manager().log_operation(
        operator, 'stop_recipe',
        f'设备 {behavior_sim.device_name} 停止配方: {status.get("recipe", "")}')
    return jsonify({
        'success': True,
        'message': '配方已停止',
        'device': behavior_sim.device_name
    })


@control_bp.route('/control/recipe/status', methods=['GET'])
@_require_auth
def get_recipe_status():
    """获取配方状态"""
    device_id = request.args.get('device_id')

    behavior_sim = _get_behavior_simulator(device_id)
    if not behavior_sim:
        return jsonify({'error': '未找到可用的设备行为模拟器'}), 404

    status = behavior_sim.get_recipe_status()
    if not status:
        return jsonify({'running': False, 'message': '没有正在运行的配方'})
    return jsonify(status)


@control_bp.route('/control/recipe/list', methods=['GET'])
@_require_auth
def list_recipes():
    """获取可用配方列表"""
    from 采集层.recipe_simulator import RecipeSimulator
    recipes = {}
    for key, recipe in RecipeSimulator.RECIPES.items():
        recipes[key] = {
            'name': recipe.name,
            'version': recipe.version,
            'phase_count': len(recipe.phases),
            'phases': [p.name for p in recipe.phases],
            'total_duration': sum(p.duration for p in recipe.phases),
            'parameters': recipe.parameters,
        }
    return jsonify({'recipes': recipes, 'count': len(recipes)})


def _get_behavior_simulator(device_id: Optional[str]):
    """获取设备行为模拟器实例"""
    device_manager = getattr(current_app, 'device_manager', None)
    if not device_manager:
        return None

    # 模拟模式下查找行为模拟器
    simulators = getattr(current_app, '_behavior_simulators', {})
    if device_id and device_id in simulators:
        return simulators[device_id]

    # 尝试从设备管理器获取
    if device_id:
        client = device_manager.get_client(device_id)
        if client and hasattr(client, 'behavior_simulator'):
            return client.behavior_simulator

    # 返回任意可用的模拟器
    if simulators:
        return next(iter(simulators.values()))

    return None
