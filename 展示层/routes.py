"""
Flask路由模块
定义Web页面路由
"""

import jwt
from flask import Flask, render_template, jsonify, request, redirect, url_for, g
from flask_socketio import SocketIO

from .api import register_api_blueprints
from .websocket import init_socketio
from core.rate_limiter import create_limiter
from 用户层.auth import AuthManager
from config import AuthConfig


def create_app(database, device_manager, alarm_manager, data_collector,
               predictive_maintenance=None, oee_calculator=None,
               spc_analyzer=None, energy_manager=None, edge_decision=None,
               device_control=None, vibration_analyzer=None):
    """
    创建Flask应用

    Args:
        database: 数据库实例
        device_manager: 设备管理器实例
        alarm_manager: 报警管理器实例
        data_collector: 数据采集器实例
        predictive_maintenance: 预测性维护实例（可选）
        oee_calculator: OEE计算器实例（可选）
        spc_analyzer: SPC分析器实例（可选）
        energy_manager: 能源管理实例（可选）
        edge_decision: 边缘决策引擎实例（可选）
        device_control: 设备控制安全管理实例（可选）
        vibration_analyzer: 振动分析器实例（可选）

    Returns:
        Flask: Flask应用实例
    """
    app = Flask(__name__,
                template_folder='../模板',
                static_folder='../静态资源')

    from config import FlaskConfig, SecurityConfig
    app.config['SECRET_KEY'] = FlaskConfig.SECRET_KEY

    # 初始化认证管理器
    auth_manager = AuthManager(database)

    # 速率限制 (GB/T 22239 等保2.0)
    limiter = create_limiter(app)
    app.limiter = limiter

    # 注册API蓝图（模块化拆分后的多个Blueprint）
    register_api_blueprints(app)

    # 初始化WebSocket
    socketio = init_socketio(app, database, data_collector)

    # 存储实例
    app.database = database
    app.device_manager = device_manager
    app.alarm_manager = alarm_manager
    app.data_collector = data_collector
    app.auth_manager = auth_manager

    # 工业4.0智能层实例
    app.predictive_maintenance = predictive_maintenance
    app.oee_calculator = oee_calculator
    app.spc_analyzer = spc_analyzer
    app.energy_manager = energy_manager
    app.edge_decision = edge_decision
    app.device_control = device_control
    app.vibration_analyzer = vibration_analyzer

    # 安全响应头 (GB/T 22239 等保2.0)
    @app.after_request
    def add_security_headers(response):
        if not SecurityConfig.SECURITY_HEADERS:
            return response
        # 防止点击劫持
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        # 防止MIME类型嗅探
        response.headers['X-Content-Type-Options'] = 'nosniff'
        # XSS保护
        response.headers['X-XSS-Protection'] = '1; mode=block'
        # 内容安全策略 - 允许CDN资源
        csp_directives = [
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com" + (' ' + ' '.join(SecurityConfig.CSP_EXTRA_SCRIPTS) if SecurityConfig.CSP_EXTRA_SCRIPTS else ''),
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://fonts.googleapis.com",
            "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net",
            "img-src 'self' data: blob:",
            "connect-src 'self' ws: wss:",
            "frame-ancestors 'self'",
        ]
        response.headers['Content-Security-Policy'] = '; '.join(csp_directives)
        # 严格传输安全 (仅在HTTPS时启用)
        if request.is_secure:
            response.headers['Strict-Transport-Security'] = f'max-age={SecurityConfig.HSTS_MAX_AGE}; includeSubDomains'
        # 引荐来源策略
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        # 权限策略
        response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
        return response

    # 页面级认证 - 等保2.0 (GB/T 22239)
    # 所有页面路由检查JWT，API路由已有自己的jwt_required装饰器
    @app.before_request
    def check_page_auth():
        """页面级JWT认证 - 等保2.0要求"""
        path = request.path

        # 跳过API路由（已有自己的认证）、静态资源、登录页
        if path.startswith('/api/') or path.startswith('/static/') or path == '/login':
            return None

        # 跳过favicon等非页面请求
        last_segment = path.split('/')[-1]
        if '.' in last_segment:
            return None

        # 检查JWT token（从cookie或Authorization header获取）
        token = request.cookies.get('token')
        if not token:
            auth_header = request.headers.get('Authorization', '')
            if auth_header.startswith('Bearer '):
                token = auth_header[7:]

        if not token:
            return redirect('/login')

        try:
            payload = jwt.decode(token, AuthConfig.JWT_SECRET, algorithms=[AuthConfig.JWT_ALGORITHM])
            g.current_user = payload
        except jwt.ExpiredSignatureError:
            return redirect('/login')
        except jwt.InvalidTokenError:
            return redirect('/login')

        return None

    # 页面路由
    @app.route('/')
    def index():
        """首页 - 仪表盘"""
        return render_template('dashboard.html')

    @app.route('/dashboard')
    def dashboard():
        """仪表盘页面"""
        return render_template('dashboard.html')

    @app.route('/screen')
    def big_screen():
        """数据大屏（全屏工业可视化）"""
        return render_template('screen.html')

    @app.route('/history')
    def history():
        """历史数据页面"""
        return render_template('history.html')

    @app.route('/alarms')
    def alarms():
        """报警管理页面"""
        return render_template('alarms.html')

    @app.route('/config')
    def config():
        """配置页面"""
        return render_template('config.html')

    @app.route('/devices')
    def devices():
        """设备管理页面"""
        return render_template('devices.html')

    @app.route('/login')
    def login_page():
        """登录页面"""
        return render_template('login.html')

    @app.route('/users')
    def users_page():
        """用户管理页面"""
        return render_template('users.html')

    @app.route('/control')
    def control_page():
        """设备控制页面"""
        return render_template('control.html')

    @app.route('/alarm-output')
    def alarm_output_page():
        """报警输出与广播控制页面"""
        return render_template('alarm_output.html')

    @app.route('/industry40')
    def industry40_page():
        """工业4.0智能仪表盘"""
        return render_template('industry40.html')

    # 隐藏图表分析路由
    # @app.route('/charts')
    # def charts_page():
    #     """图表自选页面"""
    #     return render_template('charts.html')

    # 错误处理
    @app.errorhandler(404)
    def not_found(error):
        return jsonify({'error': 'Not found'}), 404

    @app.errorhandler(500)
    def internal_error(error):
        return jsonify({'error': 'Internal server error'}), 500

    @app.errorhandler(429)
    def rate_limit_exceeded(e):
        return jsonify({
            'error': 'Rate limit exceeded',
            'message': str(e.description),
            'retry_after': e.retry_after
        }), 429

    return app
