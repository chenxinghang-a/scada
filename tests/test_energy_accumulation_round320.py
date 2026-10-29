# -*- coding: utf-8 -*-
"""round 320：能耗累计的**口径** —— 窗口、跨重启、双通道重复计。

## 这一轮查出来的三件事（前两件本轮修，第三件记进报告）

### 1. 累计量没有任何窗口，重启即归零

`reset_accumulated()` 的 docstring 写着「新班次/新日」，但**全仓库零调用点**。
于是 `energy_accumulated` 的真实口径是「自进程启动以来」：

```
累计后 总能耗: 301.0 kWh | 电费: 451.5 | 碳排: 210.7
—— 进程重启 ——
重启后 总能耗: 0 kWh    | 电费: 0.0   | 碳排: 0.0
```

看板上的「能耗汇总 / 电费 / 碳排放」就这么掉到 0。反方向也错：进程跑三个月，
它就是三个月的累计，而页面上没有任何地方说这是三个月。

修法：口径钉成**今天（本地日）**，跨日惰性重置（保留电表读数基线 ——
那是绝对值，清掉会让每天午夜丢一段用电量），并落库跨重启恢复。

### 2. 同一次调用里既给功率又给累积读数 → 同一个电量算两遍

`feed_power_data(dev, 52, energy_kwh=1250)` 原来**两条路径都累加**：
累积读数差（250 kWh）+ 功率梯形积分（51 kWh）= **301 kWh**。

两者是同一个物理量的两个通道，不是两个独立的量。累积读数是精确值、
功率积分是近似值 —— 有精确值时不该再叠加近似值。

修法：同一次调用里给了累积读数时，功率路径**只更新 `realtime_power`**
（「总功率」看板靠它），不再累加能耗。

### 3. 本轮**没修**（记进 AUDIT 报告，下一轮）

* **跨寄存器双通道**：一台设备同时有 `power` 和 `kwh` 两个寄存器时，
  采集层分两次调用（一次功率、一次读数），仍然两边都算。
  要修得先决定"一台设备只能有一个能耗真源"，会改变现有设备的口径；
* **非单调累积读数被当成消耗**：累积读数路径把"读数变大"一律当用电量。
  真实电表单调递增没问题，但仿真预设里的 kWh 寄存器是
  `base + 噪声`（会来回抖），每次抖动都被算成用电量 ——
  实测噪声 5/20/10/3 的四个分项，每 60 秒抖动约 ±32 kWh ≈ 1920 kWh/h，
  而真实总功率只有 320 kW。**这是仿真保真度问题**（kWh 寄存器本应单调），
  改它要动 `采集层/device_behavior_simulator.py` 的物理模型，独立一轮。
"""

import os
from datetime import datetime, timedelta

import pytest

from 存储层.database import Database
from 智能层.energy_manager import EnergyManager


@pytest.fixture
def env(tmp_path):
    """真实 SQLite（要验持久化）+ 一份最小 energy.yaml。"""
    db = Database(str(tmp_path / 't.db'))
    cfg = tmp_path / 'energy.yaml'
    cfg.write_text(
        'tariff: {peak: 1.5, flat: 0.9, valley: 0.4}\n'
        'tariff_periods: {peak: [[8, 11]], valley: [[0, 7]]}\n'
        'carbon_factor: 0.7\n',
        encoding='utf-8')

    def build():
        return EnergyManager(db, config_path=str(cfg))

    return db, build


def _t0():
    """两小时前（保证落在"今天"里，不会因为跑测试时刚好跨日而飘）。"""
    now = datetime.now()
    base = now - timedelta(hours=2)
    # 若当前时间不足两小时，就退回今天 00:30，仍在今天内
    if base.date() != now.date():
        base = now.replace(hour=0, minute=30, second=0, microsecond=0)
    return base.replace(microsecond=0)


def _feed_250(em):
    """电表读数 1000 → 1250（真实 250 kWh），同时给功率 50/52 kW。"""
    t0 = _t0()
    em.feed_power_data('d1', 50.0, energy_kwh=1000.0, timestamp=t0)
    em.feed_power_data('d1', 52.0, energy_kwh=1250.0, timestamp=t0 + timedelta(hours=1))


# ---------------------------------------------------------------------------
# 1. 双通道重复计
# ---------------------------------------------------------------------------

