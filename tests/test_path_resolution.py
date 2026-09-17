"""项目路径解析回归测试。

背景（2026-09 审计 P2-5）：
代码库里散落着 ``'配置/alarms.yaml'``、``'data/scada.db'`` 这类相对路径字面量，
它们**只在「当前工作目录恰好是项目根目录」时才成立**。从别处启动
（系统服务、计划任务、PyInstaller 产物、被别的模块 import）就找不到文件。

而失败方式往往是**静默**的：``load_yaml_config`` 读不到文件返回 ``{}``，
接口照常回 200，只是配置"莫名变空"；``DataCleaner`` 拿到 ``None`` 路径
抛 TypeError 被吞成 ``{'status':'error'}`，再被包成 HTTP 200。

本测试用「切换到别的 CWD」来复现这个前提，锁死 ``paths.resolve`` 的行为。
"""
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import paths  # noqa: E402


@pytest.fixture
def elsewhere(tmp_path):
    """切到一个与项目根无关的临时目录，结束后恢复。"""
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(old)


def test_base_dir_is_project_root():
    """BASE_DIR 必须由 __file__ 推导，与 CWD 无关"""
    assert paths.BASE_DIR == PROJECT_ROOT
    assert (paths.BASE_DIR / '配置').is_dir()
    assert (paths.BASE_DIR / 'paths.py').is_file()


def test_resolve_absolute_passthrough():
    """绝对路径原样返回，不做拼接"""
    p = paths.resolve('C:/tmp/whatever' if os.name == 'nt' else '/tmp/whatever')
    assert p.is_absolute()


def test_resolve_relative_ignores_cwd(elsewhere):
    """相对路径按项目根解析 —— 即使 CWD 已被换到别处"""
    assert Path.cwd() == elsewhere
    resolved = paths.resolve('配置/alarms.yaml')
    assert resolved.is_absolute()
    assert resolved == PROJECT_ROOT / '配置' / 'alarms.yaml'
    assert resolved.exists(), '解析结果必须指向真实存在的配置文件'


def test_load_yaml_config_works_from_other_cwd(elsewhere):
    """核心回归：从别的 CWD 也能读到配置（修复前会返回 {} 而静默失败）"""
    from 展示层.api._common import load_yaml_config

    config = load_yaml_config('配置/alarms.yaml')
    assert config, (
        f'从 CWD={Path.cwd()} 读取 配置/alarms.yaml 得到空配置 —— '
        '说明相对路径没有按项目根解析'
    )
    assert isinstance(config, dict)


def test_save_yaml_config_writes_to_project_root(elsewhere, tmp_path):
    """保存也必须落到项目根下的目标文件，而不是当前 CWD"""
    from 展示层.api._common import save_yaml_config, load_yaml_config

    rel = '配置/.pytest_write_probe.yaml'
    target = PROJECT_ROOT / rel
    try:
        assert save_yaml_config(rel, {'probe': 1}) is True
        # 落点必须在项目根，不能在 CWD
        assert target.exists(), '文件没有写到项目根下'
        assert not (tmp_path / rel).exists(), '文件被错误地写到了当前工作目录'
        assert load_yaml_config(rel) == {'probe': 1}
    finally:
        target.unlink(missing_ok=True)


def test_get_config_path_helper():
    """paths.get_config_path 返回绝对路径"""
    p = paths.get_config_path('alarms.yaml')
    assert Path(p).is_absolute()
    assert Path(p).name == 'alarms.yaml'


def test_db_paths_are_absolute():
    """DB_PATHS 全部为绝对路径（避免 CWD 依赖）"""
    for mode, p in paths.DB_PATHS.items():
        assert Path(p).is_absolute(), f'{mode} 的库路径不是绝对路径: {p}'
