"""
Tests for core.metrics: MetricsCollector update methods and Prometheus output format
"""

import pytest
from unittest.mock import MagicMock, PropertyMock
from queue import Queue

from core.metrics import (
    MetricsCollector,
    DEVICES_CONNECTED,
    DEVICES_FAULT,
    DEVICES_TOTAL,
    ALARMS_ACTIVE,
    QUEUE_SIZE,
    SCADA_INFO,
    DATA_COLLECTED,
    DATA_ERRORS,
    COLLECTION_DURATION,
    MODBUS_REQUESTS,
    MODBUS_ERRORS,
    MODBUS_RTT,
    WS_CONNECTIONS,
    WS_MESSAGES,
    HTTP_REQUESTS,
    HTTP_DURATION,
    HEALTH_SCORE,
    OEE_SCORE,
)


@pytest.fixture
def collector():
    """Create a fresh MetricsCollector"""
    return MetricsCollector()


# ============================================================
# Device Metrics Tests
# ============================================================

class TestDeviceMetrics:

    def test_update_device_metrics_list_format(self, collector):
        """update_device_metrics handles list status format"""
        dm = MagicMock()
        dm.get_all_status.return_value = [
            {'connected': True, 'protocol': 'modbus_tcp', 'status': 'running'},
            {'connected': True, 'protocol': 'opcua', 'status': 'running'},
            {'connected': False, 'protocol': 'modbus_tcp', 'status': 'fault'},
        ]
        collector.update_device_metrics(dm)
        # Should not raise
        assert DEVICES_CONNECTED._value.get() == 2

    def test_update_device_metrics_dict_format(self, collector):
        """update_device_metrics handles dict status format"""
        dm = MagicMock()
        dm.get_all_status.return_value = {
            'dev1': {'connected': True, 'protocol': 'modbus_tcp', 'status': 'running'},
            'dev2': {'connected': False, 'protocol': 'opcua', 'status': 'fault'},
        }
        collector.update_device_metrics(dm)
        assert DEVICES_CONNECTED._value.get() == 1

    def test_update_device_metrics_exception_safe(self, collector):
        """update_device_metrics doesn't raise on bad input"""
        dm = MagicMock()
        dm.get_all_status.side_effect = RuntimeError('fail')
        collector.update_device_metrics(dm)  # should not raise

    def test_update_device_metrics_empty_list(self, collector):
        """update_device_metrics handles empty list"""
        dm = MagicMock()
        dm.get_all_status.return_value = []
        collector.update_device_metrics(dm)


# ============================================================
# Alarm Metrics Tests
# ============================================================

class TestAlarmMetrics:

    def test_update_alarm_metrics(self, collector):
        """update_alarm_metrics sets active alarm count"""
        am = MagicMock()
        am.get_active_alarms.return_value = [
            {'id': 'a1'}, {'id': 'a2'}, {'id': 'a3'}
        ]
        collector.update_alarm_metrics(am)
        assert ALARMS_ACTIVE._value.get() == 3

    def test_update_alarm_metrics_empty(self, collector):
        """update_alarm_metrics handles empty alarms"""
        am = MagicMock()
        am.get_active_alarms.return_value = []
        collector.update_alarm_metrics(am)
        assert ALARMS_ACTIVE._value.get() == 0

    def test_update_alarm_metrics_exception_safe(self, collector):
        """update_alarm_metrics doesn't raise on error"""
        am = MagicMock()
        am.get_active_alarms.side_effect = RuntimeError('fail')
        collector.update_alarm_metrics(am)  # should not raise


# ============================================================
# Queue Metrics Tests
# ============================================================

class TestQueueMetrics:

    def test_update_queue_metrics(self, collector):
        """update_queue_metrics sets queue size gauge"""
        dc = MagicMock()
        dc.data_queue = MagicMock()
        dc.data_queue.qsize.return_value = 42
        collector.update_queue_metrics(dc)
        assert QUEUE_SIZE._value.get() == 42

    def test_update_queue_metrics_exception_safe(self, collector):
        """update_queue_metrics doesn't raise on error"""
        dc = MagicMock()
        dc.data_queue = MagicMock()
        dc.data_queue.qsize.side_effect = RuntimeError('fail')
        collector.update_queue_metrics(dc)  # should not raise


# ============================================================
# Prometheus Output Tests
# ============================================================

class TestPrometheusOutput:

    def test_get_metrics_returns_bytes(self, collector):
        """get_metrics returns bytes (Prometheus text format)"""
        output = collector.get_metrics()
        assert isinstance(output, bytes)

    def test_output_contains_scada_info(self, collector):
        """Output contains scada_info metric"""
        output = collector.get_metrics().decode('utf-8')
        assert 'scada_info' in output

    def test_output_contains_version(self, collector):
        """Output contains version info"""
        output = collector.get_metrics().decode('utf-8')
        assert 'version' in output

    def test_output_contains_device_gauges(self, collector):
        """Output contains device gauge definitions"""
        output = collector.get_metrics().decode('utf-8')
        assert 'scada_devices_connected' in output
        assert 'scada_devices_fault' in output
        assert 'scada_devices_total' in output

    def test_output_contains_alarm_gauges(self, collector):
        """Output contains alarm gauge definitions"""
        output = collector.get_metrics().decode('utf-8')
        assert 'scada_alarms_active' in output
        assert 'scada_alarms_total' in output

    def test_output_contains_queue_gauge(self, collector):
        """Output contains queue size gauge"""
        output = collector.get_metrics().decode('utf-8')
        assert 'scada_data_queue_size' in output

    def test_output_contains_modbus_counters(self, collector):
        """Output contains Modbus counters"""
        output = collector.get_metrics().decode('utf-8')
        assert 'scada_modbus_requests_total' in output
        assert 'scada_modbus_errors_total' in output

    def test_output_contains_websocket_metrics(self, collector):
        """Output contains WebSocket metrics"""
        output = collector.get_metrics().decode('utf-8')
        assert 'scada_websocket_connections' in output
        assert 'scada_websocket_messages_total' in output

    def test_output_contains_http_metrics(self, collector):
        """Output contains HTTP metrics"""
        output = collector.get_metrics().decode('utf-8')
        assert 'scada_http_requests_total' in output
        assert 'scada_http_request_duration_seconds' in output

    def test_output_contains_smart_layer_metrics(self, collector):
        """Output contains smart layer metrics"""
        output = collector.get_metrics().decode('utf-8')
        assert 'scada_device_health_score' in output
        assert 'scada_oee_score' in output

    def test_output_contains_data_collection_metrics(self, collector):
        """Output contains data collection metrics"""
        output = collector.get_metrics().decode('utf-8')
        assert 'scada_data_collected_total' in output
        assert 'scada_data_errors_total' in output
        assert 'scada_collection_duration_seconds' in output


# ============================================================
# Global Metric Instances Tests
# ============================================================

class TestGlobalMetrics:

    def test_scada_info_has_version(self):
        """SCADA_INFO contains version"""
        # SCADA_INFO is already set at module level
        output = collector_get_output()
        assert '3.0.0' in output

    def test_metrics_collector_singleton(self):
        """metrics_collector is a MetricsCollector instance"""
        from core.metrics import metrics_collector
        assert isinstance(metrics_collector, MetricsCollector)


def collector_get_output():
    """Helper to get metrics output"""
    return MetricsCollector().get_metrics().decode('utf-8')
