"""
API公共工具函数
"""

from typing import Any
import yaml
import logging
from pathlib import Path
from functools import wraps
from flask import current_app, jsonify

logger = logging.getLogger(__name__)


def get_auth_manager() -> Any:
    """获取认证管理器"""
    return current_app.auth_manager


def api_error_handler(f):
    """API错误处理装饰器（所有API模块共用）"""
    @wraps(f)
    def decorated(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except ValueError as e:
            logger.warning(f"Validation error in {f.__name__}: {e}")
            return jsonify({'error': '请求参数验证失败'}), 400
        except PermissionError as e:
            logger.warning(f"Permission denied in {f.__name__}: {e}")
            return jsonify({'error': '权限不足'}), 403
        except Exception as e:
            from werkzeug.exceptions import HTTPException
            if isinstance(e, HTTPException):
                raise
            logger.error(f"API error in {f.__name__}: {e}", exc_info=True)
            return jsonify({'error': 'Internal server error'}), 500
    return decorated


def safe_int(val, name='value'):
    """安全整数转换"""
    try:
        return int(val)
    except (ValueError, TypeError):
        raise ValueError(f'Invalid {name}: must be integer')


def safe_float(val, name='value'):
    """安全浮点转换"""
    try:
        return float(val)
    except (ValueError, TypeError):
        raise ValueError(f'Invalid {name}: must be number')


def load_yaml_config(config_path: str) -> dict[str, Any]:
    """加载YAML配置文件"""
    path = Path(config_path)
    if not path.exists():
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f) or {}


def api_success(data=None, message: str = 'success', **kwargs):
    """标准化成功响应"""
    response = {'success': True, 'message': message}
    if data is not None:
        response['data'] = data
    response.update(kwargs)
    return jsonify(response)


def api_error(message: str, code: int = 400, error_code: str = None):
    """标准化错误响应"""
    response = {'success': False, 'error': message}
    if error_code:
        response['error_code'] = error_code
    return jsonify(response), code


def api_paginated(items: list, total: int, page: int = 1, per_page: int = 20):
    """标准化分页响应"""
    return jsonify({
        'success': True,
        'data': items,
        'pagination': {
            'total': total,
            'page': page,
            'per_page': per_page,
            'pages': (total + per_page - 1) // per_page,
        }
    })


def save_yaml_config(config_path: str, config: dict[str, Any]) -> bool:
    """保存YAML配置文件（原子写入：先写临时文件再 rename）"""
    try:
        path = Path(config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix('.tmp')
        with open(tmp_path, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
        tmp_path.replace(path)
        return True
    except Exception as e:
        logger.error(f"保存配置文件失败: {e}")
        return False
