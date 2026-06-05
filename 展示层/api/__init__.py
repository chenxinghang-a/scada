"""
REST API子模块包
将原api.py (54KB) 按功能域拆分为独立模块
"""

from flask import Blueprint

# 各子模块Blueprint
from .api_auth import auth_bp
from .api_devices import devices_bp
from .api_control import control_bp
from .api_data import data_bp
from .api_alarms import alarms_bp
from .api_system import system_bp
from .api_industry40 import industry40_bp
from .api_health import health_bp
from .api_metrics import metrics_bp
from .swagger import swagger_bp
from .api_resilience import resilience_bp
from .api_ops import ops_bp

# 统一注册的Blueprint列表
ALL_BLUEPRINTS = [
    auth_bp,
    devices_bp,
    control_bp,
    data_bp,
    alarms_bp,
    system_bp,
    industry40_bp,
    health_bp,
    metrics_bp,
    swagger_bp,
    resilience_bp,
    ops_bp,
]


class APIVersionMiddleware:
    """WSGI中间件：将 /api/v1/* 透明代理到 /api/*

    在路由匹配之前重写PATH_INFO，实现API版本化。
    """

    def __init__(self, wsgi_app):
        self.app = wsgi_app

    def __call__(self, environ, start_response):
        path = environ.get('PATH_INFO', '')
        if path.startswith('/api/v1/'):
            environ['PATH_INFO'] = '/api/' + path[8:]
            environ['SCADA_API_VERSION'] = 'v1'
        return self.app(environ, start_response)


def register_api_blueprints(app):
    """将所有API Blueprint注册到Flask应用

    支持两个路径前缀:
    - /api/*        (原始路径，向后兼容)
    - /api/v1/*     (版本化路径，新标准)

    通过 X-API-Version 响应头标识当前API版本。
    """
    API_VERSION = '1.0.0'

    # 安装版本中间件
    app.wsgi_app = APIVersionMiddleware(app.wsgi_app)

    for bp in ALL_BLUEPRINTS:
        app.register_blueprint(bp)

    @app.after_request
    def add_api_version_header(response):
        if request.path.startswith('/api/'):
            response.headers['X-API-Version'] = API_VERSION
            if getattr(request.environ, 'get', lambda k, d: d)('SCADA_API_VERSION') == 'v1':
                response.headers['X-API-Version-Prefix'] = 'v1'
        return response
