"""
Security Edge Case Tests
=========================
Covers attack vectors and security-critical paths:
- Blacklisted JWT token rejection
- Refresh token blacklist check
- permission_required enforces authentication
- Rate limiting on login endpoint
- /api/config does not expose secrets
"""

import pytest
import time
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta, timezone

import jwt
from config import AuthConfig


# ============================================================
# JWT Blacklist Tests
# ============================================================

class TestJWTBlacklist:
    """Blacklisted tokens must be rejected even if not expired."""

    def test_blacklisted_access_token_rejected(self, auth_manager):
        """After logout (blacklist), the access token is no longer valid."""
        auth_manager.register('bluser', 'Abcdef12', role='viewer')
        result = auth_manager.login('bluser', 'Abcdef12')
        assert result['success'] is True
        token = result['token']

        # Verify token works before blacklist
        user = auth_manager.verify_token(token)
        assert user is not None
        assert user['username'] == 'bluser'

        # Blacklist the token
        success = auth_manager.blacklist_token(token, 'logout')
        assert success is True

        # Token should now be rejected
        user = auth_manager.verify_token(token)
        assert user is None

    def test_blacklisted_refresh_token_rejected(self, auth_manager):
        """After blacklisting, refresh token cannot be used to get new access token."""
        auth_manager.register('blref', 'Abcdef12', role='viewer')
        result = auth_manager.login('blref', 'Abcdef12')
        refresh = result['refresh_token']

        # Refresh works before blacklist
        new_result = auth_manager.refresh_token(refresh)
        assert new_result is not None
        assert new_result['success'] is True

        # Re-login to get a fresh refresh token, then blacklist it
        result2 = auth_manager.login('blref', 'Abcdef12')
        refresh2 = result2['refresh_token']
        auth_manager.blacklist_token(refresh2, 'logout')

        # Refresh should fail after blacklist
        new_result2 = auth_manager.refresh_token(refresh2)
        assert new_result2 is None

    def test_blacklist_nonexistent_token_returns_false(self, auth_manager):
        """Blacklisting a garbage token returns False."""
        result = auth_manager.blacklist_token('not.a.valid.jwt')
        assert result is False

    def test_cleanup_expired_blacklist(self, auth_manager):
        """cleanup_expired_blacklist removes entries older than 7 days."""
        auth_manager.register('cleanuser', 'Abcdef12', role='viewer')
        result = auth_manager.login('cleanuser', 'Abcdef12')
        token = result['token']

        # Blacklist the token
        auth_manager.blacklist_token(token, 'test')

        # Manually backdate the blacklist entry
        old_time = (datetime.now() - timedelta(days=8)).isoformat()
        with auth_manager.database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'UPDATE jwt_blacklist SET blacklisted_at = ?',
                (old_time,)
            )

        # Cleanup should remove it
        auth_manager.cleanup_expired_blacklist()

        # Verify the entry is gone
        payload = jwt.decode(token, AuthConfig.JWT_SECRET, algorithms=[AuthConfig.JWT_ALGORITHM])
        jti = payload.get('jti')
        with auth_manager.database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT 1 FROM jwt_blacklist WHERE token_jti = ?', (jti,))
            assert cursor.fetchone() is None


# ============================================================
# permission_required Decorator Tests
# ============================================================

