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
from core.safe_numeric import to_finite_float, to_int, try_int
from core.config_io import dump_yaml

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
    """安全整数转换 —— 规则收在 `core/safe_numeric.py`（唯一真源）。

    这里只保留函数名，避免 15 个调用点全改。历史坑：`int(float('inf'))` 抛的是
    **OverflowError**，不在 `(ValueError, TypeError)` 里，而 JSON 的 `1e400`
    会被解析成 `inf` —— 于是 `{"address": 1e400}` 漏成 HTTP 500。
    """
    return to_int(val, name)


def safe_float(val, name='value'):
    """安全浮点转换 —— 规则收在 `core/safe_numeric.py`（唯一真源）。

    必须拒掉 **NaN / ±Infinity**：`json.loads` 默认接受裸 `NaN` / `Infinity`
    字面量，而 `NaN` 参与的任何大小比较都是 False —— `value < min or value > max`
    判不出越界，**量程校验形同虚设**，NaN 会被下发到现场设备。
    """
    return to_finite_float(val, name)


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
    n = try_int(val)
    if n is None:
        return base
    return max(1, min(n, upper))


def load_yaml_config(config_path: str) -> dict[str, Any]:
    """加载YAML配置文件

    相对路径按 **env-aware** 规则解析（见 ``paths.resolve_config_file``）：
    ``'配置/x.yaml'`` 映射到 ``CONFIG_DIR``（**吃 SCADA_CONFIG_DIR env**，
    round 213 修 round 206 漏掉的「配置那半边」）；绝对路径与其它形态
    仍按项目根目录解析（向后兼容，测试夹具不受影响）。
    """
    path = paths.resolve_config_file(config_path)
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
    """保存YAML配置文件（原子写 + 就地合并，规则收在 `core/config_io.py`）

    相对路径同样走 env-aware 解析，理由见 ``load_yaml_config``。

    round 256：原来自己拼 `.tmp` 再 rename —— 路径是 `xxx.tmp`（固定名），
    两个请求同时写同一个配置文件时共用一个临时文件，内容会互相穿插。
    且 `yaml.dump` 整份重写会抹掉注释、按字母重排键。
    """
    try:
        path = paths.resolve_config_file(config_path)
        return dump_yaml(path, config)
    except Exception as e:
        logger.error(f"保存配置文件失败: {e}")
        return False
