# -*- coding: utf-8 -*-
"""``SCADA_CONFIG_DIR`` 的**运行时接线**守卫（round 213，2026-10-13）。

与 `test_env_vars_wired.py` 的分工
================================
那个文件验的是**机制**（静态：「有没有代码读这个变量」）—— round 206 据此宣布
`SCADA_CONFIG_DIR`「已接上」（`paths.CONFIG_DIR` 确实读了它）。

本文件验的是**接线**（运行时：「读了这个变量的那条路，25 个调用点真的走它吗」）：
round 206 只修了**库**那半边（`get_db_path` 吃 env），**配置**那半边没修 ——
`展示层/api/_common.py` 的 `load_yaml_config` / `save_yaml_config` 一直用
`paths.resolve()`（按 BASE_DIR 解析，**不吃 env**）。

缺陷形态（运行时反向对照实测坐实）
--------------------------------
隔离配置里把 `web.cors.origins` 改成只有 `http://marker.invalid`（删掉 `file://`），
启动**打包好的 exe** 后实测：
  `Origin: file://`              → HTTP 200（该值只存在于**仓库**那份配置）
  `Origin: http://marker.invalid` → HTTP 400（该值只存在于**隔离**那份配置）
两条判据同时指向「读的是仓库那份」→ `SCADA_CONFIG_DIR` 被完全忽略，
多实例「配置互相覆盖」照旧存在。

本文件的用例就是把上面那个手工实验**固化成箭头**：
改了隔离配置，助手读写的必须是隔离目录；没改，才是仓库目录。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_BACKEND = pathlib.Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import paths  # noqa: E402


@pytest.fixture
def iso_config_dir(tmp_path, monkeypatch):
    """真实模拟「进程启动时设了 SCADA_CONFIG_DIR」。

    为什么要**两件套**（env + paths.CONFIG_DIR）：
    `paths.CONFIG_DIR` 是 import 时按 env 算出的常量，只改常量不改 env，
    测的是「半真实」状态；而 `resolve_config_file` 按 env 分流 ——
    不设 env 时它走 BASE_DIR 动态分支（那是 test_path_resolution 的 CWD
    独立性用例的领地）。两件套才等价于真实隔离进程。
    """
    iso = tmp_path / 'iso_config'
    iso.mkdir()
    monkeypatch.setenv('SCADA_CONFIG_DIR', str(iso))
    monkeypatch.setattr(paths, 'CONFIG_DIR', iso)
    return iso


def _helpers():
    from 展示层.api._common import load_yaml_config, save_yaml_config
    return load_yaml_config, save_yaml_config


class TestEnvAwareResolution:
    def test_iso_dir_redirects_load(self, iso_config_dir):
        """设了隔离目录后，`load_yaml_config('配置/x.yaml')` 必须读隔离那份。"""
        (iso_config_dir / 'x.yaml').write_text('k: v\n', encoding='utf-8')
        load_yaml_config, _ = _helpers()
        assert load_yaml_config('配置/x.yaml') == {'k': 'v'}, (
            'load_yaml_config 仍读仓库配置 —— SCADA_CONFIG_DIR 又被绕过了')

    def test_iso_dir_redirects_save(self, iso_config_dir):
        """写也必须落到隔离目录（否则「保存配置」写坏的是生产配置）。

        ⚠️ 判据用「**仓库那份内容不变**」而不是「文件不存在」——
        仓库里可能本来就有同名配置（实测 `配置/x.yaml` 就是个遗留文件），
        判据要钉的是「没被本次 save 改坏」。
        """
        repo_copy = _BACKEND / '配置' / 'x.yaml'
        before = repo_copy.read_bytes() if repo_copy.exists() else None
        _, save_yaml_config = _helpers()
        assert save_yaml_config('配置/x.yaml', {'a': 1}) is True
        assert (iso_config_dir / 'x.yaml').exists(), 'save 落到了别处'
        after = repo_copy.read_bytes() if repo_copy.exists() else None
        assert after == before, 'save 写进了仓库配置目录！'

    def test_iso_dir_does_not_touch_repo_copy(self, iso_config_dir):
        """隔离目录存在同名文件时，**仓库那份不得被读**（反向对照的读侧）。"""
        (iso_config_dir / 'alarms.yaml').write_text('from: iso\n', encoding='utf-8')
        load_yaml_config, _ = _helpers()
        cfg = load_yaml_config('配置/alarms.yaml')
        assert cfg == {'from': 'iso'}, f'读到的不是隔离配置: {cfg}'

    def test_absolute_path_untouched(self, iso_config_dir, tmp_path):
        """绝对路径行为不变（测试夹具用 tmp 绝对路径，不能因本修复失效）。"""
        abs_file = tmp_path / 'abs.yaml'
        abs_file.write_text('kk: vv\n', encoding='utf-8')
        load_yaml_config, _ = _helpers()
        assert load_yaml_config(str(abs_file)) == {'kk': 'vv'}

    def test_missing_in_iso_returns_empty(self, iso_config_dir):
        """隔离目录里没有该文件 → 返回 {}（不静默回退到仓库那份）。"""
        load_yaml_config, _ = _helpers()
        assert load_yaml_config('配置/no_such.yaml') == {}


class TestNoEnvKeepsOldBehavior:
    """反向对照：**不设隔离目录时，行为必须与修复前一致**。"""

    def test_resolves_under_base_without_env(self, monkeypatch):
        real = paths.CONFIG_DIR
        monkeypatch.undo()  # 确保没用 iso fixture
        p = paths.resolve_config_file('配置/alarms.yaml')
        assert p == real / 'alarms.yaml'

    def test_roundtrip_still_works(self):
        load_yaml_config, save_yaml_config = _helpers()
        cfg = load_yaml_config('配置/alarms.yaml')
        assert isinstance(cfg, dict) and cfg, '仓库 alarms.yaml 读不到了？'


class TestHelperSemantics:
    def test_backslash_variant(self, iso_config_dir):
        """Windows 风格分隔符（`配置\\x.yaml`）同样算配置区路径。"""
        load_yaml_config, _ = _helpers()
        (iso_config_dir / 'b.yaml').write_text('z: 1\n', encoding='utf-8')
        assert load_yaml_config('配置\\b.yaml') == {'z': 1}

    def test_bare_name_is_not_config_area(self, iso_config_dir):
        """非配置区路径（如 `data/x.yaml`）不走 CONFIG_DIR，保持 resolve 语义。"""
        load_yaml_config, _ = _helpers()
        p = paths.resolve_config_file('data/x.yaml')
        assert p == paths.resolve('data/x.yaml')
