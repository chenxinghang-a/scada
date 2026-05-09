"""
设备管理API
设备CRUD/连接测试/协议列表/设备模板
"""

from typing import Any
import logging
from flask import Blueprint, jsonify, request, current_app

from 用户层.auth import jwt_required, role_required
from ._common import get_auth_manager

logger = logging.getLogger(__name__)

devices_bp = Blueprint('api_devices', __name__, url_prefix='/api')

_require_auth = jwt_required
_require_engineer = role_required('admin', 'engineer')


# ==================== 设备管理API ====================

@devices_bp.route('/devices', methods=['GET'])
def get_devices():
    """获取所有设备列表"""
    devices = current_app.device_manager.get_all_status()
    return jsonify({'devices': devices})


@devices_bp.route('/devices/<device_id>', methods=['GET'])
def get_device(device_id):
    """获取单个设备详情"""
    status = current_app.device_manager.get_device_status(device_id)
    if 'error' in status:
        return jsonify(status), 404
    return jsonify(status)


@devices_bp.route('/devices', methods=['POST'])
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
        logger.warning(f"设备配置验证失败: {e}")
        return jsonify({'error': '设备配置验证失败，请检查必填字段'}), 400

    # 构建设备配置
    device_config = _build_device_config(protocol, data)

    success = current_app.device_manager.add_device(device_config)
    if success:
        get_auth_manager().log_operation(
            request.current_user['username'], 'add_device', f"添加设备: {data['id']}")
        # 设备存在 → 启动采集任务 → 数据开始入库
        data_collector = getattr(current_app, 'data_collector', None)
        if data_collector:
            data_collector.start_device_task(data['id'], device_config)
        # 获取设备状态（包含连接状态）
        device_status = current_app.device_manager.get_device_status(data['id'])
        return jsonify({
            'success': True,
            'message': f"设备 {data['id']} 添加成功",
            'connected': device_status.get('connected', False),
            'device': device_status
        })
    else:
        return jsonify({'success': False, 'message': '添加失败'}), 400


@devices_bp.route('/devices/<device_id>', methods=['PUT'])
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
    get_auth_manager().log_operation(
        request.current_user['username'], 'update_device', f"更新设备: {device_id}")
    return jsonify({'success': True, 'message': f'设备 {device_id} 更新成功'})


@devices_bp.route('/devices/<device_id>', methods=['DELETE'])
@_require_engineer
def delete_device(device_id):
    """删除设备"""
    # 第一步：停止采集（先停数据，再删配置）
    data_collector = getattr(current_app, 'data_collector', None)
    if data_collector:
        data_collector.remove_device_task(device_id)

    # 第二步：删除设备配置
    initializer = _get_simulation_initializer()
    if initializer is not None:
        result = initializer.remove_device(device_id)
        if result['success']:
            get_auth_manager().log_operation(
                request.current_user['username'], 'delete_device', f"删除设备: {device_id}")
            return jsonify({'success': True, 'message': f'设备 {device_id} 已删除'})
        # simulation_initializer 失败，回退到 device_manager 直接删除
        success = current_app.device_manager.remove_device(device_id)
        if success:
            get_auth_manager().log_operation(
                request.current_user['username'], 'delete_device', f"删除设备: {device_id}")
            return jsonify({'success': True, 'message': f'设备 {device_id} 已删除'})
        return jsonify(result), 400
    else:
        success = current_app.device_manager.remove_device(device_id)
        if success:
            get_auth_manager().log_operation(
                request.current_user['username'], 'delete_device', f"删除设备: {device_id}")
            return jsonify({'success': True, 'message': f'设备 {device_id} 已删除'})
        return jsonify({'success': False, 'message': f'删除设备 {device_id} 失败'}), 400


@devices_bp.route('/devices/<device_id>/stop', methods=['POST'])
@_require_engineer
def stop_device(device_id):
    """停止设备（仅 mechanical 类型有效）"""
    device_manager = current_app.device_manager
    config = device_manager.devices.get(device_id)
    if not config:
        return jsonify({'error': f'设备 {device_id} 不存在'}), 404

    from 采集层.interfaces import IDeviceManager
    category = IDeviceManager.get_device_category(config)
    if category != 'mechanical':
        return jsonify({'success': False, 'message': f'{category} 类型设备不支持启停操作'}), 400

    result = device_manager.stop_device(device_id)
    if result:
        get_auth_manager().log_operation(request.current_user.get('username', 'system'), 'stop_device', device_id, f'设备停止: {device_id}')
    return jsonify({'success': result, 'message': f'设备 {device_id} 已停止'})


