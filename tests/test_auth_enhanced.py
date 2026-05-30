"""
Tests for 用户层.auth: password validation, account lockout, token refresh, role-based access
"""

import pytest
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
import jwt

from config import AuthConfig


@pytest.fixture
def auth(auth_manager):
    """Use the conftest auth_manager fixture which provides a real AuthManager with temp DB"""
    return auth_manager


# ============================================================
# Password Validation Tests
# ============================================================

class TestPasswordValidation:

    def test_password_too_short(self, auth):
        """Password shorter than 8 chars is rejected"""
        valid, msg = auth._validate_password_strength('Ab1')
        assert valid is False
        assert '8' in msg

    def test_password_no_uppercase(self, auth):
        """Password without uppercase is rejected"""
        valid, msg = auth._validate_password_strength('abcdef12')
        assert valid is False
        assert '大写' in msg

    def test_password_no_lowercase(self, auth):
        """Password without lowercase is rejected"""
        valid, msg = auth._validate_password_strength('ABCDEFG1')
        assert valid is False
        assert '小写' in msg

    def test_password_no_digit(self, auth):
        """Password without digit is rejected"""
        valid, msg = auth._validate_password_strength('Abcdefgh')
        assert valid is False
        assert '数字' in msg

    def test_valid_password(self, auth):
        """Password meeting all criteria is accepted"""
        valid, msg = auth._validate_password_strength('Abcdef12')
        assert valid is True
        assert msg == ''

    def test_strong_password(self, auth):
        """Complex password is accepted"""
        valid, _ = auth._validate_password_strength('MyStr0ng!Pass')
        assert valid is True

    def test_register_rejects_weak_password(self, auth):
        """register rejects password that doesn't meet complexity requirements"""
        result = auth.register('testuser', 'weak', role='viewer')
        assert result['success'] is False
        assert '密码' in result['message']

    def test_register_accepts_strong_password(self, auth):
        """register accepts password meeting complexity requirements"""
        result = auth.register('stronguser', 'Abcdef12', role='viewer')
        assert result['success'] is True


# ============================================================
# Registration Tests
# ============================================================

class TestRegistration:

    def test_register_duplicate_username(self, auth):
        """register rejects duplicate username"""
        auth.register('dupuser', 'Abcdef12', role='viewer')
        result = auth.register('dupuser', 'Abcdef12', role='operator')
        assert result['success'] is False
        assert '已存在' in result['message']

    def test_register_invalid_role(self, auth):
        """register rejects invalid role"""
        result = auth.register('newuser', 'Abcdef12', role='superadmin')
        assert result['success'] is False
        assert '无效角色' in result['message']

    def test_register_username_too_short(self, auth):
        """register rejects username shorter than 3 chars"""
        result = auth.register('ab', 'Abcdef12', role='viewer')
        assert result['success'] is False
        assert '3' in result['message']

    def test_register_username_too_long(self, auth):
        """register rejects username longer than 20 chars"""
        result = auth.register('a' * 21, 'Abcdef12', role='viewer')
        assert result['success'] is False
        assert '20' in result['message']

    def test_register_valid_user(self, auth):
        """register creates user successfully"""
        result = auth.register('validuser', 'Abcdef12', role='engineer', display_name='Engineer')
        assert result['success'] is True

    def test_register_all_roles(self, auth):
        """register accepts all valid roles"""
        for role in ('admin', 'engineer', 'operator', 'viewer'):
            result = auth.register(f'user_{role}', 'Abcdef12', role=role)
            assert result['success'] is True, f"Failed to register with role {role}"


# ============================================================
# Login & Account Lockout Tests
# ============================================================

