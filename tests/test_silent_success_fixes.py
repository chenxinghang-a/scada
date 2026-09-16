# -*- coding: utf-8 -*-
"""静默假成功修复回归（round 160，第二批：鉴权 / 配置安全）。

第一批见 `tests/test_silent_failure_fixes.py`（chaos / etag / whitelist /
compressor / 审计表名）。本文件覆盖另外 5 处 —— 它们都属于"对外宣称成功、
实际没做成"，在鉴权路径上等同于**静默放行**：

A. `报警层/alarm_manager.py::_save_config` —— 配置文件损坏时用空 dict 覆盖，
   dedup/escalation 等非规则配置段被不可逆抹掉。
B. `用户层/auth.py::_blacklist_user_tokens` —— 落库失败被吞，撤销"看起来成功了"。
C. `用户层/auth.py::change_password` —— 撤销失败不反馈给调用方。
D. `用户层/auth.py::refresh_token` —— `password_changed_at` 不可解析时跳过
   "改密即失效"校验（fail-open），已改密用户仍能用旧刷新令牌续期。
E. `展示层/api/api_auth.py::logout` —— 忽略撤销结果，且异常路径**硬编码返回
   `success: True`**，用户以为登出了、令牌其实还活着。

每条都做过变异验证：把修复还原回去，对应测试必须变红。
"""

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# ===========================================================================
# A. alarm_manager._save_config —— 损坏配置不能被空 dict 覆盖掉
# ===========================================================================

def _bare_alarm_manager(config_path, rules=None):
    """绕过 __init__ 的副作用，只装配 _save_config 需要的属性。

    `AlarmManager.__init__` 会去读配置、建定时器、连数据库 —— 对一个纯函数级的
    配置写入测试来说是噪音。这里直接 new 一个空壳。
    """
    from 报警层.alarm_manager import AlarmManager

    mgr = object.__new__(AlarmManager)
    mgr.config_path = str(config_path)
    mgr.rules = rules if rules is not None else {
        'r1': {'id': 'r1', 'register_name': 'temperature', 'threshold': 80},
    }
    return mgr


def test_save_config_preserves_unrelated_sections(tmp_path):
    """正常路径：非规则配置段（dedup / escalation）必须原样保留。"""
    import yaml

    cfg = tmp_path / 'alarms.yaml'
    cfg.write_text(
        yaml.dump({
            'dedup': {'window_seconds': 60},
            'escalation': {'levels': [1, 2, 3]},
            'alarm_rules': [],
        }, allow_unicode=True),
        encoding='utf-8',
    )

    mgr = _bare_alarm_manager(cfg)
    assert mgr._save_config() is True

    saved = yaml.safe_load(cfg.read_text(encoding='utf-8'))
    assert saved['dedup'] == {'window_seconds': 60}
    assert saved['escalation'] == {'levels': [1, 2, 3]}
    assert saved['alarm_rules'] == list(mgr.rules.values())


def test_save_config_backs_up_corrupt_file_before_overwriting(tmp_path):
    """配置文件损坏时：必须先留下可恢复的备份，再写新配置。

    修复前 `yaml.safe_load` 抛异常 → `config` 退化为 `{}` → 直接覆盖写回，
    dedup / escalation 等配置段**永久消失**，且只留一行 warning。
    """
    cfg = tmp_path / 'alarms.yaml'
    broken = 'dedup: {window_seconds: 60\nthis is not valid yaml: [\n'
    cfg.write_text(broken, encoding='utf-8')

    mgr = _bare_alarm_manager(cfg)
    assert mgr._save_config() is True, "备份成功的前提下应当继续写入新配置"

    backups = sorted(tmp_path.glob('alarms.yaml.corrupt-*'))
    assert backups, "损坏的配置文件必须先另存备份，否则旧内容不可恢复"
    assert backups[0].read_text(encoding='utf-8') == broken, (
        "备份内容必须与原文件逐字一致"
    )

    import yaml
    saved = yaml.safe_load(cfg.read_text(encoding='utf-8'))
    assert saved['alarm_rules'] == list(mgr.rules.values())


