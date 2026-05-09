"""
数据查询与导出API
实时数据/历史数据/数据导出
"""

import logging
from flask import Blueprint, jsonify, request, current_app
from datetime import datetime, timedelta

from 用户层.auth import jwt_required

logger = logging.getLogger(__name__)

data_bp = Blueprint('api_data', __name__, url_prefix='/api')

_require_auth = jwt_required


# ==================== 数据查询API ====================

@data_bp.route('/data/realtime', methods=['GET'])
def get_realtime_data():
    """获取实时数据"""
    device_id = request.args.get('device_id')
    limit = request.args.get('limit', 100, type=int)
    return jsonify({'data': current_app.database.get_realtime_data(device_id=device_id, limit=limit)})


@data_bp.route('/data/latest/<device_id>', methods=['GET'])
def get_latest_data(device_id):
    """获取设备最新数据"""
    register_name = request.args.get('register_name')
    data = current_app.database.get_latest_data(device_id=device_id, register_name=register_name)
    if data:
        return jsonify(data)
    return jsonify({'error': '没有数据'}), 404


@data_bp.route('/data/history/<device_id>/<register_name>', methods=['GET'])
def get_history_data(device_id, register_name):
    """获取历史数据"""
    start_time = request.args.get('start_time')
    end_time = request.args.get('end_time')
    interval = request.args.get('interval', '1min')

    start_time = datetime.fromisoformat(start_time) if start_time else datetime.now() - timedelta(hours=1)
    end_time = datetime.fromisoformat(end_time) if end_time else datetime.now()

    # 前端传入 '*' 表示查询全部寄存器
    if register_name == '*':
        register_name = None

    data = current_app.database.get_history_data(
        device_id=device_id, register_name=register_name,
        start_time=start_time, end_time=end_time, interval=interval)
    return jsonify({'data': data})


# ==================== 数据导出API ====================

@data_bp.route('/export/device/<device_id>', methods=['POST'])
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


@data_bp.route('/export/alarms', methods=['POST'])
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
