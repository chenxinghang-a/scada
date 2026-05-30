"""
Prometheus指标导出 - 工业SCADA监控
提供采集/告警/连接/性能指标
"""
from prometheus_client import Counter, Gauge, Histogram, Info, generate_latest, CONTENT_TYPE_LATEST
import threading

# 系统信息
SCADA_INFO = Info('scada', 'SCADA系统信息')
SCADA_INFO.info({
    'version': '3.0.0',
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
        except Exception:
            pass

    def update_alarm_metrics(self, alarm_manager):
        """从告警管理器收集指标"""
        try:
            active = alarm_manager.get_active_alarms()
            ALARMS_ACTIVE.set(len(active))
        except Exception:
            pass

    def update_queue_metrics(self, data_collector):
        """从数据采集器收集指标"""
        try:
            QUEUE_SIZE.set(data_collector.data_queue.qsize())
        except Exception:
            pass

    def get_metrics(self) -> bytes:
        """获取所有指标（Prometheus格式）"""
        return generate_latest()


# 全局指标收集器实例
metrics_collector = MetricsCollector()
