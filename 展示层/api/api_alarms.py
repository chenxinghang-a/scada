"""
报警相关API
报警记录/报警输出控制/广播系统/报警规则管理
"""

import logging
from functools import wraps
from flask import Blueprint, jsonify, request, current_app

from 用户层.auth import jwt_required, role_required
from ._common import get_auth_manager, load_yaml_config, save_yaml_config, api_error_handler, api_error, clamp_limit
from core.service_response import module_unavailable_response

logger = logging.getLogger(__name__)

alarms_bp = Blueprint('api_alarms', __name__, url_prefix='/api')

_require_auth = jwt_required
_require_engineer = role_required('admin', 'engineer')
_require_operator = role_required('admin', 'engineer', 'operator')


# ==================== 报警相关API ====================

@alarms_bp.route('/alarms', methods=['GET'])
@_require_auth
@api_error_handler
def get_alarms():
    """获取报警记录"""
    device_id = request.args.get('device_id')
    alarm_level = request.args.get('alarm_level')
    acknowledged = request.args.get('acknowledged')
    limit = clamp_limit(request.args.get('limit', 100, type=int), default=100, maximum=10000)

    if acknowledged is not None:
        acknowledged = acknowledged.lower() == 'true'

    data = current_app.database.get_alarm_records(
        device_id=device_id, alarm_level=alarm_level,
        acknowledged=acknowledged, limit=limit)

    # round 299：把**运行时**的旁路状态合并进**库里的**历史记录。
    # `alarm_records` 只有落库时的事实（触发 / 恢复 / 确认），而旁路只存在于
    # `alarm_states`。不合并的话前端「已旁路」这个选项永远筛不出东西 ——
    # 界面上看着有这功能，实际一条都不生效。
    manager = getattr(current_app, 'alarm_manager', None)
    if manager is not None and data:
        try:
            shelved = {
                (a.get('alarm_id'), a.get('device_id'), a.get('register_name')):
                    bool(a.get('shelved'))
                for a in manager.get_active_alarms()
            }
        except Exception as e:      # noqa: BLE001 —— 合并失败不影响查历史
            logger.debug("合并旁路状态失败: %s", e)
            shelved = {}
        for rec in data:
            key = (rec.get('alarm_id'), rec.get('device_id'), rec.get('register_name'))
            rec['shelved'] = shelved.get(key, False)

    return jsonify({'alarms': data})


@alarms_bp.route('/alarms/active', methods=['GET'])
@_require_auth
@api_error_handler
def get_active_alarms():
    """获取活动报警"""
    return jsonify({'alarms': current_app.alarm_manager.get_active_alarms()})


@alarms_bp.route('/alarms/<alarm_id>/acknowledge', methods=['POST'])
@_require_auth
@_require_operator
@api_error_handler
def acknowledge_alarm(alarm_id):
    """确认报警（需要 admin/engineer/operator 角色）"""
    data = request.get_json() or {}
    manager = current_app.alarm_manager
    success = bool(manager.acknowledge_alarm(
        alarm_id=alarm_id,
        device_id=data.get('device_id'),
        register_name=data.get('register_name'),
        acknowledged_by=request.current_user.get('username', 'unknown')
    ))

    # round 281：确认成功 ≠ 蜂鸣器停了。DO 写失败时蜂鸣器还在响，
    # 旧实现照样回「报警已确认」—— 操作员站在盘前，界面说确认了、现场还在叫，
    # 只会以为界面卡了。这里与 `/alarm-output/reset` 对齐：如实分开报两件事。
    output_ok = manager.get_last_ack_output_ok() if success else True
    if not success:
        return jsonify({'success': False, 'message': '确认失败'})
    if not output_ok:
        return jsonify({
            'success': True,
            'output_ok': False,
            'message': '报警已确认，但声光消音失败（蜂鸣器可能仍在响）。'
                       '请检查 DO 通道与通信链路，必要时到现场确认。',
        }), 200
    return jsonify({'success': True, 'output_ok': True, 'message': '报警已确认'})


@alarms_bp.route('/alarms/statistics', methods=['GET'])
@_require_auth
@api_error_handler
def get_alarm_statistics():
    """获取报警统计（含声光输出+广播系统状态）"""
    return jsonify(current_app.alarm_manager.get_alarm_statistics())


# ==================== 报警输出控制API ====================

