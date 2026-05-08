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

# 统一注册的Blueprint列表
ALL_BLUEPRINTS = [
    auth_bp,
    devices_bp,
    control_bp,
    data_bp,
    alarms_bp,
    system_bp,
    industry40_bp,
]


def register_api_blueprints(app):
    """将所有API Blueprint注册到Flask应用"""
    for bp in ALL_BLUEPRINTS:
        app.register_blueprint(bp)
