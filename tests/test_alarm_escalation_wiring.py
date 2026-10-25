# -*- coding: utf-8 -*-
"""告警升级链路接线 + 入参校验（round 249）。

背景
----
round 243 修邮件通知时顺手记了一句「`AlarmEscalationManager` 只在测试里
出现」。本轮实测发现**那句是错的**，但沿着它查出三个真问题：

1. `配置/alarms.yaml` 里**没有 `escalation` 段** →
   `AlarmManager.load_config()` 的 `if escalation_cfg:` 整段跳过 →
   `AlarmEscalationManager` 从不装配（`_escalation_manager` 恒为 None）。
2. `/api/alarms/escalation/{status,rules}` 的 GET 与 PUT 用
   `hasattr(alarm_manager, '_escalation_manager')` 当守卫，而该属性在
   `__init__` 里就初始化为 None —— **hasattr 永远为 True**。
   于是三个端点全部走 `None.get_status()` → AttributeError → **500**。
3. `update_rules()` / `_load_config()` 对入参**零校验**：
   `rules: []` 会把规则清空（升级机制被关掉，接口还回"更新成功"）；
   `timeout_seconds: 0` 让报警一触发就升级。

另外两条顺带修掉的：
- `load_config()` 的升级段是 `if self._escalation_manager is None:` 整段跳过
  → **首次装配之后再改 yaml 里的 rules 永不生效**（手工编辑、热重载监控、
  PUT 全撞在这道 Guard 上）。改成已装配就走 `update_rules()` 热更新。
- `_load_config()` 里 `if rules_cfg:` 判空 → 显式的 `rules: []` 被当成
  「没配」而**静默保留 DEFAULT_RULES（300/900/1800 秒）**，与「配置写了
  600 秒」表现不同，且看起来像关掉了升级。

单级超时兜底（600 秒）**一直是生效的**（有定时器驱动 + 回调），
本轮补的 escalation 段语义与它完全一致，只是把隐式兜底变成显式配置。
"""

import os
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import paths  # noqa: E402
from 报警层.alarm_manager import AlarmManager  # noqa: E402
from 报警层.alarm_escalation import AlarmEscalationManager  # noqa: E402

REAL_ALARMS_YAML = PROJECT_ROOT / '配置' / 'alarms.yaml'


@pytest.fixture
def cfg_dir(tmp_path, monkeypatch):
    """把配置区指到临时目录（monkeypatch BASE_DIR，不动仓库真配置）。

    `resolve_config_file` 在未设 SCADA_CONFIG_DIR 时按 BASE_DIR 动态解析，
    这正是 paths.py 注释里说的「保留 monkeypatch BASE_DIR 的能力」。
    """
    monkeypatch.delenv('SCADA_CONFIG_DIR', raising=False)
    monkeypatch.setattr(paths, 'BASE_DIR', tmp_path)
    d = tmp_path / '配置'
    d.mkdir(parents=True)
    shutil.copy2(REAL_ALARMS_YAML, d / 'alarms.yaml')
    return d


def _make_manager(cfg_dir, strip_escalation=False):
    yaml_path = cfg_dir / 'alarms.yaml'
    if strip_escalation:
        data = yaml.safe_load(yaml_path.read_text(encoding='utf-8'))
        data.pop('escalation', None)
        yaml_path.write_text(
            yaml.dump(data, allow_unicode=True, default_flow_style=False),
            encoding='utf-8')
    am = AlarmManager(MagicMock(), str(yaml_path))
    am.alarm_output = None
    am.broadcast_system = None
    return am


@pytest.fixture
def manager(cfg_dir):
    am = _make_manager(cfg_dir)
    yield am
    am.stop()


@pytest.fixture
def app_manager(app, cfg_dir, manager):
    """把真实 AlarmManager 挂到 Flask app 上（替代 conftest 的 MagicMock）。"""
    app.alarm_manager = manager
    return manager


# ===========================================================================
# 1. 静态守卫：hasattr 守卫永远为真，不许再出现
# ===========================================================================

