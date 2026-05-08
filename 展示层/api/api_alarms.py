"""
报警相关API
报警记录/报警输出控制/广播系统/报警规则管理
"""

import logging
from flask import Blueprint, jsonify, request, current_app

from 用户层.auth import jwt_required, role_required
from ._common import get_auth_manager, load_yaml_config, save_yaml_config
from core.service_response import module_unavailable_response

logger = logging.getLogger(__name__)

alarms_bp = Blueprint('api_alarms', __name__, url_prefix='/api')

_require_auth = jwt_required
_require_engineer = role_required('admin', 'engineer')


# ==================== 报警相关API ====================

@alarms_bp.route('/alarms', methods=['GET'])
def get_alarms():
    """获取报警记录"""
    device_id = request.args.get('device_id')
    alarm_level = request.args.get('alarm_level')
    acknowledged = request.args.get('acknowledged')
    limit = request.args.get('limit', 100, type=int)

    if acknowledged is not None:
        acknowledged = acknowledged.lower() == 'true'

    data = current_app.database.get_alarm_records(
        device_id=device_id, alarm_level=alarm_level,
        acknowledged=acknowledged, limit=limit)
    return jsonify({'alarms': data})


@alarms_bp.route('/alarms/active', methods=['GET'])
def get_active_alarms():
    """获取活动报警"""
    return jsonify({'alarms': current_app.alarm_manager.get_active_alarms()})


@alarms_bp.route('/alarms/<alarm_id>/acknowledge', methods=['POST'])
def acknowledge_alarm(alarm_id):
    """确认报警"""
    data = request.get_json()
    success = current_app.alarm_manager.acknowledge_alarm(
        alarm_id=alarm_id,
        device_id=data.get('device_id'),
        register_name=data.get('register_name'),
        acknowledged_by=data.get('acknowledged_by', 'operator')
    )
    return jsonify({'success': success, 'message': '报警已确认' if success else '确认失败'})


@alarms_bp.route('/alarms/statistics', methods=['GET'])
def get_alarm_statistics():
    """获取报警统计（含声光输出+广播系统状态）"""
    return jsonify(current_app.alarm_manager.get_alarm_statistics())


# ==================== 报警输出控制API ====================

@alarms_bp.route('/alarm-output/status', methods=['GET'])
@_require_auth
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
def alarm_output_acknowledge():
    """消音 — 关闭蜂鸣器，报警灯保持闪烁"""
    alarm_manager = current_app.alarm_manager
    if alarm_manager.alarm_output:
        alarm_manager.alarm_output.acknowledge()
        get_auth_manager().log_operation(
            request.current_user['username'], 'alarm_acknowledge', '声光报警消音')
        return jsonify({'success': True, 'message': '已消音，指示灯保持'})
    return jsonify({'success': False, 'message': '声光报警输出未启用'})


@alarms_bp.route('/alarm-output/reset', methods=['POST'])
@_require_auth
def alarm_output_reset():
    """复位 — 全部清零，恢复绿灯正常状态"""
    alarm_manager = current_app.alarm_manager
    alarm_manager.reset_alarm()
    get_auth_manager().log_operation(
        request.current_user['username'], 'alarm_reset', '报警输出复位')
    return jsonify({'success': True, 'message': '报警输出已复位（绿灯正常）'})


@alarms_bp.route('/alarm-output/manual', methods=['POST'])
@_require_auth
@_require_engineer
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
def get_broadcast_areas():
    """获取广播区域列表"""
    alarm_manager = current_app.alarm_manager
    if not alarm_manager.broadcast_system:
        return module_unavailable_response('broadcast_system')
    return jsonify({'areas': alarm_manager.broadcast_system.get_areas()})


@alarms_bp.route('/broadcast/history', methods=['GET'])
@_require_auth
def get_broadcast_history():
    """获取广播历史"""
    limit = request.args.get('limit', 50, type=int)
    alarm_manager = current_app.alarm_manager
    if not alarm_manager.broadcast_system:
        return module_unavailable_response('broadcast_system')
    return jsonify({'history': alarm_manager.broadcast_system.get_history(limit=limit)})


# ==================== 报警规则API ====================

@alarms_bp.route('/alarm-rules', methods=['GET'])
def get_alarm_rules():
    """获取所有报警规则"""
    config = load_yaml_config('配置/alarms.yaml')
    return jsonify({'rules': config.get('alarm_rules', []), 'notification': config.get('notification', {})})


