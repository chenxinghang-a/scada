"""
Prometheus指标API端点
提供 /metrics 端点，输出Prometheus exposition format
"""
from flask import Blueprint, Response, current_app
from prometheus_client import CONTENT_TYPE_LATEST

from core.metrics import metrics_collector
from 用户层.auth import jwt_required

metrics_bp = Blueprint('metrics', __name__)


@metrics_bp.route('/metrics')
@jwt_required
def prometheus_metrics():
    """Prometheus指标端点"""
    # 更新动态指标
    if hasattr(current_app, 'device_manager'):
        metrics_collector.update_device_metrics(current_app.device_manager)
    if hasattr(current_app, 'alarm_manager'):
        metrics_collector.update_alarm_metrics(current_app.alarm_manager)
    if hasattr(current_app, 'data_collector'):
        metrics_collector.update_queue_metrics(current_app.data_collector)

    return Response(
        metrics_collector.get_metrics(),
        mimetype=CONTENT_TYPE_LATEST
    )