def test_no_hasattr_guard_on_escalation_manager():
    """`hasattr(x, '_escalation_manager')` 恒为 True（属性初值就是 None）。

    这是本轮 500 的根因。守卫必须是 `is not None`。

    正则带 `if` 前缀 + 行首锚点：只匹配**真的守卫语句**，不匹配注释/文档
    字符串里提到这个词的地方（本轮自己的 docstring 就提到了它，
    第一版不过滤直接把文档也算违规，误报）。
    """
    import re

    source = (PROJECT_ROOT / '展示层' / 'api' / 'api_alarms.py').read_text(encoding='utf-8')
    bad = re.findall(
        r"^\s*if\s+hasattr\([^)]*_escalation_manager[^)]*\)",
        source, flags=re.MULTILINE)
    assert not bad, (
        f'又出现 hasattr 守卫了 —— 该属性在 AlarmManager.__init__ 里就设为 None，'
        f'hasattr 永远为 True，未装配时端点必 500: {bad}')
    assert "getattr(alarm_manager, '_escalation_manager', None)" in source, (
        '守卫应基于 getattr(...) is not None')


# ===========================================================================
# 2. 生产配置（已补 escalation 段）：管理器应装配，API 应可用
# ===========================================================================

class TestAssembled:
    def test_manager_assembled_from_yaml(self, manager):
        assert manager._escalation_manager is not None, (
            '配置/alarms.yaml 有 escalation 段，但管理器没装配')
        rules = manager._escalation_manager.get_rules()
        assert len(rules) == 1
        assert rules[0]['timeout_seconds'] == 600

    def test_status_endpoint_ok(self, app_manager, client, auth_headers):
        r = client.get('/api/alarms/escalation/status', headers=auth_headers)
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body['mode'] == 'multi_level'
        assert body['rules_count'] == 1
        assert body['running'] is False, (
            '管理器不该自起线程（统一由报警升级定时器驱动）')

    def test_rules_get_ok(self, app_manager, client, auth_headers):
        r = client.get('/api/alarms/escalation/rules', headers=auth_headers)
        assert r.status_code == 200
        body = r.get_json()
        assert body['mode'] == 'multi_level'
        assert len(body['rules']) == 1
        assert body['rules'][0]['timeout_seconds'] == 600


# ===========================================================================
# 3. 未装配时：不再 500，而是如实说明当前生效的策略
# ===========================================================================

class TestUnassembled:
    @pytest.fixture
    def bare_manager(self, app, cfg_dir):
        am = _make_manager(cfg_dir, strip_escalation=True)
        app.alarm_manager = am
        yield am
        am.stop()

    def test_status_does_not_500(self, bare_manager, client, auth_headers):
        """修复前这里必 500（None.get_status()）。"""
        r = client.get('/api/alarms/escalation/status', headers=auth_headers)
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body['mode'] == 'single_level_fallback'
        assert body['timeout_seconds'] == 600
        assert '单级超时' in body['message'], body['message']

    def test_rules_get_does_not_500(self, bare_manager, client, auth_headers):
        r = client.get('/api/alarms/escalation/rules', headers=auth_headers)
        assert r.status_code == 200
        body = r.get_json()
        assert body['mode'] == 'single_level_fallback'
        assert body['rules'] == []
        assert body['fallback']['timeout_seconds'] == 600, (
            '空 rules 不能看起来像"升级已关闭"—— 兜底策略仍在跑，要说出来')

    def test_put_assembles_manager_and_persists(self, bare_manager, client,
                                                auth_headers, cfg_dir):
        """未装配时 PUT 也应可用：写 yaml + 装配 + 重启后仍生效。"""
        rules = [
            {'level': 1, 'timeout_seconds': 300,
             'actions': ['notify'], 'notify_roles': ['operator'],
             'message_template': 't1'},
            {'level': 2, 'timeout_seconds': 900,
             'actions': ['notify', 'broadcast'], 'notify_roles': ['engineer'],
             'message_template': 't2'},
        ]
        r = client.put('/api/alarms/escalation/rules',
                       json={'rules': rules}, headers=auth_headers)
        assert r.status_code == 200, r.get_json()

        # 运行中的管理器已装配
        assert bare_manager._escalation_manager is not None
        assert len(bare_manager._escalation_manager.get_rules()) == 2

        # 真源（yaml）已写回
        saved = yaml.safe_load(
            (cfg_dir / 'alarms.yaml').read_text(encoding='utf-8'))
        assert len(saved['escalation']['rules']) == 2

        # 重启后仍生效：用同一份 yaml 全新构造
        fresh = _make_manager(cfg_dir)
        try:
            assert fresh._escalation_manager is not None
            assert len(fresh._escalation_manager.get_rules()) == 2
        finally:
            fresh.stop()


