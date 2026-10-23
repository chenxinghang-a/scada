# -*- coding: utf-8 -*-
"""告警去重 / 洪水抑制的窗口计时必须用**单调时钟**（round 258）。

缺陷形态
--------
去重窗口（冷却 300s、确认后抑制 600s）与洪水检测滑动窗口（60s）表达的都是
**时长**，而原实现用 `time.time()`（墙钟）算。系统时间被改动一次就整体错位：

* **向后跳**（NTP 校时、现场机无电池开机后校时、手工改时间）：
  `now` 比记录的 `ts` 还小 → `now - ts` 为负 → 记录永不过期；
  抑制窗口 `suppress_until = ts + 600` 远在未来 → **这些报警被静默抑制
  整整一个跳变量**。洪水抑制更惨：窗口永远算不出"过期"→
  抑制**永久卡在激活态**，所有低于阈值的报警被吞掉。
* **向前跳**：窗口瞬间失效，同一报警重复轰炸。

最坏的后果是「操作员确认过的那条报警，校时之后再也没弹过」——
现场等于漏掉一条已确认、但故障仍未恢复的报警。

判据：**时长用 monotonic，时刻用 wall clock**。凡是"记录一个时间点、
之后拿它做差"的地方，先问一句：这个差值怕不怕系统时间被改？
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ALARM_MANAGER = Path(__file__).resolve().parents[1] / "报警层" / "alarm_manager.py"


def _strip_comments(src: str) -> str:
    """剥掉 `#` 注释 —— 否则解释「原来用 time.time()」的注释会把负向断言喂饱。"""
    return re.sub(r"#.*$", "", src, flags=re.M)


@pytest.fixture
def mgr():
    from 报警层.alarm_manager import AlarmManager

    db = MagicMock()
    db.get_alarm_records.return_value = []
    db.get_active_alarms.return_value = []
    m = AlarmManager(db)
    yield m
    try:
        m.stop()
    except Exception:      # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 1. 去重窗口：墙钟回拨不影响抑制时长
# ---------------------------------------------------------------------------

def test_ack_suppression_survives_backward_clock_jump(mgr, monkeypatch):
    """确认后抑制窗口只认单调时钟：墙钟回拨 8 小时，抑制不该被拉长。

    推进的是**被测对象自己的时钟缝** `mgr._clock`，不是全局
    `time.monotonic`（那是进程级的，会把后台线程的 `Event.wait(timeout)`
    一起改掉，teardown join 直接卡死 —— 本轮实测踩过）。
    """
    key = ('r1', 'd1', 'temp')
    mgr.dedup_config.enabled = True
    mgr.dedup_config.acknowledge_suppress_seconds = 600

    mgr._record_acknowledge(*key)          # 记录确认时刻

    base = mgr._clock()
    # 墙钟被回拨 8 小时（单调时钟不动）
    monkeypatch.setattr('time.time', lambda: 1_000_000.0)
    assert mgr._should_emit(*key) is False, '抑制窗口内本就该抑制'

    # 单调时钟推进 601 秒 → 必须放行
    mgr._clock = lambda: base + 601
    assert mgr._should_emit(*key) is True, (
        '单调时钟已过 600s 却仍被抑制 —— 说明窗口还在用墙钟，'
        '墙钟回拨会把抑制窗口拉长整个跳变量')


def test_cooldown_survives_backward_clock_jump(mgr, monkeypatch):
    """冷却窗口同理：回拨后不该变成"永久冷却"。"""
    key = ('r2', 'd1', 'temp')
    mgr.dedup_config.enabled = True
    mgr.dedup_config.emit_cooldown_seconds = 300

    mgr._record_emit(*key)
    base = mgr._clock()
    monkeypatch.setattr('time.time', lambda: 1_000_000.0)

    assert mgr._should_emit(*key) is False
    mgr._clock = lambda: base + 301
    assert mgr._should_emit(*key) is True


def test_dedup_clock_is_monotonic(mgr):
    """`_dedup_now()` 必须是单调时钟（否则上面两条只是碰巧过）。"""
    import time as _time
    a = mgr._dedup_now()
    b = _time.monotonic()
    assert abs(b - a) < 5.0, f'_dedup_now()={a!r} 与 monotonic()={b!r} 相差过大'
    wall = _time.time()
    assert abs(wall - a) > 1.0 or abs(wall - b) < 1.0, (
        '`_dedup_now()` 看起来返回的是墙钟 —— 去重窗口会在校时后错乱')


# ---------------------------------------------------------------------------
# 2. 洪水抑制：墙钟回拨不该让它永久卡住
# ---------------------------------------------------------------------------

def test_flood_window_expires_after_backward_jump(monkeypatch):
    """洪水检测的滑动窗口也必须按单调时钟过期。"""
    from 报警层.alarm_manager import AlarmFloodDetector

    det = AlarmFloodDetector(window_seconds=60, threshold=3,
                             suppress_below='critical')
    # 打满窗口
    for _ in range(5):
        det.record_alarm('warning')
    assert det._flood_active is True

    # 墙钟回拨 8 小时，然后被测对象自己的时钟推进到窗口之外
    base = det._clock()
    monkeypatch.setattr('time.time', lambda: 1_000_000.0)
    det._clock = lambda: base + 3600

    det.check_flood_end()
    assert det._flood_active is False, (
        '窗口早已过期，洪水抑制却仍处于激活态 —— 墙钟回拨会让它永久卡住，'
        '之后所有低级别报警都被静默吞掉')


def test_flood_timestamps_are_monotonic():
    """洪水检测存的时间戳必须是单调时钟（与墙钟差 > 1 天即视为用错时钟）。"""
    import time as _time
    from 报警层.alarm_manager import AlarmFloodDetector

    det = AlarmFloodDetector(window_seconds=60, threshold=3)
    det.record_alarm('warning')
    ts = det._timestamps[-1]
    assert abs(_time.monotonic() - ts) < 5.0, (
        f'洪水检测记录的时间戳 {ts!r} 不是 monotonic（差 '
        f'{abs(_time.monotonic() - ts):.0f}s）—— 校时后窗口会算错')

# ---------------------------------------------------------------------------
# 3. 静态守卫：这些窗口计算里不许再出现墙钟
# ---------------------------------------------------------------------------

def test_no_wall_clock_in_window_math():
    """`alarm_manager.py` 里"算时长"的地方不许用 `time.time()`。"""
    code = _strip_comments(ALARM_MANAGER.read_text(encoding='utf-8'))

    forbidden = [
        (r'now\s*=\s*time\.time\(\)', 'now = time.time()'),
        (r'_last_shelf_cleanup[^\n]*=\s*0\b', '_last_shelf_cleanup = 0（应用 -inf）'),
    ]
    hits = [label for pattern, label in forbidden if re.search(pattern, code)]
    assert not hits, (
        '告警窗口/节流里又出现了墙钟：' + '、'.join(hits) + '\n'
        '  时长（窗口、节流、超时）一律用 time.monotonic()；'
        '墙钟只用于"记录时刻"（落库、展示）。'
    )


def test_ack_duration_metric_still_uses_wall_clock():
    """反面：**确认时长指标**必须继续用墙钟 —— 它要和 `first_trigger_time`
    （datetime，落库的绝对时刻）相减，换成单调时钟会算出天文数字。"""
    src = ALARM_MANAGER.read_text(encoding='utf-8')
    assert 'ALARM_DURATION.labels' in src
    idx = src.index('ALARM_DURATION.labels')
    window = src[max(0, idx - 800):idx]
    assert 'datetime.now()' in window, (
        '报警持续时长的计算不再用 datetime.now() —— 它与 first_trigger_time '
        '（datetime）相减，必须同为墙钟')
    assert isinstance(datetime.now(), datetime)
