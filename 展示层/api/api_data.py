"""
数据查询与导出API
实时数据/历史数据/数据导出
"""

import logging
from functools import wraps
from flask import Blueprint, jsonify, request, current_app
from datetime import datetime, timedelta

from 用户层.auth import jwt_required

logger = logging.getLogger(__name__)

data_bp = Blueprint('api_data', __name__, url_prefix='/api')

_require_auth = jwt_required


def api_error_handler(f):
    """API错误处理装饰器"""
    @wraps(f)
    def decorated(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except PermissionError as e:
            return jsonify({'error': str(e)}), 403
        except Exception as e:
            logger.error(f"API error in {f.__name__}: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500
    return decorated


def _parse_time(time_str, default):
    """安全解析时间字符串"""
    if not time_str:
        return default
    try:
        return datetime.fromisoformat(time_str)
    except (ValueError, TypeError):
        return default


# ==================== 数据查询API ====================

@data_bp.route('/data/realtime', methods=['GET'])
@_require_auth
@api_error_handler
def get_realtime_data():
    """获取实时数据（realtime_data 是 UPSERT 表，每设备每寄存器只有一行）"""
    device_id = request.args.get('device_id')
    # 不限制：realtime_data 数据量 = 设备数 × 寄存器数，通常 < 500
    limit = request.args.get('limit', 10000, type=int)
    return jsonify({'data': current_app.database.get_realtime_data(device_id=device_id, limit=limit)})


@data_bp.route('/data/latest/<device_id>', methods=['GET'])
@_require_auth
@api_error_handler
def get_latest_data(device_id):
    """获取设备最新数据"""
    register_name = request.args.get('register_name')
    data = current_app.database.get_latest_data(device_id=device_id, register_name=register_name)
    if data:
        return jsonify(data)
    return jsonify({'error': '没有数据'}), 404


@data_bp.route('/data/history/<device_id>/<register_name>', methods=['GET'])
@_require_auth
@api_error_handler
def get_history_data(device_id, register_name):
    """获取历史数据"""
    start_time = request.args.get('start_time')
    end_time = request.args.get('end_time')
    interval = request.args.get('interval', '1min')

    start_time = _parse_time(start_time, datetime.now() - timedelta(hours=1))
    end_time = _parse_time(end_time, datetime.now())

    # 前端传入 '*' 表示查询全部寄存器
    if register_name == '*':
        register_name = None

    data = current_app.database.get_history_data(
        device_id=device_id, register_name=register_name,
        start_time=start_time, end_time=end_time, interval=interval)
    return jsonify({'data': data})


# ==================== 数据导出API ====================

@data_bp.route('/export/device/<device_id>', methods=['POST'])
@_require_auth
@api_error_handler
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
        start_time=_parse_time(start_time_str, datetime.now() - timedelta(hours=24)),
        end_time=_parse_time(end_time_str, datetime.now()),
        format=data.get('format', 'csv')
    )

    if filepath:
        from pathlib import Path
        return jsonify({'success': True, 'filename': Path(filepath).name})
    return jsonify({'success': False, 'message': '导出失败'}), 500


@data_bp.route('/export/alarms', methods=['POST'])
@_require_auth
@api_error_handler
def export_alarms():
    """导出报警记录"""
    from 存储层.data_export import DataExport

    data = request.get_json() or {}
    exporter = DataExport()
    filepath = exporter.export_alarm_records(
        database=current_app.database,
        start_time=_parse_time(data.get('start_time'), None),
        end_time=_parse_time(data.get('end_time'), None),
        format=data.get('format', 'csv')
    )

    if filepath:
        from pathlib import Path
        return jsonify({'success': True, 'filename': Path(filepath).name})
    return jsonify({'success': False, 'message': '没有报警记录可导出'})