# ===========================================================================
# 4. PUT 入参校验：空列表 = 关闭全部升级，一律拒绝
# ===========================================================================

class TestPutValidation:
    @pytest.fixture
    def wired(self, app, cfg_dir, manager):
        app.alarm_manager = manager
        return manager

    def test_empty_rules_rejected(self, wired, client, auth_headers):
        r = client.put('/api/alarms/escalation/rules',
                       json={'rules': []}, headers=auth_headers)
        assert r.status_code == 400, r.get_json()
        # 注意：api_error 的字段名是 error（不是 message）
        assert '空列表' in r.get_json()['error'] or '关闭' in r.get_json()['error']

    def test_missing_rules_key_rejected(self, wired, client, auth_headers):
        r = client.put('/api/alarms/escalation/rules',
                       json={'foo': 1}, headers=auth_headers)
        assert r.status_code == 400

    @pytest.mark.parametrize('rules,why', [
        ([{'level': 0, 'timeout_seconds': 300}], 'level 必须 >= 1'),
        ([{'level': 1, 'timeout_seconds': 0}], 'timeout 0 = 一触发就升级'),
        ([{'level': 1, 'timeout_seconds': -5}], '负数超时'),
        ([{'level': 1, 'timeout_seconds': 300},
          {'level': 1, 'timeout_seconds': 600}], '同一级两条规则'),
        (['not-a-dict'], '规则必须是字典'),
    ])
    def test_bad_rule_rejected_and_yaml_untouched(self, wired, client,
                                                  auth_headers, rules, why, cfg_dir):
        before = (cfg_dir / 'alarms.yaml').read_text(encoding='utf-8')
        r = client.put('/api/alarms/escalation/rules',
                       json={'rules': rules}, headers=auth_headers)
        assert r.status_code == 400, f'{why} 没被拒: {r.get_json()}'
        assert (cfg_dir / 'alarms.yaml').read_text(encoding='utf-8') == before, (
            f'非法规则（{why}）被写进了 yaml —— 写坏了比拒绝更难恢复')

    def test_valid_rules_accepted(self, wired, client, auth_headers, cfg_dir):
        """反向验证：合法规则必须能写进去（守卫没有把正常路径堵死）。"""
        rules = [{'level': 1, 'timeout_seconds': 300,
                  'actions': ['notify'], 'notify_roles': ['operator'],
                  'message_template': 'x'}]
        r = client.put('/api/alarms/escalation/rules',
                       json={'rules': rules}, headers=auth_headers)
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['mode'] == 'multi_level'
        assert len(r.get_json()['rules']) == 1


# ===========================================================================
# 5. 领域层校验（不靠 API 好心）
# ===========================================================================

class TestManagerValidation:
    def test_update_rules_empty_raises(self):
        mgr = AlarmEscalationManager()
        with pytest.raises(ValueError, match='非空'):
            mgr.update_rules([])

    def test_update_rules_zero_timeout_raises(self):
        mgr = AlarmEscalationManager()
        with pytest.raises(ValueError, match='timeout_seconds'):
            mgr.update_rules([{'level': 1, 'timeout_seconds': 0}])

    def test_update_rules_duplicate_level_raises(self):
        mgr = AlarmEscalationManager()
        with pytest.raises(ValueError, match='重复'):
            mgr.update_rules([
                {'level': 1, 'timeout_seconds': 300},
                {'level': 1, 'timeout_seconds': 600},
            ])

    def test_load_config_empty_rules_raises(self):
        """显式 `rules: []` 不容错 —— 否则静默保留 DEFAULT_RULES(300/900/1800)。"""
        with pytest.raises(ValueError, match='空列表'):
            AlarmEscalationManager({'rules': []})

    def test_valid_rules_accepted(self):
        mgr = AlarmEscalationManager()
        mgr.update_rules([
            {'level': 1, 'timeout_seconds': 300, 'actions': ['notify']},
            {'level': 2, 'timeout_seconds': 900, 'actions': ['notify', 'sms']},
        ])
        rules = mgr.get_rules()
        assert [r['level'] for r in rules] == [1, 2]
        assert rules[1]['timeout_seconds'] == 900


