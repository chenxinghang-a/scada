# -*- coding: utf-8 -*-
"""长跑内存守卫（round 374）：只 append 不裁剪的历史容器必须有上限。

这些列表每触发一次就多一条 dict，短期测试看不出来；现场 24/7 跑几个月，
内存曲线只涨不降，最后 OOM 重启 —— 而重启后一切正常，极难复现。
"""
from __future__ import annotations

from core.fault_injection import FaultInjector
from 采集层.recipe_simulator import (
    PhaseConfig, Recipe, RecipeSimulator,
)


class TestFaultInjectorHistory:
    def test_history_is_capped(self):
        injector = FaultInjector()
        limit = FaultInjector.HISTORY_LIMIT

        for i in range(limit + 50):
            injector._record_history({'action': 'inject', 'seq': i})

        assert len(injector._history) == limit
        # 保留的是**最近**的，不是最早的
        assert injector._history[-1]['seq'] == limit + 49
        assert injector._history[0]['seq'] == 50

    def test_under_limit_keeps_everything(self):
        injector = FaultInjector()
        for i in range(3):
            injector._record_history({'seq': i})
        assert len(injector._history) == 3


class TestRecipeHistory:
    def test_phase_history_is_capped(self):
        recipe = Recipe(
            name='测试配方', version='1.0',
            phases=[PhaseConfig(name='heating', duration=1.0, setpoints={})],
        )
        sim = RecipeSimulator(recipe)
        sim.HISTORY_LIMIT = 5

        for _ in range(20):
            # 单阶段配方每次推进都会"完成一轮"，复位后继续制造历史
            sim.current_phase_index = 0
            sim._advance_phase()

        assert len(sim._history) == 5
        assert all(entry['phase'] == 'heating' for entry in sim._history)
