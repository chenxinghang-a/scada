"""
用户认证与权限管理模块
实现JWT令牌认证、角色权限控制
"""

import jwt
import bcrypt
import logging
from datetime import datetime, timedelta
from functools import wraps
from typing import Any
from pathlib import Path
from flask import request, jsonify, current_app

logger = logging.getLogger(__name__)

# 角色权限定义
ROLES = {
    'admin': {
        'name': '管理员',
        'permissions': ['read', 'write', 'delete', 'manage_users', 'manage_devices', 'acknowledge_alarms', 'export_data', 'system_config']
    },
    'engineer': {
        'name': '工程师',
        'permissions': ['read', 'write', 'acknowledge_alarms', 'export_data', 'manage_devices']
    },
    'operator': {
        'name': '操作员',
        'permissions': ['read', 'acknowledge_alarms']
    },
    'viewer': {
        'name': '观察者',
        'permissions': ['read']
    }
}

# JWT配置（从config.py统一读取）
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from config import AuthConfig

JWT_SECRET = AuthConfig.JWT_SECRET
JWT_ALGORITHM = AuthConfig.JWT_ALGORITHM
JWT_EXPIRATION_HOURS = AuthConfig.JWT_EXPIRATION_HOURS
JWT_REFRESH_DAYS = AuthConfig.JWT_REFRESH_DAYS


