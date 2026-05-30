"""
Tests for TSDB adapter and RealtimeDataBridge - covers 智能层/tsdb_adapter.py (12% -> high)
"""
import threading
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, PropertyMock


@pytest.fixture
def mock_tdengine():
    te = MagicMock()
    te.query_telemetry.return_value = [
        {'value': 25.5, 'timestamp': '2024-01-01T00:00:00'},
        {'value': 26.0, 'timestamp': '2024-01-01T00:01:00'},
    ]
    te.write_telemetry_batch.return_value = None
    te.write_oee.return_value = None
    te.write_predictive.return_value = None
    te.write_energy.return_value = None
    return te


@pytest.fixture
def mock_oee():
    oee = MagicMock()
    oee.get_all_oee.return_value = {
        'dev1': {
            'availability': 0.95,
            'performance': 0.90,
            'quality': 0.98,
            'oee': 0.837,
            'total_count': 1000,
            'good_count': 980,
            'actual_run_time': 450,
            'downtime': 50,
        }
    }
    return oee


@pytest.fixture
def mock_predictive():
    pm = MagicMock()
    pm.get_health_scores.return_value = {
        'dev1_temp': {
            'device_id': 'dev1',
            'health_score': 85,
            'failure_prediction': {'days_to_limit': 30, 'confidence': 0.7},
            'anomaly_count': 2,
            'trend': {'direction': 'declining'},
        }
    }
    return pm


@pytest.fixture
def mock_spc():
    spc = MagicMock()
    return spc


@pytest.fixture
def mock_energy():
    em = MagicMock()
    em.realtime_power = {
        'dev1': {'power_kw': 5.5, 'voltage': 220, 'current': 25, 'power_factor': 0.95}
    }
    em.energy_accumulated = {
        'dev1': {'energy_kwh': 100.5}
    }
    return em


class TestTSDBAdapterInit:
    def test_init_basic(self, mock_tdengine):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine)

        assert adapter.tdengine is mock_tdengine
        assert adapter.oee_calculator is None
        assert adapter.predictive_maintenance is None
        assert adapter.spc_analyzer is None
        assert adapter.energy_manager is None
        assert adapter._running is False
        assert adapter.sync_interval == 10
        assert adapter.result_interval == 60

    def test_init_with_config(self, mock_tdengine):
        from 智能层.tsdb_adapter import TSDBAdapter

        config = {'sync_interval': 5, 'result_interval': 30}
        adapter = TSDBAdapter(mock_tdengine, config=config)

        assert adapter.sync_interval == 5
        assert adapter.result_interval == 30

    def test_init_with_all_modules(self, mock_tdengine, mock_oee, mock_predictive, mock_spc, mock_energy):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(
            mock_tdengine,
            oee_calculator=mock_oee,
            predictive_maintenance=mock_predictive,
            spc_analyzer=mock_spc,
            energy_manager=mock_energy,
        )

        assert adapter.oee_calculator is mock_oee
        assert adapter.predictive_maintenance is mock_predictive
        assert adapter.spc_analyzer is mock_spc
        assert adapter.energy_manager is mock_energy


class TestRegisterDevice:
    def test_register_device(self, mock_tdengine):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine)
        adapter.register_device('dev1', [
            {'name': 'temperature', 'unit': 'C'},
            {'name': 'pressure', 'unit': 'MPa'},
        ])

        assert 'dev1' in adapter._registered_devices
        assert len(adapter._registered_devices['dev1']['registers']) == 2


class TestAdapterStartStop:
    def test_start_and_stop(self, mock_tdengine):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine, config={'sync_interval': 1, 'result_interval': 1})
        adapter.start()

        assert adapter._running is True
        assert adapter._sync_thread is not None
        assert adapter._result_thread is not None

        adapter.stop()

        assert adapter._running is False

    def test_start_twice(self, mock_tdengine):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine, config={'sync_interval': 60, 'result_interval': 60})
        adapter.start()
        adapter.start()  # Should warn and return

        adapter.stop()

    def test_stop_when_not_running(self, mock_tdengine):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine)
        adapter.stop()  # Should be a no-op