def test_same_call_power_and_reading_not_double_counted(env):
    """同一次调用里既有累积读数又有功率 → **只按累积读数算**。

    修复前：250（读数差）+ 51（功率积分）= 301 kWh，能耗/电费/碳排全部虚增。
    """
    _db, build = env
    em = build()
    try:
        _feed_250(em)
        total = em.get_energy_summary()['total_energy_kwh']
        assert total == pytest.approx(250.0, abs=0.01), (
            f'累计 {total} kWh —— 功率积分被重复叠加了（真实 250）')
    finally:
        em.stop()


def test_power_only_device_still_integrates(env):
    """反向对照：只给功率（没有累积读数）的设备**必须**继续积分。

    否则"没有电表的设备"能耗恒为 0 —— 比虚增更糟（完全没数据）。
    """
    _db, build = env
    em = build()
    try:
        t0 = _t0()
        em.feed_power_data('d1', 10.0, timestamp=t0)
        em.feed_power_data('d1', 10.0, timestamp=t0 + timedelta(hours=1))
        total = em.get_energy_summary()['total_energy_kwh']
        assert total == pytest.approx(10.0, abs=0.01), '纯功率设备不再积分了'
    finally:
        em.stop()


# ---------------------------------------------------------------------------
# 2. 跨重启
# ---------------------------------------------------------------------------

def test_accumulation_survives_restart(env):
    """重启后「今日能耗/电费/碳排」不许归零。"""
    _db, build = env
    em1 = build()
    _feed_250(em1)
    before = em1.get_energy_summary()
    em1.stop()          # 优雅停机 → 落库

    em2 = build()
    try:
        after = em2.get_energy_summary()
        assert after['total_energy_kwh'] == pytest.approx(before['total_energy_kwh']), (
            '重启后能耗归零 —— 看板上的"今日能耗/电费/碳排放"会突然变成 0')
        assert after['electricity_cost'] == pytest.approx(before['electricity_cost'])
    finally:
        em2.stop()


def test_reading_baseline_survives_restart(env):
    """重启后电表基线必须恢复 —— 否则停机期间的用电量永久丢失。"""
    _db, build = env
    em1 = build()
    _feed_250(em1)
    em1.stop()

    em2 = build()
    try:
        assert em2._last_energy_reading.get('d1') == pytest.approx(1250.0), (
            '电表基线没恢复 —— 重启后第一次读数只能建基线，停机那段用电量丢了')
        # 继续 +50 → 应累加到 300（而不是从 0 重新算）
        em2.feed_power_data('d1', 55.0, energy_kwh=1300.0, timestamp=_t0() + timedelta(hours=2))
        assert em2.get_energy_summary()['total_energy_kwh'] == pytest.approx(300.0, abs=0.01)
    finally:
        em2.stop()


def test_cross_day_does_not_restore_accumulation_but_keeps_baseline(env):
    """库里的累计属于**昨天** → 累计量从 0 开始，但电表基线仍要恢复。

    昨天的电费不能算进今天；而电表读数是绝对值，丢了就永久少一段。
    """
    db, build = env
    em1 = build()
    _feed_250(em1)
    em1.stop()

    # 把库里那条记录改成"昨天"
    stored = db.get_system_state(EnergyManager.STATE_KEY_ACCUM)
    stored['date'] = (datetime.now().date() - timedelta(days=1)).isoformat()
    db.set_system_state(EnergyManager.STATE_KEY_ACCUM, stored)

    em2 = build()
    try:
        assert em2.get_energy_summary()['total_energy_kwh'] == 0.0, (
            '昨天的累计被算进了今天')
        assert em2._last_energy_reading.get('d1') == pytest.approx(1250.0), (
            '跨天时电表基线也被丢了 —— 每天午夜丢一段用电量')
    finally:
        em2.stop()


