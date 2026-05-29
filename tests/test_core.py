"""
Tests for core module: DI container, event bus, config manager, health checker, service response
"""

import pytest
import tempfile
import os
import yaml
from pathlib import Path
from unittest.mock import MagicMock

from core.di_container import DIContainer
from core.event_bus import EventBus
from core.config_manager import ConfigManager
from core.health_checker import HealthChecker, HealthStatus
from core.service_response import ServiceResponse


# ============================================================
# DIContainer Tests
# ============================================================

class TestDIContainer:

    def test_register_and_resolve_transient(self):
        """Transient lifecycle creates a new instance each time"""
        DIContainer.register('svc_transient', lambda: {'val': 42}, lifecycle='transient')

        a = DIContainer.resolve('svc_transient')
        b = DIContainer.resolve('svc_transient')

        assert a == {'val': 42}
        assert a is not b  # different instances

    def test_register_and_resolve_singleton(self):
        """Singleton lifecycle returns the same instance"""
        DIContainer.register('svc_singleton', lambda: {'val': 99}, lifecycle='singleton')

        a = DIContainer.resolve('svc_singleton')
        b = DIContainer.resolve('svc_singleton')

        assert a is b

    def test_register_instance(self):
        """register_instance puts an object directly into singletons"""
        obj = {'prebuilt': True}
        DIContainer.register_instance('svc_instance', obj)

        assert DIContainer.resolve('svc_instance') is obj

    def test_resolve_unregistered_raises(self):
        """Resolving an unregistered service raises KeyError"""
        with pytest.raises(KeyError):
            DIContainer.resolve('nonexistent_service')

    def test_resolve_with_dependencies(self):
        """Resolve wires dependencies through factory args"""
        DIContainer.register('dep_a', lambda: {'name': 'A'})
        DIContainer.register('with_dep', lambda dep: {'child': dep}, dependencies=['dep_a'])

        result = DIContainer.resolve('with_dep')
        assert result == {'child': {'name': 'A'}}

    def test_invalid_lifecycle_raises(self):
        """Invalid lifecycle value raises ValueError"""
        with pytest.raises(ValueError):
            DIContainer.register('bad', lambda: None, lifecycle='invalid')

    def test_is_registered(self):
        """is_registered correctly reports registration state"""
        assert not DIContainer.is_registered('no_such_svc')
        DIContainer.register('known_svc', lambda: None)
        assert DIContainer.is_registered('known_svc')

    def test_get_registered_services(self):
        """get_registered_services returns lifecycle info"""
        DIContainer.register('svc_a', lambda: None, lifecycle='transient')
        DIContainer.register('svc_b', lambda: None, lifecycle='singleton')

        services = DIContainer.get_registered_services()
        assert services['svc_a'] == 'transient'
        assert services['svc_b'] == 'singleton'

    def test_clear_all(self):
        """clear_all removes all registrations"""
        DIContainer.register('to_clear', lambda: 1)
        assert DIContainer.is_registered('to_clear')

        DIContainer.clear_all()
        assert not DIContainer.is_registered('to_clear')

    def test_scoped_lifecycle(self):
        """Scoped lifecycle shares instance within same scope_id"""
        DIContainer.register('scoped_svc', lambda: {'scope': True}, lifecycle='scoped')

        a = DIContainer.resolve('scoped_svc', scope_id='req_1')
        b = DIContainer.resolve('scoped_svc', scope_id='req_1')
        c = DIContainer.resolve('scoped_svc', scope_id='req_2')

        assert a is b
        assert a is not c

    def test_scoped_without_scope_id_raises(self):
        """Scoped lifecycle without scope_id raises ValueError"""
        DIContainer.register('scoped_no_id', lambda: None, lifecycle='scoped')
        with pytest.raises(ValueError):
            DIContainer.resolve('scoped_no_id')

    def test_clear_scope(self):
        """clear_scope removes instances for a specific scope"""
        DIContainer.register('scope_test', lambda: object(), lifecycle='scoped')

        a = DIContainer.resolve('scope_test', scope_id='to_clear')
        DIContainer.clear_scope('to_clear')
        b = DIContainer.resolve('scope_test', scope_id='to_clear')

        assert a is not b


# ============================================================
# EventBus Tests
# ============================================================

