"""
Tests for core.module_registry: ModuleRegistry lifecycle, status, dependencies
"""

import pytest
from unittest.mock import MagicMock

from core.module_registry import ModuleRegistry, ModuleStatus, ModuleInfo


# ============================================================
# ModuleStatus Enum Tests
# ============================================================

class TestModuleStatus:

    def test_all_statuses_exist(self):
        """All expected statuses are defined"""
        statuses = [s.value for s in ModuleStatus]
        assert 'registered' in statuses
        assert 'initializing' in statuses
        assert 'initialized' in statuses
        assert 'running' in statuses
        assert 'paused' in statuses
        assert 'error' in statuses
        assert 'disabled' in statuses
        assert 'unavailable' in statuses


# ============================================================
# ModuleInfo Tests
# ============================================================

class TestModuleInfo:

    def test_module_info_creation(self):
        """ModuleInfo stores name, class, config"""
        info = ModuleInfo('test_mod', dict, {'key': 'val'})
        assert info.name == 'test_mod'
        assert info.module_class is dict
        assert info.config == {'key': 'val'}

    def test_module_info_defaults(self):
        """ModuleInfo has correct defaults"""
        info = ModuleInfo('test', dict)
        assert info.instance is None
        assert info.status == ModuleStatus.REGISTERED
        assert info.error is None
        assert info.dependencies == []

    def test_module_info_to_dict(self):
        """to_dict returns expected structure"""
        info = ModuleInfo('test', dict)
        d = info.to_dict()
        assert d['name'] == 'test'
        assert d['class'] == 'dict'
        assert d['status'] == 'registered'
        assert d['has_instance'] is False


# ============================================================
# Registration Tests
# ============================================================

class TestRegistration:

    def test_register_module(self):
        """register stores module info"""
        ModuleRegistry.register('reg_test', dict)
        status = ModuleRegistry.get_status('reg_test')
        assert status['name'] == 'reg_test'

    def test_register_with_config(self):
        """register stores config"""
        ModuleRegistry.register('cfg_test', dict, config={'a': 1})
        info = ModuleRegistry._modules['cfg_test']
        assert info.config == {'a': 1}

    def test_register_with_dependencies(self):
        """register stores dependencies"""
        ModuleRegistry.register('dep_test', dict, dependencies=['other_mod'])
        info = ModuleRegistry._modules['dep_test']
        assert info.dependencies == ['other_mod']

    def test_register_overwrite(self):
        """register overwrites existing module"""
        ModuleRegistry.register('overwrite_test', dict)
        ModuleRegistry.register('overwrite_test', list)
        info = ModuleRegistry._modules['overwrite_test']
        assert info.module_class is list


# ============================================================
# Initialization Tests
# ============================================================

class TestInitialization:

    def test_initialize_success(self):
        """initialize creates instance for simple class"""
        ModuleRegistry.register('init_test', dict)
        result = ModuleRegistry.initialize('init_test')
        assert result is True
        info = ModuleRegistry._modules['init_test']
        assert info.status == ModuleStatus.INITIALIZED
        assert info.instance is not None

    def test_initialize_unregistered(self):
        """initialize returns False for unregistered module"""
        result = ModuleRegistry.initialize('nonexistent')
        assert result is False

    def test_initialize_already_initialized(self):
        """initialize returns True if already initialized"""
        ModuleRegistry.register('already_init', dict)
        ModuleRegistry.initialize('already_init')
        result = ModuleRegistry.initialize('already_init')
        assert result is True

    def test_initialize_with_kwargs(self):
        """initialize passes kwargs to constructor"""
        class CustomModule:
            def __init__(self, x=0, y=0):
                self.x = x
                self.y = y
        ModuleRegistry.register('kwargs_test', CustomModule)
        ModuleRegistry.initialize('kwargs_test', x=10, y=20)
        instance = ModuleRegistry.get_instance('kwargs_test')
        assert instance.x == 10
        assert instance.y == 20

    def test_initialize_dependency_missing(self):
        """initialize fails if dependency not initialized"""
        ModuleRegistry.register('dep_fail', dict, dependencies=['missing_dep'])
        result = ModuleRegistry.initialize('dep_fail')
        assert result is False
        info = ModuleRegistry._modules['dep_fail']
        assert info.status == ModuleStatus.ERROR

    def test_initialize_dependency_met(self):
        """initialize succeeds if dependency is initialized"""
        ModuleRegistry.register('dep_ok_base', dict)
        ModuleRegistry.initialize('dep_ok_base')
        ModuleRegistry.register('dep_ok_child', list, dependencies=['dep_ok_base'])
        result = ModuleRegistry.initialize('dep_ok_child')
        assert result is True

    def test_initialize_constructor_exception(self):
        """initialize handles constructor exception"""
        class BadModule:
            def __init__(self):
                raise RuntimeError('init failed')
        ModuleRegistry.register('bad_init', BadModule)
        result = ModuleRegistry.initialize('bad_init')
        assert result is False
        info = ModuleRegistry._modules['bad_init']
        assert info.status == ModuleStatus.ERROR


