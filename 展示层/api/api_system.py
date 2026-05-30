"""
系统信息与配置API
系统状态/数据库统计/模拟模式切换/系统配置管理
"""

import logging
from pathlib import Path
import yaml
from flask import Blueprint, jsonify, request, current_app
from datetime import datetime

from 用户层.auth import role_required, jwt_required
from ._common import load_yaml_config, save_yaml_config

logger = logging.getLogger(__name__)

system_bp = Blueprint('api_system', __name__, url_prefix='/api')


# ==================== 系统信息API ====================

@system_bp.route('/system/status', methods=['GET'])
@jwt_required
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


@system_bp.route('/system/database', methods=['GET'])
@jwt_required
def get_database_stats():
    """获取数据库统计"""
    return jsonify(current_app.database.get_database_stats())


@system_bp.route('/system/simulation-mode', methods=['GET'])
@jwt_required
def get_simulation_mode():
    """获取当前模拟/真实模式状态"""
    return jsonify({
        'simulation_mode': current_app.device_manager.simulation_mode
    })


@system_bp.route('/system/simulation-mode', methods=['POST'])
@role_required('admin', 'engineer')
def toggle_simulation_mode():
    """切换模拟/真实模式（运行时热切换，无需重启）"""
    data = request.get_json() or {}
    new_mode = data.get('simulation_mode')

    if new_mode is None:
        return jsonify({'success': False, 'message': '缺少simulation_mode参数'}), 400

    new_mode = bool(new_mode)

    # 运行时热切换
    dm = current_app.device_manager
    result = dm.switch_simulation_mode(new_mode)

    # 同时更新配置文件（持久化）
    config_path = Path('配置/system.yaml')
    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}

    if 'system' not in config:
        config['system'] = {}
    config['system']['simulation_mode'] = new_mode

    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, 'w', encoding='utf-8') as f:
        yaml.dump(config, f, allow_unicode=True, default_flow_style=False)

    return jsonify(result)


# ==================== 系统配置API ====================

@system_bp.route('/config', methods=['GET'])
@jwt_required
def get_config():
    """获取系统配置"""
    config = load_yaml_config('配置/system.yaml')
    if not config:
        return jsonify({'error': '配置文件不存在'}), 404
    return jsonify({'config': config})


# ==================== 高可用API ====================

@system_bp.route('/system/ha-status', methods=['GET'])
@jwt_required
def get_ha_status():
    """获取高可用状态"""
    ha_manager = getattr(current_app, 'ha_manager', None)
    if not ha_manager:
        return jsonify({'enabled': False, 'message': 'HA未启用'}), 200
    return jsonify({'enabled': True, **ha_manager.get_status()})


@system_bp.route('/system/ha-force-role', methods=['POST'])
@role_required('admin')
def ha_force_role():
    """强制切换HA角色"""
    ha_manager = getattr(current_app, 'ha_manager', None)
    if not ha_manager:
        return jsonify({'success': False, 'message': 'HA未启用'}), 400

    data = request.get_json() or {}
    role_str = data.get('role', '').lower()

    if role_str not in ('primary', 'standby'):
        return jsonify({'success': False, 'message': 'role必须是primary或standby'}), 400

    from core.ha_manager import HARole
    ha_manager.force_role(HARole(role_str))
    return jsonify({'success': True, 'message': f'角色已切换为{role_str}', **ha_manager.get_status()})


@system_bp.route('/config', methods=['PUT'])
@role_required('admin', 'engineer')
def update_config():
    """更新系统配置"""
    from 用户层.auth import jwt_required
    from ._common import get_auth_manager

    data = request.get_json()
    if not data:
        return jsonify({'error': '请提供配置数据'}), 400

    config = load_yaml_config('配置/system.yaml')

    section = data.get('section')
    if section and section in config:
        config[section].update(data.get('data', {}))
    else:
        config.update(data)

    save_yaml_config('配置/system.yaml', config)

    get_auth_manager().log_operation(
        request.current_user['username'], 'update_config', f"更新系统配置: {section or 'global'}")
    return jsonify({'success': True, 'message': '配置已保存'})
