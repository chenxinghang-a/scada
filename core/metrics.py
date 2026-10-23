"""
Prometheus指标导出 - 工业SCADA监控
提供采集/告警/连接/性能指标

⚠️ round 253 实测：本模块定义了 15 个指标家族，而**其中 12 个从来没有
被任何生产代码递增过** —— `/metrics` 会把它们全部导出成恒为 0 的线。
对 Prometheus/Grafana 来说，这比"没有这个指标"更糟：看板上会出现一条
平直的 0 曲线，看上去像"系统安静"，实际是"没人往这里写"。

现在分两类，见文件末尾的 `WIRED_METRICS` / `UNWIRED_METRICS`：
- 已接线：采集读数、采集错误、报警总数、WebSocket 连接数、HTTP 请求与耗时；
- 未接线：在 `UNWIRED_METRICS` 里逐条写明**为什么没接**。
守卫 `tests/test_metrics_wiring.py` 会核对：每个定义出来的指标要么有真实
写入点，要么在未接线清单里 —— 新增指标时必须二选一，不许默默躺平。
"""
from prometheus_client import Counter, Gauge, Histogram, Info, generate_latest, CONTENT_TYPE_LATEST
import threading
import logging

from .version import get_version
from .safe_numeric import try_finite_float

logger = logging.getLogger(__name__)

# 系统信息。版本号来自唯一真源 VERSION 文件（core.version），不在此硬编码。
SCADA_INFO = Info('scada', 'SCADA系统信息')
SCADA_INFO.info({
    'version': get_version(),
    'python': '3.13',
    'protocol': 'Modbus/OPC-UA/MQTT/IEC104'
})

# 设备指标
DEVICES_TOTAL = Gauge('scada_devices_total', '设备总数', ['protocol', 'status'])
DEVICES_CONNECTED = Gauge('scada_devices_connected', '已连接设备数')
DEVICES_FAULT = Gauge('scada_devices_fault', '故障设备数')

# 数据采集指标
DATA_COLLECTED = Counter('scada_data_collected_total', '采集数据总数', ['device_id', 'protocol'])
DATA_ERRORS = Counter('scada_data_errors_total', '采集错误总数', ['device_id', 'error_type'])
COLLECTION_DURATION = Histogram('scada_collection_duration_seconds', '采集耗时',
                                ['device_id'], buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 5.0])
QUEUE_SIZE = Gauge('scada_data_queue_size', '数据队列大小')

# 告警指标
ALARMS_TOTAL = Counter('scada_alarms_total', '告警总数', ['level', 'device_id'])
ALARMS_ACTIVE = Gauge('scada_alarms_active', '活跃告警数')
ALARMS_ACKNOWLEDGED = Counter('scada_alarms_acknowledged_total', '已确认告警数')
ALARM_DURATION = Histogram('scada_alarm_duration_seconds', '告警持续时间', ['level'])

# 通信指标
MODBUS_REQUESTS = Counter('scada_modbus_requests_total', 'Modbus请求总数', ['function_code'])
MODBUS_ERRORS = Counter('scada_modbus_errors_total', 'Modbus错误总数', ['exception_code'])
MODBUS_RTT = Histogram('scada_modbus_rtt_seconds', 'Modbus往返时间',
                       buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5])

# WebSocket指标
WS_CONNECTIONS = Gauge('scada_websocket_connections', 'WebSocket连接数')
WS_MESSAGES = Counter('scada_websocket_messages_total', 'WebSocket消息总数', ['direction'])

# 系统性能
HTTP_REQUESTS = Counter('scada_http_requests_total', 'HTTP请求总数', ['method', 'endpoint', 'status'])
HTTP_DURATION = Histogram('scada_http_request_duration_seconds', 'HTTP请求耗时',
                          ['method', 'endpoint'], buckets=[0.01, 0.05, 0.1, 0.5, 1.0, 5.0])

# 智能层指标
HEALTH_SCORE = Gauge('scada_device_health_score', '设备健康评分', ['device_id'])
OEE_SCORE = Gauge('scada_oee_score', 'OEE评分', ['device_id', 'metric'])