@alarms_bp.route('/alarm-output/status', methods=['GET'])
@_require_auth
@api_error_handler
def get_alarm_output_status():
    """获取声光报警器和广播系统状态"""
    result = {}
    alarm_manager = current_app.alarm_manager
    if alarm_manager.alarm_output:
        result['alarm_output'] = alarm_manager.alarm_output.get_status()
    else:
        result['alarm_output'] = {'status': 'unavailable', 'message': '声光报警输出未启用'}
    
    if alarm_manager.broadcast_system:
        result['broadcast'] = alarm_manager.broadcast_system.get_status()
    else:
        result['broadcast'] = {'status': 'unavailable', 'message': '广播系统未启用'}
    
    return jsonify(result)


@alarms_bp.route('/alarm-output/acknowledge', methods=['POST'])
@_require_auth
@_require_operator
@api_error_handler
def alarm_output_acknowledge():
    """消音 — 关闭蜂鸣器，报警灯保持闪烁"""
    alarm_manager = current_app.alarm_manager
    if alarm_manager.alarm_output:
        alarm_manager.alarm_output.acknowledge()
        get_auth_manager().log_operation(
            request.current_user['username'], 'alarm_acknowledge', '声光报警消音')
        return jsonify({'success': True, 'message': '已消音，指示灯保持'})
    return jsonify({'success': False, 'message': '声光报警输出未启用'}), 400


@alarms_bp.route('/alarm-output/reset', methods=['POST'])
@_require_auth
@_require_operator
@api_error_handler
def alarm_output_reset():
    """复位 — 全部清零，恢复绿灯正常状态"""
    alarm_manager = current_app.alarm_manager
    if not alarm_manager.alarm_output:
        return jsonify({'success': False, 'message': '声光报警输出未启用'}), 400
    output_ok = alarm_manager.reset_alarm()
    get_auth_manager().log_operation(
        request.current_user['username'], 'alarm_reset', '报警输出复位')
    if not output_ok:
        # 软件状态已清零，但硬件没确认 —— 必须如实报给操作员。
        # 旧实现无论 DO 写没写成功都回「已复位（绿灯正常）」，等于
        # 「界面说绿灯、现场红灯还在亮」。
        return jsonify({
            'success': False,
            'message': '报警状态已清除，但声光输出复位失败（硬件未确认）。'
                       '请检查 DO 通道与通信链路，必要时到现场确认指示灯状态。',
            'state_cleared': True,
            'output_ok': False,
        }), 500
    return jsonify({'success': True, 'message': '报警输出已复位（绿灯正常）',
                    'output_ok': True})


@alarms_bp.route('/alarm-output/manual', methods=['POST'])
@_require_auth
@_require_engineer
@api_error_handler
def alarm_output_manual():
    """手动控制报警灯和蜂鸣器（调试/巡检用）"""
    data = request.get_json() or {}
    alarm_manager = current_app.alarm_manager
    if not alarm_manager.alarm_output:
        return jsonify({'success': False, 'message': '声光报警输出未启用'}), 400

    result = alarm_manager.alarm_output.manual_control(
        red=data.get('red'),
        yellow=data.get('yellow'),
        green=data.get('green'),
        buzzer=data.get('buzzer'),
        duration=data.get('duration', 0)
    )
    get_auth_manager().log_operation(
        request.current_user['username'], 'alarm_manual',
        f'手动控制报警灯: {data}')
    return jsonify({
        'success': result.get('success', True),
        'state': result.get('state', {}),
        'message': '手动控制指令已发送'
    })


@alarms_bp.route('/broadcast/speak', methods=['POST'])
@_require_auth
@_require_operator
@api_error_handler
def broadcast_speak():
    """手动广播喊话"""
    data = request.get_json()
    if not data or not data.get('text'):
        return jsonify({'success': False, 'message': '请提供广播内容(text)'}), 400

    alarm_manager = current_app.alarm_manager
    if not alarm_manager.broadcast_system:
        return jsonify({'success': False, 'message': '广播系统未启用'}), 400

    result = alarm_manager.broadcast_system.speak(
        text=data['text'],
        level=data.get('level', 'info'),
        area=data.get('area'),
        source='manual'
    )
    get_auth_manager().log_operation(
        request.current_user['username'], 'broadcast_speak',
        f'广播喊话: {data["text"][:50]}')
    return jsonify(result)


@alarms_bp.route('/broadcast/areas', methods=['GET'])
@_require_auth
@api_error_handler
def get_broadcast_areas():
    """获取广播区域列表"""
    alarm_manager = current_app.alarm_manager
    if not alarm_manager.broadcast_system:
        return module_unavailable_response('broadcast_system')
    return jsonify({'areas': alarm_manager.broadcast_system.get_areas()})


