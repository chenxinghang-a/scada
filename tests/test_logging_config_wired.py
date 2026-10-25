# -*- coding: utf-8 -*-
"""日志配置必须真的生效（round 266）。

缺陷形态
--------
`配置/system.yaml` 的 `logging` 段写得很像回事：

    logging:
      file: {max_size_mb: 10, backup_count: 5, enabled: true, path: logs/scada.log}
      level: DEBUG

而实际生效的是 `config.LogConfig`：轮转 **100 MB**、保留 **30 天**、级别 **INFO**，
只认环境变量（`SCADA_LOG_ROTATION` 等）。也就是说：

* 运维把 `max_size_mb` 从 10 改成 5 再重启 —— **毫无变化**；
* 配置里写着 `level: DEBUG`，实际一直是 INFO（这条算"运气好"，否则磁盘会被刷满）；
* `format` 段写的是 Python logging 的 `%(asctime)s` 语法，而本项目用 loguru，
  这一段从来没被读过。

判据：**配置里的每个键都必须有消费方**，否则就是装饰品（round 257 同一类问题）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYSTEM_YAML = PROJECT_ROOT / '配置' / 'system.yaml'


@pytest.fixture
def yaml_logging():
    import yaml
    cfg = yaml.safe_load(SYSTEM_YAML.read_text(encoding='utf-8')) or {}
    return cfg.get('logging') or {}


# ---------------------------------------------------------------------------
# 1. 读出来的值 = 文件里的值
# ---------------------------------------------------------------------------

def test_rotation_comes_from_max_size_mb(yaml_logging):
    from core.config_manager import load_logging_settings

    settings = load_logging_settings()
    size = yaml_logging['file']['max_size_mb']
    assert settings['rotation'] == f'{size} MB', (
        f"配置写 {size}MB，实际轮转 {settings['rotation']} —— 配置没生效")


def test_retention_comes_from_backup_count(yaml_logging):
    from core.config_manager import load_logging_settings

    settings = load_logging_settings()
    assert settings['retention'] == yaml_logging['file']['backup_count']


def test_level_comes_from_yaml(yaml_logging):
    from core.config_manager import load_logging_settings

    settings = load_logging_settings()
    assert settings['level'] == str(yaml_logging['level']).upper()


def test_level_is_not_debug_in_production_config(yaml_logging):
    """生产配置不许是 DEBUG —— 每秒的采集/推送日志会把磁盘刷满。"""
    assert str(yaml_logging['level']).upper() != 'DEBUG', (
        'system.yaml 的 logging.level 是 DEBUG —— 生产会刷爆磁盘与日志文件')


def test_log_dir_comes_from_path(yaml_logging):
    from core.config_manager import load_logging_settings

    settings = load_logging_settings()
    expected = str(Path(yaml_logging['file']['path']).parent)
    assert settings['log_dir'] == expected


# ---------------------------------------------------------------------------
# 2. 环境变量优先（兼容既有部署脚本）
# ---------------------------------------------------------------------------

def test_env_overrides_yaml(monkeypatch):
    from core.config_manager import load_logging_settings

    monkeypatch.setenv('SCADA_LOG_ROTATION', '7 MB')
    monkeypatch.setenv('SCADA_LOG_LEVEL', 'WARNING')
    monkeypatch.setenv('SCADA_LOG_RETENTION', '12')

    settings = load_logging_settings()
    assert settings['rotation'] == '7 MB'
    assert settings['level'] == 'WARNING'
    assert settings['retention'] == '12'


def test_invalid_values_fall_back(monkeypatch):
    """配置写坏（字符串/负数/inf）不许把日志系统带崩。"""
    import yaml as _yaml

    from core import config_manager

    bad = {'logging': {'level': 'LOUD', 'file': {
        'max_size_mb': 'abc', 'backup_count': -3, 'enabled': True}}}
    monkeypatch.setattr(config_manager, 'load_system_config', lambda *a, **k: bad)

    settings = config_manager.load_logging_settings()
    assert settings['level'] == 'INFO'
    assert settings['rotation'] == '100 MB'
    assert settings['retention'] == 5


# ---------------------------------------------------------------------------
# 3. 每个键都有消费方（防再长出假旋钮）
# ---------------------------------------------------------------------------

def test_every_logging_key_is_consumed(yaml_logging):
    """`logging` 段里的每个键都必须在 `load_logging_settings` 里被读到。"""
    src = (PROJECT_ROOT / 'core' / 'config_manager.py').read_text(encoding='utf-8')
    start = src.find('def load_logging_settings')
    end = src.find('def load_collection_settings')
    body = src[start:end]
    assert body, '找不到 load_logging_settings'

    keys = set()
    for sub in ('console', 'file'):
        for key in (yaml_logging.get(sub) or {}):
            keys.add(key)
    keys.add('level')

    missing = [k for k in keys if f"'{k}'" not in body]
    assert not missing, (
        f'这些配置键没有任何消费方（改了不生效）：{missing}\n'
        '要么接线，要么从 system.yaml 里删掉 —— 留着就是骗人')


def test_format_section_removed():
    """`format` 是 Python logging 语法，loguru 用不了，必须删掉而不是留着装样子。"""
    import yaml
    cfg = yaml.safe_load(SYSTEM_YAML.read_text(encoding='utf-8')) or {}
    assert 'format' not in (cfg.get('logging') or {}), (
        'logging.format 又出现了 —— loguru 不认 %(asctime)s 这种语法，'
        '留着只会让人以为改了有用')


# ---------------------------------------------------------------------------
# 4. 启动路径真的用了它
# ---------------------------------------------------------------------------

def test_run_py_uses_yaml_logging_settings():
    src = (PROJECT_ROOT / 'run.py').read_text(encoding='utf-8')
    assert 'load_logging_settings()' in src, (
        'run.py 没有读 logging 段 —— 配置又变回装饰品')
    # 不许再直接拿 LogConfig 的硬编码值喂 setup_logging
    call = src[src.find('setup_logging('):src.find('logger = get_logger(__name__)')]
    assert 'LogConfig.LOG_ROTATION' not in call, (
        'setup_logging 仍在用 LogConfig.LOG_ROTATION 硬编码值')


def test_setup_logging_supports_switches():
    """`file.enabled` / `console.enabled` 必须有对应开关，否则又是一组假旋钮。"""
    import inspect

    from core.structured_logging import setup_logging

    params = inspect.signature(setup_logging).parameters
    assert 'enable_file' in params
    assert 'enable_console' in params
