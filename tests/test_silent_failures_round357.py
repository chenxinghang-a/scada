# -*- coding: utf-8 -*-
"""round 357：配置写入与端口来源。

三条都是「看起来正常、其实有坑」的形态：
· 非原子写 → 一次异常就把配置文件截成 0 字节，且抹掉现场人员写的注释；
· 端口硬编码 → 换端口跑就恒定判定「API 不可响应」；
· 裸 `int(os.environ)` → 环境变量写成非数字，模块导入阶段直接崩。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent


def _mock_db():
    from unittest.mock import MagicMock
    return MagicMock()


# ---------------------------------------------------------------------------
# 1. 能耗配置必须走原子写
# ---------------------------------------------------------------------------

def test_energy_save_config_uses_atomic_writer(tmp_path, monkeypatch):
    """`_save_config` 必须调 `core.config_io.dump_yaml`，而不是 `open(...,'w')`。

    直写的三个问题：非原子（异常留 0 字节文件）、抹掉 YAML 注释、
    绕过 `conftest.py::isolate_config_writes` 的测试写盘守卫。
    """
    from 智能层 import energy_manager as mod
    from core import config_io

    cfg = tmp_path / 'energy.yaml'
    calls = []

    def fake_dump(path, data, **kw):
        calls.append((Path(path), data))
        return True

    monkeypatch.setattr(config_io, 'dump_yaml', fake_dump)

    em = mod.EnergyManager(_mock_db(), config_path=str(cfg))
    assert em._save_config() is True
    assert calls, '没有走 config_io.dump_yaml（仍在直接 open+yaml.dump）'
    assert calls[0][0] == cfg


def test_energy_save_config_reports_failure(tmp_path, monkeypatch):
    """写入失败必须返回 False —— 调用方据此回滚内存配置。"""
    from 智能层 import energy_manager as mod
    from core import config_io

    monkeypatch.setattr(config_io, 'dump_yaml', lambda *a, **k: False)
    em = mod.EnergyManager(_mock_db(), config_path=str(tmp_path / 'energy.yaml'))
    assert em._save_config() is False


# ---------------------------------------------------------------------------
# 2. 混沌工程的端口必须来自配置
# ---------------------------------------------------------------------------

def test_chaos_api_check_uses_configured_port(monkeypatch):
    """不能硬编码 5000：真实/模拟器模式跑 5001，自定义 `SCADA_PORT` 又是别的值。"""
    from core import chaos_engineering as mod

    seen = {}

    class FakeSock:
        def settimeout(self, *_a): pass
        def connect_ex(self, addr):
            seen['addr'] = addr
            return 0
        def close(self): pass

    monkeypatch.setattr('socket.socket', lambda *a, **k: FakeSock())
    monkeypatch.setattr('config.FlaskConfig.PORT', 5077, raising=False)

    eng = mod.ChaosEngine()
    assert eng._check_api_responsive() is True
    assert seen['addr'][1] == 5077, f"仍在用硬编码端口: {seen['addr']}"


# ---------------------------------------------------------------------------
# 3. 配置里的数字解析必须走 `_get_int`（源码守卫）
# ---------------------------------------------------------------------------

def test_no_bare_int_on_environ_in_config():
    """`int(os.environ.get(...))` 在环境变量写错时会让**模块导入**直接崩。

    用精确形态匹配（`int(os.environ.get(`）而不是裸 `int(` ——
    避免把注释或 docstring 里的说明当成违规（round 309 那次守卫假红）。
    """
    src = (_REPO / 'config.py').read_text(encoding='utf-8')
    tree = ast.parse(src)

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == 'int'):
            continue
        if node.args and isinstance(node.args[0], ast.Call):
            inner = node.args[0].func
            if (isinstance(inner, ast.Attribute) and inner.attr == 'get'
                    and isinstance(inner.value, ast.Attribute)
                    and inner.value.attr == 'environ'):
                offenders.append(node.lineno)

    assert not offenders, (
        f"config.py 第 {offenders} 行仍在用裸 int(os.environ.get(...)) —— "
        "改用 _get_int(name, default, min_val=, max_val=)")