class TestLoginAndLockout:

    def test_login_success(self, auth):
        """Successful login returns token"""
        auth.register('loginuser', 'Abcdef12', role='operator')
        result = auth.login('loginuser', 'Abcdef12')
        assert result['success'] is True
        assert 'token' in result

    def test_login_wrong_password(self, auth):
        """Wrong password returns failure"""
        auth.register('wrongpw', 'Abcdef12', role='viewer')
        result = auth.login('wrongpw', 'WrongPass1')
        assert result['success'] is False
        assert '错误' in result['message']

    def test_login_nonexistent_user(self, auth):
        """Nonexistent user returns generic error"""
        result = auth.login('ghost', 'Abcdef12')
        assert result['success'] is False

    def test_account_lockout_after_5_failures(self, auth):
        """Account locks after 5 consecutive failed login attempts"""
        auth.register('lockuser', 'Abcdef12', role='viewer')
        for i in range(4):
            result = auth.login('lockuser', 'Wrong123')
            assert result['success'] is False
            assert '机会' in result['message']  # still has chances

        # 5th attempt triggers lockout
        result = auth.login('lockuser', 'Wrong123')
        assert result['success'] is False
        assert '锁定' in result['message']

    def test_locked_account_rejects_even_correct_password(self, auth):
        """Locked account rejects correct password"""
        auth.register('locked', 'Abcdef12', role='viewer')
        for _ in range(5):
            auth.login('locked', 'Wrong123')

        result = auth.login('locked', 'Abcdef12')
        assert result['success'] is False
        assert '锁定' in result['message']

    def test_login_resets_attempts_on_success(self, auth):
        """Successful login resets failure counter"""
        auth.register('resetuser', 'Abcdef12', role='viewer')
        # Fail a few times
        auth.login('resetuser', 'Wrong123')
        auth.login('resetuser', 'Wrong123')
        # Then succeed
        result = auth.login('resetuser', 'Abcdef12')
        assert result['success'] is True


# ============================================================
# Token Tests
# ============================================================

class TestTokenGeneration:

    def test_token_contains_user_info(self, auth):
        """Generated token contains correct user info"""
        auth.register('tokenuser', 'Abcdef12', role='engineer')
        result = auth.login('tokenuser', 'Abcdef12')
        token = result['token']
        payload = jwt.decode(token, AuthConfig.JWT_SECRET, algorithms=[AuthConfig.JWT_ALGORITHM])
        assert payload['username'] == 'tokenuser'
        assert payload['role'] == 'engineer'
        assert payload['type'] == 'access'

    def test_refresh_token_type(self, auth):
        """Refresh token has type=refresh"""
        auth.register('refuser', 'Abcdef12', role='viewer')
        result = auth.login('refuser', 'Abcdef12')
        refresh = result['refresh_token']
        payload = jwt.decode(refresh, AuthConfig.JWT_SECRET, algorithms=[AuthConfig.JWT_ALGORITHM])
        assert payload['type'] == 'refresh'

    def test_verify_token_valid(self, auth):
        """verify_token returns user info for valid token"""
        auth.register('verifyuser', 'Abcdef12', role='operator')
        result = auth.login('verifyuser', 'Abcdef12')
        user = auth.verify_token(result['token'])
        assert user is not None
        assert user['username'] == 'verifyuser'
        assert user['role'] == 'operator'

    def test_verify_token_invalid(self, auth):
        """verify_token returns None for invalid token"""
        assert auth.verify_token('invalid.token.here') is None

    def test_verify_token_expired(self, auth):
        """verify_token returns None for expired token"""
        # Generate an already-expired token
        payload = {
            'username': 'admin',
            'role': 'admin',
            'type': 'access',
            'iat': datetime.utcnow() - timedelta(hours=48),
            'exp': datetime.utcnow() - timedelta(hours=24),
        }
        expired_token = jwt.encode(payload, AuthConfig.JWT_SECRET, algorithm=AuthConfig.JWT_ALGORITHM)
        assert auth.verify_token(expired_token) is None

    def test_refresh_token_success(self, auth):
        """refresh_token returns new access token"""
        auth.register('refreshok', 'Abcdef12', role='viewer')
        result = auth.login('refreshok', 'Abcdef12')
        new_result = auth.refresh_token(result['refresh_token'])
        assert new_result is not None
        assert new_result['success'] is True
        assert 'token' in new_result

    def test_refresh_token_with_access_token_returns_none(self, auth):
        """Using access token as refresh token returns None"""
        auth.register('badrefresh', 'Abcdef12', role='viewer')
        result = auth.login('badrefresh', 'Abcdef12')
        new_result = auth.refresh_token(result['token'])  # access token, not refresh
        assert new_result is None

    def test_refresh_token_invalid(self, auth):
        """Invalid refresh token returns None"""
        assert auth.refresh_token('garbage.token.value') is None


# ============================================================
# Password Change Tests
# ============================================================

