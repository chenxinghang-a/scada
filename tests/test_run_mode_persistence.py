# -*- coding: utf-8 -*-
"""启动模式必须读回「界面上切换后持久化的选择」（round 256）。

缺陷形态：`POST /api/system/simulation-mode` 一直把新值写进
`配置/system.yaml` 的 `system.simulation_mode`，而**启动侧从来没有读取方** ——
运行时切到真实模式、重启又回到模拟，接口却回 `success: true`。
典型「算出来的值没人读」：字段齐全、看起来正常、实际是错的。

优先级（本次固化）：命令行 `--simulator` / `--real` > 该字段 > 默认模拟。
且「配置要求真实模式但设备清单为空」时**响亮地退回模拟**，不让服务起不来。
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope='module')
def run_mod():
    import run          # 导入只做路径/日志初始化，不会启动服务
    return run


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / 'system.yaml'
    p.write_text(body, encoding='utf-8')
    return p


def test_persisted_false_is_read(run_mod, tmp_path, monkeypatch):
    p = _write(tmp_path, 'system:\n  simulation_mode: false\n')
    monkeypatch.setattr(run_mod.paths, 'resolve', lambda rel: p)
    assert run_mod._persisted_simulation_mode() is False


def test_persisted_true_is_read(run_mod, tmp_path, monkeypatch):
    p = _write(tmp_path, 'system:\n  simulation_mode: true\n')
    monkeypatch.setattr(run_mod.paths, 'resolve', lambda rel: p)
    assert run_mod._persisted_simulation_mode() is True


@pytest.mark.parametrize('body', [
    'system:\n  name: x\n',                 # 没有该键
    'system:\n  simulation_mode: "false"\n',  # 字符串不算布尔
    'other: 1\n',                            # 没有 system 段
])
def test_persisted_missing_or_invalid_is_none(run_mod, tmp_path, monkeypatch, body):
    p = _write(tmp_path, body)
    monkeypatch.setattr(run_mod.paths, 'resolve', lambda rel: p)
    assert run_mod._persisted_simulation_mode() is None


def test_persisted_missing_file_is_none(run_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(run_mod.paths, 'resolve',
                        lambda rel: tmp_path / 'not-there.yaml')
    assert run_mod._persisted_simulation_mode() is None


def test_real_devices_ready_false_when_empty(run_mod, tmp_path, monkeypatch):
    p = tmp_path / 'devices_real.yaml'
    p.write_text('devices: []\n', encoding='utf-8')
    monkeypatch.setattr(run_mod.paths, 'get_config_path', lambda name: str(p))
    monkeypatch.setattr(run_mod.paths, 'resolve', lambda rel: Path(rel))
    assert run_mod._real_devices_ready() is False


def test_real_devices_ready_true_when_present(run_mod, tmp_path, monkeypatch):
    p = tmp_path / 'devices_real.yaml'
    p.write_text('devices:\n- id: plc1\n  host: 127.0.0.1\n', encoding='utf-8')
    monkeypatch.setattr(run_mod.paths, 'get_config_path', lambda name: str(p))
    monkeypatch.setattr(run_mod.paths, 'resolve', lambda rel: Path(rel))
    assert run_mod._real_devices_ready() is True


def test_shipped_system_yaml_defaults_to_simulated():
    """开箱配置必须是模拟模式 —— 否则裸跑 `python run.py` 会去连现场设备。"""
    from core.config_io import read_text
    import paths
    text, _ = read_text(paths.resolve('配置/system.yaml'))
    import yaml
    cfg = yaml.safe_load(text)
    assert cfg['system']['simulation_mode'] is True