def test_reset_accumulated_persists(env):
    """清零必须落库 —— 否则重启后数字又跳回来。

    ⚠️ 必须先 `_persist_accumulated(force=True)` 把**非零**值写进库：
    第一次喂数建立基线时累计还是 0，那次落库写进去的是 0 ——
    如果只靠它，即使"清零不落库"这个缺陷存在，库里的值也恰好是 0，
    测试会**假通过**（实测：第一版就是这么漏掉的，变异存活）。
    这里显式模拟监控循环的那一分钟落库。
    """
    _db, build = env
    em1 = build()
    _feed_250(em1)
    em1._persist_accumulated(force=True)      # 模拟 60s 监控 tick
    assert em1.get_energy_summary()['total_energy_kwh'] > 0
    em1.reset_accumulated()
    assert em1.get_energy_summary()['total_energy_kwh'] == 0.0

    # ⚠️ **不调 `em1.stop()`** —— 优雅停机里有一次 force 落库，
    # 它会把清零结果写进库，从而掩盖"清零本身没落库"这个缺陷
    # （实测：调了 stop 的版本，变异存活）。这里模拟**崩溃重启**。
    em2 = build()
    try:
        assert em2.get_energy_summary()['total_energy_kwh'] == 0.0, (
            '点了"清零"后崩溃重启，累计又回来了 —— 清零没有落库')
    finally:
        em2.stop()


# ---------------------------------------------------------------------------
# 3. 跨日惰性重置
# ---------------------------------------------------------------------------

def test_rollover_clears_accumulation_but_keeps_readings(env):
    """窗口日期是昨天 → 第一次喂数时重置累计（保留读数基线）。"""
    _db, build = env
    em = build()
    try:
        em._window_date = datetime.now().date() - timedelta(days=1)
        em.energy_accumulated['d1']['energy_kwh'] = 999.0
        em._last_energy_reading['d1'] = 1250.0

        em.feed_power_data('d1', 0, energy_kwh=1300.0)      # 触发跨日检查

        assert em.get_energy_summary()['total_energy_kwh'] == pytest.approx(50.0, abs=0.01), (
            '跨日没清掉昨天的 999（或者把读数基线也清了导致增量丢失）')
        assert em._window_date == datetime.now().date()
    finally:
        em.stop()


def test_summary_reports_window(env):
    """口径必须写在返回值里 —— 否则前端标"总能耗"会被当成累计总量。"""
    _db, build = env
    em = build()
    try:
        summary = em.get_energy_summary()
        assert summary['window'] == 'today'
        assert summary['window_date'] == datetime.now().date().isoformat()
    finally:
        em.stop()


# ---------------------------------------------------------------------------
# 4. 健壮性
# ---------------------------------------------------------------------------

def test_persist_failure_does_not_break_accumulation(env, monkeypatch):
    """落库失败只记 ERROR，**不许**影响内存里的累计（采集链路不能因此报错）。"""
    db, build = env
    em = build()
    try:
        monkeypatch.setattr(db, 'set_system_state', lambda *a, **k: False)
        _feed_250(em)
        assert em.get_energy_summary()['total_energy_kwh'] == pytest.approx(250.0, abs=0.01), (
            '落库失败把内存里的累计也弄丢了')
    finally:
        em.stop()


def test_database_without_system_state_is_safe(tmp_path):
    """没有 `system_state` 能力的 database（老库 / 简化对象）不许炸。"""
    cfg = tmp_path / 'energy.yaml'
    cfg.write_text('tariff: {peak: 1.0}\n', encoding='utf-8')

    class _BareDB:
        pass

    em = EnergyManager(_BareDB(), config_path=str(cfg))
    try:
        em.feed_power_data('d1', 10.0, energy_kwh=5.0)
        em.feed_power_data('d1', 10.0, energy_kwh=8.0)
        assert em.get_energy_summary()['total_energy_kwh'] == pytest.approx(3.0)
    finally:
        em.stop()


def test_forget_device_persists(env):
    """删设备要同步落库 —— 否则重启后它的累计又被恢复回来。

    同 `test_reset_accumulated_persists`：先 force 落一次**非零**值，
    否则库里的 0 会让这条用例假通过。
    """
    _db, build = env
    em1 = build()
    _feed_250(em1)
    em1._persist_accumulated(force=True)      # 模拟 60s 监控 tick
    assert em1.forget_device('d1') is True

    # 同 `test_reset_accumulated_persists`：不调 `stop()`（它有一次 force 落库，
    # 会掩盖"删设备没落库"）。模拟崩溃重启。
    em2 = build()
    try:
        assert em2.get_energy_summary()['total_energy_kwh'] == 0.0, (
            '已删除设备的累计被重启恢复了')
        assert 'd1' not in em2._last_energy_reading
    finally:
        em2.stop()