# ===========================================================================
# 6. 热更新：yaml 改了，已装配的管理器必须跟上
# ===========================================================================

class TestHotUpdate:
    def test_yaml_edit_after_assembly_takes_effect(self, cfg_dir, manager):
        """修复前 `if self._escalation_manager is None:` 让二次加载整段跳过。"""
        assert manager._escalation_manager is not None

        yaml_path = cfg_dir / 'alarms.yaml'
        data = yaml.safe_load(yaml_path.read_text(encoding='utf-8'))
        data['escalation']['rules'] = [
            {'level': 1, 'timeout_seconds': 120,
             'actions': ['notify'], 'notify_roles': ['operator'],
             'message_template': 'x'},
        ]
        yaml_path.write_text(
            yaml.dump(data, allow_unicode=True, default_flow_style=False),
            encoding='utf-8')

        manager.load_config()      # 模拟热重载监控触发
        rules = manager._escalation_manager.get_rules()
        assert len(rules) == 1, 'yaml 里的新规则没热更进去'
        assert rules[0]['timeout_seconds'] == 120
        assert manager._escalation_timeout == 120, (
            '_escalation_timeout 应随规则重算（API 的兜底说明要用它）')

    def test_bad_yaml_rules_keep_previous_policy(self, cfg_dir, manager):
        """yaml 写坏时保留既有策略，不清空（安全功能不能因一次手滑消失）。"""
        yaml_path = cfg_dir / 'alarms.yaml'
        data = yaml.safe_load(yaml_path.read_text(encoding='utf-8'))
        data['escalation']['rules'] = []          # 非法：空列表
        yaml_path.write_text(
            yaml.dump(data, allow_unicode=True, default_flow_style=False),
            encoding='utf-8')

        manager.load_config()
        assert manager._escalation_manager is not None, '装配被异常清掉了'
        assert len(manager._escalation_manager.get_rules()) == 1, (
            '非法 yaml 把既有规则清空了')


# ===========================================================================
# 7. 端到端：多级规则真的能逐级升级
# ===========================================================================

