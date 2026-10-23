# -*- coding: utf-8 -*-
"""`配置/system.yaml` 的**旋钮契约**守卫（round 210）。

缺陷类别：**配置旋钮是装饰品，甚至与生效值对不上**
================================================
实测（2026-10-10）：`配置/system.yaml` 的 `collection` 段**整段零读取点** ——
生产代码里没有任何地方读 `default_interval` / `max_concurrent` /
`retry.*` / `timeout`。而且：

* `collection.max_concurrent: 10` vs `采集层/data_collector.py` 的
  **硬编码 `max_workers=20`** —— **两边数值都对不上**（配置那侧没人读，所以没人发现）；
* `collection.default_interval: 5` vs 硬编码 `device_config.get('collection_interval', 5)`
  —— 数值**恰好一样**，所以这个死旋钮更难被发现；
* `collection.retry.interval_seconds / max_attempts` 描述的策略**在代码里不存在**
  （实际是**指数退避** `min(2 ** failures, BACKOFF_CEILING_S=60)`）；
* `collection.timeout` 也不是系统级旋钮（实际超时在**设备/客户端**配置里）。

处置：把真正有意义的两个接上（`max_concurrent` / `default_interval`），
把描述不存在策略的三个删掉。本文件钉住「接上的不许再断」与「不许再长出装饰品」。

口径（为什么用**闭集**而不是模糊扫描）
--------------------------------------
「键名在代码里出现过」这个判据**不可靠**：`check_interval` / `timeout` /
`max_rows` 这类键在**别的**配置字典里也被读（如 `报警层/alarm_escalation.py`
读它自己的 `config.get('check_interval', 30)`），所以「出现过」不等于
「`system.yaml` 的这一个被读了」。
→ 改用**策展闭集**：`collection` 段允许出现的键 = :data:`COLLECTION_DEFAULTS`
的键集合。要新增旋钮，必须同时（a）接上读取点、（b）加进那个表。
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

BACKEND_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_YAML = BACKEND_ROOT / "配置" / "system.yaml"
CONFIG_MANAGER = BACKEND_ROOT / "core" / "config_manager.py"
COLLECTOR = BACKEND_ROOT / "采集层" / "data_collector.py"


def _strip_comments(src: str) -> str:
    """剥掉 `#` 注释。

    ⚠️ 必须剥：本轮的注释里就写着「此前是硬编码 20」这类**说明文字**，
    不剥的话下面那条「不得出现 `max_workers=<数字>`」的负向断言会被
    自己的注释喂饱/误伤（round 191/197/199/203/207 反复踩）。
    """
    return re.sub(r"#.*$", "", src, flags=re.M)


@pytest.fixture(scope="module")
def system_cfg() -> dict:
    return yaml.safe_load(SYSTEM_YAML.read_text(encoding="utf-8")) or {}


class TestCollectionSectionIsAClosedSet:
    def test_config_section_matches_the_curated_table(self, system_cfg):
        """`collection` 段允许出现的键 = `COLLECTION_DEFAULTS` 的键集合。

        这是本守卫的核心：**新增旋钮必须同时接上读取点**，
        否则这里会红（而不是又长出一个没人读的装饰品）。
        """
        from core.config_manager import COLLECTION_DEFAULTS
        section = system_cfg.get("collection")
        assert isinstance(section, dict), "配置/system.yaml 里没有 collection 段"
        assert set(section) == set(COLLECTION_DEFAULTS), (
            f"collection 段的键与策展表不一致：\n"
            f"  配置里多出（没接读取点的装饰品？）：{sorted(set(section) - set(COLLECTION_DEFAULTS))}\n"
            f"  表里有但配置缺失：{sorted(set(COLLECTION_DEFAULTS) - set(section))}\n"
            "  新增旋钮请：① 接上读取点；② 加进 core/config_manager.py 的 COLLECTION_DEFAULTS。"
        )

    def test_fallback_defaults_equal_the_config_file(self, system_cfg):
        """兜底默认值必须**等于**配置文件里的值 —— 否则就是第二个真源。"""
        from core.config_manager import COLLECTION_DEFAULTS
        section = system_cfg["collection"]
        for key, fallback in COLLECTION_DEFAULTS.items():
            assert fallback == section[key], (
                f"COLLECTION_DEFAULTS['{key}']={fallback} 与 配置/system.yaml 的 "
                f"{section[key]} 不一致 —— 配置读不到时行为会与配置写明时不同"
            )

    def test_every_key_is_actually_read(self):
        """策展表里的每个键，`load_collection_settings` 都必须去读它。

        ⚠️ 判据刻意**不写成**「逐个字面量 `section.get('max_concurrent')`」——
        `load_collection_settings` 是**表驱动循环**：

            for key, fallback in COLLECTION_DEFAULTS.items():
                raw = section.get(key)

        第一版按字面量判，于是对**正确**的代码报红（判形状不判意图，
        与 round 208 那条 `setTimeout(markLaunchSucceeded, …)` 同类）。
        现在判的是**循环覆盖整张表**：循环变量取自 `COLLECTION_DEFAULTS.items()`、
        循环体里用 `section.get(key)` —— 这比逐键字面量**更强**
        （往表里加键会自动被读，不会漏）。
        配合上面那条「配置键集合 == 表键集合」，两端就都闭住了。
        """
        code = _strip_comments(CONFIG_MANAGER.read_text(encoding="utf-8"))
        assert re.search(
            r"for\s+\w+\s*,\s*\w+\s+in\s+COLLECTION_DEFAULTS\.items\(\)", code), (
            "load_collection_settings 没有遍历 COLLECTION_DEFAULTS —— "
            "表里的键不保证被读"
        )
        assert re.search(r"section\.get\(\s*key\s*\)", code), (
            "遍历了表却没有用 section.get(key) 取值 —— 读了个寂寞"
        )


class TestCollectorUsesTheKnobs:
    def test_pool_size_comes_from_the_knob_not_a_literal(self):
        code = _strip_comments(COLLECTOR.read_text(encoding="utf-8"))
        assert re.search(r"max_workers\s*=\s*self\._max_concurrent", code), (
            "线程池上限不是来自 self._max_concurrent（配置旋钮又断了）"
        )
        assert not re.search(r"max_workers\s*=\s*\d+", code), (
            "又出现了 max_workers=<数字> 的硬编码 —— 配置里的 collection.max_concurrent 会再次失效"
        )

    def test_device_fallback_interval_comes_from_the_knob(self):
        code = _strip_comments(COLLECTOR.read_text(encoding="utf-8"))
        assert re.search(r"collection_interval'\s*,\s*self\._default_interval", code), (
            "设备缺 collection_interval 时的兜底不是来自 self._default_interval"
        )
        assert not re.search(r"collection_interval'\s*,\s*\d+", code), (
            "又出现了 collection_interval', <数字> 的硬编码 —— "
            "配置里的 collection.default_interval 会再次失效"
        )

    def test_collector_really_picks_up_the_configured_value(self, monkeypatch):
        """**真跑**：把配置改成别的值，`DataCollector` 必须取到新值。"""
        from core import config_manager as cm
        monkeypatch.setattr(cm, "load_collection_settings",
                            lambda: {"max_concurrent": 7, "default_interval": 13})
        # data_collector 是在 __init__ 里 `from core.config_manager import ...` 的，
        # 所以直接打桩模块属性即可生效。
        from 采集层.data_collector import DataCollector
        c = DataCollector(MagicMock(), MagicMock(), MagicMock())
        try:
            assert c._max_concurrent == 7, "线程池上限没跟着配置走"
            assert c._default_interval == 13, "兜底间隔没跟着配置走"
        finally:
            c.stop() if hasattr(c, "stop") else None


class TestLoadCollectionSettingsIsFailSafe:
    def test_missing_section_returns_fallbacks(self, monkeypatch):
        from core import config_manager as cm
        monkeypatch.setattr(cm, "load_system_config", lambda *a, **k: {})
        assert cm.load_collection_settings() == dict(cm.COLLECTION_DEFAULTS)

    @pytest.mark.parametrize("bad", ["0", "-3", "abc", None])
    def test_illegal_values_fall_back(self, monkeypatch, bad):
        from core import config_manager as cm
        monkeypatch.setattr(cm, "load_system_config",
                            lambda *a, **k: {"collection": {"max_concurrent": bad,
                                                            "default_interval": 9}})
        out = cm.load_collection_settings()
        assert out["max_concurrent"] == cm.COLLECTION_DEFAULTS["max_concurrent"]
        assert out["default_interval"] == 9

    def test_reads_the_real_config(self):
        """防瞎：真仓库里 `collection` 段必须真的被解析出两个键。"""
        from core.config_manager import load_collection_settings
        out = load_collection_settings()
        assert set(out) == {"max_concurrent", "default_interval"}
        assert all(isinstance(v, int) and v > 0 for v in out.values())


# ===========================================================================
# round 257：`performance.connection_pool` 接线 + 「不许再长装饰段」
# ===========================================================================

DEVICE_MANAGER = BACKEND_ROOT / "采集层" / "device_manager.py"

#: `配置/system.yaml` **允许存在**的顶层段 → 谁在真正读它。
#:
#: 加段必须同时给读取点：这个表是闭集，多一段就红。
#: （`alarm` / `export` / `mqtt` 在 round 257 被删掉了，见各自在配置文件里的说明。）
ALLOWED_SECTIONS: dict[str, str] = {
    "collection": "core/config_manager.load_collection_settings → 采集层/data_collector",
    "database": "core/retention_scheduler.read_retention_config（保留策略唯一真源）",
    "logging": "配置页 GET/PUT /api/config 的展示与保存（前端 Config.vue）",
    "performance": "core/config_manager.load_connection_pool_settings → 采集层/device_manager",
    "system": "run.py（simulation_mode）+ 配置页展示",
    "web": "展示层/websocket._load_cors_origins（socket.io 白名单）",
}


class TestConnectionPoolSectionIsWired:
    def test_defaults_equal_the_config_file(self, system_cfg):
        """兜底默认值必须等于配置文件里的值，否则就是第二个真源。"""
        from core.config_manager import CONNECTION_POOL_DEFAULTS

        section = system_cfg["performance"]["connection_pool"]
        assert set(section) == set(CONNECTION_POOL_DEFAULTS), (
            f"connection_pool 段的键与策展表不一致：\n"
            f"  配置里多出：{sorted(set(section) - set(CONNECTION_POOL_DEFAULTS))}\n"
            f"  表里有但配置缺失：{sorted(set(CONNECTION_POOL_DEFAULTS) - set(section))}"
        )
        for key, expected in CONNECTION_POOL_DEFAULTS.items():
            assert section[key] == expected, (
                f"performance.connection_pool.{key}={section[key]!r} 与兜底默认 "
                f"{expected!r} 不一致 —— 两处都是真源，会悄悄漂移")

    def test_old_ambiguous_timeout_key_is_gone(self, system_cfg):
        """`timeout: 30` 语义不明（建连超时？空闲回收？）且没人读，已删。"""
        section = system_cfg["performance"]["connection_pool"]
        assert "timeout" not in section

    def test_device_manager_has_no_hardcoded_pool_size(self):
        """`max_size=<数字>` 的硬编码不许再出现 —— 否则配置又会失效。"""
        code = _strip_comments(DEVICE_MANAGER.read_text(encoding="utf-8"))
        assert not re.search(r"max_size\s*=\s*\d+", code), (
            "device_manager 里又出现了 max_size=<数字> 的硬编码 —— "
            "配置里的 performance.connection_pool.size 会再次失效")
        assert "load_connection_pool_settings()" in code, (
            "device_manager 没有调用 load_connection_pool_settings()")

    def test_device_manager_really_uses_the_values(self, monkeypatch):
        """把配置改成一组怪值，构造出来的连接池必须跟着变。"""
        from core import config_manager as cm
        import 采集层.device_manager as dm_mod

        monkeypatch.setattr(cm, "load_connection_pool_settings",
                            lambda: {"size": 7, "idle_timeout_seconds": 11,
                                     "max_lifetime_seconds": 13})
        monkeypatch.setattr(dm_mod, "load_connection_pool_settings",
                            cm.load_connection_pool_settings)
        mgr = dm_mod.DeviceManager(config_path=str(BACKEND_ROOT / "配置" / "devices.yaml"),
                                   simulation_mode=True)
        try:
            pool = mgr._connection_pool
            assert pool._max_size == 7 or getattr(pool, "max_size", None) == 7, (
                f"连接池容量没跟着配置走: {pool.__dict__}")
        finally:
            try:
                mgr.shutdown()
            except Exception:      # noqa: BLE001
                pass

    def test_load_settings_falls_back(self, monkeypatch):
        from core import config_manager as cm
        monkeypatch.setattr(cm, "load_system_config", lambda *a, **k: {})
        assert cm.load_connection_pool_settings() == dict(cm.CONNECTION_POOL_DEFAULTS)

    @pytest.mark.parametrize("bad", ["0", "-1", "abc", None, "inf"])
    def test_load_settings_illegal_values(self, monkeypatch, bad):
        from core import config_manager as cm
        monkeypatch.setattr(cm, "load_system_config", lambda *a, **k: {
            "performance": {"connection_pool": {"size": bad,
                                                "idle_timeout_seconds": 21,
                                                "max_lifetime_seconds": 22}}})
        out = cm.load_connection_pool_settings()
        assert out["size"] == cm.CONNECTION_POOL_DEFAULTS["size"]
        assert out["idle_timeout_seconds"] == 21

    def test_reads_the_real_config(self):
        from core.config_manager import load_connection_pool_settings
        out = load_connection_pool_settings()
        assert set(out) == {"size", "idle_timeout_seconds", "max_lifetime_seconds"}
        assert all(isinstance(v, int) and v > 0 for v in out.values())


class TestNoDecorativeSections:
    """`配置/system.yaml` 的顶层段必须是**闭集**，且每段都有读取点。"""

    def test_top_level_sections_are_curated(self, system_cfg):
        actual = set(system_cfg)
        assert actual == set(ALLOWED_SECTIONS), (
            f"system.yaml 顶层段与策展表不一致：\n"
            f"  多出（谁在读它？没有就删掉，别当装饰品）：{sorted(actual - set(ALLOWED_SECTIONS))}\n"
            f"  表里有但配置缺失：{sorted(set(ALLOWED_SECTIONS) - actual)}\n"
            "  新增段请：① 接上真实读取点；② 加进 ALLOWED_SECTIONS 并写明读它的位置。"
        )

    def test_deleted_decorative_keys_stay_deleted(self, system_cfg):
        """round 257 删掉的假旋钮不许悄悄回来（每个都有真实替代品）。"""
        assert "alarm" not in system_cfg, (
            "alarm 段是装饰品：检查间隔按样本触发、确认超时在规则里、"
            "升级超时在 alarms.yaml 的 escalation.rules")
        assert "export" not in system_cfg, (
            "export 段是装饰品：导出目录 = paths.EXPORT_DIR，格式/上限由导出器自己校验")
        assert "cache" not in (system_cfg.get("performance") or {}), (
            "缓存模块在 round 247 已删除，performance.cache 承诺的是不存在的功能")
