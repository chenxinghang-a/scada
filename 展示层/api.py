"""
REST API模块
提供数据查询和设备控制接口
"""

from flask import Blueprint, jsonify, request, current_app
from datetime import datetime, timedelta

api_bp = Blueprint('api', __name__)


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
    
    auth_manager = current_app.auth_manager
    ip_address = request.remote_addr
    
    result = auth_manager.login(username, password, ip_address)
    
    if result['success']:
        return jsonify(result)
    else:
        return jsonify(result), 401


@api_bp.route('/auth/register', methods=['POST'])
def register():
    """用户注册（仅管理员可操作）"""
    from 用户层.auth import jwt_required, role_required
    
    # 检查是否有管理员token
    token = None
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        token = auth_header[7:]
    
    auth_manager = current_app.auth_manager
    
    # 如果没有token，检查是否是第一个用户（允许直接注册admin）
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
    
    if result['success']:
        return jsonify(result), 201
    else:
        return jsonify(result), 400


@api_bp.route('/auth/verify', methods=['GET'])
def verify_token():
    """验证令牌有效性"""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'valid': False}), 401
    
    token = auth_header[7:]
    auth_manager = current_app.auth_manager
    user = auth_manager.verify_token(token)
    
    if user:
        return jsonify({'valid': True, 'user': user})
    else:
        return jsonify({'valid': False}), 401


@api_bp.route('/auth/refresh', methods=['POST'])
def refresh_token():
    """刷新令牌"""
    data = request.get_json()
    refresh_token = data.get('refresh_token')
    
    if not refresh_token:
        return jsonify({'success': False, 'message': '请提供刷新令牌'}), 400
    
    auth_manager = current_app.auth_manager
    result = auth_manager.refresh_token(refresh_token)
    
    if result:
        return jsonify(result)
    else:
        return jsonify({'success': False, 'message': '刷新令牌无效'}), 401


@api_bp.route('/auth/change-password', methods=['POST'])
def change_password():
    """修改密码"""
    from 用户层.auth import jwt_required
    
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'success': False, 'message': '未认证'}), 401
    
    token = auth_header[7:]
    auth_manager = current_app.auth_manager
    user = auth_manager.verify_token(token)
    
    if not user:
        return jsonify({'success': False, 'message': '令牌无效'}), 401
    
    data = request.get_json()
    result = auth_manager.change_password(
        username=user['username'],
        old_password=data.get('old_password', ''),
        new_password=data.get('new_password', '')
    )
    
    return jsonify(result)


@api_bp.route('/auth/users', methods=['GET'])
def get_users():
    """获取用户列表（仅管理员）"""
    from 用户层.auth import jwt_required, role_required
    
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'error': '未认证'}), 401
    
    token = auth_header[7:]
    auth_manager = current_app.auth_manager
    user = auth_manager.verify_token(token)
    
    if not user or user['role'] != 'admin':
        return jsonify({'error': '需要管理员权限'}), 403
    
    users = auth_manager.get_users()
    return jsonify({'users': users})


@api_bp.route('/auth/users/<username>', methods=['PUT'])
def update_user(username):
    """更新用户信息（仅管理员）"""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'error': '未认证'}), 401
    
    token = auth_header[7:]
    auth_manager = current_app.auth_manager
    user = auth_manager.verify_token(token)
    
    if not user or user['role'] != 'admin':
        return jsonify({'error': '需要管理员权限'}), 403
    
    data = request.get_json()
    result = auth_manager.update_user(username, **data)
    return jsonify(result)


@api_bp.route('/auth/users/<username>', methods=['DELETE'])
def delete_user(username):
    """删除用户（仅管理员）"""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'error': '未认证'}), 401
    
    token = auth_header[7:]
    auth_manager = current_app.auth_manager
    user = auth_manager.verify_token(token)
    
    if not user or user['role'] != 'admin':
        return jsonify({'error': '需要管理员权限'}), 403
    
    result = auth_manager.delete_user(username)
    return jsonify(result)