class TestFeedToModules:
    def test_feed_predictive_maintenance(self, mock_tdengine, mock_predictive):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine, predictive_maintenance=mock_predictive)

        data = [
            {'value': 25.5, 'timestamp': '2024-01-01T00:00:00'},
        ]
        adapter._feed_to_modules('dev1', 'temperature', data)

        mock_predictive.feed_data.assert_called_once()

    def test_feed_spc_quality_data(self, mock_tdengine, mock_spc):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine, spc_analyzer=mock_spc)

        data = [{'value': 25.5, 'timestamp': '2024-01-01T00:00:00'}]
        adapter._feed_to_modules('dev1', 'temperature', data)

        mock_spc.feed_data.assert_called_once()

    def test_feed_spc_non_quality_data(self, mock_tdengine, mock_spc):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine, spc_analyzer=mock_spc)

        data = [{'value': 25.5, 'timestamp': '2024-01-01T00:00:00'}]
        adapter._feed_to_modules('dev1', 'random_register', data)

        mock_spc.feed_data.assert_not_called()

    def test_feed_energy_power(self, mock_tdengine, mock_energy):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine, energy_manager=mock_energy)

        data = [{'value': 5.5, 'timestamp': '2024-01-01T00:00:00'}]
        adapter._feed_to_modules('dev1', 'power_meter', data)

        mock_energy.feed_power_data.assert_called_once()

    def test_feed_energy_kwh(self, mock_tdengine, mock_energy):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine, energy_manager=mock_energy)

        data = [{'value': 100.5, 'timestamp': '2024-01-01T00:00:00'}]
        adapter._feed_to_modules('dev1', 'energy_kwh_total', data)

        mock_energy.feed_power_data.assert_called_once()

    def test_feed_oee_running_status(self, mock_tdengine, mock_oee):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine, oee_calculator=mock_oee)

        data = [{'value': 1, 'timestamp': '2024-01-01T00:00:00'}]
        adapter._feed_to_modules('dev1', 'running_status', data)

        mock_oee.update_device_state.assert_called_once_with('dev1', 'running')

    def test_feed_oee_product_count(self, mock_tdengine, mock_oee):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine, oee_calculator=mock_oee)

        data = [{'value': 100, 'timestamp': '2024-01-01T00:00:00'}]
        adapter._feed_to_modules('dev1', 'product_count', data)

        mock_oee.record_production.assert_called_once()

    def test_feed_oee_good_count(self, mock_tdengine, mock_oee):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine, oee_calculator=mock_oee)

        data = [{'value': 95, 'timestamp': '2024-01-01T00:00:00'}]
        adapter._feed_to_modules('dev1', 'good_count', data)

        mock_oee.record_production.assert_called_once()

    def test_feed_skip_none_values(self, mock_tdengine, mock_predictive):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine, predictive_maintenance=mock_predictive)

        data = [{'value': None, 'timestamp': '2024-01-01T00:00:00'}]
        adapter._feed_to_modules('dev1', 'temperature', data)

        mock_predictive.feed_data.assert_not_called()

    def test_feed_with_datetime_timestamp(self, mock_tdengine, mock_predictive):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine, predictive_maintenance=mock_predictive)

        data = [{'value': 25.5, 'timestamp': datetime(2024, 1, 1)}]
        adapter._feed_to_modules('dev1', 'temperature', data)

        mock_predictive.feed_data.assert_called_once()


class TestSyncData:
    def test_sync_data(self, mock_tdengine):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine)
        adapter.register_device('dev1', [{'name': 'temperature', 'unit': 'C'}])

        adapter._sync_data()

        mock_tdengine.query_telemetry.assert_called()
        assert adapter.stats['sync_cycles'] == 1

    def test_sync_data_empty_result(self, mock_tdengine):
        from 智能层.tsdb_adapter import TSDBAdapter

        mock_tdengine.query_telemetry.return_value = []
        adapter = TSDBAdapter(mock_tdengine)
        adapter.register_device('dev1', [{'name': 'temperature', 'unit': 'C'}])

        adapter._sync_data()

        assert adapter.stats['records_synced'] == 0

    def test_sync_data_error(self, mock_tdengine):
        from 智能层.tsdb_adapter import TSDBAdapter

        mock_tdengine.query_telemetry.side_effect = Exception("db error")
        adapter = TSDBAdapter(mock_tdengine)
        adapter.register_device('dev1', [{'name': 'temperature', 'unit': 'C'}])

        adapter._sync_data()

        assert adapter.stats['errors'] == 1

    def test_sync_data_with_last_sync(self, mock_tdengine):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine)
        adapter.register_device('dev1', [{'name': 'temperature', 'unit': 'C'}])
        adapter._registered_devices['dev1']['last_sync'] = datetime.now() - timedelta(minutes=5)

        adapter._sync_data()

        mock_tdengine.query_telemetry.assert_called()


