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
# round 299：**必须带 level 维度**。原来是无 label 的单值，
# 而 `docs/MONITORING.md` 的正式告警规则写的是
# `scada_alarms_active{level="critical"} > 10` —— 这条序列根本不存在，
# PromQL 返回空向量，规则**永不触发**：200 条 critical 堆积也一声不响。
ALARMS_ACTIVE = Gauge('scada_alarms_active', '活跃告警数', ['level'])
ALARMS_ACKNOWLEDGED = Counter('scada_alarms_acknowledged_total', '已确认告警数')
ALARM_DURATION = Histogram('scada_alarm_duration_seconds', '告警持续时间', ['level'])
# 被**抑制**掉的告警数（round 290）。
#
# 为什么必须单独计数：抑制是"把报警压下去"的动作，压下去以后看板上一切安静 ——
# 而"现场真的安静"和"有 200 条报警被抑制吞了"在看板上长得**一模一样**。
# 此前抑制只落一行日志（洪水是 WARNING、去重是 DEBUG），既不能聚合也不能告警。
# 有了这条计数，"报警率下降"才能区分成两种完全不同的原因。
#
# reason 取值：
#   * `flood`           —— 洪水抑制期（ISA-18.2：记录照旧，只压播报）
#   * `dedup_cooldown`  —— 同一报警在冷却窗口内重复出现，压掉前端推送
#   * `dedup_acked`     —— 已确认报警在抑制窗口内重复出现，压掉前端推送
ALARMS_SUPPRESSED = Counter('scada_alarms_suppressed_total',
                            '被抑制的告警数（记录照旧，播报/推送被压掉）',
                            ['level', 'reason'])

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
            # get_all_status() 的真实契约是 list（device_manager.py:599），
            # 但历史上也接受 dict。这里统一归一化成 [(protocol, connected), ...]，
            # 两条路径共用同一套聚合 —— 否则改了一条忘另一条，
            # 修复就只活在 mock 里。
            if isinstance(status, list):
                rows = [(s.get('protocol', 'unknown'), bool(s.get('connected')))
                        for s in status]
                fault = sum(1 for s in status if s.get('status') == 'fault')
            elif isinstance(status, dict):
                rows = [(info.get('protocol', 'unknown'), bool(info.get('connected')))
                        for info in status.values()]
                fault = sum(1 for info in status.values()
                            if info.get('status') == 'fault')
            else:
                return

            DEVICES_CONNECTED.set(sum(1 for _proto, ok in rows if ok))
            DEVICES_FAULT.set(fault)
            # round 299：`status` 维度原来**恒为字面量 'total'**，
            # 而 `docs/MONITORING.md` 的规则是
            # `scada_devices_total{status="offline"} > 0` —— 这条序列根本不存在，
            # 规则永不触发：全厂掉线也一声不响。
            # round 299 续：第一版只改了 dict 分支，而 get_all_status() 真实返回
            # **list** —— 生产路径反而退化成 protocol="unknown" 且三个值全 0，
            # 比修复前更糟（原 total 至少是对的），而测试因为用 dict 做 mock 仍全绿。
            per_proto: dict[str, list[int]] = {}
            for proto, is_connected in rows:
                slot = per_proto.setdefault(proto, [0, 0])  # [online, total]
                slot[1] += 1
                if is_connected:
                    slot[0] += 1
            for proto, (online, total) in per_proto.items():
                DEVICES_TOTAL.labels(protocol=proto, status='total').set(total)
                DEVICES_TOTAL.labels(protocol=proto, status='online').set(online)
                DEVICES_TOTAL.labels(protocol=proto, status='offline').set(total - online)
        except Exception as e:
            logger.debug(f"设备指标更新失败: {e}")

    def update_alarm_metrics(self, alarm_manager):
        """从告警管理器收集指标"""
        try:
            active = alarm_manager.get_active_alarms() or []
            # 按级别分组；本轮没出现的级别显式置 0，
            # 否则某级别清零后旧值会一直挂着（比"没有样本"更误导）。
            by_level: dict[str, int] = {}
            for alarm in active:
                level = 'unknown'
                if isinstance(alarm, dict):
                    level = alarm.get('alarm_level') or alarm.get('level') or 'unknown'
                by_level[level] = by_level.get(level, 0) + 1
            for level in ('critical', 'warning', 'info', 'unknown'):
                ALARMS_ACTIVE.labels(level=level).set(by_level.get(level, 0))
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
