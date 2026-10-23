"""
Prometheus指标API端点
提供 /metrics 端点，输出Prometheus exposition format
"""
from flask import Blueprint, Response, current_app
from prometheus_client import CONTENT_TYPE_LATEST

from core.metrics import metrics_collector

metrics_bp = Blueprint('metrics', __name__)


@metrics_bp.route('/metrics')
def prometheus_metrics():
    """Prometheus指标端点"""
    # 更新动态指标
    if hasattr(current_app, 'device_manager'):
        metrics_collector.update_device_metrics(current_app.device_manager)
    if hasattr(current_app, 'alarm_manager'):
        metrics_collector.update_alarm_metrics(current_app.alarm_manager)
    if hasattr(current_app, 'data_collector'):
        metrics_collector.update_queue_metrics(current_app.data_collector)
    # 智能层评分（round 255）：这两个 gauge 此前从未被设置，恒 0。
    # 用 hasattr 而不是硬取：不是所有装配都会挂智能层（测试夹具/精简部署）。
    if hasattr(current_app, 'predictive_maintenance'):
        metrics_collector.update_health_scores(current_app.predictive_maintenance)
    if hasattr(current_app, 'oee_calculator'):
        metrics_collector.update_oee_scores(current_app.oee_calculator)

    return Response(
        metrics_collector.get_metrics(),
        mimetype=CONTENT_TYPE_LATEST
    )
