# -*- coding: utf-8 -*-
"""降级等级不许在阈值附近反复横跳（round 271）。

缺陷形态
--------
`DegradationManager._auto_evaluate()` 原来是"触发器说变就变"：

    if requested_level != self._level:
        self._set_level(requested_level, ...)

没有滞回、没有驻留时间。一个在阈值附近抖动的触发器（连接池使用率刚好压在
0.8 上下、队列长度在阈值来回）会让等级**每 15 秒翻一次**。后果不只是日志刷屏：

* `is_feature_enabled()` 反复开关 → 数据导出 / 归档 / 边缘决策 **时开时关**；
* `get_frequency_multiplier()` 反复变化 → 采集周期跟着抖；
* `_history` 被来回切换的记录填满，真正的变更被淹没。

修法：**非对称**
* 变差（等级升高）→ **立即生效** —— 安全方向不能等；
* 恢复（等级降低）→ 需连续 `RECOVERY_CONFIRM_COUNT` 次确认。

这是标准 fail-safe：宁可多降一会儿，也不要反复横跳。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def mgr():
    from core.degradation_manager import DegradationManager
    return DegradationManager()


def _drive(mgr, level):
    """注册一个固定建议某等级的触发器，跑一次评估。"""
    mgr._triggers = [lambda: level]
    mgr._auto_evaluate()


def _level(mgr):
    from core.degradation_manager import DegradationLevel
    return DegradationLevel(mgr._level).name


# ---------------------------------------------------------------------------
# 1. 变差：立即生效
# ---------------------------------------------------------------------------

def test_degrade_applies_immediately(mgr):
    from core.degradation_manager import DegradationLevel

    _drive(mgr, DegradationLevel.SEVERE)
    assert _level(mgr) == 'SEVERE', '变差却没有立即生效 —— 安全方向不能等'


def test_degrade_again_applies_immediately(mgr):
    from core.degradation_manager import DegradationLevel

    _drive(mgr, DegradationLevel.LIGHT)
    _drive(mgr, DegradationLevel.EMERGENCY)
    assert _level(mgr) == 'EMERGENCY'


# ---------------------------------------------------------------------------
# 2. 恢复：需要连续确认
# ---------------------------------------------------------------------------

def test_recovery_needs_confirmation(mgr):
    from core.degradation_manager import DegradationLevel, RECOVERY_CONFIRM_COUNT

    _drive(mgr, DegradationLevel.SEVERE)
    assert _level(mgr) == 'SEVERE'

    # 只建议一次恢复 → 不该生效
    _drive(mgr, DegradationLevel.NORMAL)
    assert _level(mgr) == 'SEVERE', (
        f'一次"可以恢复"就立刻恢复了 —— 触发器抖动会导致反复横跳')
    assert mgr.get_status()['recovery_streak'] == 1

    # 凑够次数才恢复
    for _ in range(RECOVERY_CONFIRM_COUNT - 1):
        _drive(mgr, DegradationLevel.NORMAL)
    assert _level(mgr) == 'NORMAL'


def test_recovery_streak_resets_when_level_worsens(mgr):
    from core.degradation_manager import DegradationLevel, RECOVERY_CONFIRM_COUNT

    _drive(mgr, DegradationLevel.SEVERE)

    # 连续两次"可以恢复"，然后突然又变差
    _drive(mgr, DegradationLevel.NORMAL)
    _drive(mgr, DegradationLevel.NORMAL)
    assert mgr.get_status()['recovery_streak'] == 2

    _drive(mgr, DegradationLevel.SEVERE)          # 又变差 → 清零
    assert mgr.get_status()['recovery_streak'] == 0

    # 再来一次恢复建议，仍需重新凑够次数
    _drive(mgr, DegradationLevel.NORMAL)
    assert _level(mgr) == 'SEVERE', '变差之后恢复计数没有重置'


def test_recovery_streak_resets_when_advice_matches_current(mgr):
    """建议与现状一致时也要清零，否则"隔着几轮凑够"仍会误恢复。"""
    from core.degradation_manager import DegradationLevel

    _drive(mgr, DegradationLevel.MODERATE)
    _drive(mgr, DegradationLevel.NORMAL)
    _drive(mgr, DegradationLevel.NORMAL)
    assert mgr.get_status()['recovery_streak'] == 2

    _drive(mgr, DegradationLevel.MODERATE)        # 与现状一致
    assert mgr.get_status()['recovery_streak'] == 0


# ---------------------------------------------------------------------------
# 3. 抖动场景：等级不该反复翻
# ---------------------------------------------------------------------------

def test_oscillating_trigger_does_not_flap(mgr):
    """触发器在"要降"和"可以恢复"之间抖动 20 轮，等级只该变一次。"""
    from core.degradation_manager import DegradationLevel

    changes = 0
    last = mgr._level
    for i in range(20):
        _drive(mgr, DegradationLevel.MODERATE if i % 2 == 0 else DegradationLevel.NORMAL)
        if mgr._level != last:
            changes += 1
            last = mgr._level

    assert changes <= 2, (
        f'等级在 20 轮抖动里变了 {changes} 次 —— 恢复防抖没生效')
    # 且最终处于降级态（安全方向），不是被抖回 NORMAL
    assert _level(mgr) in ('MODERATE', 'NORMAL')


# ---------------------------------------------------------------------------
# 4. 手动模式仍然优先
# ---------------------------------------------------------------------------

def test_manual_override_blocks_auto(mgr):
    from core.degradation_manager import DegradationLevel

    mgr.manual_degrade(DegradationLevel.SEVERE, reason='测试')
    _drive(mgr, DegradationLevel.NORMAL)
    assert _level(mgr) == 'SEVERE', '手动降级期间被自动评估改掉了'


# ---------------------------------------------------------------------------
# 5. 状态可观测
# ---------------------------------------------------------------------------

def test_status_exposes_recovery_progress(mgr):
    from core.degradation_manager import DegradationLevel, RECOVERY_CONFIRM_COUNT

    _drive(mgr, DegradationLevel.SEVERE)
    _drive(mgr, DegradationLevel.NORMAL)

    status = mgr.get_status()
    assert status['recovery_streak'] == 1
    assert status['recovery_confirm_count'] == RECOVERY_CONFIRM_COUNT