@api_bp.route('/auth/logs', methods=['GET'])
def get_operation_logs():
    """获取操作日志（仅管理员）"""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'error': '未认证'}), 401
    
    token = auth_header[7:]
    auth_manager = current_app.auth_manager
    user = auth_manager.verify_token(token)
    
    if not user or user['role'] != 'admin':
        return jsonify({'error': '需要管理员权限'}), 403
    
    username = request.args.get('username')
    limit = request.args.get('limit', 100, type=int)
    
    logs = auth_manager.get_operation_logs(username=username, limit=limit)
    return jsonify({'logs': logs})


# ==================== 设备管理API ====================

@api_bp.route('/devices', methods=['GET'])
def get_devices():
    """获取所有设备列表"""
    device_manager = current_app.device_manager
    devices = device_manager.get_all_status()
    return jsonify({'devices': devices})


@api_bp.route('/devices/<device_id>', methods=['GET'])
def get_device(device_id):
    """获取单个设备详情"""
    device_manager = current_app.device_manager
    status = device_manager.get_device_status(device_id)
    
    if 'error' in status:
        return jsonify(status), 404
    
    return jsonify(status)


@api_bp.route('/devices', methods=['POST'])
def add_device():
    """添加新设备"""
    from 用户层.auth import jwt_required, role_required
    
    # 权限检查
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'error': '未认证'}), 401
    
    token = auth_header[7:]
    auth_manager = current_app.auth_manager
    user = auth_manager.verify_token(token)
    
    if not user or user['role'] not in ['admin', 'engineer']:
        return jsonify({'error': '需要管理员或工程师权限'}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({'error': '请提供设备配置'}), 400
    
    # 验证必填字段
    required_fields = ['id', 'name', 'host', 'port']
    for field in required_fields:
        if field not in data:
            return jsonify({'error': f'缺少必填字段: {field}'}), 400
    
    # 设置默认值
    device_config = {
        'id': data['id'],
        'name': data['name'],
        'description': data.get('description', ''),
        'protocol': data.get('protocol', 'modbus_tcp'),
        'host': data['host'],
        'port': int(data['port']),
        'slave_id': int(data.get('slave_id', 1)),
        'enabled': data.get('enabled', True),
        'collection_interval': int(data.get('collection_interval', 5)),
        'registers': data.get('registers', [])
    }
    
    device_manager = current_app.device_manager
    success = device_manager.add_device(device_config)
    
    if success:
        # 记录操作日志
        auth_manager.log_operation(user['username'], 'add_device', f"添加设备: {data['id']}")
        return jsonify({'success': True, 'message': f"设备 {data['id']} 添加成功"})
    else:
        return jsonify({'success': False, 'message': '添加失败'}), 400


@api_bp.route('/devices/<device_id>', methods=['PUT'])
def update_device(device_id):
    """更新设备配置"""
    # 权限检查
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'error': '未认证'}), 401
    
    token = auth_header[7:]
    auth_manager = current_app.auth_manager
    user = auth_manager.verify_token(token)
    
    if not user or user['role'] not in ['admin', 'engineer']:
        return jsonify({'error': '需要管理员或工程师权限'}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({'error': '请提供设备配置'}), 400
    
    device_manager = current_app.device_manager
    
    # 检查设备是否存在
    if device_id not in device_manager.devices:
        return jsonify({'error': f'设备 {device_id} 不存在'}), 404
    
    # 更新配置
    device_config = device_manager.devices[device_id]
    for key in ['name', 'description', 'host', 'port', 'slave_id', 'enabled', 'collection_interval', 'registers']:
        if key in data:
            if key in ['port', 'slave_id', 'collection_interval']:
                device_config[key] = int(data[key])
            else:
                device_config[key] = data[key]
    
    # 保存配置
    device_manager._save_config()
    
    # 记录操作日志
    auth_manager.log_operation(user['username'], 'update_device', f"更新设备: {device_id}")
    
    return jsonify({'success': True, 'message': f'设备 {device_id} 更新成功'})


@api_bp.route('/devices/<device_id>', methods=['DELETE'])
def delete_device(device_id):
    """删除设备"""
    # 权限检查
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'error': '未认证'}), 401
    
    token = auth_header[7:]
    auth_manager = current_app.auth_manager
    user = auth_manager.verify_token(token)
    
    if not user or user['role'] not in ['admin', 'engineer']:
        return jsonify({'error': '需要管理员或工程师权限'}), 403
    
    device_manager = current_app.device_manager
    success = device_manager.remove_device(device_id)
    
    if success:
        # 记录操作日志
        auth_manager.log_operation(user['username'], 'delete_device', f"删除设备: {device_id}")
        return jsonify({'success': True, 'message': f'设备 {device_id} 已删除'})
    else:
        return jsonify({'success': False, 'message': '删除失败'}), 400


