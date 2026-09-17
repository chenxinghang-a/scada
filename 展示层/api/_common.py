"""
API公共工具函数
"""

from typing import Any
import yaml
import logging
from pathlib import Path
from functools import wraps
from flask import current_app, jsonify, make_response

import paths

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
            return api_error('请求参数验证失败')
        except PermissionError as e:
            logger.warning(f"Permission denied in {f.__name__}: {e}")
            return api_error('权限不足', 403)
        except Exception as e:
            from werkzeug.exceptions import HTTPException
            if isinstance(e, HTTPException):
                raise
            logger.error(f"API error in {f.__name__}: {e}", exc_info=True)
            return api_error('Internal server error', 500)
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


# 查询类接口 limit 参数的硬上限。未设上限时，`?limit=999999999` 会直接
# 打穿到数据库层（全表扫描 + 大对象分配），是最省事的一种 DoS 面。
MAX_QUERY_LIMIT = 10000


def clamp_limit(val, default: int = 50, maximum: int = MAX_QUERY_LIMIT) -> int:
    """把查询参数 limit 规整到 [1, maximum]。

    - val 为 None / 不可转换 → 返回 default（并同样受 maximum 约束）
    - 负数 / 0 → 1
    - 超过上限 → 上限

    调用方传入的 default 若本身大于 maximum，会被一起夹紧，保证任何路径
    都不可能拿到超过上限的值。
    """
    upper = max(1, int(maximum))
    base = max(1, min(int(default), upper))
    if val is None:
        return base
    try:
        n = int(val)
    except (ValueError, TypeError):
        return base
    return max(1, min(n, upper))


def load_yaml_config(config_path: str) -> dict[str, Any]:
    """加载YAML配置文件

    相对路径一律按项目根目录解析（见 ``paths.resolve``）。历史调用方传的是
    ``'配置/alarms.yaml'`` 这类字面量，只在 CWD 恰好是项目根目录时才成立；
    从别处启动会读不到文件 → 返回 ``{}`` → 接口回 200 但配置"莫名变空"，
    属于典型的静默失败。
    """
    path = paths.resolve(config_path)
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
    """标准化错误响应（返回带状态码的Response，可直接作为视图返回值）"""
    response = {'success': False, 'error': message}
    if error_code:
        response['error_code'] = error_code
    return make_response(jsonify(response), code)


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


def get_pagination_params(default_page: int = 1, default_per_page: int = 20, max_per_page: int = 100):
    """从请求参数中解析分页参数

    Returns:
        tuple: (page, per_page, offset)
    """
    from flask import request
    page = request.args.get('page', default_page, type=int)
    per_page = request.args.get('per_page', default_per_page, type=int)
    if page < 1:
        page = 1
    if per_page < 1:
        per_page = default_per_page
    if per_page > max_per_page:
        per_page = max_per_page
    offset = (page - 1) * per_page
    return page, per_page, offset


def save_yaml_config(config_path: str, config: dict[str, Any]) -> bool:
    """保存YAML配置文件（原子写入：先写临时文件再 rename）

    相对路径同样按项目根目录解析，理由见 ``load_yaml_config``。
    """
    try:
        path = paths.resolve(config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix('.tmp')
        with open(tmp_path, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
        tmp_path.replace(path)
        return True
    except Exception as e:
        logger.error(f"保存配置文件失败: {e}")
        return False