class TestWriteResults:
    def test_write_oee_results(self, mock_tdengine, mock_oee):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine, oee_calculator=mock_oee)
        adapter._write_oee_results()

        mock_tdengine.write_oee.assert_called_once()
        assert adapter.stats['results_written'] == 1

    def test_write_oee_no_calculator(self, mock_tdengine):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine)
        adapter._write_oee_results()  # Should not raise

    def test_write_oee_error(self, mock_tdengine, mock_oee):
        from 智能层.tsdb_adapter import TSDBAdapter

        mock_tdengine.write_oee.side_effect = Exception("write error")
        adapter = TSDBAdapter(mock_tdengine, oee_calculator=mock_oee)
        adapter._write_oee_results()

        assert adapter.stats['errors'] == 1

    def test_write_predictive_results(self, mock_tdengine, mock_predictive):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine, predictive_maintenance=mock_predictive)
        adapter._write_predictive_results()

        mock_tdengine.write_predictive.assert_called_once()

    def test_write_predictive_no_module(self, mock_tdengine):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine)
        adapter._write_predictive_results()

    def test_write_predictive_no_device_id(self, mock_tdengine, mock_predictive):
        from 智能层.tsdb_adapter import TSDBAdapter

        mock_predictive.get_health_scores.return_value = {'key': {'health_score': 80}}
        adapter = TSDBAdapter(mock_tdengine, predictive_maintenance=mock_predictive)
        adapter._write_predictive_results()

    def test_write_predictive_error(self, mock_tdengine, mock_predictive):
        from 智能层.tsdb_adapter import TSDBAdapter

        mock_tdengine.write_predictive.side_effect = Exception("error")
        adapter = TSDBAdapter(mock_tdengine, predictive_maintenance=mock_predictive)
        adapter._write_predictive_results()

        assert adapter.stats['errors'] == 1

    def test_write_energy_results(self, mock_tdengine, mock_energy):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine, energy_manager=mock_energy)
        adapter._registered_devices = {'dev1': {'registers': []}}
        adapter._write_energy_results()

        mock_tdengine.write_energy.assert_called_once()

    def test_write_energy_no_manager(self, mock_tdengine):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine)
        adapter._write_energy_results()

    def test_write_energy_no_power_data(self, mock_tdengine, mock_energy):
        from 智能层.tsdb_adapter import TSDBAdapter

        mock_energy.realtime_power = {}
        adapter = TSDBAdapter(mock_tdengine, energy_manager=mock_energy)
        adapter._registered_devices = {'dev1': {'registers': []}}
        adapter._write_energy_results()

    def test_write_energy_error(self, mock_tdengine, mock_energy):
        from 智能层.tsdb_adapter import TSDBAdapter

        mock_tdengine.write_energy.side_effect = Exception("error")
        adapter = TSDBAdapter(mock_tdengine, energy_manager=mock_energy)
        adapter._registered_devices = {'dev1': {'registers': []}}
        adapter._write_energy_results()

        assert adapter.stats['errors'] == 1

    def test_write_results_all(self, mock_tdengine, mock_oee, mock_predictive, mock_energy):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(
            mock_tdengine,
            oee_calculator=mock_oee,
            predictive_maintenance=mock_predictive,
            energy_manager=mock_energy,
        )
        adapter._registered_devices = {'dev1': {'registers': []}}
        adapter._write_results()

        assert adapter.stats['last_result_time'] is not None


class TestGetStats:
    def test_get_stats(self, mock_tdengine):
        from 智能层.tsdb_adapter import TSDBAdapter

        adapter = TSDBAdapter(mock_tdengine)
        adapter.register_device('dev1', [{'name': 'temp'}])

        stats = adapter.get_stats()

        assert 'running' in stats
        assert 'registered_devices' in stats
        assert stats['registered_devices'] == 1


# ── RealtimeDataBridge Tests ──