@alarms_bp.route('/broadcast/history', methods=['GET'])
@_require_auth
@api_error_handler
def get_broadcast_history():
    """获取广播历史"""
    limit = clamp_limit(request.args.get('limit', 50, type=int), default=50, maximum=500)
    alarm_manager = current_app.alarm_manager
    if not alarm_manager.broadcast_system:
        return module_unavailable_response('broadcast_system')
    return jsonify({'history': alarm_manager.broadcast_system.get_history(limit=limit)})


# ==================== 报警规则API ====================

@alarms_bp.route('/alarm-rules', methods=['GET'])
@_require_auth
@api_error_handler
def get_alarm_rules():
    """获取所有报警规则"""
    config = load_yaml_config('配置/alarms.yaml')
    return jsonify({'rules': config.get('alarm_rules', []), 'notification': config.get('notification', {})})


@alarms_bp.route('/alarm-rules', methods=['POST'])
@_require_engineer
@api_error_handler
def add_alarm_rule():
    """添加报警规则"""
    data = request.get_json()
    if not data or 'id' not in data:
        return api_error('请提供规则ID')

    config = load_yaml_config('配置/alarms.yaml')

    rules = config.get('alarm_rules', [])
    if any(r.get('id') == data['id'] for r in rules):
        return api_error(f'规则 {data["id"]} 已存在')

    rules.append(data)
    config['alarm_rules'] = rules

    save_yaml_config('配置/alarms.yaml', config)

    current_app.alarm_manager.rules[data['id']] = data
    current_app.alarm_manager._rebuild_rules_index()
    get_auth_manager().log_operation(
        request.current_user['username'], 'add_alarm_rule', f"添加报警规则: {data['id']}")
    return jsonify({'success': True, 'message': f"规则 {data['id']} 已添加"})


@alarms_bp.route('/alarm-rules/<rule_id>', methods=['PUT'])
@_require_engineer
@api_error_handler
def update_alarm_rule(rule_id):
    """更新报警规则"""
    data = request.get_json()
    if not data:
        return api_error('请提供更新数据')

    config = load_yaml_config('配置/alarms.yaml')

    rules = config.get('alarm_rules', [])
    found = False
    for i, r in enumerate(rules):
        if r.get('id') == rule_id:
            rules[i].update({k: v for k, v in data.items() if k != 'id'})
            found = True
            break

    if not found:
        return api_error(f'规则 {rule_id} 不存在', 404)

    config['alarm_rules'] = rules
    save_yaml_config('配置/alarms.yaml', config)

    current_app.alarm_manager.rules[rule_id] = rules[[r['id'] for r in rules].index(rule_id)]
    current_app.alarm_manager._rebuild_rules_index()
    get_auth_manager().log_operation(
        request.current_user['username'], 'update_alarm_rule', f"更新报警规则: {rule_id}")
    return jsonify({'success': True, 'message': f'规则 {rule_id} 已更新'})


@alarms_bp.route('/alarm-rules/<rule_id>', methods=['DELETE'])
@_require_engineer
@api_error_handler
def delete_alarm_rule(rule_id):
    """删除报警规则"""
    config = load_yaml_config('配置/alarms.yaml')

    rules = config.get('alarm_rules', [])
    new_rules = [r for r in rules if r.get('id') != rule_id]

    if len(new_rules) == len(rules):
        return api_error(f'规则 {rule_id} 不存在', 404)

    config['alarm_rules'] = new_rules
    save_yaml_config('配置/alarms.yaml', config)

    if rule_id in current_app.alarm_manager.rules:
        del current_app.alarm_manager.rules[rule_id]
        current_app.alarm_manager._rebuild_rules_index()

    get_auth_manager().log_operation(
        request.current_user['username'], 'delete_alarm_rule', f"删除报警规则: {rule_id}")
    return jsonify({'success': True, 'message': f'规则 {rule_id} 已删除'})


@alarms_bp.route('/alarm-rules/notification', methods=['PUT'])
@_require_engineer
@api_error_handler
def update_notification():
    """更新通知设置"""
    data = request.get_json()
    if not data:
        return api_error('请提供通知配置')

    config = load_yaml_config('配置/alarms.yaml')

    if 'email' in data:
        config.setdefault('notification', {})['email'] = data['email']
    if 'sound' in data:
        config.setdefault('notification', {})['sound'] = data['sound']

    save_yaml_config('配置/alarms.yaml', config)

    get_auth_manager().log_operation(
        request.current_user['username'], 'update_notification', '更新通知设置')
    return jsonify({'success': True, 'message': '通知设置已保存'})


