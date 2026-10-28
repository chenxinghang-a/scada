"""
系统信息与配置API
系统状态/数据库统计/模拟模式切换/系统配置管理
"""

import logging
from pathlib import Path
import yaml
from functools import wraps
from flask import Blueprint, jsonify, request, current_app
from datetime import datetime

from 用户层.auth import role_required, jwt_required
from ._common import load_yaml_config, save_yaml_config, api_error
import paths
from core.version import get_version
from core.config_io import dump_yaml

logger = logging.getLogger(__name__)

system_bp = Blueprint('api_system', __name__, url_prefix='/api')


# ==================== 系统信息API ====================

@system_bp.route('/system/status', methods=['GET'])
@jwt_required
def get_system_status():
    """获取系统状态

    Query params:
        brief: 1=返回精简设备状态（100+设备场景），0=完整状态（默认）
    """
    start_time = getattr(current_app, 'system_start_time', None)
    uptime_seconds = (datetime.now() - start_time).total_seconds() if start_time else 0

    brief = request.args.get('brief', '0') == '1'

    return jsonify({
        'version': get_version(),
        'database': current_app.database.get_database_stats(),
        'devices': current_app.device_manager.get_all_status(brief=brief),
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
    raw = data.get('simulation_mode')

    if raw is None:
        return jsonify({'success': False, 'message': '缺少simulation_mode参数'}), 400

    # round 302：**不许把任意真值当 True**。
    # `bool("false") == True` —— 前端表单/脚本传字符串 `"false"` 会直接切到模拟模式；
    # `bool([])`、`bool(0)` 又把明确的 0 当 False，语义完全靠 Python 真值表。
    if isinstance(raw, bool):
        new_mode = raw
    elif isinstance(raw, str) and raw.strip().lower() in ('true', 'false'):
        new_mode = raw.strip().lower() == 'true'
    elif isinstance(raw, int) and raw in (0, 1):
        new_mode = bool(raw)
    else:
        return jsonify({
            'success': False,
            'message': f'simulation_mode 必须是布尔值 true/false（收到 {raw!r}）',
        }), 400

    # 运行时热切换
    dm = current_app.device_manager
    result = dm.switch_simulation_mode(new_mode) or {}

    # round 302：**运行时没切换成功，就绝不写盘**。
    # 原来完全不看 `result['success']`：`RealDeviceManager.switch_simulation_mode(True)`
    # 在真实模式下会明确回 `{'success': False, 'message': '...无法切换...'}`，
    # 但这里照样把 `simulation_mode: true` 写进 `配置/system.yaml`，
    # 而 `run.py` 启动时正是读这个键 ——
    # 运维点了「切模拟」当场被拒（界面提示失败），下次重启却真的进了模拟模式：
    # 采集的是合成数据，看板曲线依然"正常"。这是比报警更坏的一类假成功：
    # 它静默地换掉了**全部数据源**。反向同理（模拟模式下被拒的 false 会让
    # 下次启动连上现场真设备）。
    if not result.get('success', True):
        return jsonify({
            **result,
            'persisted': False,
            'message': result.get('message') or '模式切换失败，配置未修改',
        }), 400

    # 同时更新配置文件（持久化）
    config_path = paths.resolve('配置/system.yaml')
    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}

    if 'system' not in config:
        config['system'] = {}
    config['system']['simulation_mode'] = new_mode

    # round 256：原子写 + 就地合并。原来 `open(..., 'w')` 先截断再 dump，
    # 序列化一失败就把 system.yaml 清成 0 字节（端口、数据库、JWT 密钥段全没），
    # 而异常被上层吞成 500 —— 运行中的进程还能用，重启直接起不来。
    if not dump_yaml(config_path, config):
        logger.error("模拟模式已热切换，但写入 %s 失败（重启后不会保持）", config_path)
        # 注意顺序：`**result` 在前 —— 它在失败分支里必须被显式的
        # success/message 覆盖（`switch_simulation_mode` 自己会回 success: true）。
        return jsonify({
            **result,
            'success': False,
            'message': '模式已热切换，但配置持久化失败（重启后会恢复原模式）',
            'persisted': False,
        }), 500

    return jsonify(result)


# ==================== 系统配置API ====================

@system_bp.route('/config', methods=['GET'])
@jwt_required
def get_config():
    """获取系统配置（过滤敏感字段）"""
    config = load_yaml_config('配置/system.yaml')
    if not config:
        return api_error('配置文件不存在', 404)
    # 移除敏感字段，防止密钥泄露
    sensitive_keys = {'secret_key', 'jwt_secret', 'password', 'token', 'api_key'}
    def _sanitize(obj):
        if isinstance(obj, dict):
            return {k: ('***' if k.lower() in sensitive_keys else _sanitize(v))
                    for k, v in obj.items()}
        elif isinstance(obj, list):
            return [_sanitize(i) for i in obj]
        return obj
    return jsonify({'config': _sanitize(config)})


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
    from ._common import get_auth_manager

    data = request.get_json()
    if not data:
        return api_error('请提供配置数据')

    config = load_yaml_config('配置/system.yaml')

    section = data.get('section')
    if section and section in config and isinstance(config[section], dict):
        config[section].update(data.get('data', {}))
    elif section:
        return jsonify({'success': False, 'message': f'配置段 {section} 不存在或不是字典'}), 400
    else:
        # 安全限制：只允许更新白名单中的顶层字段
        allowed_keys = {'system', 'web', 'security'}
        filtered = {k: v for k, v in data.items() if k in allowed_keys}
        if not filtered:
            return jsonify({'success': False, 'message': '无有效的配置段'}), 400
        config.update(filtered)

    if not save_yaml_config('配置/system.yaml', config):
        return jsonify({'success': False, 'message': '配置保存失败'}), 500

    get_auth_manager().log_operation(
        request.current_user['username'], 'update_config', f"更新系统配置: {section or 'global'}")
    return jsonify({'success': True, 'message': '配置已保存'})


# ==================== 前端错误日志上报 ====================

_client_error_logger = logging.getLogger('client_error')


@system_bp.route('/system/client-errors', methods=['POST'])
def report_client_errors():
    """接收前端错误日志上报

    等保2.0 GB/T 22239 要求：应记录安全事件，便于事后追溯。
    无需认证（错误可能发生在认证失败时），但限流保护。
    """
    data = request.get_json()
    if not data or 'errors' not in data:
        return jsonify({'success': False, 'message': '无效的错误数据'}), 400

    errors = data['errors']
    if not isinstance(errors, list) or len(errors) > 50:
        return jsonify({'success': False, 'message': '错误数据格式不正确'}), 400

    client_ip = request.remote_addr or 'unknown'
    for err in errors:
        _client_error_logger.warning(
            "前端错误 [%s] %s | url=%s user=%s ip=%s",
            err.get('type', 'unknown'),
            err.get('message', '')[:200],
            err.get('url', ''),
            err.get('userId', 'anonymous'),
            client_ip,
            extra={'client_error': err}
        )

    return jsonify({'success': True, 'received': len(errors)})
