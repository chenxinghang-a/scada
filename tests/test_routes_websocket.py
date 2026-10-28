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

            # round 306：**匿名抓取必须直接返回指标**。
            # 原来这里断言 302 到 /login（注释还解释了原因：「不在 /api/ 前缀下」）——
            # 等于把「Prometheus 一条都采不到」钉成了正确行为：抓取端是外部系统，
            # 不带 cookie、也不走登录，永远命中这条分支。
            # 现在 /metrics 在 check_page_auth 的白名单里；
            # 只有显式配置了 PROMETHEUS_SCRAPE_TOKEN 时才要求抓取端带 header。
            anon = client.get('/metrics')
            assert anon.status_code == 200, \
                f"未认证访问 /metrics 应直接返回指标，实际 {anon.status_code}"

            # 带令牌（Prometheus 的常规路径）：同样 200
            auth = client.get('/metrics', headers={'Authorization': 'Bearer dummy'})
            assert auth.status_code == 200, \
                f"认证后 /metrics 应返回 200，实际 {auth.status_code}"


class TestRateLimiter:
    """速率限制器测试"""

    def test_rate_limiter_import(self):
        """导入速率限制器"""
        from core.rate_limiter import create_limiter
        assert callable(create_limiter)
