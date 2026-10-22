# -*- coding: utf-8 -*-
"""Prometheus 指标必须有人写（round 253）。

背景
----
`core/metrics.py` 定义了 15 个指标家族，而**其中 12 个从来没有被任何生产
代码递增过** —— `/metrics` 把它们全部导出成恒为 0 的线。

对 Prometheus/Grafana 来说，这比"没有这个指标"更糟：看板上会出现一条平直
的 0 曲线，看上去像"系统安静"，实际是"没人往这里写"。
具体到本项目：
- 采集一直在失败，`scada_data_errors_total` 恒 0 → 看不出设备失联；
- 报警在刷，`scada_alarms_total` 恒 0 → 看不出报警率；
- 有 5 个客户端连着，`scada_websocket_connections` 恒 0 → 连接数面板永远 0；
- 接口在慢，`scada_http_request_duration_seconds` 恒 0 → 没有延迟数据。

本轮接线：采集读数 / 采集错误 / 报警总数 / WebSocket 连接数 / HTTP 请求与耗时。
其余指标在 `UNWIRED_METRICS` 里逐条写明**为什么没接**（不是"忘了"）。

本文件的守卫分两类：
1. 行为：接线后的指标**真的会动**（不是只写了个 `inc()` 却走不到）；
2. 静态：每个定义出来的指标要么有真实写入点，要么在未接线清单里 ——
   新增指标时必须二选一，不许默默躺平。
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _sample(name: str) -> float:
    """读一个指标家族的当前值（所有标签合计）。"""
    from prometheus_client import REGISTRY
    total = 0.0
    for metric in REGISTRY.collect():
        if metric.name != name:
            continue
        for s in metric.samples:
            if s.name.endswith('_total') and not s.name.endswith('_created'):
                total += s.value
    return total


# ===========================================================================
# 1. 采集读数：真的会涨
# ===========================================================================

class TestDataCollected:
    def _collector(self):
        import queue
        from 采集层.data_collector import DataCollector
        dc = object.__new__(DataCollector)
        dc.data_queue = queue.Queue(maxsize=100)
        dc.stats = {}
        dc._stats_lock = __import__('threading').Lock()
        dc._protocol_cache = {}
        dc.device_manager = MagicMock()
        dc.device_manager.devices = {'D1': {'protocol': 'modbus_tcp'}}
        return dc

    def test_reading_increments_counter(self):
        from core.metrics import DATA_COLLECTED
        dc = self._collector()
        before = DATA_COLLECTED.labels(device_id='D1', protocol='modbus_tcp')._value.get()
        dc._enqueue_drop_oldest({'device_id': 'D1', 'register_name': 'r',
                                 'value': 1.0, 'quality': 192})
        after = DATA_COLLECTED.labels(device_id='D1', protocol='modbus_tcp')._value.get()
        assert after == before + 1

    def test_bad_quality_fallback_is_not_counted(self):
        """断路器兜底数据是合成的，不能算进"采集速率"。

        否则设备失联时看板上的采集速率依然漂亮 —— 那正是最需要看出来的场景。
        """
        from core.metrics import DATA_COLLECTED
        dc = self._collector()
        before = DATA_COLLECTED.labels(device_id='D1', protocol='modbus_tcp')._value.get()
        dc._enqueue_drop_oldest({'device_id': 'D1', 'register_name': 'r',
                                 'value': 1.0, 'quality': 'BAD'})
        after = DATA_COLLECTED.labels(device_id='D1', protocol='modbus_tcp')._value.get()
        assert after == before, 'BAD 质量的兜底数据被算成了真实采集'

    def test_protocol_label_comes_from_device_config(self):
        """协议标签必须来自设备配置（基数可控），不能是未知常量。"""
        dc = self._collector()
        assert dc._protocol_of('D1') == 'modbus_tcp'
        # 缓存命中
        assert dc._protocol_of('D1') == 'modbus_tcp'

    def test_unknown_device_does_not_crash(self):
        dc = self._collector()
        assert dc._protocol_of('no-such-device') == 'unknown'


# ===========================================================================
# 2. 采集错误：失败分支必须计数
# ===========================================================================

class TestDataErrors:
    def _collector(self):
        from 采集层.data_collector import DataCollector
        dc = object.__new__(DataCollector)
        dc.stats = {}
        dc._stats_lock = __import__('threading').Lock()
        dc._protocol_cache = {}
        dc.device_manager = MagicMock()
        dc.device_manager.devices = {'D1': {'protocol': 'modbus_tcp'}}
        dc.device_manager.get_client.return_value = None      # 触发 no_client 分支
        return dc

    def test_no_client_counts_error(self):
        from core.metrics import DATA_ERRORS
        dc = self._collector()
        before = DATA_ERRORS.labels(device_id='D1', error_type='no_client')._value.get()
        ok = dc._collect_device_data('D1', {'protocol': 'modbus_tcp'}, 'modbus_tcp')
        after = DATA_ERRORS.labels(device_id='D1', error_type='no_client')._value.get()
        assert ok is False
        assert after == before + 1

    def test_unsupported_protocol_counts_error(self):
        from core.metrics import DATA_ERRORS
        dc = self._collector()
        dc.device_manager.get_client.return_value = MagicMock(connected=True)
        before = DATA_ERRORS.labels(device_id='D1', error_type='unsupported_protocol')._value.get()
        ok = dc._collect_device_data('D1', {'protocol': 'weird'}, 'weird')
        after = DATA_ERRORS.labels(device_id='D1', error_type='unsupported_protocol')._value.get()
        assert ok is False
        assert after == before + 1


# ===========================================================================
# 3. 报警 / WebSocket / HTTP
# ===========================================================================

class TestAlarmAndHttp:
    def test_alarm_counter_increments(self, tmp_path):
        import yaml
        from 报警层.alarm_manager import AlarmManager
        from core.metrics import ALARMS_TOTAL

        cfg = {'alarm_rules': [{
            'id': 'r_m', 'name': '温度过高', 'device_id': 'D1',
            'register_name': 'temp', 'condition': 'greater_than',
            'threshold': 100, 'level': 'critical', 'enabled': True}]}
        p = tmp_path / 'alarms.yaml'
        p.write_text(yaml.dump(cfg, allow_unicode=True), encoding='utf-8')
        am = AlarmManager(MagicMock(), str(p))
        am.alarm_output = None
        am.broadcast_system = None
        try:
            before = ALARMS_TOTAL.labels(level='critical', device_id='D1')._value.get()
            am._trigger_alarm(
                {'id': 'r_m', 'name': '温度过高', 'level': 'critical',
                 'device_id': 'D1', 'register_name': 'temp', 'threshold': 100},
                'D1', 'temp', 120.0, __import__('datetime').datetime.now())
            after = ALARMS_TOTAL.labels(level='critical', device_id='D1')._value.get()
            assert after == before + 1
        finally:
            am.stop()

    def test_http_metrics_use_route_template_not_raw_path(self):
        """标签必须是路由模板，否则每个设备 id 一条时间序列 → 基数爆炸。

        静态守卫而非行为断言：`after_request` 钩子装在 `create_app()` 里，
        而测试用的最小 Flask app（conftest 的 `app`）不走那条路径，
        行为断言会恒绿（拿不到数据）。这里直接读源码，盯住真正的风险点：
        **endpoint 必须由 `request.url_rule` 派生**，不能用 `request.path`。

        ⚠️ 不能用「函数体里不许出现 request.path」来判 —— 慢请求日志那行
        本来就会打 `request.path`（那是合理的，日志要具体路径）。
        所以只盯 `endpoint = ...` 这一处赋值。
        """
        import re
        src = (PROJECT_ROOT / '展示层' / 'routes.py').read_text(encoding='utf-8')
        assert "HTTP_REQUESTS.labels(" in src
        # endpoint 必须由 url_rule 派生。注意不能只搜 `endpoint = ...` 再看
        # 里面有没有 'url_rule' —— 实际写法是 `rule = request.url_rule` 然后
        # `endpoint = rule.rule`，捕获组里只有 `rule.rule`（第一版就这么假红）。
        assert re.search(r'rule\s*=\s*request\.url_rule', src), (
            'HTTP 指标没有从 request.url_rule 取路由模板')
        assert re.search(r'endpoint\s*=\s*rule\.rule', src), (
            'endpoint 标签不是由 url_rule 派生的（可能用了 request.path，基数会爆炸）')
        assert not re.search(r'endpoint\s*=\s*request\.path', src), (
            'endpoint 标签直接用了 request.path —— 每个路径参数都会生成新时间序列')

    def test_ws_connections_set_on_both_transitions(self):
        """连接数 gauge 必须在 connect 与 disconnect **两处**都设置。

        只设 connect 的话，客户端断开后计数只增不减，看板上的连接数会
        一路虚高（比恒 0 更误导）。所以按函数切段，逐个要求出现 `.set(`。
        """
        src = (PROJECT_ROOT / '展示层' / 'websocket.py').read_text(encoding='utf-8')
        for func in ('handle_connect', 'handle_disconnect'):
            start = src.index(f'def {func}(')
            # 切到下一个 @socketio.on 或下一个 def 为止
            nxt = src.find('\n    @socketio.on', start)
            body = src[start:nxt if nxt != -1 else len(src)]
            assert 'WS_CONNECTIONS.set(' in body, (
                f'{func} 没有设置 WS_CONNECTIONS —— 连接数会与实际不符')


# ===========================================================================
# 4. 静态守卫：定义出来的指标必须二选一
# ===========================================================================

class TestRegistryConsistency:
    def test_every_metric_is_wired_or_declared_unwired(self):
        """每个指标家族要么有真实写入点，要么在 UNWIRED_METRICS 里写明原因。

        这条守卫防的是本轮修掉的那个病：定义了 15 个指标、只有 3 个被设置，
        其余全是恒 0 的线，而没有任何地方提示"它们没接"。
        """
        from core.metrics import DEFINED_METRICS, WIRED_METRICS, UNWIRED_METRICS
        declared = set(WIRED_METRICS) | set(UNWIRED_METRICS)
        missing = [m for m in DEFINED_METRICS if m not in declared]
        assert not missing, (
            f'这些指标既没接线也没登记原因: {missing} —— '
            f'它们会以恒 0 的形式出现在 /metrics 上')

    def test_unwired_entries_have_reasons(self):
        from core.metrics import UNWIRED_METRICS
        for name, reason in UNWIRED_METRICS.items():
            assert reason and len(reason) > 10, f'{name} 没写清为什么没接线'

    @pytest.mark.parametrize('metric,location,const', [
        ('scada_data_collected_total', '采集层/data_collector.py', 'DATA_COLLECTED'),
        ('scada_data_errors_total', '采集层/data_collector.py', 'DATA_ERRORS'),
        ('scada_alarms_total', '报警层/alarm_manager.py', 'ALARMS_TOTAL'),
        ('scada_websocket_connections', '展示层/websocket.py', 'WS_CONNECTIONS'),
        ('scada_http_requests_total', '展示层/routes.py', 'HTTP_REQUESTS'),
    ])
    def test_wired_metric_has_real_writer(self, metric, location, const):
        """WIRED_METRICS 声称的写入点必须真实存在（防止声明漂移）。

        注意常量名与指标名的关系：Prometheus 的 `_total` 后缀是**导出时**
        由 Counter 自动加的，Python 常量里没有（`HTTP_REQUESTS` 而不是
        `HTTP_REQUESTS_TOTAL`）。第一版按指标名推常量名，四个用例全假红。
        """
        from core.metrics import WIRED_METRICS
        assert metric in WIRED_METRICS
        src = (PROJECT_ROOT / location).read_text(encoding='utf-8')
        assert const in src, (
            f'{location} 里找不到 {const} —— WIRED_METRICS 的声明已漂移')