@devices_bp.route('/devices/<device_id>/start', methods=['POST'])
@_require_engineer
def start_device(device_id):
    """启动设备"""
    device_manager = current_app.device_manager
    config = device_manager.devices.get(device_id)
    if not config:
        return jsonify({'error': f'设备 {device_id} 不存在'}), 404

    from 采集层.interfaces import IDeviceManager
    category = IDeviceManager.get_device_category(config)
    if category != 'mechanical':
        return jsonify({'success': False, 'message': f'{category} 类型设备不支持启停操作'}), 400

    result = device_manager.start_device(device_id)
    if result:
        get_auth_manager().log_operation(request.current_user.get('username', 'system'), 'start_device', device_id, f'设备启动: {device_id}')
    return jsonify({'success': result, 'message': f'设备 {device_id} 已启动'})


def _start_device_collection(device_id: str):
    """为已添加的设备启动采集任务"""
    data_collector = getattr(current_app, 'data_collector', None)
    if not data_collector:
        return
    device_config = current_app.device_manager.get_all_devices().get(device_id)
    if device_config:
        data_collector.start_device_task(device_id, device_config)


@devices_bp.route('/devices/<device_id>/test', methods=['POST'])
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
        logger.error(f"连接测试失败: {e}", exc_info=True)
        return jsonify({'success': False, 'message': '连接测试失败，请检查设备配置', 'protocol': protocol})


@devices_bp.route('/devices/protocols', methods=['GET'])
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


@devices_bp.route('/devices/<device_id>/behavior', methods=['GET'])
def get_device_behavior(device_id):
    """获取设备行为模拟状态（增强版模拟专用）"""
    device_manager = current_app.device_manager
    client = device_manager.get_client(device_id)
    
    if not client:
        return jsonify({'error': f'设备 {device_id} 不存在或未连接'}), 404
    
    # 检查是否是增强版模拟客户端
    if hasattr(client, 'behavior_simulator'):
        simulator = client.behavior_simulator
        return jsonify({
            'device_id': device_id,
            'state': simulator.state.name,
            'state_value': simulator.state.value,
            'health_score': round(simulator.health.overall_score, 1),
            'health_details': {
                'mechanical': round(simulator.health.mechanical_health, 1),
                'electrical': round(simulator.health.electrical_health, 1),
                'thermal': round(simulator.health.thermal_health, 1),
                'vibration': round(simulator.health.vibration_health, 1)
            },
            'active_fault': simulator.active_fault.value,
            'fault_severity': round(simulator.fault_severity, 2),
            'operating_hours': round(simulator.health.operating_hours, 2),
            'stats': simulator.stats
        })
    else:
        return jsonify({
            'device_id': device_id,
            'message': '该设备未使用增强版模拟',
            'connected': getattr(client, 'connected', False)
        })


