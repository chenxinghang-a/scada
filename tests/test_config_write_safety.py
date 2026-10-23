# -*- coding: utf-8 -*-
"""配置写盘的守卫：**不许把配置文件写坏，也不许假装写成功**（round 256）。

背景（三个真实缺陷，同一类"看着能用"）：

1. `open(path, 'w')` 先截断、再 `yaml.dump` —— 序列化一抛异常（值不可序列化 /
   磁盘满 / 编码错），配置文件就成 **0 字节**，而外层 `except` 只记一条日志。
   `配置/alarms.yaml` 被清空 = 所有报警规则消失；`配置/system.yaml` 被清空 =
   端口、数据库、JWT 密钥段一起消失，重启直接起不来。
2. `yaml.dump` 整份重写：**注释全抹、键按字母重排**。`alarms.yaml` 里关于
   "sms 未实现""sound 没有消费方""升级策略语义"的注释块被反复抹平又补回来。
3. 落盘失败仍返回成功：`add_rule` / `update_dedup_config` / `ConfigManager.save_yaml`
   都是"内存改了就算成功"，接口回 `success: true`，重启后全部还原。

本文件把这三条钉死。
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from core import config_io
from core.config_io import atomic_write_text, dump_yaml


SAMPLE = textwrap.dedent("""\
    # 文件级说明：这段注释必须活下来
    alarm:
      # 冷却窗口（秒）
      cooldown: 300
      enabled: true

    # 下面这段说明属于 escalation 段，改 alarm 时不许丢
    escalation:
      # 一级超时
      timeout_seconds: 600
      rules:
      - level: 1
        actions: [notify, broadcast]
    """)


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    p = tmp_path / 'alarms.yaml'
    p.write_text(SAMPLE, encoding='utf-8')
    return p


# ---------------------------------------------------------------------------
# 1. 原子写：失败时原文件分毫不动
# ---------------------------------------------------------------------------

def test_atomic_write_leaves_no_temp_files(tmp_path: Path):
    p = tmp_path / 'x.yaml'
    p.write_text('a: 1\n', encoding='utf-8')
    atomic_write_text(p, 'a: 2\n')
    assert p.read_text(encoding='utf-8') == 'a: 2\n'
    leftovers = [f.name for f in tmp_path.iterdir() if f.name != 'x.yaml']
    assert leftovers == [], f'临时文件没清干净: {leftovers}'


def test_dump_yaml_serialization_failure_keeps_original(cfg: Path):
    """序列化抛异常时，原文件必须**一个字节都没变**。"""
    before = cfg.read_bytes()

    def boom(*a, **kw):
        raise yaml.representer.RepresenterError('不可序列化的值')

    with patch.object(config_io.yaml, 'dump', side_effect=boom):
        ok = dump_yaml(cfg, {'alarm': {'cooldown': 1}})

    assert ok is False
    assert cfg.read_bytes() == before, '写失败后原文件被破坏了（截断/半截内容）'


def test_dump_yaml_write_permission_error_keeps_original(cfg: Path):
    before = cfg.read_bytes()
    with patch.object(config_io, 'atomic_write_text', side_effect=OSError('磁盘满')):
        assert dump_yaml(cfg, {'alarm': {'cooldown': 1}}) is False
    assert cfg.read_bytes() == before


# ---------------------------------------------------------------------------
# 2. 就地合并：只动改了的键，注释与键序原样保留
# ---------------------------------------------------------------------------

def test_dump_yaml_preserves_comments(cfg: Path):
    data = yaml.safe_load(cfg.read_text(encoding='utf-8'))
    data['alarm']['cooldown'] = 120
    assert dump_yaml(cfg, data) is True

    text = cfg.read_text(encoding='utf-8')
    assert '# 文件级说明：这段注释必须活下来' in text
    assert '# 冷却窗口（秒）' in text
    assert '# 下面这段说明属于 escalation 段，改 alarm 时不许丢' in text
    assert '# 一级超时' in text
    assert 'cooldown: 120' in text
    assert yaml.safe_load(text) == data


def test_dump_yaml_only_touches_changed_lines(cfg: Path):
    before = cfg.read_text(encoding='utf-8').splitlines()
    data = yaml.safe_load(cfg.read_text(encoding='utf-8'))
    data['alarm']['cooldown'] = 60
    dump_yaml(cfg, data)
    after = cfg.read_text(encoding='utf-8').splitlines()

    assert len(before) == len(after), '行数变了说明整份被重写'
    diff = [(a, b) for a, b in zip(before, after) if a != b]
    assert diff == [('  cooldown: 300', '  cooldown: 60')], diff


def test_dump_yaml_keeps_key_order(cfg: Path):
    """原来 `yaml.dump` 默认 sort_keys=True，会把键按字母重排。"""
    data = yaml.safe_load(cfg.read_text(encoding='utf-8'))
    data['alarm']['cooldown'] = 30
    dump_yaml(cfg, data)
    text = cfg.read_text(encoding='utf-8')
    assert text.index('alarm:') < text.index('escalation:')


def test_dump_yaml_adds_new_key(cfg: Path):
    data = yaml.safe_load(cfg.read_text(encoding='utf-8'))
    data['notification'] = {'email': {'enabled': False}}
    assert dump_yaml(cfg, data) is True
    parsed = yaml.safe_load(cfg.read_text(encoding='utf-8'))
    assert parsed == data
    assert '# 文件级说明' in cfg.read_text(encoding='utf-8')


def test_dump_yaml_removes_key_but_keeps_trailing_comment(cfg: Path):
    data = yaml.safe_load(cfg.read_text(encoding='utf-8'))
    del data['escalation']
    assert dump_yaml(cfg, data) is True
    text = cfg.read_text(encoding='utf-8')
    assert 'escalation' not in yaml.safe_load(text)
    assert '# 下面这段说明属于 escalation 段' in text


def test_dump_yaml_preserves_crlf(tmp_path: Path):
    p = tmp_path / 'win.yaml'
    p.write_bytes(b'a:\r\n  b: 1\r\n')
    assert dump_yaml(p, {'a': {'b': 2}}) is True
    raw = p.read_bytes()
    assert b'\r\n' in raw and b'a:\r\n' in raw, raw


def test_dump_yaml_falls_back_to_full_rewrite_when_merge_mismatches(cfg: Path):
    """合并结果若与目标 dict 不等价，必须整份重写 —— 内容正确优先于保留注释。"""
    data = {'alarm': {'cooldown': 1, 'enabled': False}, 'extra': [1, 2, 3]}
    assert dump_yaml(cfg, data) is True
    assert yaml.safe_load(cfg.read_text(encoding='utf-8')) == data


# ---------------------------------------------------------------------------
# 3. 「写失败」必须被上层感知，不许假装成功
# ---------------------------------------------------------------------------

def _alarm_manager(config_path: Path):
    from 报警层.alarm_manager import AlarmManager
    db = MagicMock()
    db.get_alarm_records.return_value = []
    db.get_active_alarms.return_value = []
    return AlarmManager(db, config_path=str(config_path))


def test_add_rule_rolls_back_when_save_fails(cfg: Path):
    am = _alarm_manager(cfg)
    before = dict(am.rules)
    rule = {'id': 'r_new', 'name': 'n', 'device_id': 'd', 'register_name': 'x',
            'condition': 'greater_than', 'threshold': 1.0, 'level': 'warning'}

    with patch('报警层.alarm_manager.dump_yaml', return_value=False):
        assert am.add_rule(rule) is False

    assert 'r_new' not in am.rules, '落盘失败却把规则留在内存里（重启即丢）'
    assert am.rules == before


def test_remove_rule_rolls_back_when_save_fails(cfg: Path):
    am = _alarm_manager(cfg)
    rule = {'id': 'r_keep', 'name': 'n', 'device_id': 'd', 'register_name': 'x',
            'condition': 'greater_than', 'threshold': 1.0, 'level': 'warning'}
    am.rules['r_keep'] = rule
    am._rebuild_rules_index()

    with patch('报警层.alarm_manager.dump_yaml', return_value=False):
        assert am.remove_rule('r_keep') is False

    assert 'r_keep' in am.rules, '落盘失败却把规则从内存删了（重启后规则复活）'


def test_update_dedup_config_rolls_back_when_save_fails(cfg: Path):
    from core.config_io import ConfigPersistError
    am = _alarm_manager(cfg)
    before = am.get_dedup_config()['emit_cooldown_seconds']

    with patch('报警层.alarm_manager.dump_yaml', return_value=False):
        with pytest.raises(ConfigPersistError):
            am.update_dedup_config({'emit_cooldown_seconds': 30})

    assert am.get_dedup_config()['emit_cooldown_seconds'] == before


def test_config_manager_does_not_cache_failed_write(tmp_path: Path):
    from core.config_manager import ConfigManager
    p = tmp_path / 'c.yaml'
    p.write_text('a: 1\n', encoding='utf-8')
    ConfigManager.set(str(p), 'a', 1)          # 先让缓存里是旧值

    with patch('core.config_manager.dump_yaml', return_value=False):
        ok = ConfigManager.save_yaml(str(p), {'a': 2})

    assert ok is False
    assert p.read_text(encoding='utf-8') == 'a: 1\n'
    assert ConfigManager.get(str(p), 'a') == 1, '写失败却把新值放进了缓存'


def test_save_config_keeps_other_sections(cfg: Path):
    """报警规则落盘不能顺手抹掉 dedup / escalation 段。"""
    am = _alarm_manager(cfg)
    am.rules['r1'] = {'id': 'r1', 'name': 'n', 'device_id': 'd',
                      'register_name': 'x', 'condition': 'greater_than',
                      'threshold': 1.0, 'level': 'warning'}
    am._rebuild_rules_index()
    assert am._save_config() is True

    parsed = yaml.safe_load(cfg.read_text(encoding='utf-8'))
    assert 'alarm_rules' in parsed
    assert parsed['escalation']['timeout_seconds'] == 600
    assert parsed['alarm']['cooldown'] == 300