@api_bp.route('/devices/<device_id>/test', methods=['POST'])
def test_device_connection(device_id):
    """测试设备连接"""
    device_manager = current_app.device_manager
    device_config = device_manager.devices.get(device_id)
    
    if not device_config:
        return jsonify({'success': False, 'message': f'设备 {device_id} 不存在'}), 404
    
    # 尝试连接
    try:
        from 采集层.modbus_client import ModbusClient
        
        # 创建临时客户端进行测试
        test_client = ModbusClient(device_config)
        connected = test_client.connect()
        
        if connected:
            # 尝试读取一个寄存器
            registers = device_config.get('registers', [])
            if registers:
                reg = registers[0]
                result = test_client.read_register(
                    reg['address'],
                    reg.get('length', 1)
                )
                test_client.disconnect()
                
                if result is not None:
                    return jsonify({
                        'success': True,
                        'message': '连接成功，数据读取正常',
                        'sample_data': {
                            'register': reg['name'],
                            'value': result
                        }
                    })
                else:
                    return jsonify({
                        'success': True,
                        'message': '连接成功，但读取数据失败',
                        'warning': '请检查寄存器地址是否正确'
                    })
            else:
                test_client.disconnect()
                return jsonify({
                    'success': True,
                    'message': '连接成功（未配置寄存器）'
                })
        else:
            return jsonify({
                'success': False,
                'message': '连接失败，请检查IP地址和端口'
            })
            
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'连接测试失败: {str(e)}'
        })