@devices_bp.route('/devices/<device_id>/inject-fault', methods=['POST'])
@_require_engineer
def inject_device_fault(device_id):
    """注入设备故障（测试用）"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '请提供故障配置'}), 400
    
    fault_type = data.get('fault_type', 'sensor_drift')
    severity = data.get('severity', 0.5)
    
    device_manager = current_app.device_manager
    client = device_manager.get_client(device_id)
    
    if not client:
        return jsonify({'error': f'设备 {device_id} 不存在或未连接'}), 404
    
    # 检查是否是增强版模拟客户端
    if hasattr(client, 'inject_fault'):
        from 采集层.device_behavior_simulator import FaultType
        
        # 映射故障类型
        fault_map = {
            'sensor_drift': FaultType.SENSOR_DRIFT,
            'overheating': FaultType.OVERHEATING,
            'pressure_leak': FaultType.PRESSURE_LEAK,
            'motor_wear': FaultType.MOTOR_WEAR,
            'communication': FaultType.COMMUNICATION,
            'power_fluctuation': FaultType.POWER_FLUCTUATION
        }
        
        fault = fault_map.get(fault_type)
        if not fault:
            return jsonify({'error': f'不支持的故障类型: {fault_type}'}), 400
        
        client.inject_fault(fault, severity)
        
        get_auth_manager().log_operation(
            request.current_user['username'], 'inject_fault',
            f"注入故障: {device_id} - {fault_type} (严重度: {severity})")
        
        return jsonify({
            'success': True,
            'message': f'已注入故障: {fault_type}',
            'device_id': device_id,
            'fault_type': fault_type,
            'severity': severity
        })
    else:
        return jsonify({'error': '该设备不支持故障注入'}), 400


@devices_bp.route('/devices/<device_id>/force-state', methods=['POST'])
@_require_engineer
def force_device_state(device_id):
    """强制设置设备状态（测试用）"""
    data = request.get_json()
    if not data:
        return jsonify({'error': '请提供状态配置'}), 400
    
    state_name = data.get('state', 'RUNNING')
    
    device_manager = current_app.device_manager
    client = device_manager.get_client(device_id)
    
    if not client:
        return jsonify({'error': f'设备 {device_id} 不存在或未连接'}), 404
    
    # 检查是否是增强版模拟客户端
    if hasattr(client, 'force_state'):
        from 采集层.device_behavior_simulator import DeviceState
        
        # 映射状态
        state_map = {
            'STOPPED': DeviceState.STOPPED,
            'IDLE': DeviceState.IDLE,
            'RUNNING': DeviceState.RUNNING,
            'FAULT': DeviceState.FAULT,
            'MAINTENANCE': DeviceState.MAINTENANCE,
            'SETUP': DeviceState.SETUP
        }
        
        state = state_map.get(state_name)
        if not state:
            return jsonify({'error': f'不支持的状态: {state_name}'}), 400
        
        client.force_state(state)
        
        get_auth_manager().log_operation(
            request.current_user['username'], 'force_state',
            f"强制状态: {device_id} → {state_name}")
        
        return jsonify({
            'success': True,
            'message': f'已设置状态: {state_name}',
            'device_id': device_id,
            'state': state_name
        })
    else:
        return jsonify({'error': '该设备不支持状态强制设置'}), 400


@devices_bp.route('/devices/templates', methods=['GET'])
def get_device_templates():
    """获取设备模板列表"""
    templates = [
        # ==================== 传感器类 ====================
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
        
        # ==================== PLC控制器类 ====================
        {'id': 'siemens_s7_1500', 'name': '西门子S7-1500 PLC', 'description': '高端PLC，支持Profinet/OPC UA', 'protocol': 'modbus_tcp', 'port': 502,
         'vendor': '西门子', 'device_model': 'S7-1516-3 PN/DP',
         'registers': [
             {'name': 'boiler_temperature', 'description': '锅炉出口温度', 'address': 0, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': '°C'},
             {'name': 'boiler_pressure', 'description': '锅炉压力', 'address': 2, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': 'MPa'},
             {'name': 'steam_flow', 'description': '蒸汽流量', 'address': 4, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': 't/h'},
             {'name': 'feed_water_level', 'description': '给水液位', 'address': 6, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': 'mm'},
             {'name': 'boiler_status', 'description': '锅炉状态 0=停炉/1=运行/2=故障', 'address': 8, 'length': 1, 'data_type': 'uint16', 'scale': 1, 'unit': ''}
         ]},
        {'id': 'siemens_s7_1200', 'name': '西门子S7-1200 PLC', 'description': '紧凑型PLC，适用于中小型设备', 'protocol': 'modbus_tcp', 'port': 502,
         'vendor': '西门子', 'device_model': 'S7-1214C DC/DC/DC',
         'registers': [
             {'name': 'motor_speed', 'description': '电机转速', 'address': 0, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': 'RPM'},
             {'name': 'motor_current', 'description': '电机电流', 'address': 2, 'length': 2, 'data_type': 'float32', 'scale': 0.01, 'unit': 'A'},
             {'name': 'vibration', 'description': '振动值', 'address': 4, 'length': 2, 'data_type': 'float32', 'scale': 0.1, 'unit': 'mm/s'},
             {'name': 'temperature', 'description': '轴承温度', 'address': 6, 'length': 2, 'data_type': 'float32', 'scale': 0.1, 'unit': '°C'},
             {'name': 'run_status', 'description': '运行状态', 'address': 8, 'length': 1, 'data_type': 'uint16', 'scale': 1, 'unit': ''}
         ]},
        {'id': 'mitsubishi_fx5u', 'name': '三菱FX5U PLC', 'description': '高性能小型PLC，适用于注塑机/包装机', 'protocol': 'modbus_tcp', 'port': 502,
         'vendor': '三菱电机', 'device_model': 'FX5U-64M',
         'registers': [
             {'name': 'mold_temperature', 'description': '模具温度', 'address': 0, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': '°C'},
             {'name': 'injection_pressure', 'description': '注射压力', 'address': 2, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': 'MPa'},
             {'name': 'injection_speed', 'description': '注射速度', 'address': 4, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': 'mm/s'},
             {'name': 'cycle_time', 'description': '成型周期', 'address': 6, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': 's'},
             {'name': 'shot_count', 'description': '注射计数', 'address': 8, 'length': 1, 'data_type': 'uint16', 'scale': 1, 'unit': '个'}
         ]},
        {'id': 'schneider_m340', 'name': '施耐德M340 PLC', 'description': '中型PLC，适用于水处理/化工', 'protocol': 'modbus_tcp', 'port': 502,
         'vendor': '施耐德电气', 'device_model': 'BMX P34 2020',
         'registers': [
             {'name': 'inlet_flow', 'description': '进水流量', 'address': 0, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': 'm³/h'},
             {'name': 'outlet_flow', 'description': '出水流量', 'address': 2, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': 'm³/h'},
             {'name': 'water_level', 'description': '沉淀池液位', 'address': 4, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': 'm'},
             {'name': 'ph_value', 'description': '出水pH值', 'address': 6, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': ''},
             {'name': 'turbidity', 'description': '出水浊度', 'address': 8, 'length': 2, 'data_type': 'float33', 'scale': 1.0, 'unit': 'NTU'}
         ]},
        {'id': 'omron_nj', 'name': '欧姆龙NJ系列PLC', 'description': '机器自动化控制器，支持EtherCAT', 'protocol': 'modbus_tcp', 'port': 502,
         'vendor': '欧姆龙', 'device_model': 'NJ501-1300',
         'registers': [
             {'name': 'position_x', 'description': 'X轴位置', 'address': 0, 'length': 2, 'data_type': 'float32', 'scale': 0.001, 'unit': 'mm'},
             {'name': 'position_y', 'description': 'Y轴位置', 'address': 2, 'length': 2, 'data_type': 'float32', 'scale': 0.001, 'unit': 'mm'},
             {'name': 'speed', 'description': '运行速度', 'address': 4, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': 'mm/s'},
             {'name': 'torque', 'description': '扭矩', 'address': 6, 'length': 2, 'data_type': 'float32', 'scale': 0.1, 'unit': 'N·m'},
             {'name': 'cycle_count', 'description': '循环计数', 'address': 8, 'length': 1, 'data_type': 'uint16', 'scale': 1, 'unit': '个'}
         ]},
        {'id': 'delta_dvp', 'name': '台达DVP PLC', 'description': '经济型PLC，适用于包装/输送', 'protocol': 'modbus_tcp', 'port': 502,
         'vendor': '台达电子', 'device_model': 'DVP32EH00T3',
         'registers': [
             {'name': 'conveyor_speed', 'description': '输送带速度', 'address': 0, 'length': 1, 'data_type': 'uint16', 'scale': 0.1, 'unit': 'm/min'},
             {'name': 'sealing_temperature', 'description': '封箱温度', 'address': 1, 'length': 1, 'data_type': 'uint16', 'scale': 0.1, 'unit': '°C'},
             {'name': 'label_count', 'description': '贴标计数', 'address': 2, 'length': 1, 'data_type': 'uint16', 'scale': 1, 'unit': '个'},
             {'name': 'reject_count', 'description': '剔除计数', 'address': 3, 'length': 1, 'data_type': 'uint16', 'scale': 1, 'unit': '个'},
             {'name': 'packing_status', 'description': '包装线状态', 'address': 4, 'length': 1, 'data_type': 'uint16', 'scale': 1, 'unit': ''}
         ]},
        {'id': 'inovance_h5u', 'name': '汇川H5U PLC', 'description': '国产高性能PLC，适用于涂装/喷涂', 'protocol': 'modbus_tcp', 'port': 502,
         'vendor': '汇川技术', 'device_model': 'H5U-3232MT',
         'registers': [
             {'name': 'spray_pressure', 'description': '喷涂压力', 'address': 0, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': 'MPa'},
             {'name': 'oven_temperature', 'description': '烘干炉温度', 'address': 2, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': '°C'},
             {'name': 'spray_speed', 'description': '喷涂速度', 'address': 4, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': 'm/min'},
             {'name': 'coating_thickness', 'description': '涂层厚度', 'address': 6, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': 'μm'},
             {'name': 'painted_count', 'description': '喷涂计数', 'address': 8, 'length': 1, 'data_type': 'uint16', 'scale': 1, 'unit': '个'}
         ]},
        {'id': 'hollysys_lk', 'name': '和利时LK PLC', 'description': '国产大型PLC，适用于化工/制药', 'protocol': 'modbus_tcp', 'port': 502,
         'vendor': '和利时', 'device_model': 'LK210',
         'registers': [
             {'name': 'distill_temperature', 'description': '蒸馏塔顶温度', 'address': 0, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': '°C'},
             {'name': 'distill_pressure', 'description': '蒸馏塔压力', 'address': 2, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': 'kPa'},
             {'name': 'dryer_temperature', 'description': '干燥机温度', 'address': 4, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': '°C'},
             {'name': 'reflux_ratio', 'description': '回流比', 'address': 6, 'length': 2, 'data_type': 'float32', 'scale': 1.0, 'unit': ''},
             {'name': 'batch_count', 'description': '批次计数', 'address': 8, 'length': 1, 'data_type': 'uint16', 'scale': 1, 'unit': '个'}
         ]},
        
        # ==================== 变频器/驱动类 ====================
        {'id': 'inverter', 'name': '变频器', 'description': 'ABB/西门子等变频器', 'protocol': 'modbus_tcp', 'port': 502,
         'registers': [
             {'name': 'frequency', 'description': '输出频率', 'address': 0, 'length': 1, 'data_type': 'float32', 'scale': 0.01, 'unit': 'Hz'},
             {'name': 'current', 'description': '输出电流', 'address': 2, 'length': 1, 'data_type': 'float32', 'scale': 0.01, 'unit': 'A'},
             {'name': 'voltage', 'description': '输出电压', 'address': 4, 'length': 1, 'data_type': 'float32', 'scale': 0.1, 'unit': 'V'},
             {'name': 'status', 'description': '运行状态', 'address': 100, 'length': 1, 'data_type': 'uint16', 'scale': 1, 'unit': ''}
         ]},
        
        # ==================== OPC UA设备 ====================
        {'id': 'opcua_plc', 'name': 'OPC UA PLC控制器', 'description': '支持OPC UA的PLC（如西门子S7-1500）', 'protocol': 'opcua',
         'endpoint': 'opc.tcp://192.168.1.50:4840',
         'nodes': [
             {'node_id': 'ns=3;s=Temperature', 'name': 'temperature', 'description': '温度值', 'unit': '°C'},
             {'node_id': 'ns=3;s=Pressure', 'name': 'pressure', 'description': '压力值', 'unit': 'MPa'},
             {'node_id': 'ns=3;s=MotorSpeed', 'name': 'motor_speed', 'description': '电机转速', 'unit': 'RPM'},
             {'node_id': 'ns=3;s=RunningStatus', 'name': 'running_status', 'description': '运行状态', 'unit': ''}
         ]},
        
        # ==================== MQTT设备 ====================
        {'id': 'mqtt_iot_sensor', 'name': 'MQTT IoT传感器', 'description': '无线环境监测节点（MQTT协议）', 'protocol': 'mqtt', 'port': 1883,
         'topics': [
             {'topic': 'factory/workshop_a/temperature', 'name': 'temperature', 'unit': '°C', 'json_path': 'value'},
             {'topic': 'factory/workshop_a/humidity', 'name': 'humidity', 'unit': '%RH', 'json_path': 'value'},
             {'topic': 'factory/workshop_a/co2', 'name': 'co2', 'unit': 'ppm', 'json_path': 'value'}
         ]},
        
        # ==================== REST设备 ====================
        {'id': 'rest_gateway', 'name': 'REST智能网关', 'description': '产线数据网关（HTTP接口）', 'protocol': 'rest',
         'base_url': 'http://192.168.1.250/api',
         'endpoints': [
             {'name': 'temperature', 'path': '/sensors/temp', 'method': 'GET', 'json_path': 'data.value', 'unit': '°C'},
             {'name': 'humidity', 'path': '/sensors/humi', 'method': 'GET', 'json_path': 'data.value', 'unit': '%RH'},
             {'name': 'production_count', 'path': '/production/count', 'method': 'GET', 'json_path': 'data.total', 'unit': '个'}
         ]},
    ]
    return jsonify({'templates': templates})


# ==================== 内部辅助函数 ====================

def _validate_protocol_fields(protocol: str, data: dict[str, Any]):
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


def _build_device_config(protocol: str, data: dict[str, Any]) -> dict[str, Any]:
    """根据协议类型构建设备配置字典"""
    config = {
        'id': data['id'],
        'name': data['name'],
        'description': data.get('description', ''),
        'protocol': protocol,
        'device_category': data.get('device_category', 'instrument'),
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


def _update_protocol_fields(protocol: str, device_config: dict[str, Any], data: dict[str, Any]):
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


# ==================== 模拟设备预设API ====================

def _get_simulation_initializer():
    """安全获取SimulationInitializer实例"""
    try:
        return getattr(current_app, 'simulation_initializer', None)
    except Exception:
        return None


@devices_bp.route('/devices/presets', methods=['GET'])
def get_simulation_presets():
    """获取所有模拟设备预设列表"""
    initializer = _get_simulation_initializer()
    if initializer is None:
        return jsonify({'success': False, 'message': '模拟初始化器未加载'}), 500

    presets = initializer.get_presets()
    categories = initializer.get_preset_categories()
    return jsonify({'success': True, 'presets': presets, 'categories': categories})


@devices_bp.route('/devices/presets/<preset_id>', methods=['GET'])
def get_preset_detail(preset_id):
    """获取单个预设详情"""
    initializer = _get_simulation_initializer()
    if initializer is None:
        return jsonify({'success': False, 'message': '模拟初始化器未加载'}), 500

    preset = initializer.get_preset_detail(preset_id)
    if preset is None:
        return jsonify({'success': False, 'message': f'预设 {preset_id} 不存在'}), 404

    return jsonify({'success': True, 'preset': preset})


@devices_bp.route('/devices/presets/add', methods=['POST'])
@_require_engineer
def add_preset_device():
    """一键添加预设设备"""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': '请提供预设配置'}), 400

    preset_id = data.get('preset_id')
    if not preset_id:
        return jsonify({'success': False, 'message': '缺少 preset_id'}), 400

    custom_device_id = data.get('device_id')  # 可选

    initializer = _get_simulation_initializer()
    if initializer is None:
        return jsonify({'success': False, 'message': '模拟初始化器未加载'}), 500

    result = initializer.add_preset_device(preset_id, custom_device_id)
    if result['success']:
        get_auth_manager().log_operation(
            request.current_user['username'], 'add_preset',
            f"添加预设设备: {preset_id} -> {result.get('device_id', '')}")
        # 启动采集
        _start_device_collection(result.get('device_id', ''))
    return jsonify(result)


@devices_bp.route('/devices/presets/batch-add', methods=['POST'])
@_require_engineer
def batch_add_presets():
    """批量添加预设设备"""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': '请提供预设列表'}), 400

    preset_ids = data.get('preset_ids', [])
    if not preset_ids:
        return jsonify({'success': False, 'message': '缺少 preset_ids'}), 400

    initializer = _get_simulation_initializer()
    if initializer is None:
        return jsonify({'success': False, 'message': '模拟初始化器未加载'}), 500

    result = initializer.add_preset_batch(preset_ids)
    if result['success']:
        get_auth_manager().log_operation(
            request.current_user['username'], 'batch_add_presets',
            f"批量添加预设: {preset_ids}")
        # 启动所有成功添加的设备的采集
        for r in result.get('results', []):
            if r.get('success') and r.get('device_id'):
                _start_device_collection(r['device_id'])
    return jsonify(result)


@devices_bp.route('/devices/presets/add-all', methods=['POST'])
@_require_engineer
def add_all_presets():
    """一键添加全部预设设备"""
    initializer = _get_simulation_initializer()
    if initializer is None:
        return jsonify({'success': False, 'message': '模拟初始化器未加载'}), 500

    presets = getattr(initializer, 'presets', [])
    if not presets:
        return jsonify({'success': False, 'message': '没有可用的预设设备配置，请检查 simulation_presets.yaml 文件'}), 400

    all_preset_ids = [p['id'] for p in presets]
    result = initializer.add_preset_batch(all_preset_ids)
    if result['success']:
        get_auth_manager().log_operation(
            request.current_user['username'], 'add_all_presets',
            f"添加全部预设: {result['message']}")
        for r in result.get('results', []):
            if r.get('success') and r.get('device_id'):
                _start_device_collection(r['device_id'])
    return jsonify(result)