class MetricsCollector:
    """指标收集器 - 从各模块收集指标"""

    def __init__(self):
        self._lock = threading.Lock()
        self._last_update = 0

    def update_device_metrics(self, device_manager):
        """从设备管理器收集指标"""
        try:
            status = device_manager.get_all_status()
            # get_all_status may return a list or dict
            if isinstance(status, list):
                connected = sum(1 for s in status if s.get('connected'))
                fault = sum(1 for s in status if s.get('status') == 'fault')
                protocols = {}
                for s in status:
                    proto = s.get('protocol', 'unknown')
                    protocols[proto] = protocols.get(proto, 0) + 1
            elif isinstance(status, dict):
                connected = 0
                fault = 0
                protocols = {}
                for dev_id, info in status.items():
                    proto = info.get('protocol', 'unknown')
                    protocols[proto] = protocols.get(proto, 0) + 1
                    if info.get('connected'):
                        connected += 1
                    if info.get('status') == 'fault':
                        fault += 1
            else:
                return

            DEVICES_CONNECTED.set(connected)
            DEVICES_FAULT.set(fault)
            for proto, count in protocols.items():
                DEVICES_TOTAL.labels(protocol=proto, status='total').set(count)
        except Exception as e:
            logger.debug(f"设备指标更新失败: {e}")

    def update_alarm_metrics(self, alarm_manager):
        """从告警管理器收集指标"""
        try:
            active = alarm_manager.get_active_alarms()
            ALARMS_ACTIVE.set(len(active))
        except Exception as e:
            logger.debug(f"告警指标更新失败: {e}")

    def update_queue_metrics(self, data_collector):
        """从数据采集器收集指标"""
        try:
            QUEUE_SIZE.set(data_collector.data_queue.qsize())
        except Exception as e:
            logger.debug(f"队列指标更新失败: {e}")

    def update_health_scores(self, predictive_maintenance):
        """设备健康评分（round 255：此前该 gauge 从未被设置，恒 0）。"""
        try:
            scores = predictive_maintenance.get_health_scores() or {}
            for key, item in scores.items():
                if not isinstance(item, dict):
                    continue
                device_id = item.get('device_id')
                value = try_finite_float(item.get('health_score'))
                if device_id is None or value is None:
                    # NaN/Inf 会让 gauge 变成 nan —— Prometheus 侧聚合全被污染，
                    # 比「没这个样本」更糟，直接跳过。
                    continue
                HEALTH_SCORE.labels(device_id=str(device_id)).set(value)
        except Exception as e:
            logger.debug(f"健康评分指标更新失败: {e}")

    def update_oee_scores(self, oee_calculator):
        """OEE 评分（round 255：此前恒 0）。"""
        try:
            summary = oee_calculator.get_all_oee() or {}
            for device_id, metrics in summary.items():
                if not isinstance(metrics, dict):
                    continue
                for metric in ('oee', 'availability', 'performance', 'quality'):
                    value = try_finite_float(metrics.get(metric))
                    if value is None:
                        # 同上：NaN/Inf 不许进 gauge
                        continue
                    OEE_SCORE.labels(device_id=str(device_id),
                                     metric=metric).set(value)
        except Exception as e:
            logger.debug(f"OEE 指标更新失败: {e}")

    def get_metrics(self) -> bytes:
        """获取所有指标（Prometheus格式）"""
        return generate_latest()


# 全局指标收集器实例
metrics_collector = MetricsCollector()


#: **已接线**的指标 → 写入点（`文件:位置`）。
#: 守卫 `tests/test_metrics_wiring.py` 会核对这里的消费方真实存在。
WIRED_METRICS = {
    # —— 采集路径 ——
    'scada_data_collected_total': '采集层/data_collector.py:_enqueue_drop_oldest',
    'scada_data_errors_total': '采集层/data_collector.py:_collect_device_data',
    'scada_collection_duration_seconds': '采集层/data_collector.py:_run_collection',
    # —— 报警路径 ——
    'scada_alarms_total': '报警层/alarm_manager.py:_trigger_alarm',
    'scada_alarms_acknowledged_total': '报警层/alarm_manager.py:acknowledge_alarm',
    'scada_alarm_duration_seconds': '报警层/alarm_manager.py:acknowledge_alarm',
    # —— 连接 / 接口 ——
    'scada_websocket_connections': '展示层/websocket.py:connect/disconnect',
    'scada_websocket_messages_total': '报警层/alarm_manager.py:_emit_websocket_alarm',
    'scada_http_requests_total': '展示层/routes.py:record_http_metrics',
    'scada_http_request_duration_seconds': '展示层/routes.py:record_http_metrics',
    # —— 每次 /metrics 请求时刷新（由 MetricsCollector 从各模块现取） ——
    'scada_devices_total': 'core/metrics.py:update_device_metrics',
    'scada_devices_connected': 'core/metrics.py:update_device_metrics',
    'scada_devices_fault': 'core/metrics.py:update_device_metrics',
    'scada_alarms_active': 'core/metrics.py:update_alarm_metrics',
    'scada_data_queue_size': 'core/metrics.py:update_queue_metrics',
    'scada_device_health_score': 'core/metrics.py:update_health_scores',
    'scada_oee_score': 'core/metrics.py:update_oee_scores',
}

#: **未接线**的指标 → 为什么不接。
#:
#: 仍然导出（保持与既有 Grafana 看板兼容），但**恒为 0**。
#: 与其假装它们在监控什么，不如把原因写在这里。
#: 守卫要求：`DEFINED_METRICS` 里每个指标必须二选一（WIRED 或 UNWIRED+原因）。
UNWIRED_METRICS = {
    'scada_modbus_requests_total': (
        '需要包住 Modbus 客户端的每一次请求（采集层/base_client.py 及各协议实现），'
        '逐请求打点会侵入协议层；本轮未做'),
    'scada_modbus_errors_total': (
        '同上：Modbus 异常码分类打点需要改协议层，本轮未做'),
    'scada_modbus_rtt_seconds': (
        '同上：往返耗时计时需要在协议层记录起止时刻，本轮未做'),
}

#: 一致性守卫用：本模块定义的全部指标家族名
DEFINED_METRICS = (
    'scada_devices_total',
    'scada_devices_connected',
    'scada_devices_fault',
    'scada_data_collected_total',
    'scada_data_errors_total',
    'scada_collection_duration_seconds',
    'scada_data_queue_size',
    'scada_alarms_total',
    'scada_alarms_active',
    'scada_alarms_acknowledged_total',
    'scada_alarm_duration_seconds',
    'scada_modbus_requests_total',
    'scada_modbus_errors_total',
    'scada_modbus_rtt_seconds',
    'scada_websocket_connections',
    'scada_websocket_messages_total',
    'scada_http_requests_total',
    'scada_http_request_duration_seconds',
    'scada_device_health_score',
    'scada_oee_score',
)
