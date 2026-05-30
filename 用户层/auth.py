"""
用户认证与权限管理模块
实现JWT令牌认证、角色权限控制
"""

import jwt
import uuid
import bcrypt
import logging
from datetime import datetime, timedelta
from functools import wraps
from typing import Any
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
# run.py已将项目根目录加入sys.path，直接导入即可
from config import AuthConfig

JWT_SECRET = AuthConfig.JWT_SECRET
JWT_ALGORITHM = AuthConfig.JWT_ALGORITHM
JWT_EXPIRATION_HOURS = AuthConfig.JWT_EXPIRATION_HOURS
JWT_REFRESH_DAYS = AuthConfig.JWT_REFRESH_DAYS

# 默认管理员密码（可通过环境变量 SCADA_ADMIN_PASSWORD 覆盖）
DEFAULT_ADMIN_PASSWORD = AuthConfig.SCADA_ADMIN_PASSWORD


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
                    is_active BOOLEAN DEFAULT 1,
                    must_change_password BOOLEAN DEFAULT 0
                )
            ''')

            # 为已有表添加 must_change_password 列（兼容升级）
            try:
                cursor.execute('ALTER TABLE users ADD COLUMN must_change_password BOOLEAN DEFAULT 0')
            except Exception:
                pass  # 列已存在

            # 为已有表添加 password_changed_at 列（GB/T 35718: 密码变更时间戳）
            try:
                cursor.execute('ALTER TABLE users ADD COLUMN password_changed_at DATETIME')
            except Exception:
                pass  # 列已存在

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

            # 创建JWT黑名单表 (GB/T 35718: 令牌撤销机制)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS jwt_blacklist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_jti TEXT UNIQUE NOT NULL,
                    blacklisted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    reason TEXT
                )
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_jwt_blacklist_jti
                ON jwt_blacklist(token_jti)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_jwt_blacklist_time
                ON jwt_blacklist(blacklisted_at)
            ''')

            logger.info("用户表初始化完成")

    def _create_default_admin(self):
        """创建默认管理员账户"""
        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM users WHERE username = ?', ('admin',))
            if cursor.fetchone()[0] == 0:
                password_hash = bcrypt.hashpw(DEFAULT_ADMIN_PASSWORD.encode('utf-8'), bcrypt.gensalt())
                cursor.execute('''
                    INSERT INTO users (username, password_hash, role, display_name, must_change_password)
                    VALUES (?, ?, ?, ?, 1)
                ''', ('admin', password_hash.decode('utf-8'), 'admin', '系统管理员'))
                logger.info("已创建默认管理员账户 (密码通过 SCADA_ADMIN_PASSWORD 环境变量配置)")

    def _validate_password_strength(self, password: str) -> tuple[bool, str]:
        """验证密码强度 - 等保2.0要求"""
        if len(password) < 8:
            return False, "密码长度至少8位"
        if not any(c.isupper() for c in password):
            return False, "密码必须包含大写字母"
        if not any(c.islower() for c in password):
            return False, "密码必须包含小写字母"
        if not any(c.isdigit() for c in password):
            return False, "密码必须包含数字"
        return True, ""

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

        # 验证密码强度（等保2.0）
        valid, msg = self._validate_password_strength(password)
        if not valid:
            return {'success': False, 'message': msg}

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
            attempts = (user['login_attempts'] or 0) + 1
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

        # 检查是否需要强制改密
        if user.get('must_change_password'):
            return {
                'success': True,
                'status': 'must_change_password',
                'token': token,
                'refresh_token': refresh_token,
                'message': '首次登录请修改密码',
                'user': {
                    'username': user['username'],
                    'role': user['role'],
                    'display_name': user['display_name'],
                    'permissions': ROLES.get(user['role'], {}).get('permissions', [])
                }
            }

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
        """验证 JWT 令牌的有效性（含黑名单检查 — GB/T 35718 令牌撤销机制）。

        解码并验证 JWT 令牌，检查以下条件：
        1. 令牌签名和有效期是否合法。
        2. 令牌是否在黑名单中（已撤销的令牌）。
        3. 对应用户是否仍然活跃（未被禁用）。

        Args:
            token: JWT 令牌字符串（不含 ``Bearer`` 前缀）。

        Returns:
            dict[str, Any] | None: 验证成功时返回用户信息字典，包含：
                - ``username`` (str): 用户名。
                - ``role`` (str): 角色名。
                - ``display_name`` (str): 显示名称。
                - ``permissions`` (list[str]): 权限列表。
            验证失败时返回 ``None``（令牌过期、无效、被撤销或用户不存在）。

        Side Effects:
            - 查询 ``jwt_blacklist`` 表检查令牌是否被撤销。
            - 查询 ``users`` 表验证用户状态。
            - 令牌过期或无效时记录警告日志。

        Exceptions:
            不会主动抛出异常。``jwt.ExpiredSignatureError`` 和
            ``jwt.InvalidTokenError`` 均被捕获并返回 ``None``。
        """
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])

            # 检查令牌是否在黑名单中 (GB/T 35718)
            jti = payload.get('jti')
            if jti:
                with self.database.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute('SELECT 1 FROM jwt_blacklist WHERE token_jti = ?', (jti,))
                    if cursor.fetchone():
                        logger.warning(f"JWT令牌已被撤销: jti={jti}")
                        return None

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

    def blacklist_token(self, token: str, reason: str = 'logout') -> bool:
        """
        将JWT令牌加入黑名单 (GB/T 35718: 令牌撤销)

        Args:
            token: JWT令牌字符串
            reason: 黑名单原因

        Returns:
            bool: 是否成功
        """
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
            jti = payload.get('jti')
            if not jti:
                return False

            with self.database.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    'INSERT OR IGNORE INTO jwt_blacklist (token_jti, reason) VALUES (?, ?)',
                    (jti, reason)
                )
            logger.info(f"JWT令牌已加入黑名单: jti={jti}, reason={reason}")
            return True
        except jwt.InvalidTokenError:
            return False
        except Exception as e:
            logger.error(f"令牌黑名单操作失败: {e}")
            return False

    def cleanup_expired_blacklist(self):
        """清理过期的黑名单条目（7天前的记录）"""
        try:
            cutoff = (datetime.now() - timedelta(days=7)).isoformat()
            with self.database.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM jwt_blacklist WHERE blacklisted_at < ?', (cutoff,))
                deleted = cursor.rowcount
            if deleted > 0:
                logger.info(f"清理了 {deleted} 条过期黑名单记录")
        except Exception as e:
            logger.error(f"清理黑名单失败: {e}")

    def _blacklist_user_tokens(self, username: str, reason: str = 'user_action'):
        """
        将用户当前请求的令牌加入黑名单
        注意：无法枚举所有活跃令牌，仅标记当前操作的令牌
        依赖verify_token中的黑名单检查来拒绝已撤销的令牌
        """
        # 从Flask request上下文获取当前令牌
        try:
            from flask import request as flask_request
            auth_header = flask_request.headers.get('Authorization', '')
            if auth_header.startswith('Bearer '):
                token = auth_header[7:]
                self.blacklist_token(token, reason)
        except Exception:
            pass  # 非HTTP上下文时忽略

    def refresh_token(self, refresh_token: str) -> dict[str, Any] | None:
        """使用刷新令牌获取新的访问令牌。

        验证刷新令牌的有效性（签名、类型、黑名单、用户状态），
        检查密码是否在令牌签发后被修改（防止旧令牌被滥用），
        验证通过后签发新的访问令牌。

        Args:
            refresh_token: JWT 刷新令牌字符串（``type='refresh'``）。

        Returns:
            dict[str, Any] | None: 刷新成功时返回字典，包含：
                - ``success`` (bool): 始终为 ``True``。
                - ``token`` (str): 新的访问令牌。
                - ``user`` (dict): 用户信息（username、role 等）。
            刷新失败时返回 ``None``（令牌无效、被撤销、密码已变更或
            用户不存在）。

        Side Effects:
            - 查询 ``jwt_blacklist`` 表检查令牌是否被撤销。
            - 查询 ``users`` 表验证用户状态和密码变更时间。
            - 令牌被撤销或密码已变更时记录警告日志。

        Exceptions:
            不会主动抛出异常。所有异常均被捕获并返回 ``None``。
        """
        try:
            payload = jwt.decode(refresh_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])

            if payload.get('type') != 'refresh':
                return None

            # 检查黑名单 (GB/T 35718)
            jti = payload.get('jti')
            if jti:
                with self.database.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute('SELECT 1 FROM jwt_blacklist WHERE token_jti = ?', (jti,))
                    if cursor.fetchone():
                        logger.warning(f"刷新令牌已被撤销: jti={jti}")
                        return None

            with self.database.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT * FROM users WHERE username = ? AND is_active = 1
                ''', (payload.get('username'),))
                user = cursor.fetchone()

            if not user:
                return None

            # 检查密码是否在令牌签发后被修改（BUG 3修复）
            user = dict(user)
            token_iat = payload.get('iat', 0)
            password_changed = user.get('password_changed_at')
            if password_changed:
                try:
                    pwd_time = datetime.fromisoformat(password_changed).timestamp()
                    if pwd_time > token_iat:
                        logger.warning(f"密码已变更，刷新令牌失效: user={user['username']}")
                        return None
                except (ValueError, TypeError):
                    pass

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
        """修改用户密码（需要验证旧密码）。

        验证旧密码正确后更新密码哈希，并撤销该用户的所有活跃 JWT
        令牌（GB/T 35718 要求：密码变更后旧令牌立即失效）。

        新密码需满足等保 2.0 强度要求：至少 8 位，包含大写字母、
        小写字母和数字。

        Args:
            username: 用户名。
            old_password: 旧密码（明文，用于验证）。
            new_password: 新密码（明文，将被哈希存储）。

        Returns:
            dict[str, Any]: 操作结果，包含：
                - ``success`` (bool): 是否成功。
                - ``message`` (str): 结果描述。

        Side Effects:
            - 更新 ``users`` 表中的 ``password_hash`` 和
              ``password_changed_at`` 字段。
            - 调用 ``_blacklist_user_tokens`` 撤销当前令牌。
            - 记录操作日志到 ``operation_logs`` 表。

        Exceptions:
            不会主动抛出异常。用户不存在或旧密码错误返回失败结果。
        """
        valid, msg = self._validate_password_strength(new_password)
        if not valid:
            return {'success': False, 'message': msg}

        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT password_hash FROM users WHERE username = ?', (username,))
            user = cursor.fetchone()

        if not user:
            return {'success': False, 'message': '用户不存在'}

        if not bcrypt.checkpw(old_password.encode('utf-8'), user['password_hash'].encode('utf-8')):
            return {'success': False, 'message': '旧密码错误'}

        new_hash = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt())

        now = datetime.now()
        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE users SET password_hash = ?, updated_at = ?, password_changed_at = ?
                WHERE username = ?
            ''', (new_hash.decode('utf-8'), now, now.isoformat(), username))

        # GB/T 35718: 密码修改后撤销该用户的所有活跃令牌
        self._blacklist_user_tokens(username, 'password_changed')

        self._log_operation(username, 'change_password', None, '修改密码成功')
        return {'success': True, 'message': '密码修改成功'}

    def force_change_password(self, username: str, new_password: str) -> dict[str, Any]:
        """
        强制修改密码（首次登录改密专用，不需要旧密码）

        Args:
            username: 用户名
            new_password: 新密码

        Returns:
            dict[str, Any]: 修改结果
        """
        valid, msg = self._validate_password_strength(new_password)
        if not valid:
            return {'success': False, 'message': msg}

        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT must_change_password FROM users WHERE username = ?', (username,))
            user = cursor.fetchone()

        if not user:
            return {'success': False, 'message': '用户不存在'}

        new_hash = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt())

        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE users SET password_hash = ?, must_change_password = 0, updated_at = ?
                WHERE username = ?
            ''', (new_hash.decode('utf-8'), datetime.now(), username))

        self._log_operation(username, 'force_change_password', None, '首次登录改密成功')
        logger.info(f"用户 {username} 首次登录改密成功")

        # 生成新token
        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
            row = cursor.fetchone()
            if not row:
                return {'success': False, 'message': '用户不存在或已被删除'}
            user = dict(row)

        token = self._generate_token(user)
        refresh_token = self._generate_refresh_token(user)

        return {
            'success': True,
            'message': '密码修改成功',
            'token': token,
            'refresh_token': refresh_token,
            'user': {
                'username': user['username'],
                'role': user['role'],
                'display_name': user['display_name'],
                'permissions': ROLES.get(user['role'], {}).get('permissions', [])
            }
        }

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
        allowed_fields = {'role', 'display_name', 'email', 'phone', 'is_active'}
        # Only keep allowed fields and validate they are safe identifiers
        safe_updates = {k: v for k, v in kwargs.items()
                        if k in allowed_fields and k.isidentifier() and v is not None}
        if not safe_updates:
            return {'success': False, 'message': '没有可更新的字段'}
        updates = safe_updates

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
            'jti': str(uuid.uuid4()),
            'iat': datetime.utcnow(),
            'exp': datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS)
        }
        return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

    def _generate_refresh_token(self, user: dict[str, Any]) -> str:
        """生成JWT刷新令牌"""
        payload = {
            'username': user['username'],
            'type': 'refresh',
            'jti': str(uuid.uuid4()),
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
    角色权限装饰器（组合jwt_required）
    要求用户具有指定角色之一

    Usage:
        @role_required('admin', 'engineer')
        def admin_only():
            ...
    """
    def decorator(f):
        # 先应用jwt_required进行认证
        @jwt_required
        @wraps(f)
        def decorated(*args, **kwargs):
            user = request.current_user
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
    权限装饰器（组合jwt_required）
    要求用户具有指定权限

    Usage:
        @permission_required('manage_users')
        def manage_users():
            ...
    """
    def decorator(f):
        # 先应用jwt_required进行认证
        @jwt_required
        @wraps(f)
        def decorated(*args, **kwargs):
            user = request.current_user
            if permission not in user.get('permissions', []):
                return jsonify({
                    'error': '权限不足',
                    'required_permission': permission
                }), 403

            return f(*args, **kwargs)
        return decorated
    return decorator