@api_bp.route('/devices/templates', methods=['GET'])
def get_device_templates():
    """获取设备模板列表"""
    templates = [
        {
            'id': 'temperature_sensor',
            'name': '温度传感器',
            'description': '常见温度传感器模板',
            'protocol': 'modbus_tcp',
            'port': 502,
            'registers': [
                {'name': 'temperature', 'description': '温度值', 'address': 0, 'length': 1, 'data_type': 'float32', 'scale': 0.1, 'unit': '°C'},
                {'name': 'humidity', 'description': '湿度值', 'address': 2, 'length': 1, 'data_type': 'float32', 'scale': 0.1, 'unit': '%RH'}
            ]
        },
        {
            'id': 'pressure_sensor',
            'name': '压力传感器',
            'description': '管道压力监测传感器',
            'protocol': 'modbus_tcp',
            'port': 502,
            'registers': [
                {'name': 'pressure', 'description': '压力值', 'address': 0, 'length': 1, 'data_type': 'float32', 'scale': 0.01, 'unit': 'MPa'},
                {'name': 'temperature', 'description': '介质温度', 'address': 2, 'length': 1, 'data_type': 'float32', 'scale': 0.1, 'unit': '°C'}
            ]
        },
        {
            'id': 'power_meter',
            'name': '电力仪表',
            'description': '多功能电力监测仪表',
            'protocol': 'modbus_tcp',
            'port': 502,
            'registers': [
                {'name': 'voltage', 'description': '电压', 'address': 0, 'length': 1, 'data_type': 'float32', 'scale': 0.1, 'unit': 'V'},
                {'name': 'current', 'description': '电流', 'address': 2, 'length': 1, 'data_type': 'float32', 'scale': 0.01, 'unit': 'A'},
                {'name': 'power', 'description': '有功功率', 'address': 4, 'length': 1, 'data_type': 'float32', 'scale': 0.001, 'unit': 'kW'},
                {'name': 'energy', 'description': '累计电量', 'address': 6, 'length': 2, 'data_type': 'float64', 'scale': 0.01, 'unit': 'kWh'}
            ]
        },
        {
            'id': 'flow_meter',
            'name': '流量计',
            'description': '管道流量监测仪表',
            'protocol': 'modbus_tcp',
            'port': 502,
            'registers': [
                {'name': 'flow_rate', 'description': '瞬时流量', 'address': 0, 'length': 1, 'data_type': 'float32', 'scale': 0.01, 'unit': 'm³/h'},
                {'name': 'total_flow', 'description': '累计流量', 'address': 2, 'length': 2, 'data_type': 'float64', 'scale': 0.01, 'unit': 'm³'},
                {'name': 'temperature', 'description': '介质温度', 'address': 4, 'length': 1, 'data_type': 'float32', 'scale': 0.1, 'unit': '°C'}
            ]
        },
        {
            'id': 'inverter',
            'name': '变频器',
            'description': 'ABB/西门子等变频器',
            'protocol': 'modbus_tcp',
            'port': 502,
            'registers': [
                {'name': 'frequency', 'description': '输出频率', 'address': 0, 'length': 1, 'data_type': 'float32', 'scale': 0.01, 'unit': 'Hz'},
                {'name': 'current', 'description': '输出电流', 'address': 2, 'length': 1, 'data_type': 'float32', 'scale': 0.01, 'unit': 'A'},
                {'name': 'voltage', 'description': '输出电压', 'address': 4, 'length': 1, 'data_type': 'float32', 'scale': 0.1, 'unit': 'V'},
                {'name': 'status', 'description': '运行状态', 'address': 100, 'length': 1, 'data_type': 'uint16', 'scale': 1, 'unit': ''}
            ]
        },
        {
            'id': 'plc',
            'name': 'PLC控制器',
            'description': '西门子/三菱等PLC',
            'protocol': 'modbus_tcp',
            'port': 502,
            'registers': [
                {'name': 'input_1', 'description': '输入点1', 'address': 0, 'length': 1, 'data_type': 'uint16', 'scale': 1, 'unit': ''},
                {'name': 'input_2', 'description': '输入点2', 'address': 1, 'length': 1, 'data_type': 'uint16', 'scale': 1, 'unit': ''},
                {'name': 'output_1', 'description': '输出点1', 'address': 100, 'length': 1, 'data_type': 'uint16', 'scale': 1, 'unit': ''},
                {'name': 'output_2', 'description': '输出点2', 'address': 101, 'length': 1, 'data_type': 'uint16', 'scale': 1, 'unit': ''}
            ]
        }
    ]
    
    return jsonify({'templates': templates})


# ==================== 设备控制API ====================

@api_bp.route('/devices/<device_id>/write-register', methods=['POST'])
def write_register(device_id):
    """写入寄存器"""
    # 权限检查：仅管理员和工程师可操作
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'error': '未认证'}), 401
    
    token = auth_header[7:]
    auth_manager = current_app.auth_manager
    user = auth_manager.verify_token(token)
    
    if not user or user['role'] not in ['admin', 'engineer']:
        return jsonify({'error': '需要管理员或工程师权限'}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({'error': '请提供写入参数'}), 400
    
    address = data.get('address')
    value = data.get('value')
    
    if address is None or value is None:
        return jsonify({'error': '缺少address或value参数'}), 400
    
    device_manager = current_app.device_manager
    client = device_manager.get_client(device_id)
    
    if not client:
        return jsonify({'error': f'设备 {device_id} 不存在'}), 404
    
    if not client.connected:
        return jsonify({'error': f'设备 {device_id} 未连接'}), 400
    
    # 写入寄存器
    success = client.write_single_register(int(address), int(value))
    
    if success:
        # 记录操作日志
        auth_manager._log_operation(
            user['username'], 'write_register',
            f'设备 {device_id} 写入寄存器 address={address} value={value}'
        )
        return jsonify({
            'success': True,
            'message': f'写入成功: 地址={address}, 值={value}'
        })
    else:
        return jsonify({
            'success': False,
            'message': '写入失败，请检查设备连接和寄存器地址'
        }), 400


@api_bp.route('/devices/<device_id>/write-coil', methods=['POST'])
def write_coil(device_id):
    """写入线圈（开关控制）"""
    # 权限检查
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'error': '未认证'}), 401
    
    token = auth_header[7:]
    auth_manager = current_app.auth_manager
    user = auth_manager.verify_token(token)
    
    if not user or user['role'] not in ['admin', 'engineer']:
        return jsonify({'error': '需要管理员或工程师权限'}), 403
    
    data = request.get_json()
    if not data:
        return jsonify({'error': '请提供写入参数'}), 400
    
    address = data.get('address')
    value = data.get('value')
    
    if address is None or value is None:
        return jsonify({'error': '缺少address或value参数'}), 400
    
    device_manager = current_app.device_manager
    client = device_manager.get_client(device_id)
    
    if not client:
        return jsonify({'error': f'设备 {device_id} 不存在'}), 404
    
    if not client.connected:
        return jsonify({'error': f'设备 {device_id} 未连接'}), 400
    
    # 写入线圈
    success = client.write_single_coil(int(address), bool(value))
    
    if success:
        # 记录操作日志
        auth_manager._log_operation(
            user['username'], 'write_coil',
            f'设备 {device_id} 写入线圈 address={address} value={value}'
        )
        return jsonify({
            'success': True,
            'message': f'写入成功: 地址={address}, 值={value}'
        })
    else:
        return jsonify({
            'success': False,
            'message': '写入失败，请检查设备连接和线圈地址'
        }), 400