class TestRealtimeDataBridge:
    def test_init(self, mock_tdengine):
        from 智能层.tsdb_adapter import RealtimeDataBridge

        bridge = RealtimeDataBridge(mock_tdengine)

        assert bridge.tdengine is mock_tdengine
        assert bridge._running is False
        assert bridge._batch_size == 100

    def test_init_with_offline_buffer(self, mock_tdengine):
        from 智能层.tsdb_adapter import RealtimeDataBridge

        buffer = MagicMock()
        bridge = RealtimeDataBridge(mock_tdengine, offline_buffer=buffer)

        assert bridge.offline_buffer is buffer

    def test_start_stop(self, mock_tdengine):
        from 智能层.tsdb_adapter import RealtimeDataBridge

        bridge = RealtimeDataBridge(mock_tdengine)
        bridge.start()

        assert bridge._running is True

        bridge.stop()
        assert bridge._running is False

    def test_start_twice(self, mock_tdengine):
        from 智能层.tsdb_adapter import RealtimeDataBridge

        bridge = RealtimeDataBridge(mock_tdengine)
        bridge.start()
        bridge.start()  # Should be no-op

        bridge.stop()

    def test_feed(self, mock_tdengine):
        from 智能层.tsdb_adapter import RealtimeDataBridge

        bridge = RealtimeDataBridge(mock_tdengine)
        bridge.feed('dev1', 'temperature', 25.5)

        assert bridge.stats['records_received'] == 1

    def test_feed_with_custom_params(self, mock_tdengine):
        from 智能层.tsdb_adapter import RealtimeDataBridge

        bridge = RealtimeDataBridge(mock_tdengine)
        bridge.feed('dev1', 'temperature', 25.5, timestamp=datetime.now(),
                    unit='C', protocol='modbus', gateway_id='gw1', quality=192)

        assert bridge.stats['records_received'] == 1

    def test_feed_triggers_flush_at_batch_size(self, mock_tdengine):
        from 智能层.tsdb_adapter import RealtimeDataBridge

        bridge = RealtimeDataBridge(mock_tdengine)
        bridge._batch_size = 3

        bridge.feed('dev1', 'temp', 1.0)
        bridge.feed('dev1', 'temp', 2.0)
        bridge.feed('dev1', 'temp', 3.0)  # Should trigger flush

        mock_tdengine.write_telemetry_batch.assert_called()

    def test_flush_buffer_empty(self, mock_tdengine):
        from 智能层.tsdb_adapter import RealtimeDataBridge

        bridge = RealtimeDataBridge(mock_tdengine)
        bridge._flush_buffer()  # Should do nothing

        mock_tdengine.write_telemetry_batch.assert_not_called()

    def test_flush_buffer_with_data(self, mock_tdengine):
        from 智能层.tsdb_adapter import RealtimeDataBridge

        bridge = RealtimeDataBridge(mock_tdengine)
        bridge.feed('dev1', 'temp', 25.5)
        bridge._flush_buffer()

        mock_tdengine.write_telemetry_batch.assert_called_once()
        assert bridge.stats['records_written'] == 1

    def test_flush_buffer_error_with_offline(self, mock_tdengine):
        from 智能层.tsdb_adapter import RealtimeDataBridge

        mock_tdengine.write_telemetry_batch.side_effect = Exception("write error")
        offline_buf = MagicMock()
        bridge = RealtimeDataBridge(mock_tdengine, offline_buffer=offline_buf)

        bridge.feed('dev1', 'temp', 25.5)
        bridge._flush_buffer()

        assert bridge.stats['errors'] == 1
        offline_buf.buffer_records.assert_called_once()

    def test_flush_buffer_error_no_offline(self, mock_tdengine):
        from 智能层.tsdb_adapter import RealtimeDataBridge

        mock_tdengine.write_telemetry_batch.side_effect = Exception("write error")
        bridge = RealtimeDataBridge(mock_tdengine)

        bridge.feed('dev1', 'temp', 25.5)
        bridge._flush_buffer()

        assert bridge.stats['errors'] == 1
        # Records should be put back in buffer
        assert len(bridge._buffer) == 1

    def test_flush_buffer_offline_also_fails(self, mock_tdengine):
        from 智能层.tsdb_adapter import RealtimeDataBridge

        mock_tdengine.write_telemetry_batch.side_effect = Exception("write error")
        offline_buf = MagicMock()
        offline_buf.buffer_records.side_effect = Exception("offline error")
        bridge = RealtimeDataBridge(mock_tdengine, offline_buffer=offline_buf)

        bridge.feed('dev1', 'temp', 25.5)
        bridge._flush_buffer()

        assert bridge.stats['errors'] == 1
        # Records should be put back in buffer
        assert len(bridge._buffer) == 1

    def test_stop_flushes_remaining(self, mock_tdengine):
        from 智能层.tsdb_adapter import RealtimeDataBridge

        bridge = RealtimeDataBridge(mock_tdengine)
        bridge.feed('dev1', 'temp', 25.5)
        bridge.start()
        bridge.stop()

        mock_tdengine.write_telemetry_batch.assert_called()

    def test_get_stats(self, mock_tdengine):
        from 智能层.tsdb_adapter import RealtimeDataBridge

        bridge = RealtimeDataBridge(mock_tdengine)
        stats = bridge.get_stats()

        assert 'running' in stats
        assert 'buffer_size' in stats
        assert stats['running'] is False
