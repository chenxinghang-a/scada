# -*- coding: utf-8 -*-
"""`auto_alerts` 产物死胡同的**反向对照**（round 213，2026-10-13）。

缺陷链（只读核实，工具文档已写明）
================================
`tools/auto_alerts.py generate` 产出 `配置/alarms_generated.yaml`，
而**没有任何代码读它**。更关键的是它**光改文件名也接不上** ——
差三样：文件名 / 顶层键 / 合并策略（详见 auto_alerts.py 头注）。

本文件做的事：把「产物确实喂不进告警引擎」钉成箭头，
并给「能喂进去的格式长什么样」做正向对照 —— 否则后人会以为
「告警规则自动在跑」，而实际前端规则永远是人工维护的那几条。

这不是缺陷修复（修复 = 三选一接线，属功能决策，等主人拍板），
是**把死胡同钉住**，防止它被误当作活能力。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_BACKEND = pathlib.Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _load_alarm_manager(monkeypatch, config_path: pathlib.Path):
    """用最小桩构造 AlarmManager（不起线程、不连库）。"""
    from unittest.mock import MagicMock

    from 报警层.alarm_manager import AlarmManager
    import paths

    monkeypatch.setattr(paths, "CONFIG_DIR", config_path.parent)
    am = AlarmManager.__new__(AlarmManager)          # 跳过 __init__（会拉 database）
    am.database = MagicMock()
    am.config_path = str(config_path)
    am.rules = {}
    am.alarm_states = {}
    am.dedup_config = None
    am.logger = __import__("logging").getLogger("test_auto_alerts")
    am._rebuild_rules_index = lambda: None
    return am


class TestProductIsDeadEnd:
    """负向：auto_alerts 的产物喂不进 AlarmManager。"""

    def _generate(self, tmp_path):
        """跑一次真实生成（不 mock 生成逻辑本身）。"""
        import tools.auto_alerts as aa
        assert hasattr(aa, "AlertRuleGenerator"), (
            "auto_alerts.AlertRuleGenerator 没了 —— 本测试的定位失效，请更新")
        gen = aa.AlertRuleGenerator()
        rules = gen.generate_all_rules()
        out = tmp_path / "alarms_generated.yaml"
        gen.save_rules(rules, str(out))
        return out, rules

    def test_generated_file_is_not_readable_as_engine_config(self, tmp_path, monkeypatch):
        """产物即便改名叫 alarms.yaml，引擎也读不到规则（顶层键不符）。"""
        out, rules = self._generate(tmp_path)
        assert out.exists() and rules, "生成器应该产出东西（否则测的不是死胡同）"

        import yaml
        data = yaml.safe_load(out.read_text(encoding="utf-8"))
        # 口径 1：顶层键是 'rules'（不是引擎读的 'alarm_rules'）
        assert "rules" in data and "alarm_rules" not in data, (
            f"产物顶层键变了（现在是 {sorted(data)}）—— "
            "若已改成 alarm_rules，请同步更新本测试与 auto_alerts 头注")

        # 把产物**改名**成引擎的配置名，再喂给引擎 → 规则仍是一条都不加载
        renamed = tmp_path / "配置" / "alarms.yaml"
        renamed.parent.mkdir(exist_ok=True)
        renamed.write_text(out.read_text(encoding="utf-8"), encoding="utf-8")

        am = _load_alarm_manager(monkeypatch, renamed)
        am.load_config()
        assert am.rules == {}, (
            f"改名后的产物居然加载出了 {len(am.rules)} 条规则 —— "
            "说明接线已经改变，请把 auto_alerts 头注从「死胡同」改成实际状态")


class TestEngineReadableFormatIsDocumented:
    """正向对照：引擎**能**读的格式长什么样（证明不是引擎坏了）。"""

    def test_engine_loads_alarm_rules_key(self, tmp_path, monkeypatch):
        cfg = tmp_path / "配置" / "alarms.yaml"
        cfg.parent.mkdir(exist_ok=True)
        cfg.write_text(
            "alarm_output:\n  enabled: true\n"
            "alarm_rules:\n"
            "  - id: t1_high\n"
            "    device_id: t1\n"
            "    register_name: temp\n"
            "    condition: greater_than\n"
            "    threshold: 90\n"
            "    level: critical\n",
            encoding="utf-8")
        am = _load_alarm_manager(monkeypatch, cfg)
        am.load_config()
        assert "t1_high" in am.rules, "引擎读 alarm_rules 的路径坏了？（这会是真的缺陷）"