def test_save_config_refuses_write_when_backup_impossible(tmp_path, monkeypatch):
    """连备份都做不出来时必须放弃写入 —— 宁可规则存不下，也不能毁配置。"""
    cfg = tmp_path / 'alarms.yaml'
    cfg.write_text('dedup: {broken\n', encoding='utf-8')

    import shutil as _shutil

    def _boom(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(_shutil, 'copy2', _boom)

    mgr = _bare_alarm_manager(cfg)
    assert mgr._save_config() is False
    assert cfg.read_text(encoding='utf-8') == 'dedup: {broken\n', (
        "备份失败时原文件必须原封不动"
    )


def test_save_config_rejects_non_mapping_config(tmp_path):
    """配置文件内容是合法 YAML 但不是映射（比如一个字符串）时同样要备份。"""
    cfg = tmp_path / 'alarms.yaml'
    cfg.write_text('just a plain string\n', encoding='utf-8')

    mgr = _bare_alarm_manager(cfg)
    assert mgr._save_config() is True

    assert sorted(tmp_path.glob('alarms.yaml.corrupt-*')), (
        "非映射的配置同样会被空 dict 覆盖，必须先备份"
    )


# ===========================================================================
# B / C. 令牌撤销必须能被调用方感知
# ===========================================================================

def test_blacklist_user_tokens_returns_true_outside_http_context(auth_manager):
    """非 HTTP 上下文（定时任务 / 脚本）本就无令牌可撤销 → True，且不应报错。"""
    assert auth_manager._blacklist_user_tokens('someone', 'test') is True


def test_blacklist_user_tokens_reports_failure_when_store_fails(auth_manager, monkeypatch):
    """落库失败必须返回 False —— 此前被吞掉，撤销"看起来成功了"。

    修复前这个 try 同时罩住"取请求令牌"和"写黑名单"，落库失败被 except 吃掉，
    调用方（change_password / logout）无从得知令牌其实还活着。
    """
    from flask import Flask

    app = Flask(__name__)
    monkeypatch.setattr(auth_manager, 'blacklist_token', lambda *a, **k: False)

    with app.test_request_context(headers={'Authorization': 'Bearer faketoken'}):
        assert auth_manager._blacklist_user_tokens('u', 'logout') is False


def test_blacklist_user_tokens_reports_success_when_store_succeeds(auth_manager, monkeypatch):
    """反向验证：落库成功时必须返回 True，不能一律报失败。"""
    from flask import Flask

    app = Flask(__name__)
    monkeypatch.setattr(auth_manager, 'blacklist_token', lambda *a, **k: True)

    with app.test_request_context(headers={'Authorization': 'Bearer faketoken'}):
        assert auth_manager._blacklist_user_tokens('u', 'logout') is True


def test_blacklist_user_tokens_returns_true_when_request_has_no_bearer(auth_manager, monkeypatch):
    """请求没带 Bearer 令牌 → 无可撤销，属正常情况。"""
    from flask import Flask

    app = Flask(__name__)
    monkeypatch.setattr(auth_manager, 'blacklist_token',
                        lambda *a, **k: pytest.fail('不应被调用'))

    with app.test_request_context(headers={}):
        assert auth_manager._blacklist_user_tokens('u', 'logout') is True


def test_change_password_surfaces_revocation_failure(auth_manager, monkeypatch):
    """改密成功但旧令牌撤销失败时，返回值必须带 warning，不能只字不提。"""
    auth_manager.register('pwdfail', 'Abcdef12', role='viewer')
    monkeypatch.setattr(auth_manager, '_blacklist_user_tokens', lambda *a, **k: False)

    result = auth_manager.change_password('pwdfail', 'Abcdef12', 'Newpass99')

    assert result['success'] is True, "密码本身确实改成功了"
    assert 'warning' in result, (
        "旧令牌撤销失败必须反馈给调用方，否则用户以为旧令牌已作废"
    )
    assert '令牌' in result['warning']


# ===========================================================================
# D. refresh_token —— 无法确认密码是否变更时必须 fail-closed
# ===========================================================================

def test_refresh_token_works_when_password_never_changed(auth_manager):
    """反向验证：password_changed_at 为 NULL（从未改密）时正常续期。"""
    auth_manager.register('pwdnull', 'Abcdef12', role='viewer')
    login = auth_manager.login('pwdnull', 'Abcdef12')

    assert auth_manager.refresh_token(login['refresh_token']) is not None


def test_refresh_token_fails_closed_when_password_changed_at_is_unparseable(auth_manager):
    """`password_changed_at` 不可解析时必须**拒绝续期**。

    修复前是 fail-open：解析失败被吞掉 → 跳过"改密即失效"校验 →
    已改密用户的旧刷新令牌照常续期。等于密码轮换形同虚设。
    """
    auth_manager.register('pwdbad', 'Abcdef12', role='viewer')
    auth_manager.login('pwdbad', 'Abcdef12')
    login = auth_manager.login('pwdbad', 'Abcdef12')
    refresh = login['refresh_token']

    with auth_manager.database.get_connection() as conn:
        conn.execute(
            'UPDATE users SET password_changed_at = ? WHERE username = ?',
            ('not-a-timestamp', 'pwdbad'),
        )
        conn.commit()

    assert auth_manager.refresh_token(refresh) is None, (
        "无法确认密码是否已变更时必须拒绝续期（fail-closed），不能静默放行"
    )


def test_refresh_token_rejected_when_password_changed_after_issue(auth_manager):
    """反向验证：正常的"改密即失效"路径不能被改坏。"""
    auth_manager.register('pwdrot', 'Abcdef12', role='viewer')
    login = auth_manager.login('pwdrot', 'Abcdef12')
    refresh = login['refresh_token']

    with auth_manager.database.get_connection() as conn:
        conn.execute(
            'UPDATE users SET password_changed_at = ? WHERE username = ?',
            ('2999-01-01 00:00:00.000000', 'pwdrot'),
        )
        conn.commit()

    assert auth_manager.refresh_token(refresh) is None


# ===========================================================================
# E. logout —— 不能谎报成功
# ===========================================================================

def test_logout_reports_failure_when_revocation_fails(app, client, auth_headers):
    """撤销失败时不能回 "已登出" —— 客户端会以为令牌死了，其实还能用。"""
    app.auth_manager.blacklist_token.return_value = False

    resp = client.post('/api/auth/logout', headers=auth_headers)

    assert resp.status_code == 500, "撤销失败必须如实报错"
    body = resp.get_json()
    assert body['success'] is False
    assert '登出' in body['message']


def test_logout_reports_success_when_revocation_succeeds(app, client, auth_headers):
    """反向验证：正常登出仍然是 200 + success。"""
    app.auth_manager.blacklist_token.return_value = True

    resp = client.post('/api/auth/logout', headers=auth_headers)

    assert resp.status_code == 200
    assert resp.get_json()['success'] is True


def test_logout_does_not_hardcode_success_on_exception(app, client, auth_headers):
    """异常路径此前**硬编码返回 `success: True`** —— 必须改掉。"""
    app.auth_manager.log_operation.side_effect = RuntimeError('audit db down')

    resp = client.post('/api/auth/logout', headers=auth_headers)

    assert resp.status_code == 500
    assert resp.get_json()['success'] is False, (
        "登出过程中出异常却对外宣称成功，等于骗客户端令牌已作废"
    )