@api_bp.route('/control/logs', methods=['GET'])
def get_control_logs():
    """获取控制操作日志"""
    auth_header = request.headers.get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        return jsonify({'error': '未认证'}), 401
    
    token = auth_header[7:]
    auth_manager = current_app.auth_manager
    user = auth_manager.verify_token(token)
    
    if not user:
        return jsonify({'error': '令牌无效'}), 401
    
    # 获取控制操作日志
    limit = request.args.get('limit', 50, type=int)
    logs = auth_manager.get_operation_logs(limit=limit)
    
    # 只返回控制相关的日志
    control_logs = [log for log in logs if log.get('action') in ['write_register', 'write_coil']]
    
    return jsonify({'logs': control_logs})


# ==================== 数据查询API ====================

@api_bp.route('/data/realtime', methods=['GET'])
def get_realtime_data():
    """获取实时数据"""
    database = current_app.database
    device_id = request.args.get('device_id')
    limit = request.args.get('limit', 100, type=int)
    
    data = database.get_realtime_data(device_id=device_id, limit=limit)
    
    return jsonify({'data': data})


@api_bp.route('/data/latest/<device_id>', methods=['GET'])
def get_latest_data(device_id):
    """获取设备最新数据"""
    database = current_app.database
    register_name = request.args.get('register_name')
    
    data = database.get_latest_data(device_id=device_id, register_name=register_name)
    
    if data:
        return jsonify(data)
    else:
        return jsonify({'error': '没有数据'}), 404


@api_bp.route('/data/history/<device_id>/<register_name>', methods=['GET'])
def get_history_data(device_id, register_name):
    """获取历史数据"""
    database = current_app.database
    
    # 解析时间参数
    start_time = request.args.get('start_time')
    end_time = request.args.get('end_time')
    interval = request.args.get('interval', '1min')
    
    if start_time:
        start_time = datetime.fromisoformat(start_time)
    else:
        start_time = datetime.now() - timedelta(hours=1)
    
    if end_time:
        end_time = datetime.fromisoformat(end_time)
    else:
        end_time = datetime.now()
    
    data = database.get_history_data(
        device_id=device_id,
        register_name=register_name,
        start_time=start_time,
        end_time=end_time,
        interval=interval
    )
    
    return jsonify({'data': data})


# ==================== 报警相关API ====================

@api_bp.route('/alarms', methods=['GET'])
def get_alarms():
    """获取报警记录"""
    database = current_app.database
    
    device_id = request.args.get('device_id')
    alarm_level = request.args.get('alarm_level')
    acknowledged = request.args.get('acknowledged')
    limit = request.args.get('limit', 100, type=int)
    
    if acknowledged is not None:
        acknowledged = acknowledged.lower() == 'true'
    
    data = database.get_alarm_records(
        device_id=device_id,
        alarm_level=alarm_level,
        acknowledged=acknowledged,
        limit=limit
    )
    
    return jsonify({'alarms': data})