class TestPasswordChange:

    def test_change_password_success(self, auth):
        """change_password succeeds with correct old password"""
        auth.register('chpw', 'Abcdef12', role='viewer')
        result = auth.change_password('chpw', 'Abcdef12', 'NewPass123')
        assert result['success'] is True

    def test_change_password_wrong_old(self, auth):
        """change_password fails with wrong old password"""
        auth.register('chpw2', 'Abcdef12', role='viewer')
        result = auth.change_password('chpw2', 'WrongOld1', 'NewPass123')
        assert result['success'] is False

    def test_change_password_weak_new(self, auth):
        """change_password fails with weak new password"""
        auth.register('chpw3', 'Abcdef12', role='viewer')
        result = auth.change_password('chpw3', 'Abcdef12', 'weak')
        assert result['success'] is False

    def test_force_change_password(self, auth):
        """force_change_password works without old password"""
        auth.register('forcepw', 'Abcdef12', role='viewer', display_name='Force User')
        result = auth.force_change_password('forcepw', 'NewPass456')
        assert result['success'] is True
        assert 'token' in result

    def test_force_change_password_nonexistent(self, auth):
        """force_change_password fails for nonexistent user"""
        result = auth.force_change_password('ghost', 'NewPass456')
        assert result['success'] is False


# ============================================================
# User Management Tests
# ============================================================

class TestUserManagement:

    def test_get_users(self, auth):
        """get_users returns user list including default admin"""
        users = auth.get_users()
        assert len(users) >= 1  # at least default admin
        usernames = [u['username'] for u in users]
        assert 'admin' in usernames

    def test_update_user_role(self, auth):
        """update_user changes role"""
        auth.register('upuser', 'Abcdef12', role='viewer')
        result = auth.update_user('upuser', role='engineer')
        assert result['success'] is True

    def test_update_user_invalid_role(self, auth):
        """update_user rejects invalid role"""
        auth.register('upuser2', 'Abcdef12', role='viewer')
        result = auth.update_user('upuser2', role='superadmin')
        assert result['success'] is False

    def test_update_user_nonexistent(self, auth):
        """update_user fails for nonexistent user"""
        result = auth.update_user('ghost', role='admin')
        assert result['success'] is False

    def test_delete_user(self, auth):
        """delete_user soft-deletes user (sets is_active=0)"""
        auth.register('deluser', 'Abcdef12', role='viewer')
        result = auth.delete_user('deluser')
        assert result['success'] is True

    def test_delete_admin_rejected(self, auth):
        """delete_user refuses to delete admin"""
        result = auth.delete_user('admin')
        assert result['success'] is False
        assert '管理员' in result['message']

    def test_delete_nonexistent_user(self, auth):
        """delete_user fails for nonexistent user"""
        result = auth.delete_user('ghost')
        assert result['success'] is False


# ============================================================
# Operation Logs Tests
# ============================================================

class TestOperationLogs:

    def test_operation_logs_recorded(self, auth):
        """Login operations are recorded in logs"""
        auth.register('loguser', 'Abcdef12', role='viewer')
        auth.login('loguser', 'Abcdef12')
        logs = auth.get_operation_logs(username='loguser')
        assert len(logs) > 0

    def test_get_operation_logs_all(self, auth):
        """get_operation_logs without username returns all logs"""
        auth.register('loguser2', 'Abcdef12', role='viewer')
        logs = auth.get_operation_logs()
        assert len(logs) > 0


# ============================================================
# ROLES Definition Tests
# ============================================================

class TestRolesDefinition:

    def test_admin_has_all_permissions(self):
        """Admin role has all permissions"""
        from 用户层.auth import ROLES
        perms = ROLES['admin']['permissions']
        assert 'read' in perms
        assert 'write' in perms
        assert 'delete' in perms
        assert 'manage_users' in perms
        assert 'system_config' in perms

    def test_viewer_read_only(self):
        """Viewer role only has read permission"""
        from 用户层.auth import ROLES
        perms = ROLES['viewer']['permissions']
        assert perms == ['read']

    def test_all_roles_exist(self):
        """All expected roles are defined"""
        from 用户层.auth import ROLES
        for role in ('admin', 'engineer', 'operator', 'viewer'):
            assert role in ROLES
            assert 'name' in ROLES[role]
            assert 'permissions' in ROLES[role]
