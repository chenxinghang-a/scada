# -*- coding: utf-8 -*-
"""报警合理性（ISA-18.2 §9）与外部报警链路（round 261）。

为什么要这些守卫
----------------
一份"能跑"的报警配置和一份"经得起审计"的报警配置，差别不在代码里，在
**每条报警能不能回答四个问题**：

    cause                 —— 为什么会报（根因）
    consequence           —— 不处理会怎样（安全/环保/质量/产量）
    action                —— 操作员具体做什么（可执行动作）
    max_response_time_sec —— 多久内必须响应（决定优先级/升级）

缺了这些，报警就只是一个"响一下的红点"：操作员不知道该做什么，审计时
每条报警都不成立，也没法判断某条报警到底该不该存在（rationalization）。

本文件同时钉住另一件事：**外部事件（边缘决策）必须走同一条报警链路**。
历史实现直接 `database.insert_alarm(alarm_id=f'edge_{当前秒}')` —— 每秒一个
新 ID，去重/计数/升级/声光全部失效，真实库 9 天堆了 20931 条同义记录。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ALARMS_YAML = BACKEND_ROOT / '配置' / 'alarms.yaml'


# ---------------------------------------------------------------------------
# 1. 合理性字段：真实配置必须完整
# ---------------------------------------------------------------------------

def test_all_enabled_rules_are_rationalized():
    """仓库里的报警规则必须全部带齐四个合理性字段。"""
    from core.config_validator import validate_alarm_rationalization

    cfg = yaml.safe_load(ALARMS_YAML.read_text(encoding='utf-8')) or {}
    gaps = validate_alarm_rationalization(cfg)
    assert not gaps, (
        '以下报警缺少 ISA-18.2 §9 合理性描述（审计时这条报警不成立）：\n  '
        + '\n  '.join(gaps))


def test_rationalization_detects_missing_fields():
    """反向对照：缺字段必须被查出来（防止函数恒返回空列表）。"""
    from core.config_validator import validate_alarm_rationalization

    cfg = {'alarm_rules': [{
        'id': 'r1', 'device_id': 'd1', 'register_name': 't',
        'condition': 'greater_than', 'threshold': 1, 'level': 'critical',
        'enabled': True,
    }]}
    gaps = validate_alarm_rationalization(cfg)
    assert len(gaps) == 4, f'应报 4 项缺失，实际 {len(gaps)}: {gaps}'
    assert all('r1' in g for g in gaps)


def test_rationalization_rejects_placeholder_text():
    """"无"/"待定"这类占位符不算填了。"""
    from core.config_validator import validate_alarm_rationalization

    cfg = {'alarm_rules': [{
        'id': 'r2', 'device_id': 'd1', 'register_name': 't',
        'condition': 'greater_than', 'threshold': 1, 'level': 'warning',
        'enabled': True,
        'cause': '无', 'consequence': '待定', 'action': '注意',
        'max_response_time_sec': 0,
    }]}
    gaps = validate_alarm_rationalization(cfg)
    assert len(gaps) == 4, f'占位符被当成有效内容了: {gaps}'


def test_disabled_rules_may_be_unrationalized():
    """停用规则允许先留空（现场常见：先停掉，再补文案）。"""
    from core.config_validator import validate_alarm_rationalization

    cfg = {'alarm_rules': [{
        'id': 'r3', 'device_id': 'd1', 'register_name': 't',
        'condition': 'greater_than', 'threshold': 1, 'enabled': False,
    }]}
    assert validate_alarm_rationalization(cfg) == []


def test_schema_vocabulary_matches_real_file():
    """schema 里必须同时认 `register_name`/`level`（真实文件的写法）。

    历史缺陷：schema 只写了 `register`/`severity` —— 校验器校验的是一套
    **配置文件里根本不存在的字段名**，等于形同虚设。
    """
    from core.config_validator import ALARM_SCHEMA

    props = ALARM_SCHEMA['properties']['alarm_rules']['items']['properties']
    for key in ('register_name', 'level', 'delay', 'description',
                'cause', 'consequence', 'action', 'max_response_time_sec'):
        assert key in props, f'schema 缺少真实配置在用的字段: {key}'

    cfg = yaml.safe_load(ALARMS_YAML.read_text(encoding='utf-8')) or {}
    for rule in cfg['alarm_rules']:
        assert 'register_name' in rule, f'规则 {rule.get("id")} 用了旧写法 register'
        assert 'level' in rule, f'规则 {rule.get("id")} 用了旧写法 severity'


def test_real_config_passes_structural_validation():
    """结构校验也要过（字段名改了之后不能只靠合理性检查兜着）。"""
    from core.config_validator import validate_config

    ok, errors = validate_config(str(ALARMS_YAML), 'alarms')
    assert ok, f'配置结构校验失败: {errors}'


def test_real_config_has_no_duplicate_rules():
    """仓库里的规则不许同一 (设备, 寄存器, 条件) 挂两条启用规则。

    真实库里出现过：`boiler_temperature` 同时挂 `temp_high` 和
    `boiler_temp_high`，一次越限弹两条，合计占全库报警一大块。
    """
    from core.config_validator import validate_alarm_uniqueness

    cfg = yaml.safe_load(ALARMS_YAML.read_text(encoding='utf-8')) or {}
    assert validate_alarm_uniqueness(cfg) == []


def test_uniqueness_detects_duplicate_rule():
    from core.config_validator import validate_alarm_uniqueness

    cfg = {'alarm_rules': [
        {'id': 'a', 'device_id': 'd1', 'register_name': 't',
         'condition': 'greater_than', 'threshold': 100, 'enabled': True},
        {'id': 'b', 'device_id': 'd1', 'register_name': 't',
         'condition': 'greater_than', 'threshold': 120, 'enabled': True},
    ]}
    problems = validate_alarm_uniqueness(cfg)
    assert len(problems) == 1 and '重复定义' in problems[0]


def test_uniqueness_allows_opposite_conditions():
    """同一点位的"过高"和"过低"是两条合法规则，不是重复。"""
    from core.config_validator import validate_alarm_uniqueness

    cfg = {'alarm_rules': [
        {'id': 'hi', 'device_id': 'd1', 'register_name': 't',
         'condition': 'greater_than', 'threshold': 100, 'enabled': True},
        {'id': 'lo', 'device_id': 'd1', 'register_name': 't',
         'condition': 'less_than', 'threshold': 10, 'enabled': True},
    ]}
    assert validate_alarm_uniqueness(cfg) == []


def test_uniqueness_ignores_disabled_rules():
    from core.config_validator import validate_alarm_uniqueness

    cfg = {'alarm_rules': [
        {'id': 'old', 'device_id': 'd1', 'register_name': 't',
         'condition': 'greater_than', 'threshold': 100, 'enabled': False},
        {'id': 'new', 'device_id': 'd1', 'register_name': 't',
         'condition': 'greater_than', 'threshold': 100, 'enabled': True},
    ]}
    assert validate_alarm_uniqueness(cfg) == []


def test_real_rules_have_damping_configured():
    """每条启用规则都必须配 delay 与 deadband（round 262）。

    真实数据回放证明：全部 delay=0 / deadband=0 时，同一份历史数据会产生
    2017 次报警；配上 5~15s 延迟 + 按量纲的死区后降到 145 次（-93%）。
    所以"没配抑制"本身就是缺陷，不能只靠默认值兜着。
    """
    cfg = yaml.safe_load(ALARMS_YAML.read_text(encoding='utf-8')) or {}
    missing = []
    for rule in cfg['alarm_rules']:
        if not rule.get('enabled', True):
            continue
        if not rule.get('delay'):
            missing.append(f'{rule["id"]}: delay 为 0')
        if not rule.get('deadband'):
            missing.append(f'{rule["id"]}: deadband 为 0')
    assert not missing, '以下规则没有配置抖动抑制：\n  ' + '\n  '.join(missing)


# ---------------------------------------------------------------------------
# 2. 外部报警：稳定 ID + 走完整链路
# ---------------------------------------------------------------------------

@pytest.fixture
def am(tmp_path):
    from 存储层.database import Database
    from 报警层.alarm_manager import AlarmManager

    db = Database(str(tmp_path / 'ext.db'))
    mgr = AlarmManager(db)
    yield mgr, db
    db.close()


def test_external_alarm_is_registered_as_active(am):
    mgr, db = am
    mgr.raise_external_alarm('edge_abc123', '管道压力超限，紧急泄压！',
                             level='critical', device_id='edge_decision',
                             register_name='auto')

    active = mgr.get_active_alarms()
    assert any(a['alarm_id'] == 'edge_abc123' for a in active), (
        '外部报警没进活跃列表 —— 操作员在界面上看不到它')
    entry = next(a for a in active if a['alarm_id'] == 'edge_abc123')
    assert entry['alarm_level'] == 'critical'
    assert entry['alarm_message'] == '管道压力超限，紧急泄压！'


def test_external_alarm_repeats_do_not_create_new_rows(am):
    """同一 ID 重复触发只累加计数（这正是旧实现 `edge_{秒}` 做不到的）。"""
    mgr, db = am
    for _ in range(3):
        mgr.raise_external_alarm('edge_abc123', '管道压力超限，紧急泄压！',
                                 level='critical', device_id='edge_decision',
                                 register_name='auto')

    rows = db.get_alarm_records(limit=50)
    assert len(rows) == 1, f'同一报警写了 {len(rows)} 行 —— ID 不稳定'
    assert rows[0]['trigger_count'] == 3


def test_external_alarm_clear_marks_record(am):
    mgr, db = am
    mgr.raise_external_alarm('edge_abc123', '管道压力超限，紧急泄压！',
                             device_id='edge_decision', register_name='auto')
    assert mgr.clear_external_alarm('edge_abc123', 'edge_decision', 'auto') is True

    assert not any(a['alarm_id'] == 'edge_abc123' for a in mgr.get_active_alarms())
    rows = db.get_alarm_records(limit=50)
    assert rows[0]['cleared'] == 1, '外部报警恢复没落库'


def test_edge_callback_uses_stable_id():
    """run.py 的边缘回调不许再用 `edge_{当前秒}` 这种随时间变化的 ID。"""
    src = (BACKEND_ROOT / 'run.py').read_text(encoding='utf-8')
    code = re.sub(r'#.*$', '', src, flags=re.M)
    assert 'edge_{int(datetime.now().timestamp())}' not in code, (
        '边缘决策报警又用时间戳当 ID —— 每秒一个新报警，去重/计数/升级全部失效')
    assert 'raise_external_alarm' in code, '边缘回调没有走报警管理层'
    assert 'hashlib' in code, '边缘回调没有按消息内容派生稳定 ID'