class TestEventBus:

    def test_subscribe_and_publish(self):
        """Subscriber callback receives published event"""
        received = []
        EventBus.subscribe('test_event', lambda e: received.append(e))

        EventBus.publish('test_event', data={'value': 42}, source='test')

        assert len(received) == 1
        assert received[0]['type'] == 'test_event'
        assert received[0]['data'] == {'value': 42}
        assert received[0]['source'] == 'test'

    def test_multiple_subscribers(self):
        """Multiple subscribers all get called"""
        calls_a = []
        calls_b = []

        EventBus.subscribe('multi_evt', lambda e: calls_a.append(1))
        EventBus.subscribe('multi_evt', lambda e: calls_b.append(1))

        EventBus.publish('multi_evt')

        assert len(calls_a) == 1
        assert len(calls_b) == 1

    def test_priority_ordering(self):
        """Higher priority subscribers are called first"""
        call_order = []

        EventBus.subscribe('prio_evt', lambda e: call_order.append('low'), priority=1)
        EventBus.subscribe('prio_evt', lambda e: call_order.append('high'), priority=10)

        EventBus.publish('prio_evt')

        assert call_order == ['high', 'low']

    def test_unsubscribe(self):
        """Unsubscribed callback is no longer called"""
        calls = []
        callback = lambda e: calls.append(1)

        EventBus.subscribe('unsub_evt', callback)
        EventBus.unsubscribe('unsub_evt', callback)
        EventBus.publish('unsub_evt')

        assert len(calls) == 0

    def test_event_history(self):
        """Published events are recorded in history"""
        EventBus.publish('hist_evt', data=1)
        EventBus.publish('hist_evt', data=2)

        history = EventBus.get_history('hist_evt')
        assert len(history) >= 2
        assert history[-1]['data'] == 2

    def test_event_history_all(self):
        """get_history with no filter returns all events"""
        EventBus.publish('type_a')
        EventBus.publish('type_b')

        history = EventBus.get_history()
        assert len(history) >= 2

    def test_filter_function(self):
        """Filter function prevents callback when it returns False"""
        calls = []
        EventBus.subscribe('filtered', lambda e: calls.append(1),
                          filter_func=lambda e: e.get('data', {}).get('pass', False))

        EventBus.publish('filtered', data={'pass': False})
        EventBus.publish('filtered', data={'pass': True})

        assert len(calls) == 1

    def test_get_subscribers_count(self):
        """get_subscribers_count reports correct counts"""
        EventBus.subscribe('count_evt', lambda e: None)
        EventBus.subscribe('count_evt', lambda e: None)

        counts = EventBus.get_subscribers_count('count_evt')
        assert counts['count_evt'] == 2

    def test_clear_all(self):
        """clear_all removes all subscribers and history"""
        EventBus.subscribe('clear_evt', lambda e: None)
        EventBus.publish('clear_evt')

        EventBus.clear_all()

        counts = EventBus.get_subscribers_count('clear_evt')
        assert counts['clear_evt'] == 0


# ============================================================
# ConfigManager Tests
# ============================================================

class TestConfigManager:

    def test_load_yaml(self, tmp_path):
        """load_yaml loads and caches a YAML file"""
        cfg_file = tmp_path / 'test.yaml'
        cfg_file.write_text('database:\n  host: localhost\n  port: 5432\n', encoding='utf-8')

        result = ConfigManager.load_yaml(str(cfg_file))

        assert result['database']['host'] == 'localhost'
        assert result['database']['port'] == 5432

    def test_load_yaml_caching(self, tmp_path):
        """Repeated load_yaml returns cached result without reload"""
        cfg_file = tmp_path / 'cache.yaml'
        cfg_file.write_text('key: value1\n', encoding='utf-8')

        r1 = ConfigManager.load_yaml(str(cfg_file))

        # Modify file on disk
        cfg_file.write_text('key: value2\n', encoding='utf-8')

        r2 = ConfigManager.load_yaml(str(cfg_file))
        assert r2['key'] == 'value1'  # still cached

        r3 = ConfigManager.load_yaml(str(cfg_file), reload=True)
        assert r3['key'] == 'value2'  # reloaded

    def test_load_yaml_nonexistent(self):
        """load_yaml returns empty dict for missing file"""
        result = ConfigManager.load_yaml('/nonexistent/path.yaml')
        assert result == {}

    def test_get_with_dot_path(self, tmp_path):
        """get supports dot-separated key paths"""
        cfg_file = tmp_path / 'dot.yaml'
        cfg_file.write_text('a:\n  b:\n    c: deep_value\n', encoding='utf-8')

        ConfigManager.load_yaml(str(cfg_file))
        val = ConfigManager.get(str(cfg_file), 'a.b.c')
        assert val == 'deep_value'

    def test_get_default(self, tmp_path):
        """get returns default for missing keys"""
        cfg_file = tmp_path / 'defaults.yaml'
        cfg_file.write_text('exists: true\n', encoding='utf-8')

        ConfigManager.load_yaml(str(cfg_file))
        assert ConfigManager.get(str(cfg_file), 'missing_key', 'fallback') == 'fallback'

    def test_set_value(self, tmp_path):
        """set updates cached config"""
        cfg_file = tmp_path / 'set.yaml'
        cfg_file.write_text('original: true\n', encoding='utf-8')

        ConfigManager.load_yaml(str(cfg_file))
        ConfigManager.set(str(cfg_file), 'new_key', 123)

        assert ConfigManager.get(str(cfg_file), 'new_key') == 123

    def test_watch_callback(self, tmp_path):
        """watch callback is called on load"""
        cfg_file = tmp_path / 'watch.yaml'
        cfg_file.write_text('watched: true\n', encoding='utf-8')

        notifications = []
        ConfigManager.watch(str(cfg_file), lambda p, c: notifications.append(c))

        ConfigManager.load_yaml(str(cfg_file))

        assert len(notifications) == 1
        assert notifications[0]['watched'] is True

    def test_clear(self, tmp_path):
        """clear removes all cached configs"""
        cfg_file = tmp_path / 'clear.yaml'
        cfg_file.write_text('data: 1\n', encoding='utf-8')

        ConfigManager.load_yaml(str(cfg_file))
        ConfigManager.clear()

        assert ConfigManager.get_all_configs() == {}


