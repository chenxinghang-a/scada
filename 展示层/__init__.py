"""
Web展示模块
"""

from .routes import create_app
from .api import register_api_blueprints
from .websocket import init_socketio

__all__ = ['create_app', 'register_api_blueprints', 'init_socketio']
