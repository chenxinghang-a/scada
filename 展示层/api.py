"""
REST API模块
提供数据查询和设备控制接口

优化点:
- 使用装饰器替代重复认证代码（_require_auth / _require_admin / _require_engineer）
- 统一错误响应格式
- 消除重复的 import yaml / Path
"""

import yaml
import logging
from pathlib import Path
from functools import wraps
from flask import Blueprint, jsonify, request, current_app
from datetime import datetime, timedelta

# 导入统一的认证装饰器
from 用户层.auth import jwt_required, role_required, permission_required

logger = logging.getLogger(__name__)

api_bp = Blueprint('api', __name__)


# ==================== 认证辅助函数 ====================

def _get_auth_manager():
    """获取认证管理器"""
    return current_app.auth_manager


# 统一使用auth.py中的装饰器，保持向后兼容
_require_auth = jwt_required
_require_admin = role_required('admin')
_require_engineer = role_required('admin', 'engineer')


# ==================== 配置工具函数 ====================

def _load_yaml_config(config_path: str) -> dict:
    """加载YAML配置文件"""
    path = Path(config_path)
    if not path.exists():
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def _save_yaml_config(config_path: str, config: dict) -> bool:
    """保存YAML配置文件"""
    try:
        path = Path(config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
        return True
    except Exception as e:
        logger.error(f"保存配置文件失败: {e}")
        return False


# ==================== 认证相关API ====================

@api_bp.route('/auth/login', methods=['POST'])
def login():
    """用户登录"""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': '请提供登录信息'}), 400
    
    username = data.get('username', '').strip()
    password = data.get('password', '')
    
    if not username or not password:
        return jsonify({'success': False, 'message': '用户名和密码不能为空'}), 400
    
    auth_manager = _get_auth_manager()
    ip_address = request.remote_addr
    result = auth_manager.login(username, password, ip_address)
    
    return jsonify(result), (200 if result['success'] else 401)


@api_bp.route('/auth/register', methods=['POST'])
def register():
    """用户注册（仅管理员，或首个用户直接注册admin）"""
    token = None
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        token = auth_header[7:]
    
    auth_manager = _get_auth_manager()
    
    # 无token时：检查是否是第一个用户（允许直接注册admin）
    if not token:
        users = auth_manager.get_users()
        if len(users) > 0:
            return jsonify({'success': False, 'message': '需要管理员权限'}), 403
    else:
        user = auth_manager.verify_token(token)
        if not user or user['role'] != 'admin':
            return jsonify({'success': False, 'message': '需要管理员权限'}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': '请提供注册信息'}), 400
    
    result = auth_manager.register(
        username=data.get('username', '').strip(),
        password=data.get('password', ''),
        role=data.get('role', 'viewer'),
        display_name=data.get('display_name'),
        email=data.get('email'),
        phone=data.get('phone')
    )
    
    return jsonify(result), (201 if result['success'] else 400)


@api_bp.route('/auth/verify', methods=['GET'])
@_require_auth
def verify_token():
    """验证令牌有效性"""
    return jsonify({'valid': True, 'user': request.current_user})


@api_bp.route('/auth/refresh', methods=['POST'])
def refresh_token():
    """刷新令牌"""
    data = request.get_json()
    rtoken = data.get('refresh_token')
    
    if not rtoken:
        return jsonify({'success': False, 'message': '请提供刷新令牌'}), 400
    
    auth_manager = _get_auth_manager()
    result = auth_manager.refresh_token(rtoken)
    
    if result:
        return jsonify(result)
    else:
        return jsonify({'success': False, 'message': '刷新令牌无效'}), 401


@api_bp.route('/auth/change-password', methods=['POST'])
@_require_auth
def change_password():
    """修改密码"""
    data = request.get_json()
    auth_manager = _get_auth_manager()
    result = auth_manager.change_password(
        username=request.current_user['username'],
        old_password=data.get('old_password', ''),
        new_password=data.get('new_password', '')
    )
    return jsonify(result)


@api_bp.route('/auth/users', methods=['GET'])
@_require_admin
def get_users():
    """获取用户列表（仅管理员）"""
    auth_manager = _get_auth_manager()
    return jsonify({'users': auth_manager.get_users()})


@api_bp.route('/auth/users/<username>', methods=['PUT'])
@_require_admin
def update_user(username):
    """更新用户信息（仅管理员）"""
    data = request.get_json()
    auth_manager = _get_auth_manager()
    return jsonify(auth_manager.update_user(username, **data))


@api_bp.route('/auth/users/<username>', methods=['DELETE'])
@_require_admin
def delete_user(username):
    """删除用户（仅管理员）"""
    auth_manager = _get_auth_manager()
    return jsonify(auth_manager.delete_user(username))


@api_bp.route('/auth/logs', methods=['GET'])
@_require_admin
def get_operation_logs():
    """获取操作日志（仅管理员）"""
    auth_manager = _get_auth_manager()
    username = request.args.get('username')
    limit = request.args.get('limit', 100, type=int)
    return jsonify({'logs': auth_manager.get_operation_logs(username=username, limit=limit)})


# ==================== 设备管理API ====================

@api_bp.route('/devices', methods=['GET'])
def get_devices():
    """获取所有设备列表"""
    devices = current_app.device_manager.get_all_status()
    return jsonify({'devices': devices})


@api_bp.route('/devices/<device_id>', methods=['GET'])
def get_device(device_id):
    """获取单个设备详情"""
    status = current_app.device_manager.get_device_status(device_id)
    if 'error' in status:
        return jsonify(status), 404
    return jsonify(status)


@api_bp.route('/devices', methods=['POST'])
@_require_engineer
def add_device():
    """添加新设备（支持所有协议）"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '请提供设备配置'}), 400
    
    # 基础必填字段
    for field in ('id', 'name'):
        if field not in data:
            return jsonify({'error': f'缺少必填字段: {field}'}), 400
    
    protocol = data.get('protocol', 'modbus_tcp')
    
    # 协议级必填字段验证
    try:
        _validate_protocol_fields(protocol, data)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    
    # 构建设备配置
    device_config = _build_device_config(protocol, data)
    
    success = current_app.device_manager.add_device(device_config)
    if success:
        _get_auth_manager().log_operation(
            request.current_user['username'], 'add_device', f"添加设备: {data['id']}")
        return jsonify({'success': True, 'message': f"设备 {data['id']} 添加成功"})
    else:
        return jsonify({'success': False, 'message': '添加失败'}), 400


@api_bp.route('/devices/<device_id>', methods=['PUT'])
@_require_engineer
def update_device(device_id):
    """更新设备配置（支持所有协议）"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '请提供设备配置'}), 400
    
    device_manager = current_app.device_manager
    if device_id not in device_manager.devices:
        return jsonify({'error': f'设备 {device_id} 不存在'}), 404
    
    device_config = device_manager.devices[device_id]
    protocol = data.get('protocol', device_config.get('protocol', 'modbus_tcp'))
    
    # 通用字段
    for key in ('name', 'description', 'enabled', 'collection_interval', 'protocol'):
        if key in data:
            device_config[key] = int(data[key]) if key == 'collection_interval' else data[key]
    
    # 协议专属字段
    _update_protocol_fields(protocol, device_config, data)
    
    device_manager._save_config()
    _get_auth_manager().log_operation(
        request.current_user['username'], 'update_device', f"更新设备: {device_id}")
    return jsonify({'success': True, 'message': f'设备 {device_id} 更新成功'})


@api_bp.route('/devices/<device_id>', methods=['DELETE'])
@_require_engineer
def delete_device(device_id):
    """删除设备"""
    success = current_app.device_manager.remove_device(device_id)
    if success:
        _get_auth_manager().log_operation(
            request.current_user['username'], 'delete_device', f"删除设备: {device_id}")
        return jsonify({'success': True, 'message': f'设备 {device_id} 已删除'})
    return jsonify({'success': False, 'message': '删除失败'}), 400


@api_bp.route('/devices/<device_id>/test', methods=['POST'])
def test_device_connection(device_id):
    """测试设备连接（支持所有协议）"""
    device_manager = current_app.device_manager
    device_config = device_manager.devices.get(device_id)
    
    if not device_config:
        return jsonify({'success': False, 'message': f'设备 {device_id} 不存在'}), 404
    
    protocol = device_config.get('protocol', 'modbus_tcp')
    
    try:
        from 采集层.device_manager import _create_client
        
        test_client = _create_client(device_config, device_manager.simulation_mode)
        if test_client is None:
            return jsonify({'success': False, 'message': f'不支持的协议: {protocol}'}), 400
        
        connected = test_client.connect()
        if not connected:
            return jsonify({
                'success': False,
                'message': f'连接失败（协议: {protocol}）',
                'protocol': protocol,
                'endpoint': device_config.get('host', device_config.get('endpoint', ''))
            })
        
        # 读取示例数据
        sample = {}
        if protocol in ('modbus_tcp', 'modbus_rtu'):
            registers = device_config.get('registers', [])
            if registers:
                reg = registers[0]
                result = test_client.read_holding_registers(reg['address'], reg.get('length', 1))
                if result is not None:
                    sample = {'register': reg['name'], 'value': result, 'unit': reg.get('unit', '')}
            test_client.disconnect()
        elif protocol == 'opcua':
            latest = test_client.get_latest_data()
            if latest:
                name, ldata = list(latest.items())[0]
                sample = {'node': name, 'value': ldata.get('value'), 'unit': ldata.get('unit', '')}
            test_client.disconnect()
        elif protocol == 'rest':
            for ep in device_config.get('endpoints', []):
                value = test_client.read_endpoint(ep)
                if value is not None:
                    sample = {'endpoint': ep.get('name'), 'value': value, 'unit': ep.get('unit', '')}
                    break
            test_client.disconnect()
        elif protocol == 'mqtt':
            test_client.disconnect()
        
        return jsonify({
            'success': True, 'message': f'连接成功（协议: {protocol}）',
            'protocol': protocol, 'sample_data': sample
        })
    except Exception as e:
        return jsonify({'success': False, 'message': f'连接测试失败: {str(e)}', 'protocol': protocol})


@api_bp.route('/devices/protocols', methods=['GET'])
def get_supported_protocols():
    """获取系统支持的协议列表"""
    protocols = [
        {'id': 'modbus_tcp', 'name': 'Modbus TCP', 'description': '工业以太网标准协议，适用于PLC/仪表/传感器', 'default_port': 502, 'requires': ['host', 'port', 'slave_id', 'registers']},
        {'id': 'modbus_rtu', 'name': 'Modbus RTU', 'description': '串口通信协议，适用于传统工业设备', 'default_port': None, 'requires': ['serial_port', 'baudrate', 'slave_id', 'registers']},
        {'id': 'opcua', 'name': 'OPC UA', 'description': '工业4.0标准协议，面向对象，内置安全认证', 'default_port': 4840, 'requires': ['endpoint', 'nodes']},
        {'id': 'mqtt', 'name': 'MQTT', 'description': '物联网消息协议，适合无线传感器和IoT设备', 'default_port': 1883, 'requires': ['host', 'port', 'topics']},
        {'id': 'rest', 'name': 'REST HTTP', 'description': 'HTTP接口，适合智能网关和云平台对接', 'default_port': 80, 'requires': ['base_url', 'endpoints']},
    ]
    return jsonify({'protocols': protocols, 'summary': current_app.device_manager.get_protocol_summary()})


@api_bp.route('/devices/templates', methods=['GET'])
def get_device_templates():
    """获取设备模板列表"""
    templates = [
        {'id': 'temperature_sensor', 'name': '温度传感器', 'description': '常见温度传感器模板', 'protocol': 'modbus_tcp', 'port': 502,
         'registers': [
             {'name': 'temperature', 'description': '温度值', 'address': 0, 'length': 1, 'data_type': 'float32', 'scale': 0.1, 'unit': '°C'},
             {'name': 'humidity', 'description': '湿度值', 'address': 2, 'length': 1, 'data_type': 'float32', 'scale': 0.1, 'unit': '%RH'}
         ]},
        {'id': 'pressure_sensor', 'name': '压力传感器', 'description': '管道压力监测传感器', 'protocol': 'modbus_tcp', 'port': 502,
         'registers': [
             {'name': 'pressure', 'description': '压力值', 'address': 0, 'length': 1, 'data_type': 'float32', 'scale': 0.01, 'unit': 'MPa'},
             {'name': 'temperature', 'description': '介质温度', 'address': 2, 'length': 1, 'data_type': 'float32', 'scale': 0.1, 'unit': '°C'}
         ]},
        {'id': 'power_meter', 'name': '电力仪表', 'description': '多功能电力监测仪表', 'protocol': 'modbus_tcp', 'port': 502,
         'registers': [
             {'name': 'voltage', 'description': '电压', 'address': 0, 'length': 1, 'data_type': 'float32', 'scale': 0.1, 'unit': 'V'},
             {'name': 'current', 'description': '电流', 'address': 2, 'length': 1, 'data_type': 'float32', 'scale': 0.01, 'unit': 'A'},
             {'name': 'power', 'description': '有功功率', 'address': 4, 'length': 1, 'data_type': 'float32', 'scale': 0.001, 'unit': 'kW'},
             {'name': 'energy', 'description': '累计电量', 'address': 6, 'length': 2, 'data_type': 'float64', 'scale': 0.01, 'unit': 'kWh'}
         ]},
        {'id': 'flow_meter', 'name': '流量计', 'description': '管道流量监测仪表', 'protocol': 'modbus_tcp', 'port': 502,
         'registers': [
             {'name': 'flow_rate', 'description': '瞬时流量', 'address': 0, 'length': 1, 'data_type': 'float32', 'scale': 0.01, 'unit': 'm³/h'},
             {'name': 'total_flow', 'description': '累计流量', 'address': 2, 'length': 2, 'data_type': 'float64', 'scale': 0.01, 'unit': 'm³'},
             {'name': 'temperature', 'description': '介质温度', 'address': 4, 'length': 1, 'data_type': 'float32', 'scale': 0.1, 'unit': '°C'}
         ]},
        {'id': 'inverter', 'name': '变频器', 'description': 'ABB/西门子等变频器', 'protocol': 'modbus_tcp', 'port': 502,
         'registers': [
             {'name': 'frequency', 'description': '输出频率', 'address': 0, 'length': 1, 'data_type': 'float32', 'scale': 0.01, 'unit': 'Hz'},
             {'name': 'current', 'description': '输出电流', 'address': 2, 'length': 1, 'data_type': 'float32', 'scale': 0.01, 'unit': 'A'},
             {'name': 'voltage', 'description': '输出电压', 'address': 4, 'length': 1, 'data_type': 'float32', 'scale': 0.1, 'unit': 'V'},
             {'name': 'status', 'description': '运行状态', 'address': 100, 'length': 1, 'data_type': 'uint16', 'scale': 1, 'unit': ''}
         ]},
        {'id': 'plc', 'name': 'PLC控制器', 'description': '西门子/三菱等PLC', 'protocol': 'modbus_tcp', 'port': 502,
         'registers': [
             {'name': 'input_1', 'description': '输入点1', 'address': 0, 'length': 1, 'data_type': 'uint16', 'scale': 1, 'unit': ''},
             {'name': 'input_2', 'description': '输入点2', 'address': 1, 'length': 1, 'data_type': 'uint16', 'scale': 1, 'unit': ''},
             {'name': 'output_1', 'description': '输出点1', 'address': 100, 'length': 1, 'data_type': 'uint16', 'scale': 1, 'unit': ''},
             {'name': 'output_2', 'description': '输出点2', 'address': 101, 'length': 1, 'data_type': 'uint16', 'scale': 1, 'unit': ''}
         ]},
        {'id': 'opcua_plc', 'name': 'OPC UA PLC控制器', 'description': '支持OPC UA的PLC（如西门子S7-1500）', 'protocol': 'opcua',
         'endpoint': 'opc.tcp://192.168.1.50:4840',
         'nodes': [
             {'node_id': 'ns=3;s=Temperature', 'name': 'temperature', 'description': '温度值', 'unit': '°C'},
             {'node_id': 'ns=3;s=Pressure', 'name': 'pressure', 'description': '压力值', 'unit': 'MPa'},
             {'node_id': 'ns=3;s=MotorSpeed', 'name': 'motor_speed', 'description': '电机转速', 'unit': 'RPM'},
             {'node_id': 'ns=3;s=RunningStatus', 'name': 'running_status', 'description': '运行状态', 'unit': ''}
         ]},
        {'id': 'mqtt_iot_sensor', 'name': 'MQTT IoT传感器', 'description': '无线环境监测节点（MQTT协议）', 'protocol': 'mqtt', 'port': 1883,
         'topics': [
             {'topic': 'factory/workshop_a/temperature', 'name': 'temperature', 'unit': '°C', 'json_path': 'value'},
             {'topic': 'factory/workshop_a/humidity', 'name': 'humidity', 'unit': '%RH', 'json_path': 'value'},
             {'topic': 'factory/workshop_a/co2', 'name': 'co2', 'unit': 'ppm', 'json_path': 'value'}
         ]},
        {'id': 'rest_gateway', 'name': 'REST智能网关', 'description': '产线数据网关（HTTP接口）', 'protocol': 'rest',
         'base_url': 'http://192.168.1.250/api',
         'endpoints': [
             {'name': 'temperature', 'path': '/sensors/temp', 'method': 'GET', 'json_path': 'data.value', 'unit': '°C'},
             {'name': 'humidity', 'path': '/sensors/humi', 'method': 'GET', 'json_path': 'data.value', 'unit': '%RH'},
             {'name': 'production_count', 'path': '/production/count', 'method': 'GET', 'json_path': 'data.total', 'unit': '个'}
         ]},
    ]
    return jsonify({'templates': templates})


# ==================== 设备控制API ====================

@api_bp.route('/devices/<device_id>/write-register', methods=['POST'])
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
        _get_auth_manager().log_operation(
            operator, 'write_register',
            f'设备 {device_id} 写入寄存器 address={address} value={value}')
        return jsonify({'success': True, 'message': f'写入成功: 地址={address}, 值={value}'})
    return jsonify({'success': False, 'message': '写入失败，请检查设备连接和寄存器地址'}), 400


@api_bp.route('/devices/<device_id>/write-coil', methods=['POST'])
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
        _get_auth_manager().log_operation(
            operator, 'write_coil',
            f'设备 {device_id} 写入线圈 address={address} value={value}')
        return jsonify({'success': True, 'message': f'写入成功: 地址={address}, 值={value}'})
    return jsonify({'success': False, 'message': '写入失败，请检查设备连接和线圈地址'}), 400


@api_bp.route('/control/logs', methods=['GET'])
@_require_auth
def get_control_logs():
    """获取控制操作日志（含安全审计）"""
    limit = request.args.get('limit', 50, type=int)
    logs = _get_auth_manager().get_operation_logs(limit=limit)
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

@api_bp.route('/control/estop', methods=['POST'])
@_require_auth
def trigger_estop():
    """紧急停机（最高优先级，任何角色均可触发）"""
    data = request.get_json() or {}
    reason = data.get('reason', '操作员手动触发紧急停机')
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return jsonify({'error': '设备控制安全模块未启用'}), 503
    result = device_control.trigger_emergency_stop(reason)
    _get_auth_manager().log_operation(
        request.current_user['username'], 'emergency_stop', f'紧急停机: {reason}')
    return jsonify(result)


@api_bp.route('/control/estop/reset', methods=['POST'])
@_require_engineer
def reset_estop():
    """解除紧急停机（需工程师权限）"""
    operator = request.current_user['username']
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return jsonify({'error': '设备控制安全模块未启用'}), 503
    result = device_control.reset_emergency_stop(operator)
    _get_auth_manager().log_operation(operator, 'estop_reset', '解除紧急停机')
    return jsonify(result)


@api_bp.route('/control/estop/status', methods=['GET'])
@_require_auth
def get_estop_status():
    """获取紧急停机状态"""
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return jsonify({'active': False})
    return jsonify(device_control.get_estop_status())


@api_bp.route('/control/interlocks', methods=['GET'])
@_require_auth
def get_interlocks():
    """获取所有安全联锁状态"""
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return jsonify({'rules': {}})
    return jsonify(device_control.get_interlock_status())


@api_bp.route('/control/interlocks/<rule_id>/bypass', methods=['POST'])
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


@api_bp.route('/control/interlocks/<rule_id>/restore', methods=['POST'])
@_require_engineer
def restore_interlock(rule_id):
    """恢复联锁"""
    operator = request.current_user['username']
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return jsonify({'error': '设备控制安全模块未启用'}), 503
    success = device_control.restore_interlock(rule_id, operator)
    return jsonify({'success': success, 'message': f'联锁 {rule_id} 已恢复' if success else '恢复失败'})


@api_bp.route('/control/health', methods=['GET'])
@_require_auth
def get_device_health():
    """获取所有设备健康状态"""
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return jsonify({'devices': {}})
    return jsonify(device_control.get_device_health_summary())


@api_bp.route('/control/audit', methods=['GET'])
@_require_auth
def get_audit_log():
    """获取操作审计日志"""
    limit = request.args.get('limit', 100, type=int)
    action_filter = request.args.get('action')
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return jsonify({'logs': []})
    return jsonify({'logs': device_control.get_audit_log(limit, action_filter)})


@api_bp.route('/control/status', methods=['GET'])
@_require_auth
def get_control_safety_status():
    """获取设备控制安全系统完整状态"""
    device_control = getattr(current_app, 'device_control', None)
    if not device_control:
        return jsonify({'enabled': False})
    return jsonify({'enabled': True, **device_control.get_full_status()})


# ==================== 数据查询API ====================

@api_bp.route('/data/realtime', methods=['GET'])
def get_realtime_data():
    """获取实时数据"""
    device_id = request.args.get('device_id')
    limit = request.args.get('limit', 100, type=int)
    return jsonify({'data': current_app.database.get_realtime_data(device_id=device_id, limit=limit)})


@api_bp.route('/data/latest/<device_id>', methods=['GET'])
def get_latest_data(device_id):
    """获取设备最新数据"""
    register_name = request.args.get('register_name')
    data = current_app.database.get_latest_data(device_id=device_id, register_name=register_name)
    if data:
        return jsonify(data)
    return jsonify({'error': '没有数据'}), 404


@api_bp.route('/data/history/<device_id>/<register_name>', methods=['GET'])
def get_history_data(device_id, register_name):
    """获取历史数据"""
    start_time = request.args.get('start_time')
    end_time = request.args.get('end_time')
    interval = request.args.get('interval', '1min')
    
    start_time = datetime.fromisoformat(start_time) if start_time else datetime.now() - timedelta(hours=1)
    end_time = datetime.fromisoformat(end_time) if end_time else datetime.now()
    
    data = current_app.database.get_history_data(
        device_id=device_id, register_name=register_name,
        start_time=start_time, end_time=end_time, interval=interval)
    return jsonify({'data': data})


# ==================== 报警相关API ====================

@api_bp.route('/alarms', methods=['GET'])
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


@api_bp.route('/alarms/active', methods=['GET'])
def get_active_alarms():
    """获取活动报警"""
    return jsonify({'alarms': current_app.alarm_manager.get_active_alarms()})


@api_bp.route('/alarms/<alarm_id>/acknowledge', methods=['POST'])
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


@api_bp.route('/alarms/statistics', methods=['GET'])
def get_alarm_statistics():
    """获取报警统计（含声光输出+广播系统状态）"""
    return jsonify(current_app.alarm_manager.get_alarm_statistics())


# ==================== 报警输出控制API ====================

@api_bp.route('/alarm-output/status', methods=['GET'])
@_require_auth
def get_alarm_output_status():
    """获取声光报警器和广播系统状态"""
    result = {}
    alarm_manager = current_app.alarm_manager
    if alarm_manager.alarm_output:
        result['alarm_output'] = alarm_manager.alarm_output.get_status()
    if alarm_manager.broadcast_system:
        result['broadcast'] = alarm_manager.broadcast_system.get_status()
    return jsonify(result)


@api_bp.route('/alarm-output/acknowledge', methods=['POST'])
@_require_auth
def alarm_output_acknowledge():
    """消音 — 关闭蜂鸣器，报警灯保持闪烁"""
    alarm_manager = current_app.alarm_manager
    if alarm_manager.alarm_output:
        alarm_manager.alarm_output.acknowledge()
        _get_auth_manager().log_operation(
            request.current_user['username'], 'alarm_acknowledge', '声光报警消音')
        return jsonify({'success': True, 'message': '已消音，指示灯保持'})
    return jsonify({'success': False, 'message': '声光报警输出未启用'})


@api_bp.route('/alarm-output/reset', methods=['POST'])
@_require_auth
def alarm_output_reset():
    """复位 — 全部清零，恢复绿灯正常状态"""
    alarm_manager = current_app.alarm_manager
    alarm_manager.reset_alarm()
    _get_auth_manager().log_operation(
        request.current_user['username'], 'alarm_reset', '报警输出复位')
    return jsonify({'success': True, 'message': '报警输出已复位（绿灯正常）'})


@api_bp.route('/alarm-output/manual', methods=['POST'])
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
    _get_auth_manager().log_operation(
        request.current_user['username'], 'alarm_manual',
        f'手动控制报警灯: {data}')
    return jsonify({
        'success': result.get('success', True),
        'state': result.get('state', {}),
        'message': '手动控制指令已发送'
    })


@api_bp.route('/broadcast/speak', methods=['POST'])
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
    _get_auth_manager().log_operation(
        request.current_user['username'], 'broadcast_speak',
        f'广播喊话: {data["text"][:50]}')
    return jsonify(result)


@api_bp.route('/broadcast/areas', methods=['GET'])
@_require_auth
def get_broadcast_areas():
    """获取广播区域列表"""
    alarm_manager = current_app.alarm_manager
    if not alarm_manager.broadcast_system:
        return jsonify({'areas': []})
    return jsonify({'areas': alarm_manager.broadcast_system.get_areas()})


@api_bp.route('/broadcast/history', methods=['GET'])
@_require_auth
def get_broadcast_history():
    """获取广播历史"""
    limit = request.args.get('limit', 50, type=int)
    alarm_manager = current_app.alarm_manager
    if not alarm_manager.broadcast_system:
        return jsonify({'history': []})
    return jsonify({'history': alarm_manager.broadcast_system.get_history(limit=limit)})


# ==================== 系统信息API ====================

@api_bp.route('/system/status', methods=['GET'])
def get_system_status():
    """获取系统状态"""
    start_time = getattr(current_app, 'system_start_time', None)
    uptime_seconds = (datetime.now() - start_time).total_seconds() if start_time else 0
    
    return jsonify({
        'database': current_app.database.get_database_stats(),
        'devices': current_app.device_manager.get_all_status(),
        'collector': current_app.data_collector.get_stats(),
        'alarms': current_app.alarm_manager.get_alarm_statistics(),
        'uptime_seconds': uptime_seconds,
        'start_time': start_time.isoformat() if start_time else None,
        'simulation_mode': current_app.device_manager.simulation_mode
    })


@api_bp.route('/system/database', methods=['GET'])
def get_database_stats():
    """获取数据库统计"""
    return jsonify(current_app.database.get_database_stats())


@api_bp.route('/system/simulation-mode', methods=['GET'])
def get_simulation_mode():
    """获取当前模拟/真实模式状态"""
    return jsonify({
        'simulation_mode': current_app.device_manager.simulation_mode
    })


@api_bp.route('/system/simulation-mode', methods=['POST'])
@role_required('admin', 'engineer')
def toggle_simulation_mode():
    """切换模拟/真实模式（需要重启服务生效）"""
    data = request.get_json() or {}
    new_mode = data.get('simulation_mode')
    
    if new_mode is None:
        return jsonify({'success': False, 'message': '缺少simulation_mode参数'}), 400
    
    # 注意：运行时切换模式需要重启所有客户端连接
    # 这里只更新配置文件，提示用户重启
    from pathlib import Path
    import yaml
    
    config_path = Path('配置/system.yaml')
    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}
    
    if 'system' not in config:
        config['system'] = {}
    config['system']['simulation_mode'] = bool(new_mode)
    
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
    
    return jsonify({
        'success': True,
        'message': f'模式已切换为{"模拟" if new_mode else "真实"}模式，重启服务后生效',
        'simulation_mode': bool(new_mode),
        'restart_required': True
    })


# ==================== 数据导出API ====================

@api_bp.route('/export/device/<device_id>', methods=['POST'])
def export_device_data(device_id):
    """导出设备数据"""
    from 存储层.data_export import DataExport
    
    data = request.get_json() or {}
    start_time_str = data.get('start_time')
    end_time_str = data.get('end_time')
    
    if not start_time_str or not end_time_str:
        return jsonify({'success': False, 'message': '缺少start_time或end_time参数'}), 400
    
    exporter = DataExport()
    filepath = exporter.export_device_data(
        database=current_app.database,
        device_id=device_id,
        start_time=datetime.fromisoformat(start_time_str),
        end_time=datetime.fromisoformat(end_time_str),
        format=data.get('format', 'csv')
    )
    
    if filepath:
        return jsonify({'success': True, 'filepath': filepath})
    return jsonify({'success': False, 'message': '导出失败'}), 500


@api_bp.route('/export/alarms', methods=['POST'])
def export_alarms():
    """导出报警记录"""
    from 存储层.data_export import DataExport
    
    data = request.get_json() or {}
    exporter = DataExport()
    filepath = exporter.export_alarm_records(
        database=current_app.database,
        start_time=datetime.fromisoformat(data['start_time']) if data.get('start_time') else None,
        end_time=datetime.fromisoformat(data['end_time']) if data.get('end_time') else None,
        format=data.get('format', 'csv')
    )
    
    if filepath:
        return jsonify({'success': True, 'filepath': filepath})
    return jsonify({'success': False, 'message': '没有报警记录可导出'})


# ==================== 系统配置API ====================

@api_bp.route('/config', methods=['GET'])
def get_config():
    """获取系统配置"""
    config = _load_yaml_config('配置/system.yaml')
    if not config:
        return jsonify({'error': '配置文件不存在'}), 404
    return jsonify({'config': config})


@api_bp.route('/config', methods=['PUT'])
@_require_engineer
def update_config():
    """更新系统配置"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '请提供配置数据'}), 400
    
    config = _load_yaml_config('配置/system.yaml')
    
    section = data.get('section')
    if section and section in config:
        config[section].update(data.get('data', {}))
    else:
        config.update(data)
    
    _save_yaml_config('配置/system.yaml', config)
    
    _get_auth_manager().log_operation(
        request.current_user['username'], 'update_config', f"更新系统配置: {section or 'global'}")
    return jsonify({'success': True, 'message': '配置已保存'})


# ==================== 报警规则API ====================

@api_bp.route('/alarm-rules', methods=['GET'])
def get_alarm_rules():
    """获取所有报警规则"""
    config = _load_yaml_config('配置/alarms.yaml')
    return jsonify({'rules': config.get('alarm_rules', []), 'notification': config.get('notification', {})})


@api_bp.route('/alarm-rules', methods=['POST'])
@_require_engineer
def add_alarm_rule():
    """添加报警规则"""
    data = request.get_json()
    if not data or 'id' not in data:
        return jsonify({'error': '请提供规则ID'}), 400
    
    config = _load_yaml_config('配置/alarms.yaml')
    
    rules = config.get('alarm_rules', [])
    if any(r.get('id') == data['id'] for r in rules):
        return jsonify({'error': f'规则 {data["id"]} 已存在'}), 400
    
    rules.append(data)
    config['alarm_rules'] = rules
    
    _save_yaml_config('配置/alarms.yaml', config)
    
    current_app.alarm_manager.rules[data['id']] = data
    _get_auth_manager().log_operation(
        request.current_user['username'], 'add_alarm_rule', f"添加报警规则: {data['id']}")
    return jsonify({'success': True, 'message': f"规则 {data['id']} 已添加"})


@api_bp.route('/alarm-rules/<rule_id>', methods=['PUT'])
@_require_engineer
def update_alarm_rule(rule_id):
    """更新报警规则"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '请提供更新数据'}), 400
    
    config = _load_yaml_config('配置/alarms.yaml')
    
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
    _save_yaml_config('配置/alarms.yaml', config)
    
    current_app.alarm_manager.rules[rule_id] = rules[[r['id'] for r in rules].index(rule_id)]
    _get_auth_manager().log_operation(
        request.current_user['username'], 'update_alarm_rule', f"更新报警规则: {rule_id}")
    return jsonify({'success': True, 'message': f'规则 {rule_id} 已更新'})


@api_bp.route('/alarm-rules/<rule_id>', methods=['DELETE'])
@_require_engineer
def delete_alarm_rule(rule_id):
    """删除报警规则"""
    config = _load_yaml_config('配置/alarms.yaml')
    
    rules = config.get('alarm_rules', [])
    new_rules = [r for r in rules if r.get('id') != rule_id]
    
    if len(new_rules) == len(rules):
        return jsonify({'error': f'规则 {rule_id} 不存在'}), 404
    
    config['alarm_rules'] = new_rules
    _save_yaml_config('配置/alarms.yaml', config)
    
    if rule_id in current_app.alarm_manager.rules:
        del current_app.alarm_manager.rules[rule_id]
    
    _get_auth_manager().log_operation(
        request.current_user['username'], 'delete_alarm_rule', f"删除报警规则: {rule_id}")
    return jsonify({'success': True, 'message': f'规则 {rule_id} 已删除'})


@api_bp.route('/alarm-rules/notification', methods=['PUT'])
@_require_engineer
def update_notification():
    """更新通知设置"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '请提供通知配置'}), 400
    
    config = _load_yaml_config('配置/alarms.yaml')
    
    if 'email' in data:
        config.setdefault('notification', {})['email'] = data['email']
    if 'sound' in data:
        config.setdefault('notification', {})['sound'] = data['sound']
    
    _save_yaml_config('配置/alarms.yaml', config)
    
    _get_auth_manager().log_operation(
        request.current_user['username'], 'update_notification', '更新通知设置')
    return jsonify({'success': True, 'message': '通知设置已保存'})


# ==================== 内部辅助函数 ====================

def _validate_protocol_fields(protocol: str, data: dict):
    """验证协议必填字段，失败抛异常"""
    if protocol in ('modbus_tcp', 'modbus_rtu'):
        for field in ('host', 'port'):
            if field not in data:
                raise ValueError(f'Modbus设备缺少必填字段: {field}')
    elif protocol == 'opcua':
        if 'endpoint' not in data:
            raise ValueError('OPC UA设备缺少必填字段: endpoint')
        if not data.get('nodes'):
            raise ValueError('OPC UA设备缺少节点配置')
    elif protocol == 'mqtt':
        for field in ('host', 'port'):
            if field not in data:
                raise ValueError(f'MQTT设备缺少必填字段: {field}')
        if not data.get('topics'):
            raise ValueError('MQTT设备缺少主题配置')
    elif protocol == 'rest':
        if 'base_url' not in data:
            raise ValueError('REST设备缺少必填字段: base_url')
        if not data.get('endpoints'):
            raise ValueError('REST设备缺少端点配置')


def _build_device_config(protocol: str, data: dict) -> dict:
    """根据协议类型构建设备配置字典"""
    config = {
        'id': data['id'],
        'name': data['name'],
        'description': data.get('description', ''),
        'protocol': protocol,
        'enabled': data.get('enabled', True),
        'collection_interval': int(data.get('collection_interval', 5))
    }
    
    if protocol in ('modbus_tcp', 'modbus_rtu'):
        config['host'] = data['host']
        config['port'] = int(data['port'])
        config['slave_id'] = int(data.get('slave_id', 1))
        config['registers'] = data.get('registers', [])
        if protocol == 'modbus_rtu':
            config['baudrate'] = int(data.get('baudrate', 115200))
    elif protocol == 'opcua':
        config['endpoint'] = data['endpoint']
        config['security_mode'] = data.get('security_mode', 'None')
        if data.get('username'):
            config['username'] = data['username']
            config['password'] = data.get('password', '')
        config['nodes'] = data['nodes']
    elif protocol == 'mqtt':
        config['host'] = data['host']
        config['port'] = int(data['port'])
        if data.get('username'):
            config['username'] = data['username']
            config['password'] = data.get('password', '')
        config['topics'] = data['topics']
    elif protocol == 'rest':
        config['base_url'] = data['base_url']
        config['poll_interval'] = int(data.get('poll_interval', 10))
        auth_type = data.get('auth_type', 'none')
        if auth_type != 'none':
            config['auth_type'] = auth_type
            if auth_type in ('bearer', 'api_key'):
                config['auth_token'] = data.get('auth_token', '')
            elif auth_type == 'basic':
                config['auth_username'] = data.get('auth_username', '')
                config['auth_password'] = data.get('auth_password', '')
        config['endpoints'] = data['endpoints']
    
    return config


def _update_protocol_fields(protocol: str, device_config: dict, data: dict):
    """更新协议专属字段"""
    if protocol in ('modbus_tcp', 'modbus_rtu'):
        for key in ('host', 'port', 'slave_id', 'registers', 'baudrate'):
            if key in data:
                device_config[key] = int(data[key]) if key in ('port', 'slave_id') else data[key]
    elif protocol == 'opcua':
        for key in ('endpoint', 'security_mode', 'username', 'password', 'nodes'):
            if key in data:
                device_config[key] = data[key]
    elif protocol == 'mqtt':
        for key in ('host', 'port', 'username', 'password', 'topics'):
            if key in data:
                device_config[key] = int(data[key]) if key == 'port' else data[key]
    elif protocol == 'rest':
        for key in ('base_url', 'poll_interval', 'auth_type', 'auth_token', 'auth_username', 'auth_password', 'endpoints'):
            if key in data:
                device_config[key] = int(data[key]) if key == 'poll_interval' else data[key]


# ==================== 工业4.0智能层API ====================

@api_bp.route('/industry40/health', methods=['GET'])
@_require_auth
def get_health_scores():
    """获取所有设备健康评分"""
    pm = current_app.predictive_maintenance
    if not pm:
        return jsonify({'error': '预测性维护模块未启用'}), 503
    return jsonify(pm.get_health_scores())


@api_bp.route('/industry40/health/<device_id>', methods=['GET'])
@_require_auth
def get_device_health_by_id(device_id):
    """获取指定设备健康评分"""
    pm = current_app.predictive_maintenance
    if not pm:
        return jsonify({'error': '预测性维护模块未启用'}), 503
    return jsonify(pm.get_device_health(device_id))


@api_bp.route('/industry40/maintenance-alerts', methods=['GET'])
@_require_auth
def get_maintenance_alerts():
    """获取维护建议列表"""
    pm = current_app.predictive_maintenance
    if not pm:
        return jsonify({'error': '预测性维护模块未启用'}), 503
    limit = request.args.get('limit', 50, type=int)
    return jsonify(pm.get_maintenance_alerts(limit))


@api_bp.route('/industry40/trend/<device_id>/<register_name>', methods=['GET'])
@_require_auth
def get_trend_data(device_id, register_name):
    """获取趋势分析数据"""
    pm = current_app.predictive_maintenance
    if not pm:
        return jsonify({'error': '预测性维护模块未启用'}), 503
    return jsonify(pm.get_trend_data(device_id, register_name))


@api_bp.route('/industry40/oee', methods=['GET'])
@_require_auth
def get_all_oee():
    """获取所有设备OEE"""
    oee = current_app.oee_calculator
    if not oee:
        return jsonify({'error': 'OEE模块未启用'}), 503
    return jsonify(oee.get_all_oee())


@api_bp.route('/industry40/oee/<device_id>', methods=['GET'])
@_require_auth
def get_device_oee(device_id):
    """获取指定设备OEE"""
    oee = current_app.oee_calculator
    if not oee:
        return jsonify({'error': 'OEE模块未启用'}), 503
    result = oee.get_device_oee(device_id)
    return jsonify(result if result else {'error': '无数据'})


@api_bp.route('/industry40/spc/<device_id>/<register_name>', methods=['GET'])
@_require_auth
def get_spc_chart(device_id, register_name):
    """获取SPC控制图数据"""
    spc = current_app.spc_analyzer
    if not spc:
        return jsonify({'error': 'SPC模块未启用'}), 503
    chart = spc.get_control_chart(device_id, register_name)
    capability = spc.get_capability(device_id, register_name)
    return jsonify({
        'control_chart': chart,
        'capability': capability,
    })


@api_bp.route('/industry40/energy', methods=['GET'])
@_require_auth
def get_energy_summary():
    """获取能耗汇总"""
    em = current_app.energy_manager
    if not em:
        return jsonify({'error': '能源管理模块未启用'}), 503
    return jsonify(em.get_energy_summary())


@api_bp.route('/industry40/energy/cost', methods=['GET'])
@_require_auth
def get_energy_cost():
    """获取电费分时明细"""
    em = current_app.energy_manager
    if not em:
        return jsonify({'error': '能源管理模块未启用'}), 503
    return jsonify(em.get_energy_cost_breakdown())


@api_bp.route('/industry40/energy/carbon', methods=['GET'])
@_require_auth
def get_carbon_emission():
    """获取碳排放数据"""
    em = current_app.energy_manager
    if not em:
        return jsonify({'error': '能源管理模块未启用'}), 503
    return jsonify(em.get_carbon_emission())


@api_bp.route('/industry40/energy/power', methods=['GET'])
@_require_auth
def get_realtime_power():
    """获取实时功率"""
    em = current_app.energy_manager
    if not em:
        return jsonify({'error': '能源管理模块未启用'}), 503
    return jsonify({
        'total_power_kw': em.get_total_power(),
        'devices': em.get_realtime_power(),
    })


@api_bp.route('/industry40/edge/status', methods=['GET'])
@_require_auth
def get_edge_status():
    """获取边缘决策引擎状态"""
    edge = current_app.edge_decision
    if not edge:
        return jsonify({'error': '边缘决策引擎未启用'}), 503
    return jsonify(edge.get_status())


@api_bp.route('/industry40/edge/rules', methods=['GET'])
@_require_auth
def get_edge_rules():
    """获取边缘决策规则"""
    edge = current_app.edge_decision
    if not edge:
        return jsonify({'error': '边缘决策引擎未启用'}), 503
    return jsonify(edge.get_rules())


@api_bp.route('/industry40/edge/log', methods=['GET'])
@_require_auth
def get_edge_log():
    """获取决策日志"""
    edge = current_app.edge_decision
    if not edge:
        return jsonify({'error': '边缘决策引擎未启用'}), 503
    limit = request.args.get('limit', 50, type=int)
    return jsonify(edge.get_decision_log(limit))


@api_bp.route('/industry40/overview', methods=['GET'])
@_require_auth
def get_industry40_overview():
    """工业4.0总览数据"""
    result = {
        'predictive_maintenance': None,
        'oee': None,
        'energy': None,
        'edge_decision': None,
    }
    
    pm = current_app.predictive_maintenance
    if pm:
        scores = pm.get_health_scores()
        if scores:
            avg_health = sum(s.get('health_score', 0) for s in scores.values()) / len(scores)
            alerts = pm.get_maintenance_alerts(5)
            result['predictive_maintenance'] = {
                'device_count': len(scores),
                'avg_health_score': round(avg_health, 1),
                'recent_alerts': alerts,
            }
    
    oee_calc = current_app.oee_calculator
    if oee_calc:
        all_oee = oee_calc.get_all_oee()
        if all_oee:
            avg_oee = sum(o.get('oee_percent', 0) for o in all_oee.values()) / len(all_oee)
            result['oee'] = {
                'device_count': len(all_oee),
                'avg_oee_percent': round(avg_oee, 1),
                'devices': all_oee,
            }
    
    em = current_app.energy_manager
    if em:
        result['energy'] = em.get_energy_summary()
        result['energy']['total_power_kw'] = em.get_total_power()
    
    edge = current_app.edge_decision
    if edge:
        result['edge_decision'] = edge.get_status()
    
    return jsonify(result)