# ============================================================
# HealthChecker Tests
# ============================================================

class TestHealthChecker:

    def test_register_and_check(self):
        """Register a check and run it"""
        HealthChecker.register(
            'test_check',
            lambda: {'status': HealthStatus.HEALTHY, 'message': 'OK'}
        )

        result = HealthChecker.check('test_check')

        assert result['status'] == HealthStatus.HEALTHY
        assert result['message'] == 'OK'
        assert 'duration' in result
        assert 'timestamp' in result

    def test_check_all(self):
        """check() with no name runs all checks and returns aggregate"""
        HealthChecker.register('ok_check', lambda: {'status': HealthStatus.HEALTHY})
        HealthChecker.register('bad_check', lambda: {'status': HealthStatus.UNHEALTHY, 'message': 'fail'})

        result = HealthChecker.check()

        assert result['status'] == HealthStatus.UNHEALTHY
        assert 'ok_check' in result['checks']
        assert 'bad_check' in result['checks']

    def test_check_exception(self):
        """A check that raises is reported as unhealthy"""
        HealthChecker.register('crash_check', lambda: (_ for _ in ()).throw(RuntimeError('boom')))

        result = HealthChecker.check('crash_check')

        assert result['status'] == HealthStatus.UNHEALTHY
        assert 'boom' in result['message']

    def test_check_degraded(self):
        """Mixed healthy + degraded -> degraded overall"""
        HealthChecker.register('healthy', lambda: {'status': HealthStatus.HEALTHY})
        HealthChecker.register('degraded', lambda: {'status': HealthStatus.DEGRADED, 'message': 'slow'})

        result = HealthChecker.check()
        assert result['status'] == HealthStatus.DEGRADED

    def test_get_status(self):
        """get_status returns overview"""
        HealthChecker.register('status_check', lambda: {'status': HealthStatus.HEALTHY})
        HealthChecker.check('status_check')

        overview = HealthChecker.get_status()

        assert 'status_check' in overview['checks']
        assert overview['total_checks'] >= 1

    def test_unregister(self):
        """Unregistered check returns unknown status"""
        HealthChecker.register('to_remove', lambda: {'status': HealthStatus.HEALTHY})
        HealthChecker.unregister('to_remove')

        result = HealthChecker.check('to_remove')
        assert result['status'] == HealthStatus.UNKNOWN

    def test_clear(self):
        """clear removes all checks"""
        HealthChecker.register('will_clear', lambda: {'status': HealthStatus.HEALTHY})
        HealthChecker.clear()

        status = HealthChecker.get_status()
        assert status['total_checks'] == 0

    def test_check_history(self):
        """History records multiple runs"""
        HealthChecker.register('hist_check', lambda: {'status': HealthStatus.HEALTHY})
        HealthChecker.check('hist_check')
        HealthChecker.check('hist_check')

        history = HealthChecker.get_history('hist_check')
        assert len(history) >= 2


# ============================================================
# ServiceResponse Tests
# ============================================================

class TestServiceResponse:

    def test_ok(self):
        resp = ServiceResponse.ok(data={'id': 1}, message='done')
        assert resp.success is True
        assert resp.data == {'id': 1}
        assert resp.message == 'done'
        assert resp.code == 200

    def test_error(self):
        resp = ServiceResponse.error('bad request', code=400)
        assert resp.success is False
        assert resp.error == 'bad request'
        assert resp.code == 400

    def test_not_found(self):
        resp = ServiceResponse.not_found('device')
        assert resp.success is False
        assert 'device' in resp.error
        assert resp.code == 404

    def test_unauthorized(self):
        resp = ServiceResponse.unauthorized()
        assert resp.success is False
        assert resp.code == 401

    def test_forbidden(self):
        resp = ServiceResponse.forbidden()
        assert resp.success is False
        assert resp.code == 403

    def test_module_unavailable(self):
        resp = ServiceResponse.module_unavailable('vibration')
        assert resp.success is False
        assert resp.code == 503
        assert resp.data['module'] == 'vibration'

    def test_validation_error(self):
        resp = ServiceResponse.validation_error(['field required'])
        assert resp.success is False
        assert resp.code == 422

    def test_to_dict(self):
        d = ServiceResponse.ok(data=123).to_dict()
        assert d == {'success': True, 'data': 123}

    def test_to_dict_with_error(self):
        d = ServiceResponse.error('fail').to_dict()
        assert d == {'success': False, 'error': 'fail'}
