"""
认证相关API
用户登录/注册/令牌/密码/用户管理/操作日志
"""

import logging
from functools import wraps
from flask import Blueprint, jsonify, request

from 用户层.auth import jwt_required, role_required
from ._common import get_auth_manager

logger = logging.getLogger(__name__)

auth_bp = Blueprint('api_auth', __name__, url_prefix='/api')

# 向后兼容别名
_require_auth = jwt_required
_require_admin = role_required('admin')


def api_error_handler(f):
    """API错误处理装饰器"""
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


# ==================== 认证相关API ====================

@auth_bp.route('/auth/login', methods=['POST'])
@api_error_handler
def login():
    """用户登录"""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': '请提供登录信息'}), 400

    username = data.get('username', '').strip()
    password = data.get('password', '')

    if not username or not password:
        return jsonify({'success': False, 'message': '用户名和密码不能为空'}), 400

    auth_manager = get_auth_manager()
    ip_address = request.remote_addr
    result = auth_manager.login(username, password, ip_address)

    if not result['success']:
        return jsonify(result), 401
    if result.get('status') == 'must_change_password':
        return jsonify(result), 403
    return jsonify(result), 200


@auth_bp.route('/auth/logout', methods=['POST'])
@jwt_required
def logout():
    """服务端登出 - 记录登出时间 + 撤销令牌 (GB/T 33008 + GB/T 35718)"""
    try:
        user = getattr(request, 'current_user', {})
        username = user.get('username', 'unknown')

        auth_manager = get_auth_manager()

        # GB/T 35718: 撤销当前JWT令牌
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]
            auth_manager.blacklist_token(token, 'logout')

        # 记录登出
        auth_manager.log_operation(
            username=username,
            action='logout',
            target='session',
            ip_address=request.remote_addr
        )

        return jsonify({'message': '已登出'})
    except Exception as e:
        logger.error(f"登出失败: {e}")
        return jsonify({'message': '已登出'})


@auth_bp.route('/auth/register', methods=['POST'])
@api_error_handler
def register():
    """用户注册（仅管理员，或首个用户直接注册admin）"""
    token = None
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        token = auth_header[7:]

    auth_manager = get_auth_manager()

    # 无token时：检查是否是第一个用户（允许直接注册admin）
    if not token:
        users = auth_manager.get_users()
        if len(users) > 0:
            return jsonify({'success': False, 'message': '需要管理员权限'}), 403
    else:
        user = auth_manager.verify_token(token)
        if not user or user['role'] != 'admin':
            return jsonify({'success': False, 'message': '需要管理员权限'}), 403

    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': '请提供注册信息'}), 400

    result = auth_manager.register(
        username=data.get('username', '').strip(),
        password=data.get('password', ''),
        role=data.get('role', 'viewer'),
        display_name=data.get('display_name'),
        email=data.get('email'),
        phone=data.get('phone')
    )

    return jsonify(result), (201 if result['success'] else 400)


@auth_bp.route('/auth/verify', methods=['GET'])
@_require_auth
@api_error_handler
def verify_token():
    """验证令牌有效性"""
    return jsonify({'valid': True, 'user': request.current_user})


@auth_bp.route('/auth/refresh', methods=['POST'])
@api_error_handler
def refresh_token():
    """刷新令牌"""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': '请提供刷新令牌'}), 400
    rtoken = data.get('refresh_token')

    if not rtoken:
        return jsonify({'success': False, 'message': '请提供刷新令牌'}), 400

    auth_manager = get_auth_manager()
    result = auth_manager.refresh_token(rtoken)

    if result:
        return jsonify(result)
    else:
        return jsonify({'success': False, 'message': '刷新令牌无效'}), 401


@auth_bp.route('/auth/change-password', methods=['POST'])
@_require_auth
@api_error_handler
def change_password():
    """修改密码"""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': '请提供改密信息'}), 400
    auth_manager = get_auth_manager()
    result = auth_manager.change_password(
        username=request.current_user['username'],
        old_password=data.get('old_password', ''),
        new_password=data.get('new_password', '')
    )
    return jsonify(result)


@auth_bp.route('/auth/force-change-password', methods=['POST'])
@api_error_handler
def force_change_password():
    """首次登录强制改密（不需要旧密码，需要有效token）"""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': '请提供改密信息'}), 400

    username = data.get('username', '').strip()
    new_password = data.get('new_password', '')

    if not username or not new_password:
        return jsonify({'success': False, 'message': '用户名和新密码不能为空'}), 400

    # 验证token（从请求头或body中获取）
    token = None
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        token = auth_header[7:]
    if not token:
        token = data.get('token')

    if not token:
        return jsonify({'success': False, 'message': '请提供认证令牌'}), 401

    auth_manager = get_auth_manager()
    user = auth_manager.verify_token(token)
    if not user or user['username'] != username:
        return jsonify({'success': False, 'message': '令牌无效或用户名不匹配'}), 401

    result = auth_manager.force_change_password(username, new_password)

    if result['success']:
        return jsonify(result), 200
    return jsonify(result), 400


@auth_bp.route('/auth/users', methods=['GET'])
@_require_admin
@api_error_handler
def get_users():
    """获取用户列表（仅管理员）"""
    auth_manager = get_auth_manager()
    return jsonify({'users': auth_manager.get_users()})


@auth_bp.route('/auth/users/<username>', methods=['PUT'])
@_require_admin
@api_error_handler
def update_user(username):
    """更新用户信息（仅管理员）"""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'message': '请提供更新数据'}), 400
    # 字段白名单过滤，防止越权修改
    allowed = {'display_name', 'email', 'phone', 'role', 'is_active'}
    filtered = {k: v for k, v in data.items() if k in allowed}
    auth_manager = get_auth_manager()
    return jsonify(auth_manager.update_user(username, **filtered))


@auth_bp.route('/auth/users/<username>', methods=['DELETE'])
@_require_admin
@api_error_handler
def delete_user(username):
    """删除用户（仅管理员）"""
    auth_manager = get_auth_manager()
    return jsonify(auth_manager.delete_user(username))


@auth_bp.route('/auth/logs', methods=['GET'])
@_require_admin
@api_error_handler
def get_operation_logs():
    """获取操作日志（仅管理员）"""
    auth_manager = get_auth_manager()
    username = request.args.get('username')
    limit = request.args.get('limit', 100, type=int)
    return jsonify({'logs': auth_manager.get_operation_logs(username=username, limit=limit)})
