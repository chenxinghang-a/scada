"""
Flask路由模块
定义Web页面路由
"""

from flask import Flask, render_template, jsonify, request, redirect, url_for
from flask_socketio import SocketIO

from .api import api_bp
from .websocket import init_socketio
from 用户层.auth import AuthManager


def create_app(database, device_manager, alarm_manager, data_collector,
               predictive_maintenance=None, oee_calculator=None,
               spc_analyzer=None, energy_manager=None, edge_decision=None):
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
        
    Returns:
        Flask: Flask应用实例
    """
    app = Flask(__name__,
                template_folder='../模板',
                static_folder='../静态资源')
    
    app.config['SECRET_KEY'] = 'industrial-scada-secret-key'
    
    # 初始化认证管理器
    auth_manager = AuthManager(database)
    
    # 注册API蓝图
    app.register_blueprint(api_bp, url_prefix='/api')
    
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
    
    # 页面路由
    @app.route('/')
    def index():
        """首页 - 仪表盘"""
        return render_template('dashboard.html')
    
    @app.route('/dashboard')
    def dashboard():
        """仪表盘页面"""
        return render_template('dashboard.html')
    
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
    
    # 错误处理
    @app.errorhandler(404)
    def not_found(error):
        return jsonify({'error': 'Not found'}), 404
    
    @app.errorhandler(500)
    def internal_error(error):
        return jsonify({'error': 'Internal server error'}), 500
    
    return app
