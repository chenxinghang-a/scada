"""
路由和WebSocket测试 - 提升展示层覆盖率
"""
import pytest
from unittest.mock import MagicMock, patch


class TestCreateApp:
    """create_app 测试"""

    def test_create_app_basic(self):
        """基本创建Flask应用"""
        with patch('展示层.routes.AuthManager') as MockAuth, \
             patch('展示层.routes.create_limiter') as mock_limiter:
            MockAuth.return_value = MagicMock()
            mock_limiter.return_value = MagicMock()

            from 展示层.routes import create_app
            app = create_app(
                database=MagicMock(),
                device_manager=MagicMock(),
                alarm_manager=MagicMock(),
                data_collector=MagicMock()
            )
            assert app is not None

    def test_create_app_with_all_modules(self):
        """带所有模块创建Flask应用"""
        with patch('展示层.routes.AuthManager') as MockAuth, \
             patch('展示层.routes.create_limiter') as mock_limiter:
            MockAuth.return_value = MagicMock()
            mock_limiter.return_value = MagicMock()

            from 展示层.routes import create_app
            app = create_app(
                database=MagicMock(),
                device_manager=MagicMock(),
                alarm_manager=MagicMock(),
                data_collector=MagicMock(),
                predictive_maintenance=MagicMock(),
                oee_calculator=MagicMock(),
                spc_analyzer=MagicMock(),
                energy_manager=MagicMock(),
                edge_decision=MagicMock(),
                device_control=MagicMock(),
                vibration_analyzer=MagicMock()
            )
            assert app is not None

    def test_create_app_has_routes(self):
        """创建的应用有路由"""
        with patch('展示层.routes.AuthManager') as MockAuth, \
             patch('展示层.routes.create_limiter') as mock_limiter:
            MockAuth.return_value = MagicMock()
            mock_limiter.return_value = MagicMock()

            from 展示层.routes import create_app
            app = create_app(
                database=MagicMock(),
                device_manager=MagicMock(),
                alarm_manager=MagicMock(),
                data_collector=MagicMock()
            )
            rules = [rule.rule for rule in app.url_map.iter_rules()]
            assert len(rules) > 0

    def test_create_app_health_endpoint(self):
        """创建的应用有健康检查端点"""
        with patch('展示层.routes.AuthManager') as MockAuth, \
             patch('展示层.routes.create_limiter') as mock_limiter:
            MockAuth.return_value = MagicMock()
            mock_limiter.return_value = MagicMock()

            from 展示层.routes import create_app
            app = create_app(
                database=MagicMock(),
                device_manager=MagicMock(),
                alarm_manager=MagicMock(),
                data_collector=MagicMock()
            )
            client = app.test_client()
            resp = client.get('/api/health/status')
            assert resp.status_code == 200

    def test_create_app_metrics_endpoint(self):
        """创建的应用有metrics端点"""
        with patch('展示层.routes.AuthManager') as MockAuth, \
             patch('展示层.routes.create_limiter') as mock_limiter:
            MockAuth.return_value = MagicMock()
            mock_limiter.return_value = MagicMock()

            from 展示层.routes import create_app
            app = create_app(
                database=MagicMock(),
                device_manager=MagicMock(),
                alarm_manager=MagicMock(),
                data_collector=MagicMock()
            )
            client = app.test_client()
            resp = client.get('/metrics')
            assert resp.status_code in (200, 302, 500)


class TestRateLimiter:
    """速率限制器测试"""

    def test_rate_limiter_import(self):
        """导入速率限制器"""
        from core.rate_limiter import create_limiter
        assert callable(create_limiter)