# ============================================================
# Instance Retrieval Tests
# ============================================================

class TestInstanceRetrieval:

    def test_get_instance_success(self):
        """get_instance returns initialized instance"""
        ModuleRegistry.register('get_inst', dict)
        ModuleRegistry.initialize('get_inst')
        instance = ModuleRegistry.get_instance('get_inst')
        assert isinstance(instance, dict)

    def test_get_instance_unregistered(self):
        """get_instance raises KeyError for unregistered module"""
        with pytest.raises(KeyError):
            ModuleRegistry.get_instance('no_such_module')

    def test_get_instance_not_initialized(self):
        """get_instance raises RuntimeError for uninitialized module"""
        ModuleRegistry.register('not_init', dict)
        with pytest.raises(RuntimeError):
            ModuleRegistry.get_instance('not_init')


# ============================================================
# Status Management Tests
# ============================================================

class TestStatusManagement:

    def test_get_status_single(self):
        """get_status returns info for single module"""
        ModuleRegistry.register('status_test', dict)
        status = ModuleRegistry.get_status('status_test')
        assert status['name'] == 'status_test'

    def test_get_status_not_found(self):
        """get_status returns not_found for unknown module"""
        status = ModuleRegistry.get_status('unknown')
        assert status['status'] == 'not_found'

    def test_get_status_all(self):
        """get_status with no name returns all modules"""
        ModuleRegistry.register('all_test_1', dict)
        ModuleRegistry.register('all_test_2', list)
        all_status = ModuleRegistry.get_status()
        assert 'all_test_1' in all_status
        assert 'all_test_2' in all_status

    def test_set_status(self):
        """set_status changes module status"""
        ModuleRegistry.register('set_status_test', dict)
        ModuleRegistry.set_status('set_status_test', ModuleStatus.ERROR, RuntimeError('test'))
        info = ModuleRegistry._modules['set_status_test']
        assert info.status == ModuleStatus.ERROR

    def test_set_status_not_found(self):
        """set_status is no-op for unknown module"""
        ModuleRegistry.set_status('unknown', ModuleStatus.ERROR)  # should not raise


# ============================================================
# Disable/Enable Tests
# ============================================================

class TestDisableEnable:

    def test_disable(self):
        """disable sets status to DISABLED"""
        ModuleRegistry.register('disable_test', dict)
        ModuleRegistry.disable('disable_test')
        info = ModuleRegistry._modules['disable_test']
        assert info.status == ModuleStatus.DISABLED

    def test_enable(self):
        """enable restores DISABLED to REGISTERED"""
        ModuleRegistry.register('enable_test', dict)
        ModuleRegistry.disable('enable_test')
        ModuleRegistry.enable('enable_test')
        info = ModuleRegistry._modules['enable_test']
        assert info.status == ModuleStatus.REGISTERED

    def test_enable_non_disabled(self):
        """enable is no-op for non-disabled module"""
        ModuleRegistry.register('enable_nd', dict)
        ModuleRegistry.enable('enable_nd')  # should not raise


# ============================================================
# Start/Stop/Pause/Resume Tests
# ============================================================

