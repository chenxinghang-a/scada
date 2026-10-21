"""
Flask路由模块
定义Web页面路由
"""

import logging
import threading
import time
from pathlib import Path as _Path
_BASE_DIR = _Path(__file__).resolve().parent.parent  # industrial_scada/

import jwt
from flask import Flask, render_template, jsonify, request, redirect, url_for, g, current_app
from flask_socketio import SocketIO

logger = logging.getLogger(__name__)

from .api import register_api_blueprints
from .websocket import init_socketio
from core.rate_limiter import create_limiter
from core.circuit_breaker import circuit_breaker_manager
from core.dynamic_rate_limiter import dynamic_rate_limiter
from core.degradation_manager import degradation_manager
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
                template_folder=str(_BASE_DIR / '模板'),
                static_folder=str(_BASE_DIR / '静态资源'),
                static_url_path='/static')

    from config import FlaskConfig, SecurityConfig
    app.config['SECRET_KEY'] = FlaskConfig.SECRET_KEY

    # 请求体上限（`SCADA_MAX_CONTENT_MB`，默认 16MB）。
    #
    # ⚠️ 这个旋钮此前是**装饰品**：`FlaskConfig.MAX_CONTENT_LENGTH` 定义在
    # config.py 里，但全库没有任何一处把它交给 Flask，于是 Werkzeug 用默认值
    # （不限制）—— 任何带认证的接口都能被一个超大 JSON body 顶到 OOM。
    # `MAX_CONTENT_LENGTH` 是 Flask 读取的标准键，赋值后 Werkzeug 在解析
    # 请求体时就会拒绝超限请求（413），不需要逐个端点写校验。
    app.config['MAX_CONTENT_LENGTH'] = FlaskConfig.MAX_CONTENT_LENGTH

    # ---- 并发请求上限（`SCADA_MAX_CONCURRENT`，默认 100）----
    # 这个旋钮此前也是装饰品：`FlaskConfig.MAX_CONCURRENT_REQUESTS` 定义了但没人读。
    # 没有它时，慢请求（DB 锁等待 / 设备 I/O）会让 WSGI 线程无限堆积，
    # 最终把内存和 SQLite 连接数一起吃满 —— 而且**没有任何提示**，
    # 表现为「系统越用越慢、最后连健康检查都超时」。
    # 用 BoundedSemaphore 而不是 Lock：多释放一次会立刻抛异常，不会被掩盖。
    _request_slots = threading.BoundedSemaphore(FlaskConfig.MAX_CONCURRENT_REQUESTS)
    app.extensions['request_slots'] = _request_slots
    _slow_request_seconds = FlaskConfig.REQUEST_TIMEOUT

    @app.before_request
    def _acquire_request_slot():
        # 静态资源不占槽位：一个页面几十个 js/css 请求会把 API 挤掉
        if request.path.startswith('/static/'):
            return None
        if not _request_slots.acquire(blocking=False):
            logger.warning(
                "并发请求已达上限 %d，拒绝 %s %s",
                FlaskConfig.MAX_CONCURRENT_REQUESTS, request.method, request.path,
            )
            resp = jsonify({
                'success': False,
                'error': '服务器繁忙（并发请求已达上限），请稍后重试',
                'error_code': 'TOO_MANY_CONCURRENT_REQUESTS',
            })
            resp.status_code = 503
            resp.headers['Retry-After'] = '1'
            return resp
        g._request_slot_held = True
        g._request_started_at = time.monotonic()
        return None

    @app.teardown_request
    def _release_request_slot(exc):
        # 注意：`before_request` 短路（返回 503）时本函数**仍会执行**，
        # 但那时没有取到槽位，靠 `_request_slot_held` 区分，避免多释放。
        if getattr(g, '_request_slot_held', False):
            g._request_slot_held = False
            _request_slots.release()

    @app.after_request
    def _warn_slow_request(response):
        started = getattr(g, '_request_started_at', None)
        if started is not None:
            elapsed = time.monotonic() - started
            if elapsed > _slow_request_seconds:
                # `SCADA_REQUEST_TIMEOUT` 的语义是「一个请求最多该花多久」。
                # WSGI 里没法安全地中途杀掉处理函数，所以这里做成**可观测**的
                # 硬阈值告警：超了就打 WARNING 并带在响应头里，
                # 而不是假装「超时会被中止」。
                logger.warning(
                    "慢请求 %.1fs（阈值 %ds）: %s %s",
                    elapsed, _slow_request_seconds, request.method, request.path,
                )
                response.headers['X-Slow-Request'] = f'{elapsed:.1f}s'
        return response

    # 登记「当前进程实际在用的库路径」（唯一真源）—— 入口在这里登记一次，
    # 别的模块（性能指标、运维工具…）只读，避免各自去猜模式。
    # 实测背景（2026-10-11，round 212）：api_performance 写死 data/scada.db，
    # 而模拟模式实际用 data/scada_simulated.db → 性能页永远显示 0.0MB / 0 条。
    # 而 data/ 下两个库**同时存在**，「取第一个存在的」这种猜法会静默指错，
    # 所以必须由入口登记。
    try:
        import paths as _paths
        _paths.set_active_db_path(getattr(database, 'db_path', None))
        if _paths.get_active_db_path() is not None:
            logger.debug("已登记当前库路径: %s", _paths.get_active_db_path())
        else:
            logger.warning("无法登记当前库路径（database 未暴露 db_path）—— "
                           "性能指标将报告 unknown 而非猜测值")
    except Exception as e:  # noqa: BLE001 —— 登记失败不该让建应用失败
        logger.warning("登记当前库路径失败（非致命）: %s", e)

    # 初始化认证管理器
    auth_manager = AuthManager(database)

    # 速率限制 (GB/T 22239 等保2.0)
    limiter = create_limiter(app)
    app.limiter = limiter

    # 动态限流器 - 关联Flask-Limiter并启动
    dynamic_rate_limiter.set_flask_limiter(limiter)
    try:
        dynamic_rate_limiter.start()
    except Exception as e:
        logger.warning(f"动态限流器启动失败（非致命）: {e}")

    # 熔断器管理器 - 注册内置熔断器
    circuit_breaker_manager.get_or_create('database', failure_threshold=3, recovery_timeout=60)
    circuit_breaker_manager.get_or_create('device_comm', failure_threshold=5, recovery_timeout=30)
    circuit_breaker_manager.get_or_create('alarm_system', failure_threshold=3, recovery_timeout=45)

    # 降级管理器 - 注册默认触发器并启动
    def _health_degrade_trigger():
        """基于健康检查的降级触发器"""
        try:
            from core.health_checker import HealthChecker, HealthStatus
            status = HealthChecker.get_status()
            unhealthy_count = sum(
                1 for s in status.get('checks', {}).values()
                if s.get('status') == HealthStatus.UNHEALTHY
            )
            if unhealthy_count >= 3:
                from core.degradation_manager import DegradationLevel
                return DegradationLevel.SEVERE
            elif unhealthy_count >= 2:
                return DegradationLevel.MODERATE
            elif unhealthy_count >= 1:
                return DegradationLevel.LIGHT
        except Exception as e:
            # 健康检查读取失败会导致降级触发器失明：系统实际已不健康却不会降级，
            # 属于安全机制失效（功能降级），必须留痕
            logger.warning("健康降级触发器执行失败，本次将不做降级判定: %s", e)
        return None

    degradation_manager.register_trigger(_health_degrade_trigger)
    try:
        degradation_manager.start()
    except Exception as e:
        logger.warning(f"降级管理器启动失败（非致命）: {e}")

    # 请求追踪与响应时间中间件
    from core.request_tracking import init_request_tracking
    init_request_tracking(app)

    # 请求去重（防止重复提交）
    from core.request_dedup import init_request_dedup
    init_request_dedup(app)

    # 注册API蓝图（模块化拆分后的多个Blueprint）
    register_api_blueprints(app)

    # 分级限流绑定：登录/改密接口严格限流，防暴力破解与令牌被盗后暴力改密
    from core.rate_limiter import apply_endpoint_limits
    apply_endpoint_limits(app, limiter)

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
        # 内容安全策略 - 允许CDN资源 (GB/T 33008 + GB/T 37980)
        csp_directives = [
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://cdn.socket.io" + (' ' + ' '.join(SecurityConfig.CSP_EXTRA_SCRIPTS) if SecurityConfig.CSP_EXTRA_SCRIPTS else ''),
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://fonts.googleapis.com",
            "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net",
            "img-src 'self' data: blob:",
            "connect-src 'self' ws: wss:",
            "frame-ancestors 'self'",
        ]
        response.headers['Content-Security-Policy'] = '; '.join(csp_directives)
        # 严格传输安全 (HTTPS时启用，或配置强制启用TLS时)
        if request.is_secure or SecurityConfig.TLS_ENABLED:
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
            # Use full verification including blacklist check
            auth_manager = current_app.auth_manager
            payload = auth_manager.verify_token(token)
            if not payload:
                return redirect('/login')
            g.current_user = payload
        except Exception:
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

    # CSRF令牌端点 (GB/T 37980)
    @app.route('/api/csrf-token', methods=['GET'])
    def get_csrf_token():
        """获取CSRF令牌"""
        from core.csrf_protection import csrf
        token = csrf.generate_token()
        return jsonify({'csrf_token': token})

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
