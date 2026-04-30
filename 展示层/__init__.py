"""
Web展示模块
"""

from .routes import create_app
from .api import api_bp
from .websocket import init_socketio

__all__ = ['create_app', 'api_bp', 'init_socketio']
