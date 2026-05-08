"""
认证相关API
用户登录/注册/令牌/密码/用户管理/操作日志
"""

import logging
from flask import Blueprint, jsonify, request

from 用户层.auth import jwt_required, role_required
from ._common import get_auth_manager

logger = logging.getLogger(__name__)

auth_bp = Blueprint('api_auth', __name__, url_prefix='/api')

# 向后兼容别名
_require_auth = jwt_required
_require_admin = role_required('admin')


# ==================== 认证相关API ====================

@auth_bp.route('/auth/login', methods=['POST'])
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

    return jsonify(result), (200 if result['success'] else 401)


@auth_bp.route('/auth/register', methods=['POST'])
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
def verify_token():
    """验证令牌有效性"""
    return jsonify({'valid': True, 'user': request.current_user})


@auth_bp.route('/auth/refresh', methods=['POST'])
def refresh_token():
    """刷新令牌"""
    data = request.get_json()
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
def change_password():
    """修改密码"""
    data = request.get_json()
    auth_manager = get_auth_manager()
    result = auth_manager.change_password(
        username=request.current_user['username'],
        old_password=data.get('old_password', ''),
        new_password=data.get('new_password', '')
    )
    return jsonify(result)


@auth_bp.route('/auth/users', methods=['GET'])
@_require_admin
def get_users():
    """获取用户列表（仅管理员）"""
    auth_manager = get_auth_manager()
    return jsonify({'users': auth_manager.get_users()})


@auth_bp.route('/auth/users/<username>', methods=['PUT'])
@_require_admin
def update_user(username):
    """更新用户信息（仅管理员）"""
    data = request.get_json()
    auth_manager = get_auth_manager()
    return jsonify(auth_manager.update_user(username, **data))


@auth_bp.route('/auth/users/<username>', methods=['DELETE'])
@_require_admin
def delete_user(username):
    """删除用户（仅管理员）"""
    auth_manager = get_auth_manager()
    return jsonify(auth_manager.delete_user(username))


@auth_bp.route('/auth/logs', methods=['GET'])
@_require_admin
def get_operation_logs():
    """获取操作日志（仅管理员）"""
    auth_manager = get_auth_manager()
    username = request.args.get('username')
    limit = request.args.get('limit', 100, type=int)
    return jsonify({'logs': auth_manager.get_operation_logs(username=username, limit=limit)})
