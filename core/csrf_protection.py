"""
CSRF防护模块（round 321）

## ⚠️ 真实模型不是 Double Submit Cookie

原 docstring 写的是「Double Submit Cookie」，**实际不是** —— 没有 cookie 参与，
只有 `时间戳.HMAC(时间戳)` 一个签名串。这里按真实情况描述，并写清已知限制。

实际模型：**签名时间戳令牌（stateless signed timestamp）**

* `generate_token()` 产出 `"<unix秒>.<hmac_sha256(secret, unix秒)>"`；
* `_validate_token()` 校验签名 + **有效期 1 小时**。

## 已知限制（不能假装不存在）

1. **令牌不绑定用户/会话** —— 同一小时内的令牌对**任何用户**都有效。
   之所以现在还能接受：登录态 cookie 是 `SameSite=Strict`（见
   `展示层/api/api_auth.py`），跨站请求根本带不上 cookie。
   **一旦有人把 SameSite 放宽**（嵌 iframe / 桌面壳 / 跨域前端），
   这个模型就不够了 —— `tests/test_csrf_protection.py` 里有一条守卫
   专门盯住 `SameSite=Strict`，改它就会红。
2. `/api/csrf-token` **不需要登录**就能取（登录页也要用），
   配合"不绑定会话"意味着：攻击者可以自己取一个令牌再拿去做跨站提交。
   同上，靠 SameSite 兜住。

## 本轮修的两处（都是 fail-open 方向）

* **未来时间戳**：原来只判 `now - ts > 3600`，**没有下界** ——
  一个时间戳在未来的令牌**永不过期**（实测十年后仍通过）。
  服务器时钟被 NTP 往回校时后，此前签发的所有令牌都会变成"未来"，
  于是"1 小时有效期"形同虚设。改为允许 60 秒时钟偏差，超出即拒绝。
* **密钥缺失时 fail-open**：`if not self._secret: return True` ——
  CSRF 开着但密钥为空时，**任意字符串都能通过**。
  改为 fail-closed（拒绝 + 只记一次 ERROR）。

GB/T 37980 相关：本模块只覆盖 CSRF 一项，其余要求见 `docs/` 的合规说明。
"""
import hmac
import hashlib
import time
import secrets
import logging
from functools import wraps
from flask import request, jsonify, current_app
from core.safe_numeric import try_int

logger = logging.getLogger(__name__)


class CSRFProtection:
    """CSRF防护 - Double Submit Cookie模式"""

    def __init__(self, app=None):
        self._secret = None
        if app:
            self.init_app(app)

    def init_app(self, app):
        """初始化Flask应用"""
        from config import SecurityConfig
        self._secret = SecurityConfig.CSRF_SECRET.encode('utf-8') if SecurityConfig.CSRF_ENABLED else None

        if not SecurityConfig.CSRF_ENABLED:
            logger.info("CSRF防护已禁用")
            return

        # 注册before_request检查
        @app.before_request
        def check_csrf():
            # 只检查状态变更请求
            if request.method in ('POST', 'PUT', 'DELETE', 'PATCH'):
                # API请求检查CSRF token
                if request.path.startswith('/api/'):
                    # 登录/注册等公开端点豁免
                    exempt_paths = ['/api/auth/login', '/api/auth/register', '/api/auth/refresh']
                    if request.path in exempt_paths:
                        return

                    # JWT认证的API不需要CSRF（Bearer token本身就是防护）
                    auth_header = request.headers.get('Authorization', '')
                    if auth_header.startswith('Bearer '):
                        return

                    # 检查CSRF token
                    csrf_token = request.headers.get('X-CSRF-Token')
                    if not csrf_token:
                        return jsonify({'error': '缺少CSRF令牌'}), 403

                    # 验证token
                    if not self._validate_token(csrf_token):
                        return jsonify({'error': 'CSRF令牌无效'}), 403

    #: 允许的客户端/服务器时钟偏差（秒）。超出这个范围的**未来**时间戳一律拒绝。
    MAX_CLOCK_SKEW_S = 60

    def _validate_token(self, token: str) -> bool:
        """验证CSRF token（round 321：加未来时间戳检查 + 密钥缺失时 fail-closed）"""
        if not self._secret:
            # ⚠️ 原来是 `return True`（fail-open）：CSRF 开着但密钥为空时，
            # 任意字符串都能通过 —— 安全模块最不该出现的形状。
            # 密钥为空说明配置坏了，此时应当**拒绝所有状态变更请求**（fail-closed），
            # 而不是放行。日志只打一次，避免每个请求刷一行。
            if not getattr(self, '_secret_missing_logged', False):
                self._secret_missing_logged = True
                logger.error(
                    "CSRF 已启用但密钥为空 —— **所有带 CSRF 校验的请求都会被拒绝**。"
                    "请设置环境变量 CSRF_SECRET（或确认 .env 可读）")
            return False

        try:
            # token格式: timestamp.hmac
            parts = token.split('.', 1)
            if len(parts) != 2:
                return False

            timestamp_str, hmac_value = parts
            timestamp = try_int(timestamp_str)
            if timestamp is None:
                return False

            # 检查有效期（1 小时）—— **上下界都要判**。
            #
            # 只判上界（`now - ts > 3600`）时，`ts` 在未来的令牌永远满足条件：
            # 实测 `ts = now + 10 年` 的令牌**通过校验**。
            # 现实触发路径：服务器时钟被 NTP 往回校（或部署到时钟慢的机器），
            # 此前签发的令牌全部变成"未来"，于是"1 小时有效期"完全失效。
            now = time.time()
            age = now - timestamp
            if age > 3600:
                return False
            if age < -self.MAX_CLOCK_SKEW_S:
                logger.warning(
                    "CSRF 令牌时间戳在未来（偏差 %.0f 秒，超过允许的 %d 秒）—— "
                    "可能是服务器时钟被回拨，请检查 NTP", -age, self.MAX_CLOCK_SKEW_S)
                return False

            # 验证HMAC
            expected = hmac.new(
                self._secret,
                timestamp_str.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()

            return hmac.compare_digest(hmac_value, expected)
        except (ValueError, TypeError) as e:
            logger.debug(f"CSRF token验证异常: {e}")
            return False

    def generate_token(self) -> str:
        """生成CSRF token"""
        if not self._secret:
            return ''

        timestamp = str(int(time.time()))
        hmac_value = hmac.new(
            self._secret,
            timestamp.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return f"{timestamp}.{hmac_value}"


# 全局实例
csrf = CSRFProtection()
