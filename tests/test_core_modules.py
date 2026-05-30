"""
核心模块测试 - 提升 core/ 覆盖率
"""
import pytest
from unittest.mock import MagicMock


class TestServiceResponse:
    """服务响应测试"""

    def test_service_response_class(self):
        """ServiceResponse类测试"""
        from core.service_response import ServiceResponse
        resp = ServiceResponse.ok({'data': 1})
        assert resp.success is True
        assert resp.code == 200

        resp = ServiceResponse.error('err', 500)
        assert resp.success is False
        assert resp.code == 500

    def test_service_response_to_dict(self):
        """to_dict测试"""
        from core.service_response import ServiceResponse
        resp = ServiceResponse.ok({'key': 'value'})
        d = resp.to_dict()
        assert isinstance(d, dict)
        assert d['success'] is True


class TestDIContainer:
    """DI容器测试"""

    def test_register_instance(self):
        """注册实例"""
        from core.di_container import DIContainer
        DIContainer.clear_all()
        DIContainer.register_instance('test_key', 'test_value')
        result = DIContainer.resolve('test_key')
        assert result == 'test_value'
        DIContainer.clear_all()

    def test_clear_all(self):
        """清除所有"""
        from core.di_container import DIContainer
        DIContainer.register_instance('key1', 'value1')
        DIContainer.clear_all()

    def test_get_registered_services(self):
        """获取已注册服务"""
        from core.di_container import DIContainer
        DIContainer.clear_all()
        services = DIContainer.get_registered_services()
        assert isinstance(services, dict)
        DIContainer.clear_all()


class TestEventBus:
    """事件总线测试"""

    def test_subscribe_and_publish(self):
        """订阅和发布"""
        from core.event_bus import EventBus
        EventBus.clear_all()
        received = []
        EventBus.subscribe('test_event', lambda data: received.append(data))
        EventBus.publish('test_event', {'value': 42})
        assert len(received) == 1
        EventBus.clear_all()

    def test_clear_all(self):
        """清除所有"""
        from core.event_bus import EventBus
        EventBus.subscribe('test', lambda: None)
        EventBus.clear_all()

    def test_clear_history(self):
        """清除历史"""
        from core.event_bus import EventBus
        EventBus.clear_history()


class TestConfigManager:
    """配置管理器测试"""

    def test_clear(self):
        """清除配置"""
        from core.config_manager import ConfigManager
        ConfigManager.clear()


class TestHealthChecker:
    """健康检查器测试"""

    def test_clear(self):
        """清除"""
        from core.health_checker import HealthChecker
        HealthChecker.clear()

    def test_get_status(self):
        """获取状态"""
        from core.health_checker import HealthChecker
        status = HealthChecker.get_status()
        assert isinstance(status, dict)

    def test_check(self):
        """运行检查"""
        from core.health_checker import HealthChecker
        result = HealthChecker.check()
        assert isinstance(result, dict)


class TestModuleRegistry:
    """模块注册表测试"""

    def test_clear(self):
        """清除"""
        from core.module_registry import ModuleRegistry
        ModuleRegistry.clear()

    def test_get_status(self):
        """获取状态"""
        from core.module_registry import ModuleRegistry
        status = ModuleRegistry.get_status()
        assert isinstance(status, dict)

    def test_get_available_modules(self):
        """获取可用模块"""
        from core.module_registry import ModuleRegistry
        modules = ModuleRegistry.get_available_modules()
        assert isinstance(modules, list)

    def test_get_unavailable_modules(self):
        """获取不可用模块"""
        from core.module_registry import ModuleRegistry
        modules = ModuleRegistry.get_unavailable_modules()
        assert isinstance(modules, list)


class TestMetrics:
    """指标测试"""

    def test_metrics_collector_init(self):
        """指标收集器初始化"""
        from core.metrics import metrics_collector
        assert metrics_collector is not None

    def test_get_metrics(self):
        """获取指标"""
        from core.metrics import metrics_collector
        metrics = metrics_collector.get_metrics()
        assert isinstance(metrics, bytes)
        assert len(metrics) > 0