@api_bp.route('/alarms/active', methods=['GET'])
def get_active_alarms():
    """获取活动报警"""
    alarm_manager = current_app.alarm_manager
    alarms = alarm_manager.get_active_alarms()
    
    return jsonify({'alarms': alarms})


@api_bp.route('/alarms/<alarm_id>/acknowledge', methods=['POST'])
def acknowledge_alarm(alarm_id):
    """确认报警"""
    alarm_manager = current_app.alarm_manager
    
    data = request.get_json()
    device_id = data.get('device_id')
    register_name = data.get('register_name')
    acknowledged_by = data.get('acknowledged_by', 'operator')
    
    success = alarm_manager.acknowledge_alarm(
        alarm_id=alarm_id,
        device_id=device_id,
        register_name=register_name,
        acknowledged_by=acknowledged_by
    )
    
    return jsonify({
        'success': success,
        'message': '报警已确认' if success else '确认失败'
    })


@api_bp.route('/alarms/statistics', methods=['GET'])
def get_alarm_statistics():
    """获取报警统计"""
    alarm_manager = current_app.alarm_manager
    stats = alarm_manager.get_alarm_statistics()
    
    return jsonify(stats)


# ==================== 系统信息API ====================

@api_bp.route('/system/status', methods=['GET'])
def get_system_status():
    """获取系统状态"""
    database = current_app.database
    device_manager = current_app.device_manager
    data_collector = current_app.data_collector
    alarm_manager = current_app.alarm_manager
    
    # 计算运行时间
    start_time = getattr(current_app, 'system_start_time', None)
    uptime_seconds = 0
    if start_time:
        uptime_seconds = (datetime.now() - start_time).total_seconds()
    
    return jsonify({
        'database': database.get_database_stats(),
        'devices': device_manager.get_all_status(),
        'collector': data_collector.get_stats(),
        'alarms': alarm_manager.get_alarm_statistics(),
        'uptime_seconds': uptime_seconds,
        'start_time': start_time.isoformat() if start_time else None,
        'simulation_mode': device_manager.simulation_mode
    })


@api_bp.route('/system/database', methods=['GET'])
def get_database_stats():
    """获取数据库统计"""
    database = current_app.database
    stats = database.get_database_stats()
    
    return jsonify(stats)


# ==================== 数据导出API ====================

@api_bp.route('/export/device/<device_id>', methods=['POST'])
def export_device_data(device_id):
    """导出设备数据"""
    from 存储层.data_export import DataExport
    
    database = current_app.database
    data = request.get_json()
    
    start_time = datetime.fromisoformat(data.get('start_time'))
    end_time = datetime.fromisoformat(data.get('end_time'))
    format = data.get('format', 'csv')
    
    exporter = DataExport()
    filepath = exporter.export_device_data(
        database=database,
        device_id=device_id,
        start_time=start_time,
        end_time=end_time,
        format=format
    )
    
    if filepath:
        return jsonify({'success': True, 'filepath': filepath})
    else:
        return jsonify({'success': False, 'message': '导出失败'}), 500


@api_bp.route('/export/alarms', methods=['POST'])
def export_alarms():
    """导出报警记录"""
    from 存储层.data_export import DataExport
    
    database = current_app.database
    data = request.get_json() or {}
    
    start_time = datetime.fromisoformat(data.get('start_time')) if data.get('start_time') else None
    end_time = datetime.fromisoformat(data.get('end_time')) if data.get('end_time') else None
    format = data.get('format', 'csv')
    
    exporter = DataExport()
    filepath = exporter.export_alarm_records(
        database=database,
        start_time=start_time,
        end_time=end_time,
        format=format
    )
    
    if filepath:
        return jsonify({'success': True, 'filepath': filepath})
    else:
        return jsonify({'success': False, 'message': '没有报警记录可导出'})