# ==================== 报警输出配置API ====================

@alarms_bp.route('/alarm-output/config', methods=['GET'])
@_require_auth
@api_error_handler
def get_alarm_output_config():
    """获取声光报警器配置（Modbus连接、DO映射等）"""
    config = load_yaml_config('配置/alarms.yaml')
    alarm_output_cfg = config.get('alarm_output', {})
    return jsonify({
        'success': True,
        'config': alarm_output_cfg
    })


@alarms_bp.route('/alarm-output/config', methods=['PUT'])
@_require_auth
@_require_engineer
@api_error_handler
def update_alarm_output_config():
    """
    更新声光报警器配置（Modbus连接、DO映射等）
    
    请求体示例:
    {
        "signal_tower": {
            "host": "192.168.1.70",
            "port": 502,
            "slave_id": 1,
            "do_mapping": {
                "red_light": 0,
                "yellow_light": 1,
                "green_light": 2,
                "buzzer": 5
            }
        }
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': '请提供配置数据'}), 400

    config = load_yaml_config('配置/alarms.yaml')
    
    # 更新alarm_output配置
    if 'alarm_output' not in config:
        config['alarm_output'] = {}
    
    alarm_output = config['alarm_output']
    
    if 'enabled' in data:
        alarm_output['enabled'] = bool(data['enabled'])
    
    if 'signal_tower' in data:
        if 'signal_tower' not in alarm_output:
            alarm_output['signal_tower'] = {}
        tower = data['signal_tower']
        for key in ('device_id', 'protocol', 'host', 'port', 'slave_id'):
            if key in tower:
                alarm_output['signal_tower'][key] = tower[key]
        if 'do_mapping' in tower:
            if 'do_mapping' not in alarm_output['signal_tower']:
                alarm_output['signal_tower']['do_mapping'] = {}
            alarm_output['signal_tower']['do_mapping'].update(tower['do_mapping'])
    
    if 'relay_output' in data:
        if 'relay_output' not in alarm_output:
            alarm_output['relay_output'] = {}
        relay = data['relay_output']
        for key in ('device_id', 'protocol', 'host', 'port', 'slave_id'):
            if key in relay:
                alarm_output['relay_output'][key] = relay[key]
        if 'do_mapping' in relay:
            if 'do_mapping' not in alarm_output['relay_output']:
                alarm_output['relay_output']['do_mapping'] = {}
            alarm_output['relay_output']['do_mapping'].update(relay['do_mapping'])

    save_yaml_config('配置/alarms.yaml', config)

    # 同步更新运行时的alarm_output实例
    alarm_manager = current_app.alarm_manager
    if alarm_manager.alarm_output:
        ao = alarm_manager.alarm_output
        tower_cfg = alarm_output.get('signal_tower', {})
        if 'host' in tower_cfg:
            ao.config.setdefault('modbus', {})['host'] = tower_cfg['host']
        if 'port' in tower_cfg:
            ao.config.setdefault('modbus', {})['port'] = tower_cfg['port']
        if 'slave_id' in tower_cfg:
            ao.config.setdefault('modbus', {})['slave_id'] = tower_cfg['slave_id']
        if 'do_mapping' in tower_cfg:
            dm = tower_cfg['do_mapping']
            if 'red_light' in dm:
                ao.do_red = dm['red_light']
            if 'yellow_light' in dm:
                ao.do_yellow = dm['yellow_light']
            if 'green_light' in dm:
                ao.do_green = dm['green_light']
            if 'buzzer' in dm:
                ao.do_buzzer = dm['buzzer']
        # 断开旧连接，下次操作时重新连接
        if hasattr(ao, '_modbus_client') and ao._modbus_client:
            ao._modbus_client.close()
            ao._modbus_client = None

    get_auth_manager().log_operation(
        request.current_user['username'], 'update_alarm_output_config', '更新报警输出配置')
    return jsonify({'success': True, 'message': '报警输出配置已保存（实时生效）'})


@alarms_bp.route('/broadcast/config', methods=['GET'])
@_require_auth
@api_error_handler
def get_broadcast_config():
    """获取广播系统配置"""
    config = load_yaml_config('配置/alarms.yaml')
    broadcast_cfg = config.get('broadcast', {})
    
    # 也返回运行时状态
    alarm_manager = current_app.alarm_manager
    runtime_status = {}
    if alarm_manager.broadcast_system:
        runtime_status = alarm_manager.broadcast_system.get_status()
    
    return jsonify({
        'success': True,
        'config': broadcast_cfg,
        'runtime': runtime_status
    })


@alarms_bp.route('/broadcast/config', methods=['PUT'])
@_require_auth
@_require_engineer
@api_error_handler
def update_broadcast_config():
    """
    更新广播系统配置
    
    请求体示例:
    {
        "enabled": true,
        "mqtt": {
            "broker": "192.168.1.200",
            "port": 1883,
            "username": "",
            "password": "",
            "topic_prefix": "pa/"
        },
        "areas": ["车间A", "车间B", "仓库", "办公楼"],
        "preset_templates": {
            "alarm_critical": "注意！{area}发生严重报警：{message}，请立即处置！",
            "alarm_warning": "提醒：{area}出现告警：{message}，请关注。"
        }
    }
    """
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': '请提供配置数据'}), 400

    config = load_yaml_config('配置/alarms.yaml')
    
    if 'broadcast' not in config:
        config['broadcast'] = {}
    
    broadcast = config['broadcast']
    
    for key in ('enabled', 'mqtt', 'areas', 'default_area', 'preset_templates'):
        if key in data:
            broadcast[key] = data[key]

    save_yaml_config('配置/alarms.yaml', config)

    # 同步更新运行时的broadcast实例
    alarm_manager = current_app.alarm_manager
    if alarm_manager.broadcast_system:
        bs = alarm_manager.broadcast_system
        if 'enabled' in data:
            bs.enabled = bool(data['enabled'])
        if 'areas' in data:
            bs.areas = data['areas']
        if 'default_area' in data:
            bs.default_area = data['default_area']
        if 'preset_templates' in data:
            bs.preset_templates.update(data['preset_templates'])
        if 'mqtt' in data:
            bs._mqtt_config.update(data['mqtt'])
            # 断开旧MQTT连接，下次发送时重连
            if bs._mqtt_client:
                try:
                    bs._mqtt_client.loop_stop()
                    bs._mqtt_client.disconnect()
                except Exception as e:
                    # 旧MQTT客户端停止失败：连接可能残留并继续收发，
                    # 配置已热更新但旧连接未断开（功能降级），必须留痕
                    logger.warning("停止旧MQTT客户端失败（可能残留连接）: %s", e)
                bs._mqtt_client = None
                bs._mqtt_connected = False

    get_auth_manager().log_operation(
        request.current_user['username'], 'update_broadcast_config', '更新广播系统配置')
    return jsonify({'success': True, 'message': '广播系统配置已保存（实时生效）'})


# ==================== 报警去重配置API ====================

@alarms_bp.route('/alarms/flood-status', methods=['GET'])
@_require_auth
@api_error_handler
def get_flood_status():
    """获取告警洪水检测状态（ISA-18.2）"""
    alarm_manager = current_app.alarm_manager
    return jsonify(alarm_manager._flood_detector.get_status())


@alarms_bp.route('/alarms/dedup-config', methods=['GET'])
@_require_auth
@api_error_handler
def get_dedup_config():
    """获取报警去重配置"""
    alarm_manager = current_app.alarm_manager
    return jsonify({'config': alarm_manager.get_dedup_config()})


@alarms_bp.route('/alarms/dedup-config', methods=['PUT'])
@_require_engineer
@api_error_handler
def update_dedup_config():
    """更新报警去重配置"""
    data = request.get_json()
    if not data:
        return api_error('请提供去重配置')

    alarm_manager = current_app.alarm_manager
    updated_config = alarm_manager.update_dedup_config(data)

    get_auth_manager().log_operation(
        request.current_user['username'], 'update_dedup_config',
        f'更新去重配置: emit_cooldown={updated_config["emit_cooldown_seconds"]}s, '
        f'ack_suppress={updated_config["acknowledge_suppress_seconds"]}s')
    return jsonify({'success': True, 'config': updated_config, 'message': '去重配置已更新'})


# ==================== 告警升级配置API ====================

def _escalation_payload(alarm_manager, with_rules: bool) -> dict:
    """升级策略的**真实**状态。

    round 249 修复：原实现用 `hasattr(alarm_manager, '_escalation_manager')`
    当守卫，而该属性在 `AlarmManager.__init__` 里就设为 None ——
    **hasattr 永远为 True**。于是生产环境（配置里没有 escalation 段、
    管理器未装配）这三个端点全部 `None.get_status()` → 500。

    改成按 `is not None` 判断，并且**未装配时如实说明当前生效的是什么**：
    单级超时兜底（`_escalation_timeout` 秒）仍在跑，不是"升级已关闭"。
    运维读到这段应该知道两件事：多级规则没配、兜底策略是多久。
    """
    manager = getattr(alarm_manager, '_escalation_manager', None)
    if manager is not None:
        status = manager.get_status()
        status['mode'] = 'multi_level'
        if not with_rules:
            return status
        return {'rules': manager.get_rules(), 'mode': 'multi_level'}

    timeout = getattr(alarm_manager, '_escalation_timeout', 600)
    payload = {
        'running': False,
        'mode': 'single_level_fallback',
        'tracked_alarms': 0,
        'rules_count': 0,
        'timeout_seconds': timeout,
        'message': (
            f'多级升级管理器未装配（配置/alarms.yaml 缺 escalation 段或其 rules 为空）。'
            f'当前生效的是单级超时兜底：报警 {timeout} 秒未确认即升级一次。'
            f'如需多级规则，请 PUT /api/alarms/escalation/rules。'
        ),
    }
    if not with_rules:
        return payload
    return {
        'rules': [],
        'mode': 'single_level_fallback',
        'fallback': {'timeout_seconds': timeout},
        'message': payload['message'],
    }


@alarms_bp.route('/alarms/escalation/status', methods=['GET'])
@_require_auth
@api_error_handler
def get_escalation_status():
    """获取告警升级状态"""
    alarm_manager = current_app.alarm_manager
    return jsonify(_escalation_payload(alarm_manager, with_rules=False))


@alarms_bp.route('/alarms/escalation/rules', methods=['GET'])
@_require_auth
@api_error_handler
def get_escalation_rules():
    """获取升级规则"""
    alarm_manager = current_app.alarm_manager
    return jsonify(_escalation_payload(alarm_manager, with_rules=True))


@alarms_bp.route('/alarms/escalation/rules', methods=['PUT'])
@_require_engineer
@api_error_handler
def update_escalation_rules():
    """更新升级规则（写回 配置/alarms.yaml 并热重载，重启后仍生效）

    round 249：原实现在管理器未装配时直接回 500（hasattr 守卫永远为真），
    装配了也只能改内存 —— 重启即丢，且与 yaml 双源。现在统一以 yaml 为真源：
    校验 → 写 `escalation.rules` → `alarm_manager.load_config()` 重载
    （内部会装配或热更新管理器）。
    """
    data = request.get_json()
    if not data or 'rules' not in data:
        return api_error('请提供升级规则')

    rules = data['rules']
    if not isinstance(rules, list) or not rules:
        return api_error(
            'rules 必须是非空列表 —— 空列表等于关闭全部升级，不接受')

    # 先过一遍校验，避免把非法规则写进 yaml（写坏了更难恢复）
    from 报警层.alarm_escalation import _validate_rules
    try:
        _validate_rules(rules)
    except ValueError as e:
        return api_error(f'升级规则非法: {e}', 400)

    config = load_yaml_config('配置/alarms.yaml')
    escalation_cfg = config.get('escalation')
    if not isinstance(escalation_cfg, dict):
        escalation_cfg = {}
    escalation_cfg['rules'] = rules
    config['escalation'] = escalation_cfg

    if not save_yaml_config('配置/alarms.yaml', config):
        return api_error('升级规则保存失败', 500)

    alarm_manager = current_app.alarm_manager
    try:
        alarm_manager.load_config()      # 装配 / 热更新多级规则
    except Exception as e:               # noqa: BLE001 —— load_config 自己吞异常
        logger.error("升级规则重载失败: %s", e, exc_info=True)

    get_auth_manager().log_operation(
        request.current_user['username'], 'update_escalation_rules',
        f'更新升级规则: {len(rules)} 条')

    return jsonify({
        'success': True,
        'message': '升级规则已更新（已写回配置并热加载）',
        'rules': alarm_manager._escalation_manager.get_rules()
        if alarm_manager._escalation_manager is not None else [],
        'mode': 'multi_level' if alarm_manager._escalation_manager is not None
        else 'single_level_fallback',
    })
