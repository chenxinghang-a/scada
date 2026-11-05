"""
用户认证与权限管理模块
实现JWT令牌认证、角色权限控制
"""

import jwt
import uuid
import hashlib
import bcrypt
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any
from flask import request, jsonify, current_app

logger = logging.getLogger(__name__)


def _is_inactive_value(value: Any) -> bool:
    """把 ``is_active`` 的各种「假值」写法统一判定出来。

    HTTP JSON 里既可能是 ``0`` / ``false``，也可能是字符串 ``"0"`` / ``"false"``，
    只写 `not value` 会漏掉后者（`not "0"` 为 False）。
    """
    if isinstance(value, str):
        return value.strip().lower() in ('0', 'false', 'no', 'off', '')
    return not bool(value)


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
                    created_at DATETIME DEFAULT (datetime('now','localtime')),
                    updated_at DATETIME DEFAULT (datetime('now','localtime')),
                    is_active BOOLEAN DEFAULT 1,
                    must_change_password BOOLEAN DEFAULT 0
                )
            ''')

            # 为已有表添加 must_change_password 列（兼容升级）
            try:
                cursor.execute('ALTER TABLE users ADD COLUMN must_change_password BOOLEAN DEFAULT 0')
            except Exception as e:
                # 列已存在时 ALTER 必然失败，属预期内且无副作用
                logger.debug("users.must_change_password 列已存在或无需升级: %s", e)

            # 为已有表添加 password_changed_at 列（GB/T 35718: 密码变更时间戳）
            try:
                cursor.execute('ALTER TABLE users ADD COLUMN password_changed_at DATETIME')
            except Exception as e:
                # 列已存在时 ALTER 必然失败，属预期内且无副作用
                logger.debug("users.password_changed_at 列已存在或无需升级: %s", e)

            # 创建操作日志表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS operation_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT,
                    detail TEXT,
                    ip_address TEXT,
                    timestamp DATETIME DEFAULT (datetime('now','localtime'))
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
                    blacklisted_at DATETIME DEFAULT (datetime('now','localtime')),
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
                # round 369（BUG-33）：**显式写本地时间**。
                # 原来吃 DDL 的 `DEFAULT CURRENT_TIMESTAMP`（UTC），而全库其它时间
                # 字段都是 `datetime.now()`（本地）—— 同一行里 created_at 与
                # updated_at/login 差一个时区，按天统计会把凌晨的数据算到前一天。
                _now = datetime.now()
                cursor.execute('''
                    INSERT INTO users (username, password_hash, role, display_name,
                                       must_change_password, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                ''', ('admin', password_hash.decode('utf-8'), 'admin', '系统管理员',
                      _now, _now.isoformat(sep=' ')))
                logger.info("已创建默认管理员账户 (密码通过 SCADA_ADMIN_PASSWORD 环境变量配置)")

    def _validate_password_strength(self, password: str) -> tuple[bool, str]:
        """验证密码强度 - 等保2.0要求

        长度下限取 ``AuthConfig.MIN_PASSWORD_LENGTH``（环境变量
        ``MIN_PASSWORD_LENGTH``），但**不低于 8**。

        为什么要有 8 这个地板：这个旋钮此前是硬编码 8、配置项 `MIN_PASSWORD_LENGTH=6`
        全库零读取 —— 运维把它调成 6 以为放宽了（不生效），调成 12 以为收紧了
        （也不生效）。现在配置真的生效，但**只能收紧不能放宽**：等保2.0 要求
        长度≥8，放开这个地板等于留一个「配一下就绕过合规」的后门。
        """
        min_len = max(8, int(AuthConfig.MIN_PASSWORD_LENGTH or 8))
        if len(password) < min_len:
            return False, f"密码长度至少{min_len}位"
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
                remaining = int((locked_until - datetime.now()).total_seconds()) // 60
                return {'success': False, 'message': f'账户已锁定，请{remaining}分钟后重试'}

            # round 370（BUG-039）：**锁定期满必须归零计数**。
            # 原来只放行、不清 `login_attempts`（仍是 5），用户哪怕只输错一次
            # 也会 5+1>=5 立刻再锁 30 分钟 —— 表现是"锁一次就再也登不进去"，
            # 只能改库或等运维手工清零。
            user['login_attempts'] = 0
            try:
                with self.database.get_connection() as conn:
                    conn.execute(
                        'UPDATE users SET login_attempts = 0, locked_until = NULL '
                        'WHERE username = ?', (username,))
                logger.info("账户 %s 锁定期已满，失败计数已归零", username)
            except Exception as e:      # noqa: BLE001 —— 归零失败不该阻断登录流程
                logger.warning("重置账户 %s 的登录失败计数失败: %s", username, e)

        # 验证密码
        if not bcrypt.checkpw(password.encode('utf-8'), user['password_hash'].encode('utf-8')):
            # 增加失败次数
            attempts = (user['login_attempts'] or 0) + 1
            locked_until = None
            if attempts >= AuthConfig.MAX_LOGIN_ATTEMPTS:
                locked_until = (datetime.now() + timedelta(minutes=AuthConfig.LOCKOUT_MINUTES)).isoformat(sep=' ')

            with self.database.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE users SET login_attempts = ?, locked_until = ?
                    WHERE username = ?
                ''', (attempts, locked_until, username))

            self._log_operation(username, 'login_failed', None, 
                              f'密码错误 (尝试{attempts}次)', ip_address)

            if attempts >= AuthConfig.MAX_LOGIN_ATTEMPTS:
                return {'success': False, 'message': f'密码错误次数过多，账户已锁定{AuthConfig.LOCKOUT_MINUTES}分钟'}
            return {'success': False, 'message': f'用户名或密码错误 (还剩{AuthConfig.MAX_LOGIN_ATTEMPTS-attempts}次机会)'}

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

            # 只接受 access 令牌。refresh 令牌生命周期更长（见 create_refresh_token），
            # 若被当作 access 令牌接受，等于绕过短时效限制，必须显式拒绝。
            token_type = payload.get('type')
            if token_type != 'access':
                logger.warning("拒绝非 access 类型令牌: type=%s", token_type)
                return None

            # 检查令牌是否在黑名单中 (GB/T 35718)
            # round 255：没有 jti 的令牌也要查 —— 用同样的哈希键规则，
            # 否则「撤销了但校验时不查」= 令牌仍然可用（假撤销）。
            bl_key = self._token_blacklist_key(token, payload)
            with self.database.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT 1 FROM jwt_blacklist WHERE token_jti = ?', (bl_key,))
                if cursor.fetchone():
                    logger.warning(f"JWT令牌已被撤销: key={bl_key}")
                    return None

            # 检查用户是否仍然活跃
            with self.database.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT username, role, display_name, is_active,
                           must_change_password, password_changed_at
                    FROM users WHERE username = ?
                ''', (payload.get('username'),))
                user = cursor.fetchone()

            if not user or not user['is_active']:
                return None

            # round 335：**改密后必须让旧 access 令牌失效**。
            #
            # `refresh_token` 早就校验了这个（见其 572-594 行），但 `verify_token`
            # **完全没有** —— 而 access 令牌才是每个请求真正带的那一个。
            # 于是「改密 + 全量撤销」之后：黑名单里只有**当前请求头那一个**令牌，
            # 被盗的另一个 access 令牌仍然能通过 verify_token，
            # 继续调 `/api/control/*` 下发控制指令，直到自然过期。
            #
            # 判据：令牌的 `iat` 早于 `password_changed_at` → 该令牌是改密前签发的。
            token_iat = payload.get('iat', 0)
            password_changed = user['password_changed_at']
            if password_changed:
                try:
                    pwd_time = datetime.fromisoformat(password_changed).timestamp()
                except (ValueError, TypeError) as e:
                    # fail-closed：解析不出「密码变更时间」时不能假定密码没改过，
                    # 那等于让已改密用户的旧令牌继续可用。
                    # 不把原始值写进日志（字段名含 password，容易被误当凭据收集）。
                    logger.error(
                        "password_changed_at 非法，无法确认密码是否已变更，拒绝令牌 "
                        "user=%s: %s", user['username'], e)
                    return None

                # ⚠️ 容差 1 秒是必须的：JWT 的 `iat` 是**整数秒**（RFC 7519），
                # 而 `password_changed_at` 是 `datetime.now().isoformat()`，带**微秒**。
                # 「改密后立刻登录」时两者在同一秒内，`pwd_time` 会比 `iat` 大
                # 零点几秒 —— 不留容差的话，**刚签发的合法令牌会被自己的规则杀掉**
                # （实测：smoke 测试里 admin 强制改密后立即登录 → 401）。
                if pwd_time > token_iat + 1:
                    logger.warning(
                        "令牌签发于密码变更之前，已失效: user=%s", user['username'])
                    return None

            return {
                'username': user['username'],
                'role': user['role'],
                'display_name': user['display_name'],
                'permissions': ROLES.get(user['role'], {}).get('permissions', []),
                # 供 jwt_required 判断是否只放行改密接口
                'must_change_password': bool(user['must_change_password'])
            }

        except jwt.ExpiredSignatureError:
            logger.warning("JWT令牌已过期")
            return None
        except jwt.InvalidTokenError as e:
            logger.warning(f"无效JWT令牌: {e}")
            return None

    @staticmethod
    def _token_blacklist_key(token: str, payload: dict | None = None) -> str | None:
        """黑名单键：优先用 jti，没有 jti 时退化为 token 的 SHA-256。

        round 255：原来 `blacklist_token` 在没有 `jti` 时**直接返回 False** ——
        而 `POST /api/auth/logout` 拿到 False 会回 500「登出失败，请重试」。
        对**没有 jti 的令牌**（历史版本签发的、第三方集成手签的）来说，
        重试一万次也不会成功：这个令牌**天生不可撤销**，用户永远登不出去。

        用哈希兜底后，**签名有效**的令牌都能被撤销：
        - 有 jti → 用 jti（不变，兼容已有黑名单数据）；
        - 无 jti → `sha256:<hex>`（前缀避免与 jti 撞车）。
        校验侧（`verify_token`）用同样的规则查，两侧必须一致。

        **但签名校验失败的字符串返回 `None`**（不是哈希）：
        它根本不可能是本系统签发过的会话令牌，撤销它没有意义 ——
        且 `blacklist_token` 的既有契约是「垃圾串返回 False」。
        过期令牌仍按签名验证通过处理（`verify_exp=False`）：登出时
        令牌刚过期是最常见的情况，不能因此让用户登不出去。

        Args:
            token: 原始令牌字符串。
            payload: 已解码的载荷（调用方已解过就传进来，省一次签名验算；
                `verify_token` 每请求都会走这条路）。
        """
        if payload is None:
            try:
                payload = jwt.decode(
                    token, JWT_SECRET, algorithms=[JWT_ALGORITHM],
                    options={'verify_exp': False},
                )
            except jwt.InvalidTokenError:
                return None
        jti = payload.get('jti')
        if jti:
            return str(jti)
        return 'sha256:' + hashlib.sha256(token.encode('utf-8')).hexdigest()

    def blacklist_token(self, token: str, reason: str = 'logout') -> bool:
        """
        将JWT令牌加入黑名单 (GB/T 35718: 令牌撤销)

        Args:
            token: JWT令牌字符串
            reason: 黑名单原因

        Returns:
            bool: 是否成功。签名有效的令牌（含已过期）恒可撤销；
                  连签名都不合法的字符串返回 False（不是本系统的令牌）。
        """
        try:
            key = self._token_blacklist_key(token)
            if key is None:
                logger.warning("拒绝撤销非法令牌：签名/格式校验失败")
                return False

            with self.database.get_connection() as conn:
                cursor = conn.cursor()
                # round 369（BUG-33）：显式写本地时间，与 cleanup_expired_blacklist
                # 的 `datetime.now() - 7d` 同口径。原来靠 DDL 的 UTC 默认值，
                # 东八区下黑名单记录一开始就"老了 8 小时"，7 天保留期实际只有 6 天 16 小时。
                cursor.execute(
                    'INSERT OR IGNORE INTO jwt_blacklist '
                    '(token_jti, reason, blacklisted_at) VALUES (?, ?, ?)',
                    (key, reason, datetime.now().isoformat(sep=' '))
                )
            logger.info(f"JWT令牌已加入黑名单: key={key}, reason={reason}")
            return True
        except Exception as e:
            logger.error(f"令牌黑名单操作失败: {e}")
            return False

    def cleanup_expired_blacklist(self):
        """清理过期的黑名单条目（7天前的记录）"""
        try:
            cutoff = (datetime.now() - timedelta(days=7)).isoformat(sep=' ')
            with self.database.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM jwt_blacklist WHERE blacklisted_at < ?', (cutoff,))
                deleted = cursor.rowcount
            if deleted > 0:
                logger.info(f"清理了 {deleted} 条过期黑名单记录")
        except Exception as e:
            logger.error(f"清理黑名单失败: {e}")

    def _blacklist_user_tokens(self, username: str, reason: str = 'user_action') -> bool:
        """
        将用户当前请求的令牌加入黑名单。
        注意：无法枚举所有活跃令牌，仅标记当前操作的令牌；
        依赖 verify_token 中的黑名单检查来拒绝已撤销的令牌。

        Returns:
            bool: True  = 已撤销（或当前不在 HTTP 上下文 / 未携带令牌，本就无令牌可撤销）；
                  False = **取到了令牌但落库失败**，令牌实际仍然有效。
        """
        # 第一步：取当前请求令牌。非 HTTP 上下文（定时任务 / 脚本 / 测试）属正常情况。
        token = ''
        try:
            from flask import request as flask_request
            auth_header = flask_request.headers.get('Authorization', '')
            if auth_header.startswith('Bearer '):
                token = auth_header[7:]
            # round 369：**强制改密允许从 JSON body 传 token**
            # （展示层/api/api_auth.py 的强制改密支持 data['token']），
            # 但这里原来只读 header ——
            # 通过 body 令牌改密时旧令牌根本没进黑名单，改密后依然可用。
            if not token:
                body = flask_request.get_json(silent=True) or {}
                candidate = body.get('token') or body.get('refresh_token') or ''
                if isinstance(candidate, str):
                    token = candidate
        except Exception as e:
            logger.debug("非 HTTP 上下文，无当前请求令牌可撤销 user=%s: %s", username, e)
            return True

        if not token:
            logger.debug("当前请求未携带令牌，无可撤销 user=%s", username)
            return True

        # 第二步：落库。**刻意不包在同一个 try 里** —— 落库失败必须能被调用方感知，
        # 否则撤销"看起来成功了"，而已撤销令牌仍可继续使用（静默放行）。
        if not self.blacklist_token(token, reason):
            logger.error(
                "撤销令牌失败：令牌未被加入黑名单，仍可继续使用 user=%s reason=%s",
                username, reason)
            return False
        return True

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
            #
            # round 369：**与 verify_token 同口径**。原来这里只写 `if jti:`，
            # 而无 jti 的令牌在 verify_token 里是用 SHA-256 兜底撤销的 ——
            # 于是在 refresh_token 里"撤销了"的无 jti 令牌仍然可以续期，
            # 登出等于没登出。
            bl_key = self._token_blacklist_key(refresh_token, payload)
            if bl_key:
                with self.database.get_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute('SELECT 1 FROM jwt_blacklist WHERE token_jti = ?', (bl_key,))
                    if cursor.fetchone():
                        logger.warning(f"刷新令牌已被撤销: key={bl_key}")
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
                except (ValueError, TypeError) as e:
                    # fail-closed：解析不出"密码变更时间"时，**不能假定密码没改过** ——
                    # 那等于让已改密用户的旧刷新令牌继续续期（静默放行）。
                    # 拒绝续期，并要求人工核查该用户的 password_changed_at 数据。
                    # 不把 password_changed_at 的原始值写进日志：
                    # 该字段名含 password，日志里出现它既容易被误当凭据收集，
                    # 也会触发 CodeQL py/clear-text-logging-sensitive-data（告警 #50）。
                    # 排障所需的信息由异常 e 提供（是格式错、还是类型错）。
                    logger.error(
                        "password_changed_at 非法，无法确认密码是否已变更，拒绝刷新 "
                        "user=%s: %s", user.get('username'), e)
                    return None

                # 容差 1 秒：`iat` 是整数秒、`password_changed_at` 带微秒，
                # 「改密后立刻刷新」时 pwd_time 会略大于 iat（同 round 335 的说明）。
                if pwd_time > token_iat + 1:
                    logger.warning(f"密码已变更，刷新令牌失效: user={user['username']}")
                    return None

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

        # round 369：**新旧密码不能相同**。
        # 只校验强度不校验"是否等于原口令"时，"改密"可以原地踏步 —— 用户
        # 把旧口令再输一遍就算完成整改，等保要求的定期换密变成形式。
        if bcrypt.checkpw(new_password.encode('utf-8'), user['password_hash'].encode('utf-8')):
            return {'success': False, 'message': '新密码不能与当前密码相同'}

        new_hash = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt())

        now = datetime.now()
        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            # round 299：**必须同时清 `must_change_password`**。
            # 默认 admin 建号时该位为 1；首次登录按提示走这条"常规改密"路径，
            # 改完返回「密码修改成功」，但状态位仍是 1 →
            # 重新登录又要求改密 → `jwt_required` 把除白名单外的一切请求打成 403
            # → 改密死循环（而 `force_change_password` 的错误文案恰恰把人指向这里）。
            # 两条改密路径对同一个状态位的写入必须等价。
            cursor.execute('''
                UPDATE users SET password_hash = ?, updated_at = ?,
                                 password_changed_at = ?, must_change_password = 0
                WHERE username = ?
            ''', (new_hash.decode('utf-8'), now, now.isoformat(sep=' '), username))

        # GB/T 35718: 密码修改后撤销该用户的所有活跃令牌
        revoked = self._blacklist_user_tokens(username, 'password_changed')
        if not revoked:
            logger.error("密码已修改但旧令牌撤销失败，旧令牌仍可继续使用 user=%s", username)

        self._log_operation(username, 'change_password', None, '修改密码成功')
        result = {'success': True, 'message': '密码修改成功'}
        if not revoked:
            result['warning'] = '旧令牌撤销失败，旧令牌可能仍然有效，建议立即重新登录'
        return result

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
            cursor.execute(
                'SELECT must_change_password, password_hash FROM users WHERE username = ?',
                (username,))
            user = cursor.fetchone()

        if not user:
            return {'success': False, 'message': '用户不存在'}

        # 该接口专用于「首次登录 / 密码被管理员重置后」的强制改密场景，不校验旧密码。
        # 因此必须确认账号确实处于强制改密状态，否则等于允许仅凭一个令牌就静默改密，
        # 绕过了 change_password 的旧密码校验。非强制改密状态一律打回常规接口。
        if not user['must_change_password']:
            logger.warning("用户 %s 非强制改密状态，拒绝 force_change_password", username)
            return {
                'success': False,
                'message': '当前账号无需强制改密，请使用常规改密接口（需验证原密码）'
            }

        # round 369：**新旧密码不能相同**。
        # 初始管理员口令通常已满足复杂度要求，原来只要把原口令再输一遍就能
        # 把 must_change_password 清零 —— 强制改密完全失效。
        stored_hash = user['password_hash']
        if stored_hash and bcrypt.checkpw(new_password.encode('utf-8'),
                                          str(stored_hash).encode('utf-8')):
            return {'success': False, 'message': '新密码不能与当前密码相同'}

        new_hash = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt())

        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            # round 299：**必须写 `password_changed_at`**。
            # `refresh_token()` 用它判定"令牌是否早于最后一次改密"，
            # 这里不写 → 该字段恒为 NULL → 旧 refresh token 仍可通过校验，
            # 攻击者能持续续期 `JWT_REFRESH_DAYS` 天。
            _now = datetime.now()
            cursor.execute('''
                UPDATE users SET password_hash = ?, must_change_password = 0,
                                 updated_at = ?, password_changed_at = ?
                WHERE username = ?
            ''', (new_hash.decode('utf-8'), _now, _now.isoformat(sep=' '), username))

        # 强制改密的目的就是"把可能失陷的会话踢掉"，不撤旧令牌等于没改。
        _revoked = self._blacklist_user_tokens(username, 'force_password_changed')
        if not _revoked:
            logger.error("强制改密后旧令牌撤销失败，旧令牌仍可继续使用 user=%s", username)

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

        # round 369：**内置 admin 不能被禁用或降权**。
        #
        # `delete_user()` 早就拦了 `username == 'admin'`，但 update_user 没有：
        # `PUT /api/auth/users/admin {"is_active": 0}` 或 `{"role": "viewer"}`
        # 会把唯一的内置管理员直接废掉，系统永久失去管理权限（只能改库恢复）。
        if username == 'admin':
            if 'is_active' in updates and _is_inactive_value(updates['is_active']):
                return {'success': False,
                        'message': '不能禁用内置管理员账户 admin'}
            if 'role' in updates and updates['role'] != 'admin':
                return {'success': False,
                        'message': '不能修改内置管理员账户 admin 的角色'}

        # round 369：**set 子句只从闭集白名单里取列名**，不再用 `updates` 的 key。
        # 现在的 key 已由 `allowed_fields` + `isidentifier()` 过滤，但那是 4 行之前的
        # 约定；一旦将来有人放宽过滤，`f'{k} = ?'` 就是直接拼 SQL。
        # 从字面量元组取列名，注入在结构上不可能发生，也顺带保证占位符与值同序。
        columns = [c for c in ('role', 'display_name', 'email', 'phone', 'is_active')
                   if c in updates]
        set_clause = ', '.join(f'{c} = ?' for c in columns)
        values = [updates[c] for c in columns] + [datetime.now(), username]

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
            'iat': datetime.now(timezone.utc),
            'exp': datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION_HOURS)
        }
        return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

    def _generate_refresh_token(self, user: dict[str, Any]) -> str:
        """生成JWT刷新令牌"""
        payload = {
            'username': user['username'],
            'type': 'refresh',
            'jti': str(uuid.uuid4()),
            'iat': datetime.now(timezone.utc),
            'exp': datetime.now(timezone.utc) + timedelta(days=JWT_REFRESH_DAYS)
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
                # round 369（BUG-33）：显式写本地时间，不再依赖 DDL 的 UTC 默认值
                cursor.execute('''
                    INSERT INTO operation_logs
                        (username, action, target, detail, ip_address, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (username, action, target, detail, ip_address,
                      datetime.now().isoformat(sep=' ')))
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

        # round 340：**不再接受 `?token=`**。
        #
        # 原实现从这里兜底取查询参数，注释理由是「用于 WebSocket 等场景」——
        # 但 WebSocket 握手在 `展示层/websocket.py::handle_connect`，
        # 它自己读 `request.args`，**根本不经过本装饰器**。
        # 也就是说这条兜底对 WS 毫无用处，代价却是让令牌进入 URL：
        # nginx access log、浏览器历史、Referer 都会留下**长期有效**的 access token。
        # 凭证只从 Header 进入（页面导航另有 httpOnly cookie，见 routes.py）。
        # 已确认无调用方依赖：仓库内 `?token=` 仅出现在 main.js 的 WebSocket
        # `io(..., {query: {token}})`，走的是 socket.io 自己的握手参数。

        if not token:
            return jsonify({'error': '未提供认证令牌', 'code': 'NO_TOKEN'}), 401

        auth_manager = current_app.auth_manager
        user = auth_manager.verify_token(token)

        if not user:
            return jsonify({'error': '令牌无效或已过期', 'code': 'INVALID_TOKEN'}), 401

        # 将用户信息附加到请求上下文
        request.current_user = user

        # 强制改密闸门（GB/T 22239 身份鉴别）：首次登录/密码被重置后，
        # 在完成改密前只放行改密、登出和令牌自检接口，其余一律拒绝。
        if user.get('must_change_password'):
            allowed_paths = (
                '/api/auth/force-change-password',
                '/api/auth/change-password',
                '/api/auth/logout',
                '/api/auth/verify',
            )
            if request.path not in allowed_paths:
                logger.warning(
                    "用户 %s 未完成强制改密，拒绝访问 %s",
                    user.get('username'), request.path
                )
                return jsonify({
                    'error': '首次登录必须先修改密码',
                    'code': 'MUST_CHANGE_PASSWORD'
                }), 403

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