@alarms_bp.route('/alarm-rules', methods=['POST'])
@_require_engineer
def add_alarm_rule():
    """添加报警规则"""
    data = request.get_json()
    if not data or 'id' not in data:
        return jsonify({'error': '请提供规则ID'}), 400

    config = load_yaml_config('配置/alarms.yaml')

    rules = config.get('alarm_rules', [])
    if any(r.get('id') == data['id'] for r in rules):
        return jsonify({'error': f'规则 {data["id"]} 已存在'}), 400

    rules.append(data)
    config['alarm_rules'] = rules

    save_yaml_config('配置/alarms.yaml', config)

    current_app.alarm_manager.rules[data['id']] = data
    get_auth_manager().log_operation(
        request.current_user['username'], 'add_alarm_rule', f"添加报警规则: {data['id']}")
    return jsonify({'success': True, 'message': f"规则 {data['id']} 已添加"})


@alarms_bp.route('/alarm-rules/<rule_id>', methods=['PUT'])
@_require_engineer
def update_alarm_rule(rule_id):
    """更新报警规则"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '请提供更新数据'}), 400

    config = load_yaml_config('配置/alarms.yaml')

    rules = config.get('alarm_rules', [])
    found = False
    for i, r in enumerate(rules):
        if r.get('id') == rule_id:
            rules[i].update(data)
            found = True
            break

    if not found:
        return jsonify({'error': f'规则 {rule_id} 不存在'}), 404

    config['alarm_rules'] = rules
    save_yaml_config('配置/alarms.yaml', config)

    current_app.alarm_manager.rules[rule_id] = rules[[r['id'] for r in rules].index(rule_id)]
    get_auth_manager().log_operation(
        request.current_user['username'], 'update_alarm_rule', f"更新报警规则: {rule_id}")
    return jsonify({'success': True, 'message': f'规则 {rule_id} 已更新'})


@alarms_bp.route('/alarm-rules/<rule_id>', methods=['DELETE'])
@_require_engineer
def delete_alarm_rule(rule_id):
    """删除报警规则"""
    config = load_yaml_config('配置/alarms.yaml')

    rules = config.get('alarm_rules', [])
    new_rules = [r for r in rules if r.get('id') != rule_id]

    if len(new_rules) == len(rules):
        return jsonify({'error': f'规则 {rule_id} 不存在'}), 404

    config['alarm_rules'] = new_rules
    save_yaml_config('配置/alarms.yaml', config)

    if rule_id in current_app.alarm_manager.rules:
        del current_app.alarm_manager.rules[rule_id]

    get_auth_manager().log_operation(
        request.current_user['username'], 'delete_alarm_rule', f"删除报警规则: {rule_id}")
    return jsonify({'success': True, 'message': f'规则 {rule_id} 已删除'})


@alarms_bp.route('/alarm-rules/notification', methods=['PUT'])
@_require_engineer
def update_notification():
    """更新通知设置"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '请提供通知配置'}), 400

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
                except Exception:
                    pass
                bs._mqtt_client = None
                bs._mqtt_connected = False

    get_auth_manager().log_operation(
        request.current_user['username'], 'update_broadcast_config', '更新广播系统配置')
    return jsonify({'success': True, 'message': '广播系统配置已保存（实时生效）'})


# ==================== 报警去重配置API ====================

@alarms_bp.route('/alarms/dedup-config', methods=['GET'])
def get_dedup_config():
    """获取报警去重配置"""
    alarm_manager = current_app.alarm_manager
    return jsonify({'config': alarm_manager.get_dedup_config()})


@alarms_bp.route('/alarms/dedup-config', methods=['PUT'])
@_require_engineer
def update_dedup_config():
    """更新报警去重配置"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '请提供去重配置'}), 400

    alarm_manager = current_app.alarm_manager
    updated_config = alarm_manager.update_dedup_config(data)

    get_auth_manager().log_operation(
        request.current_user['username'], 'update_dedup_config',
        f'更新去重配置: emit_cooldown={updated_config["emit_cooldown_seconds"]}s, '
        f'ack_suppress={updated_config["acknowledge_suppress_seconds"]}s')
    return jsonify({'success': True, 'config': updated_config, 'message': '去重配置已更新'})