class TestMultiLevelEndToEnd:
    def test_escalates_level_by_level(self, cfg_dir):
        am = _make_manager(cfg_dir)
        try:
            am._escalation_manager.update_rules([
                {'level': 1, 'timeout_seconds': 1,
                 'actions': ['notify'], 'notify_roles': ['operator'],
                 'message_template': 'x'},
                {'level': 2, 'timeout_seconds': 2,
                 'actions': ['notify', 'broadcast'], 'notify_roles': ['engineer'],
                 'message_template': 'y'},
            ])
            fired = []
            am._escalation_manager.add_callback(lambda info: fired.append(info))

            am._escalation_manager.record_alarm('r1', 'D1', 'temp')
            state = am._escalation_manager._states['r1:D1:temp']
            # round 280：升级超时改用单调时钟，造数据也要用它
            state.first_trigger_mono = time.monotonic() - 5      # 5 秒前触发

            am._escalation_manager.check_escalations()

            levels = sorted(f['level'] for f in fired)
            assert levels == [1, 2], f'应逐级升到 1 和 2，实际 {levels}'
            assert state.current_level == 2
        finally:
            am.stop()

    def test_acknowledged_alarm_does_not_escalate(self, cfg_dir):
        am = _make_manager(cfg_dir)
        try:
            fired = []
            am._escalation_manager.add_callback(lambda info: fired.append(info))
            am._escalation_manager.record_alarm('r1', 'D1', 'temp')
            am._escalation_manager.acknowledge_alarm('r1', 'D1', 'temp')
            state = am._escalation_manager._states['r1:D1:temp']
            state.first_trigger_mono = time.monotonic() - 9999
            am._escalation_manager.check_escalations()
            assert fired == [], '已确认的报警不应再升级'
        finally:
            am.stop()

    def test_removed_alarm_does_not_escalate(self, cfg_dir):
        """报警恢复后要移除跟踪，否则会对着一条已消失的报警升级。"""
        am = _make_manager(cfg_dir)
        try:
            fired = []
            am._escalation_manager.add_callback(lambda info: fired.append(info))
            am._escalation_manager.record_alarm('r1', 'D1', 'temp')
            am._escalation_manager.remove_alarm('r1', 'D1', 'temp')
            am._escalation_manager.check_escalations()
            assert fired == []
        finally:
            am.stop()

    def test_late_check_does_not_skip_levels(self, cfg_dir):
        """检查迟到时逐级补跑，不跳级。

        round 249：原实现一次跳到最高适用级别，检查被阻塞超过多级阈值时，
        中间级别被静默跳过 —— 每一级有自己的 notify_roles，跳级等于
        「该第一级通知的人从来没被通知过」。
        """
        am = _make_manager(cfg_dir)
        try:
            am._escalation_manager.update_rules([
                {'level': 1, 'timeout_seconds': 1,
                 'actions': ['notify'], 'notify_roles': ['operator']},
                {'level': 2, 'timeout_seconds': 2,
                 'actions': ['notify', 'broadcast'], 'notify_roles': ['engineer']},
                {'level': 3, 'timeout_seconds': 3,
                 'actions': ['notify', 'broadcast', 'sms'], 'notify_roles': ['admin']},
            ])
            fired = []
            am._escalation_manager.add_callback(lambda info: fired.append(info))

            am._escalation_manager.record_alarm('r1', 'D1', 'temp')
            state = am._escalation_manager._states['r1:D1:temp']
            state.first_trigger_mono = time.monotonic() - 10   # 远超所有阈值

            am._escalation_manager.check_escalations()

            levels = [f['level'] for f in fired]
            assert levels == [1, 2, 3], f'迟到检查应逐级补跑 1,2,3，实际 {levels}'
        finally:
            am.stop()


# ===========================================================================
# 8. 跟踪覆盖：洪水抑制的报警也必须进升级跟踪
# ===========================================================================

class TestTrackingCoverage:
    def test_flood_suppressed_alarm_is_still_tracked(self, cfg_dir, monkeypatch):
        """洪水检测抑制了输出，但报警本身存在 → 仍要升级。

        round 249：`record_alarm` 原来在洪水检测的 early return **之后**，
        被抑制的报警不进跟踪 → 永远不升级。而它此前的单级兜底读
        alarm_states（报警已真实存在），照样升级 —— 机制一切换，
        覆盖范围就静默变小了。
        """
        am = _make_manager(cfg_dir)
        try:
            am._escalation_manager.update_rules([
                {'level': 1, 'timeout_seconds': 1,
                 'actions': ['notify'], 'notify_roles': ['operator']},
            ])
            rule = {'id': 'r_flood', 'name': '洪水报警', 'level': 'warning',
                    'device_id': 'D1', 'register_name': 'temp', 'threshold': 90}

            # 直接造「被洪水检测器抑制」的状态（真实阈值是 100 条/60 秒，
            # 逐条灌到抑制太慢；这里只关心"抑制时跟踪是否还在"）
            monkeypatch.setattr(
                am._flood_detector, 'record_alarm',
                lambda severity: (False, 'flood_suppressed'))

            fired = []
            am._escalation_callbacks.append(lambda info: fired.append(info))

            am._trigger_alarm(rule, 'D1', 'temp', 95.0, datetime.now())

            # 被抑制 ≠ 不存在：必须已进升级跟踪
            assert 'r_flood:D1:temp' in am._escalation_manager._states, (
                '洪水抑制的报警没进升级跟踪 —— 它永远不会升级')

            state = am._escalation_manager._states['r_flood:D1:temp']
            state.first_trigger_mono = time.monotonic() - 5
            am.check_escalation()
            assert fired, '被洪水抑制的报警超时后没有升级'
        finally:
            am.stop()

