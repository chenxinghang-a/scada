"""
数据查询与导出API
实时数据/历史数据/数据导出
"""

import logging
from functools import wraps
from flask import Blueprint, jsonify, request, current_app
from datetime import datetime, timedelta

from 用户层.auth import jwt_required, permission_required
from ._common import api_error_handler, api_error, clamp_limit
from .error_codes import DATA_NOT_FOUND

logger = logging.getLogger(__name__)

data_bp = Blueprint('api_data', __name__, url_prefix='/api')

_require_auth = jwt_required

# 导出接口需要 export_data 权限（engineer 及以上）；viewer 只有 read，
# 不应能把生产数据整体导出。permission_required 内部已含 jwt_required。
_require_export = permission_required('export_data')


def _parse_time(time_str, default):
    """安全解析时间字符串，并**统一成「本机时区的 naive datetime」**。

    ⚠️ 两件事缺一不可：

    1. 前端用 `Date.toISOString()` 传的是 **UTC**（`...T07:00:00.000Z`），
       `fromisoformat` 会解析成 **aware** datetime。而库里存的是
       `datetime.now()` 得到的**本地 naive** 字符串（如 `2026-10-21 15:00:00`）。
       把 aware 直接绑进 SQL，sqlite3 会写成 `2026-10-21 07:00:00+00:00`，
       与库存值做**字符串比较** → 整段窗口偏 8 小时，查出来是空的。
    2. 所以必须 `astimezone()` 转本机时区，再 `replace(tzinfo=None)` 去掉 tzinfo，
       让「用户选的 15:00」对上「库里存的 15:00」。
    """
    if not time_str:
        return default
    try:
        dt = datetime.fromisoformat(time_str)
    except (ValueError, TypeError):
        return default
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


# ==================== 数据查询API ====================

@data_bp.route('/data/realtime', methods=['GET'])
@_require_auth
@api_error_handler
def get_realtime_data():
    """获取实时数据（realtime_data 是 UPSERT 表，每设备每寄存器只有一行）"""
    device_id = request.args.get('device_id')
    # 不限制条数上限（realtime_data 数据量 = 设备数 × 寄存器数，通常 < 500），
    # 但必须挡住 `?limit=999999999` 这种把整库往内存里搬的请求。
    limit = clamp_limit(request.args.get('limit', 10000, type=int), default=10000, maximum=10000)
    return jsonify({'data': current_app.database.get_realtime_data(device_id=device_id, limit=limit)})


@data_bp.route('/data/latest/<device_id>', methods=['GET'])
@_require_auth
@api_error_handler
def get_latest_data(device_id):
    """获取设备最新数据"""
    register_name = request.args.get('register_name')
    data = current_app.database.get_latest_data(device_id=device_id, register_name=register_name)
    if data:
        return jsonify({'data': data})
    return api_error('没有数据', 404, DATA_NOT_FOUND)


@data_bp.route('/data/history/<device_id>/<register_name>', methods=['GET'])
@_require_auth
@api_error_handler
def get_history_data(device_id, register_name):
    """获取历史数据。

    两处**必须显式处理**，否则都是静默错误：

    1. `interval` 可能传来**空字符串**（前端「原始」下拉项的值就是 `''`）。
       `request.args.get('interval', '1min')` 在参数存在但为空时返回 `''`，
       不是默认值 —— 于是「原始」被当成未知粒度走了按秒聚合分支。
    2. 结果集必须**封顶并告知**。原实现按 `LIMIT 10000` 静默截断：
       图表只画到前 10000 个点，后半段直接消失，界面上没有任何提示
       （看着像「那段时间没数据」）。现在多取一条来判定是否截断，
       并在响应里带 `truncated`。
    """
    from 存储层.database import MAX_HISTORY_POINTS

    start_raw = request.args.get('start_time') or request.args.get('start')
    end_raw = request.args.get('end_time') or request.args.get('end')
    # ⚠️ 不能用 `request.args.get('interval', '1min')`：`?interval=` 会返回 ''
    # 而不是默认值。空串本身是合法语义（前端「原始」= 不聚合），交给 DB 层处理。
    interval = (request.args.get('interval') or '').strip().lower()

    start_time = _parse_time(start_raw, datetime.now() - timedelta(hours=1))
    end_time = _parse_time(end_raw, datetime.now())

    if end_time < start_time:
        return api_error(
            f'结束时间（{end_time:%Y-%m-%d %H:%M:%S}）早于开始时间'
            f'（{start_time:%Y-%m-%d %H:%M:%S}）', 400,
        )

    # 已知粒度先按「时间跨度 ÷ 粒度」预估桶数，超限直接拒绝：
    # 否则要在库里扫完整段时间才由 LIMIT 截断，CPU 白烧（30 年数据能扫几十秒）。
    _INTERVAL_SECONDS = {'1min': 60, '5min': 300, '1hour': 3600, '1day': 86400}
    step = _INTERVAL_SECONDS.get(interval)
    if step:
        expected = (end_time - start_time).total_seconds() / step + 1
        if expected > MAX_HISTORY_POINTS:
            return api_error(
                f'时间范围过大：按「{interval}」聚合约 {int(expected):,} 个点，'
                f'超过单次上限 {MAX_HISTORY_POINTS:,}。'
                f'请缩短时间范围，或选择更粗的聚合粒度。', 400,
            )

    # 前端传入 '*' 表示查询全部寄存器
    if register_name == '*':
        register_name = None

    rows = current_app.database.get_history_data(
        device_id=device_id, register_name=register_name,
        start_time=start_time, end_time=end_time, interval=interval,
        limit=MAX_HISTORY_POINTS + 1,
    )
    truncated = len(rows) > MAX_HISTORY_POINTS
    data = rows[:MAX_HISTORY_POINTS]
    return jsonify({'data': data, 'truncated': truncated})


# ==================== 数据导出API ====================

@data_bp.route('/export/device/<device_id>', methods=['POST'])
@_require_auth
@_require_export
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
        from flask import send_file
        return send_file(filepath, as_attachment=True, download_name=Path(filepath).name)
    return jsonify({'success': False, 'message': '导出失败'}), 500


@data_bp.route('/export/alarms', methods=['POST'])
@_require_auth
@_require_export
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
        from flask import send_file
        return send_file(filepath, as_attachment=True, download_name=Path(filepath).name)
    return jsonify({'success': False, 'message': '没有报警记录可导出'}), 404