class AuthManager:
    """
    用户认证管理器
    处理用户注册、登录、JWT令牌管理
    """
    
    def __init__(self, database):
        """
        初始化认证管理器
        
        Args:
            database: 数据库实例
        """
        self.database = database
        self._init_users_table()
        self._create_default_admin()
    
    def _init_users_table(self):
        """初始化用户表"""
        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'viewer',
                    display_name TEXT,
                    email TEXT,
                    phone TEXT,
                    last_login DATETIME,
                    login_attempts INTEGER DEFAULT 0,
                    locked_until DATETIME,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1
                )
            ''')
            
            # 创建操作日志表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS operation_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT,
                    detail TEXT,
                    ip_address TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 创建索引
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_users_username 
                ON users(username)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_operation_logs_user 
                ON operation_logs(username, timestamp)
            ''')
            
            logger.info("用户表初始化完成")
    
    def _create_default_admin(self):
        """创建默认管理员账户"""
        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM users WHERE username = ?', ('admin',))
            if cursor.fetchone()[0] == 0:
                password_hash = bcrypt.hashpw('admin123'.encode('utf-8'), bcrypt.gensalt())
                cursor.execute('''
                    INSERT INTO users (username, password_hash, role, display_name)
                    VALUES (?, ?, ?, ?)
                ''', ('admin', password_hash.decode('utf-8'), 'admin', '系统管理员'))
                logger.info("已创建默认管理员账户: admin/admin123")
    
    def register(self, username: str, password: str, role: str = 'viewer',
                 display_name: str | None = None, email: str | None = None, phone: str | None = None) -> dict[str, Any]:
        """
        注册新用户
        
        Args:
            username: 用户名
            password: 密码
            role: 角色
            display_name: 显示名称
            email: 邮箱
            phone: 手机号
            
        Returns:
            dict[str, Any]: 注册结果
        """
        # 验证角色
        if role not in ROLES:
            return {'success': False, 'message': f'无效角色: {role}，可选: {", ".join(ROLES.keys())}'}
        
        # 验证用户名
        if len(username) < 3 or len(username) > 20:
            return {'success': False, 'message': '用户名长度需在3-20之间'}
        
        # 验证密码强度
        if len(password) < 6:
            return {'success': False, 'message': '密码长度至少6位'}
        
        # 哈希密码
        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
        
        try:
            with self.database.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO users (username, password_hash, role, display_name, email, phone)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (username, password_hash.decode('utf-8'), role, 
                      display_name or username, email, phone))
            
            self._log_operation('system', 'register', username, f'新用户注册: {username}, 角色: {role}')
            logger.info(f"用户注册成功: {username} ({role})")
            return {'success': True, 'message': '注册成功'}
        
        except Exception as e:
            if 'UNIQUE' in str(e):
                return {'success': False, 'message': '用户名已存在'}
            return {'success': False, 'message': f'注册失败: {str(e)}'}
    
    def login(self, username: str, password: str, ip_address: str | None = None) -> dict[str, Any]:
        """
        用户登录
        
        Args:
            username: 用户名
            password: 密码
            ip_address: 客户端IP
            
        Returns:
            dict[str, Any]: 登录结果（包含JWT令牌）
        """
        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM users WHERE username = ? AND is_active = 1
            ''', (username,))
            user = cursor.fetchone()
        
        if not user:
            self._log_operation(username, 'login_failed', None, '用户不存在', ip_address)
            return {'success': False, 'message': '用户名或密码错误'}
        
        user = dict(user)
        
        # 检查账户锁定
        if user.get('locked_until'):
            locked_until = datetime.fromisoformat(user['locked_until'])
            if datetime.now() < locked_until:
                remaining = (locked_until - datetime.now()).seconds // 60
                return {'success': False, 'message': f'账户已锁定，请{remaining}分钟后重试'}
        
        # 验证密码
        if not bcrypt.checkpw(password.encode('utf-8'), user['password_hash'].encode('utf-8')):
            # 增加失败次数
            attempts = user['login_attempts'] + 1
            locked_until = None
            if attempts >= 5:
                locked_until = (datetime.now() + timedelta(minutes=30)).isoformat()
            
            with self.database.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE users SET login_attempts = ?, locked_until = ?
                    WHERE username = ?
                ''', (attempts, locked_until, username))
            
            self._log_operation(username, 'login_failed', None, 
                              f'密码错误 (尝试{attempts}次)', ip_address)
            
            if attempts >= 5:
                return {'success': False, 'message': '密码错误次数过多，账户已锁定30分钟'}
            return {'success': False, 'message': f'用户名或密码错误 (还剩{5-attempts}次机会)'}
        
        # 登录成功 - 生成JWT
        token = self._generate_token(user)
        refresh_token = self._generate_refresh_token(user)
        
        # 更新登录信息
        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE users SET last_login = ?, login_attempts = 0, locked_until = NULL
                WHERE username = ?
            ''', (datetime.now(), username))
        
        self._log_operation(username, 'login', None, '登录成功', ip_address)
        logger.info(f"用户登录成功: {username}")
        
        return {
            'success': True,
            'message': '登录成功',
            'token': token,
            'refresh_token': refresh_token,
            'user': {
                'username': user['username'],
                'role': user['role'],
                'display_name': user['display_name'],
                'permissions': ROLES.get(user['role'], {}).get('permissions', [])
            }
        }
    
    def verify_token(self, token: str) -> dict[str, Any] | None:
        """
        验证JWT令牌
        
        Args:
            token: JWT令牌
            
        Returns:
            dict[str, Any]: 用户信息（验证失败返回None）
        """
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
            
            # 检查用户是否仍然活跃
            with self.database.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT username, role, display_name, is_active 
                    FROM users WHERE username = ?
                ''', (payload.get('username'),))
                user = cursor.fetchone()
            
            if not user or not user['is_active']:
                return None
            
            return {
                'username': user['username'],
                'role': user['role'],
                'display_name': user['display_name'],
                'permissions': ROLES.get(user['role'], {}).get('permissions', [])
            }
        
        except jwt.ExpiredSignatureError:
            logger.warning("JWT令牌已过期")
            return None
        except jwt.InvalidTokenError as e:
            logger.warning(f"无效JWT令牌: {e}")
            return None
    
    def refresh_token(self, refresh_token: str) -> dict[str, Any] | None:
        """
        刷新JWT令牌
        
        Args:
            refresh_token: 刷新令牌
            
        Returns:
            dict[str, Any]: 新的令牌信息
        """
        try:
            payload = jwt.decode(refresh_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
            
            if payload.get('type') != 'refresh':
                return None
            
            with self.database.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT * FROM users WHERE username = ? AND is_active = 1
                ''', (payload.get('username'),))
                user = cursor.fetchone()
            
            if not user:
                return None
            
            user = dict(user)
            new_token = self._generate_token(user)
            
            return {
                'success': True,
                'token': new_token,
                'user': {
                    'username': user['username'],
                    'role': user['role'],
                    'display_name': user['display_name'],
                    'permissions': ROLES.get(user['role'], {}).get('permissions', [])
                }
            }
        
        except Exception:
            return None
    
    def change_password(self, username: str, old_password: str, new_password: str) -> dict[str, Any]:
        """
        修改密码
        
        Args:
            username: 用户名
            old_password: 旧密码
            new_password: 新密码
            
        Returns:
            dict[str, Any]: 修改结果
        """
        if len(new_password) < 6:
            return {'success': False, 'message': '新密码长度至少6位'}
        
        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT password_hash FROM users WHERE username = ?', (username,))
            user = cursor.fetchone()
        
        if not user:
            return {'success': False, 'message': '用户不存在'}
        
        if not bcrypt.checkpw(old_password.encode('utf-8'), user['password_hash'].encode('utf-8')):
            return {'success': False, 'message': '旧密码错误'}
        
        new_hash = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt())
        
        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE users SET password_hash = ?, updated_at = ?
                WHERE username = ?
            ''', (new_hash.decode('utf-8'), datetime.now(), username))
        
        self._log_operation(username, 'change_password', None, '修改密码成功')
        return {'success': True, 'message': '密码修改成功'}
    
    def get_users(self) -> list[dict[str, Any]]:
        """获取所有用户列表"""
        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT id, username, role, display_name, email, phone, 
                       last_login, is_active, created_at
                FROM users ORDER BY created_at DESC
            ''')
            users = [dict(row) for row in cursor.fetchall()]
        
        # 添加角色名称
        for user in users:
            user['role_name'] = ROLES.get(user['role'], {}).get('name', user['role'])
        
        return users
    
    def update_user(self, username: str, **kwargs) -> dict[str, Any]:
        """更新用户信息"""
        allowed_fields = ['role', 'display_name', 'email', 'phone', 'is_active']
        updates = {k: v for k, v in kwargs.items() if k in allowed_fields and v is not None}
        
        if not updates:
            return {'success': False, 'message': '没有可更新的字段'}
        
        if 'role' in updates and updates['role'] not in ROLES:
            return {'success': False, 'message': f'无效角色: {updates["role"]}'}
        
        set_clause = ', '.join(f'{k} = ?' for k in updates.keys())
        values = list(updates.values()) + [datetime.now(), username]
        
        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f'''
                UPDATE users SET {set_clause}, updated_at = ?
                WHERE username = ?
            ''', values)
            
            if cursor.rowcount == 0:
                return {'success': False, 'message': '用户不存在'}
        
        self._log_operation('system', 'update_user', username, f'更新字段: {", ".join(updates.keys())}')
        return {'success': True, 'message': '更新成功'}
    
    def delete_user(self, username: str) -> dict[str, Any]:
        """删除用户（软删除）"""
        if username == 'admin':
            return {'success': False, 'message': '不能删除管理员账户'}
        
        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE users SET is_active = 0, updated_at = ?
                WHERE username = ?
            ''', (datetime.now(), username))
            
            if cursor.rowcount == 0:
                return {'success': False, 'message': '用户不存在'}
        
        self._log_operation('system', 'delete_user', username, '用户已禁用')
        return {'success': True, 'message': '用户已禁用'}
    
    def get_operation_logs(self, username: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """获取操作日志"""
        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            if username:
                cursor.execute('''
                    SELECT * FROM operation_logs 
                    WHERE username = ?
                    ORDER BY timestamp DESC LIMIT ?
                ''', (username, limit))
            else:
                cursor.execute('''
                    SELECT * FROM operation_logs 
                    ORDER BY timestamp DESC LIMIT ?
                ''', (limit,))
            return [dict(row) for row in cursor.fetchall()]
    
    def _generate_token(self, user: dict[str, Any]) -> str:
        """生成JWT访问令牌"""
        payload = {
            'username': user['username'],
            'role': user['role'],
            'type': 'access',
            'iat': datetime.utcnow(),
            'exp': datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS)
        }
        return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    
    def _generate_refresh_token(self, user: dict[str, Any]) -> str:
        """生成JWT刷新令牌"""
        payload = {
            'username': user['username'],
            'type': 'refresh',
            'iat': datetime.utcnow(),
            'exp': datetime.utcnow() + timedelta(days=JWT_REFRESH_DAYS)
        }
        return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    
    def log_operation(self, username: str, action: str, target: str | None = None,
                      detail: str | None = None, ip_address: str | None = None):
        """记录操作日志（公共接口）"""
        self._log_operation(username, action, target, detail, ip_address)

    def _log_operation(self, username: str, action: str, target: str | None = None,
                       detail: str | None = None, ip_address: str | None = None):
        """记录操作日志（内部实现）"""
        try:
            with self.database.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO operation_logs (username, action, target, detail, ip_address)
                    VALUES (?, ?, ?, ?, ?)
                ''', (username, action, target, detail, ip_address))
        except Exception as e:
            logger.error(f"记录操作日志失败: {e}")


def jwt_required(f):
    """
    JWT认证装饰器
    要求请求头中包含有效的JWT令牌
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        
        # 从Header获取token
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:]
        
        # 也支持从查询参数获取（用于WebSocket等场景）
        if not token:
            token = request.args.get('token')
        
        if not token:
            return jsonify({'error': '未提供认证令牌', 'code': 'NO_TOKEN'}), 401
        
        auth_manager = current_app.auth_manager
        user = auth_manager.verify_token(token)
        
        if not user:
            return jsonify({'error': '令牌无效或已过期', 'code': 'INVALID_TOKEN'}), 401
        
        # 将用户信息附加到请求上下文
        request.current_user = user
        return f(*args, **kwargs)
    
    return decorated


def role_required(*required_roles):
    """
    角色权限装饰器
    要求用户具有指定角色之一
    
    Usage:
        @role_required('admin', 'engineer')
        def admin_only():
            ...
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = getattr(request, 'current_user', None)
            if not user:
                return jsonify({'error': '未认证'}), 401
            
            if user['role'] not in required_roles:
                return jsonify({
                    'error': '权限不足',
                    'required_roles': list(required_roles),
                    'current_role': user['role']
                }), 403
            
            return f(*args, **kwargs)
        return decorated
    return decorator


def permission_required(permission: str):
    """
    权限装饰器
    要求用户具有指定权限
    
    Usage:
        @permission_required('manage_users')
        def manage_users():
            ...
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = getattr(request, 'current_user', None)
            if not user:
                return jsonify({'error': '未认证'}), 401
            
            if permission not in user.get('permissions', []):
                return jsonify({
                    'error': '权限不足',
                    'required_permission': permission
                }), 403
            
            return f(*args, **kwargs)
        return decorated
    return decorator