class TestPermissionRequired:
    """permission_required must enforce authentication before checking permission."""

    def test_permission_required_rejects_unauthenticated(self, client):
        """An endpoint with @permission_required returns 401 without a token."""
        # /api/auth/users requires admin role (uses @role_required('admin'))
        resp = client.get('/api/auth/users')
        assert resp.status_code == 401

    def test_permission_required_rejects_wrong_permission(self, client, app):
        """User without required permission gets 403."""
        token = jwt.encode({
            'username': 'viewer_user', 'role': 'viewer', 'type': 'access',
            'jti': 'perm-test', 'iat': datetime.now(timezone.utc),
            'exp': datetime.now(timezone.utc) + timedelta(hours=1),
        }, AuthConfig.JWT_SECRET, algorithm=AuthConfig.JWT_ALGORITHM)

        app.auth_manager.verify_token.return_value = {
            'username': 'viewer_user', 'role': 'viewer',
            'display_name': 'Viewer', 'permissions': ['read']
        }

        # /api/auth/users requires admin (manage_users permission)
        resp = client.get('/api/auth/users',
                          headers={'Authorization': f'Bearer {token}'})
        assert resp.status_code == 403

    def test_role_required_rejects_wrong_role(self, client, app):
        """User with wrong role gets 403 from @role_required endpoint."""
        token = jwt.encode({
            'username': 'viewer_user', 'role': 'viewer', 'type': 'access',
            'jti': 'role-test', 'iat': datetime.now(timezone.utc),
            'exp': datetime.now(timezone.utc) + timedelta(hours=1),
        }, AuthConfig.JWT_SECRET, algorithm=AuthConfig.JWT_ALGORITHM)

        app.auth_manager.verify_token.return_value = {
            'username': 'viewer_user', 'role': 'viewer',
            'display_name': 'Viewer', 'permissions': ['read']
        }

        # /api/system/simulation-mode POST requires admin/engineer
        resp = client.post('/api/system/simulation-mode',
                           json={'simulation_mode': True},
                           headers={'Authorization': f'Bearer {token}'})
        assert resp.status_code == 403


# ============================================================
# Rate Limiting on Login
# ============================================================

class TestLoginRateLimiting:
    """Login endpoint must limit brute-force attempts."""

    def test_account_lockout_after_5_failures(self, auth_manager):
        """Account is locked after 5 consecutive failed login attempts."""
        auth_manager.register('ratelimit', 'Abcdef12', role='viewer')

        for i in range(4):
            result = auth_manager.login('ratelimit', 'WrongPass1')
            assert result['success'] is False
            # Should still have chances left (not locked yet for 1-4)
            if i < 4:
                assert '锁定' not in result.get('message', '') or i == 4

        # 5th failure triggers lockout
        result = auth_manager.login('ratelimit', 'WrongPass1')
        assert result['success'] is False
        assert '锁定' in result['message']

    def test_locked_account_rejects_correct_password(self, auth_manager):
        """Even the correct password is rejected while account is locked."""
        auth_manager.register('lockeduser', 'Abcdef12', role='viewer')

        # Trigger lockout
        for _ in range(5):
            auth_manager.login('lockeduser', 'WrongPass1')

        # Correct password should be rejected
        result = auth_manager.login('lockeduser', 'Abcdef12')
        assert result['success'] is False
        assert '锁定' in result['message']

    def test_login_resets_failure_counter(self, auth_manager):
        """Successful login resets the failure counter."""
        auth_manager.register('resetuser', 'Abcdef12', role='viewer')

        # Fail a couple times
        auth_manager.login('resetuser', 'Wrong1')
        auth_manager.login('resetuser', 'Wrong2')

        # Succeed
        result = auth_manager.login('resetuser', 'Abcdef12')
        assert result['success'] is True

        # Should be able to fail 5 more times before lockout
        for i in range(4):
            result = auth_manager.login('resetuser', 'Wrong1')
            assert result['success'] is False
            assert '锁定' not in result['message']

        result = auth_manager.login('resetuser', 'Wrong1')
        assert result['success'] is False
        assert '锁定' in result['message']


# ============================================================
# Config Endpoint Secret Protection
# ============================================================