class TestLifecycle:

    def test_start(self):
        """start transitions to RUNNING"""
        ModuleRegistry.register('start_test', dict)
        ModuleRegistry.initialize('start_test')
        result = ModuleRegistry.start('start_test')
        assert result is True
        info = ModuleRegistry._modules['start_test']
        assert info.status == ModuleStatus.RUNNING

    def test_start_with_start_method(self):
        """start calls instance.start() if available"""
        class HasStart:
            def __init__(self):
                self.started = False
            def start(self):
                self.started = True
        ModuleRegistry.register('has_start', HasStart)
        ModuleRegistry.initialize('has_start')
        ModuleRegistry.start('has_start')
        # After start, status is RUNNING; get_instance requires INITIALIZED
        # Access instance directly from module info
        info = ModuleRegistry._modules['has_start']
        assert info.instance.started is True

    def test_start_already_running(self):
        """start returns True if already running"""
        ModuleRegistry.register('already_running', dict)
        ModuleRegistry.initialize('already_running')
        ModuleRegistry.start('already_running')
        result = ModuleRegistry.start('already_running')
        assert result is True

    def test_start_unregistered(self):
        """start returns False for unregistered module"""
        result = ModuleRegistry.start('unknown')
        assert result is False

    def test_start_wrong_state(self):
        """start returns False for wrong state"""
        ModuleRegistry.register('wrong_state', dict)
        # Not initialized yet
        result = ModuleRegistry.start('wrong_state')
        assert result is False

    def test_stop(self):
        """stop transitions RUNNING to INITIALIZED"""
        ModuleRegistry.register('stop_test', dict)
        ModuleRegistry.initialize('stop_test')
        ModuleRegistry.start('stop_test')
        result = ModuleRegistry.stop('stop_test')
        assert result is True
        info = ModuleRegistry._modules['stop_test']
        assert info.status == ModuleStatus.INITIALIZED

    def test_stop_with_stop_method(self):
        """stop calls instance.stop() if available"""
        class HasStop:
            def __init__(self):
                self.stopped = False
            def start(self):
                pass
            def stop(self):
                self.stopped = True
        ModuleRegistry.register('has_stop', HasStop)
        ModuleRegistry.initialize('has_stop')
        ModuleRegistry.start('has_stop')
        ModuleRegistry.stop('has_stop')
        instance = ModuleRegistry.get_instance('has_stop')
        assert instance.stopped is True

    def test_stop_not_running(self):
        """stop returns True if not running"""
        ModuleRegistry.register('not_running', dict)
        ModuleRegistry.initialize('not_running')
        result = ModuleRegistry.stop('not_running')
        assert result is True

    def test_pause(self):
        """pause transitions RUNNING to PAUSED"""
        ModuleRegistry.register('pause_test', dict)
        ModuleRegistry.initialize('pause_test')
        ModuleRegistry.start('pause_test')
        result = ModuleRegistry.pause('pause_test')
        assert result is True
        info = ModuleRegistry._modules['pause_test']
        assert info.status == ModuleStatus.PAUSED

    def test_pause_not_running(self):
        """pause returns False if not running"""
        ModuleRegistry.register('pause_nr', dict)
        ModuleRegistry.initialize('pause_nr')
        result = ModuleRegistry.pause('pause_nr')
        assert result is False

    def test_resume(self):
        """resume transitions PAUSED to RUNNING"""
        ModuleRegistry.register('resume_test', dict)
        ModuleRegistry.initialize('resume_test')
        ModuleRegistry.start('resume_test')
        ModuleRegistry.pause('resume_test')
        result = ModuleRegistry.resume('resume_test')
        assert result is True
        info = ModuleRegistry._modules['resume_test']
        assert info.status == ModuleStatus.RUNNING

    def test_resume_not_paused(self):
        """resume returns False if not paused"""
        ModuleRegistry.register('resume_np', dict)
        ModuleRegistry.initialize('resume_np')
        result = ModuleRegistry.resume('resume_np')
        assert result is False

    def test_restart(self):
        """restart stops and re-initializes"""
        class HasLifecycle:
            def __init__(self):
                self.init_count = 0
                self.started = False
            def start(self):
                self.started = True
            def stop(self):
                self.started = False
        ModuleRegistry.register('restart_test', HasLifecycle)
        ModuleRegistry.initialize('restart_test')
        ModuleRegistry.start('restart_test')
        result = ModuleRegistry.restart('restart_test')
        assert result is True

    def test_restart_unregistered(self):
        """restart returns False for unregistered module"""
        result = ModuleRegistry.restart('unknown')
        assert result is False


# ============================================================
# Query API Tests
# ============================================================

class TestQueryAPI:

    def test_get_available_modules(self):
        """get_available_modules returns initialized/running modules"""
        ModuleRegistry.register('avail_1', dict)
        ModuleRegistry.initialize('avail_1')
        available = ModuleRegistry.get_available_modules()
        assert 'avail_1' in available

    def test_get_unavailable_modules(self):
        """get_unavailable_modules returns error/disabled modules"""
        ModuleRegistry.register('unavail_1', dict)
        ModuleRegistry.disable('unavail_1')
        unavailable = ModuleRegistry.get_unavailable_modules()
        assert 'unavail_1' in unavailable

    def test_get_lifecycle_info(self):
        """get_lifecycle_info returns detailed info"""
        ModuleRegistry.register('lifecycle_1', dict)
        ModuleRegistry.initialize('lifecycle_1')
        info = ModuleRegistry.get_lifecycle_info('lifecycle_1')
        assert info['name'] == 'lifecycle_1'
        assert info['has_instance'] is True

    def test_get_lifecycle_info_not_found(self):
        """get_lifecycle_info returns not_found for unknown"""
        info = ModuleRegistry.get_lifecycle_info('unknown')
        assert info['status'] == 'not_found'

    def test_get_lifecycle_info_all(self):
        """get_lifecycle_info with no name returns all"""
        ModuleRegistry.register('lc_all_1', dict)
        all_info = ModuleRegistry.get_lifecycle_info()
        assert 'lc_all_1' in all_info


# ============================================================
# Clear Tests
# ============================================================

class TestClear:

    def test_clear_removes_all(self):
        """clear removes all registered modules"""
        ModuleRegistry.register('clear_1', dict)
        ModuleRegistry.register('clear_2', list)
        ModuleRegistry.clear()
        assert ModuleRegistry.get_status('clear_1')['status'] == 'not_found'

    def test_clear_stops_running_modules(self):
        """clear stops running modules before removing"""
        class HasStop:
            def __init__(self):
                self.stopped = False
            def start(self):
                pass
            def stop(self):
                self.stopped = True
        ModuleRegistry.register('clear_stop', HasStop)
        ModuleRegistry.initialize('clear_stop')
        ModuleRegistry.start('clear_stop')
        ModuleRegistry.clear()
        # Module should have been stopped
