# -*- coding: utf-8 -*-
"""round 382：中危核验后确认的几条可稳定单测的回归。"""
from __future__ import annotations

import pytest


class TestBackoff:
    def test_backoff_never_shorter_than_base(self):
        """base=120s 时退避不能被 60s 上限压得比正常周期更勤。"""
        from 采集层.data_collector import compute_backoff_interval

        for failures in (1, 2, 5, 10):
            value = compute_backoff_interval(failures, base_interval=120)
            assert value >= 120

    def test_normal_base_still_grows(self):
        from 采集层.data_collector import compute_backoff_interval

        assert compute_backoff_interval(3, 5) == 8
        assert compute_backoff_interval(10, 5) == 60


class TestBriefItems:
    def test_mixed_types_do_not_raise(self):
        """registers/nodes/endpoints 被写成裸字符串时不能拖垮整批状态。"""
        from 采集层.device_manager import _brief_items

        assert _brief_items([
            {'name': 'temp', 'unit': 'C'},
            'ns=2;s=Pressure',
            None,
        ]) == [
            {'name': 'temp', 'unit': 'C'},
            {'name': 'ns=2;s=Pressure', 'unit': ''},
            {'name': 'None', 'unit': ''},
        ]


class TestPushCallbackDedupe:
    def _collector(self, client):
        from 采集层.data_collector import DataCollector

        dc = DataCollector.__new__(DataCollector)
        dc.running = True
        dc._cancelled_devices = set()
        dc._push_callbacks = {}
        dc.device_manager = type('M', (), {'get_client': lambda self, _id: client})()
        dc._enqueue_drop_oldest = lambda item: None
        return dc

    def test_repeated_setup_registers_once(self):
        from 采集层.data_collector import DataCollector

        class Client:
            connected = False

            def __init__(self):
                self.callbacks = 0
                self.connects = 0

            def add_data_callback(self, cb):
                self.callbacks += 1

            def connect(self):
                self.connects += 1
                self.connected = True
                return True

        client = Client()
        dc = self._collector(client)
        cfg = {'protocol': 'mqtt'}

        assert dc._setup_push_device('d1', cfg) is True
        assert dc._setup_push_device('d1', cfg) is True
        assert client.callbacks == 1, '重复启动叠加了多个回调'
        assert client.connects == 1, '已连接时仍重复 connect'

    def test_missing_callback_capability_returns_false(self):
        class Client:
            def connect(self):
                return True

        dc = self._collector(Client())
        assert dc._setup_push_device('d1', {'protocol': 'opcua'}) is False


class TestPushSyncNotDuplicated:
    def test_connected_push_device_is_not_restarted(self):
        from 采集层.data_collector import DataCollector

        class Client:
            connected = True

        dc = DataCollector.__new__(DataCollector)
        dc.running = True
        dc.tasks = {}
        dc._tasks_lock = __import__('threading').Lock()
        dc.device_manager = type('M', (), {
            'get_all_devices': lambda self: {'d1': {'protocol': 'mqtt'}},
            'clients': {'d1': Client()},
        })()
        dc._start_device_task = lambda *a, **k: None
        dc.start_device_task = lambda *a, **k: True

        result = dc.sync_device_tasks()
        assert result['started'] == []
        assert 'd1' in result['unchanged']