class TestConfigSecretProtection:
    """/api/config must not expose sensitive fields."""

    def test_config_endpoint_requires_auth(self, client):
        """/api/config requires authentication."""
        resp = client.get('/api/config')
        assert resp.status_code == 401

    def test_config_does_not_expose_jwt_secret(self, client, app, auth_headers, tmp_path):
        """/api/config masks jwt_secret field."""
        # Create a temporary config file
        config_dir = tmp_path / '配置'
        config_dir.mkdir(exist_ok=True)
        config_file = config_dir / 'system.yaml'

        import yaml
        test_config = {
            'system': {
                'jwt_secret': 'super_secret_key_12345',
                'secret_key': 'another_secret',
                'password': 'db_password_here',
                'api_key': 'sk-1234567890',
                'token': 'bearer_token_xyz',
                'normal_setting': 'visible_value',
            }
        }
        with open(config_file, 'w', encoding='utf-8') as f:
            yaml.dump(test_config, f)

        # Patch load_yaml_config to return our test config
        with patch('展示层.api.api_system.load_yaml_config', return_value=test_config):
            resp = client.get('/api/config', headers=auth_headers)

        assert resp.status_code == 200
        data = resp.get_json()
        config = data.get('config', {})
        sys_config = config.get('system', {})

        # Sensitive fields must be masked
        assert sys_config.get('jwt_secret') == '***'
        assert sys_config.get('secret_key') == '***'
        assert sys_config.get('password') == '***'
        assert sys_config.get('api_key') == '***'
        assert sys_config.get('token') == '***'

        # Non-sensitive fields should be visible
        assert sys_config.get('normal_setting') == 'visible_value'

    def test_config_masking_case_insensitive(self, client, app, auth_headers):
        """/api/config masks secrets regardless of key case."""
        test_config = {
            'db': {
                'JWT_Secret': 'should_be_masked',
                'API_KEY': 'also_masked',
                'host': 'localhost',
            }
        }

        with patch('展示层.api.api_system.load_yaml_config', return_value=test_config):
            resp = client.get('/api/config', headers=auth_headers)

        assert resp.status_code == 200
        data = resp.get_json()
        db_config = data.get('config', {}).get('db', {})
        assert db_config.get('JWT_Secret') == '***'
        assert db_config.get('API_KEY') == '***'
        assert db_config.get('host') == 'localhost'


# ============================================================
# Token Tampering Tests
# ============================================================

class TestTokenTampering:
    """Tampered tokens must be rejected."""

    def test_tampered_token_rejected(self, auth_manager):
        """A token with modified payload is rejected."""
        auth_manager.register('tamper', 'Abcdef12', role='viewer')
        result = auth_manager.login('tamper', 'Abcdef12')
        token = result['token']

        # Tamper with the token by changing a character
        parts = token.split('.')
        if len(parts) == 3:
            # Modify the payload slightly
            tampered = parts[0] + '.' + parts[1][:-2] + 'XX' + '.' + parts[2]
            user = auth_manager.verify_token(tampered)
            assert user is None

    def test_expired_token_rejected(self, auth_manager):
        """An expired token is rejected."""
        payload = {
            'username': 'admin', 'role': 'admin', 'type': 'access',
            'jti': 'expired-test',
            'iat': datetime.now(timezone.utc) - timedelta(hours=48),
            'exp': datetime.now(timezone.utc) - timedelta(hours=24),
        }
        expired_token = jwt.encode(payload, AuthConfig.JWT_SECRET, algorithm=AuthConfig.JWT_ALGORITHM)
        assert auth_manager.verify_token(expired_token) is None

    def test_token_with_wrong_secret_rejected(self, auth_manager):
        """A token signed with wrong secret is rejected."""
        payload = {
            'username': 'admin', 'role': 'admin', 'type': 'access',
            'jti': 'wrong-secret',
            'iat': datetime.now(timezone.utc),
            'exp': datetime.now(timezone.utc) + timedelta(hours=1),
        }
        bad_token = jwt.encode(payload, 'wrong-secret-key', algorithm='HS256')
        assert auth_manager.verify_token(bad_token) is None

    def test_deactivated_user_token_rejected(self, auth_manager):
        """Token for a deactivated user is rejected."""
        auth_manager.register('deactuser', 'Abcdef12', role='viewer')
        result = auth_manager.login('deactuser', 'Abcdef12')
        token = result['token']

        # Verify works before deactivation
        assert auth_manager.verify_token(token) is not None

        # Deactivate the user
        auth_manager.delete_user('deactuser')

        # Token should now be rejected
        assert auth_manager.verify_token(token) is None
