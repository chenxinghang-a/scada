"""
REST API模块 - 向后兼容包装器

此文件已重构为模块化结构，代码拆分到 api/ 子目录：
  api/api_auth.py      - 认证相关API
  api/api_devices.py   - 设备管理API
  api/api_control.py   - 设备控制API
  api/api_data.py      - 数据查询与导出API
  api/api_alarms.py    - 报警相关API
  api/api_system.py    - 系统信息与配置API
  api/api_industry40.py - 工业4.0智能层API

此文件保留为向后兼容入口，新代码请使用 api 包。
"""

# 向后兼容：导出api_bp供旧代码使用
from flask import Blueprint

# 创建一个兼容的api_bp，实际路由已拆分到子模块
api_bp = Blueprint('api_compat', __name__)

# 导出注册函数供routes.py使用
from .api import register_api_blueprints

__all__ = ['api_bp', 'register_api_blueprints']
