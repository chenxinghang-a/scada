# -*- coding: utf-8 -*-
"""采集中断看门狗：数据"不新鲜"要**报警**，不能只在健康页上变个颜色。

为什么需要它（round 263）
-------------------------
`core/health_checker._check_data_freshness` 早就在看数据新鲜度，但它有两个
结构性缺口，合起来就是「设备失联了，没人知道」：

1. **只报不警**：结论只进 `/api/health/status` 的 JSON。报警历史里查不到，
   声光不响、WebSocket 不推、值班手机收不到 —— 不看健康页就发现不了。
   SCADA 里"采集中断"是最严重的故障之一（等于现场失联），不该只是一个色块。

2. **全局口径掩盖单设备失联**：它查的是 `MAX(timestamp) FROM history_data`
   —— **全库最新一条**。25 台设备里只要还有一台在写，这个值就一直是新鲜的。
   而现场最常见的恰恰是**单台设备/单条链路断了**，不是全停。

所以这里按**设备**看：每台启用设备最近一条历史记录距今多久，超过
`stale_factor × 采集间隔` 就产生一条一等报警（走
`AlarmManager.raise_external_alarm`，与其它报警同一条链路：去重、声光、
广播、WebSocket、升级），恢复后自动清除。

判据
----
* 阈值 = `stale_factor × default_interval`（默认 6 × 5s = 30s）。
  取 6 个采集周期：容忍偶发丢包/重试，又不至于让"停了 5 分钟"看起来正常。
* 从没上报过数据的设备**不报警**（刚部署/刚启用，由别的检查覆盖），
  否则一上线就满屏红。
* 库查询用一条 `GROUP BY device_id` 的聚合，靠 `(device_id, timestamp)` 索引，
  30 秒一次，代价可忽略。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

#: 报警归属（与真实设备区分开，便于在报警列表里筛选"系统类报警"）
ALARM_DEVICE_ID = 'data_freshness'

#: 阈值倍数：多少倍采集间隔算"失联"
DEFAULT_STALE_FACTOR = 6

#: 阈值下限（秒）：采集间隔很小（1s）时也至少等 30 秒，避免抖动误报
MIN_STALE_SECONDS = 30


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    text = str(value).strip().replace('T', ' ')
    for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def stale_threshold_seconds(default_interval: float,
                            factor: int = DEFAULT_STALE_FACTOR) -> float:
    """算出"多久没数据算失联"（秒）。

    ⚠️ 必须挡住非有限值：`collection.default_interval: .inf` 是 YAML 合法字面量，
    `inf × 6` 仍是 inf → 阈值无穷大 → **永不判失联**，看门狗静默失效。
    这类"配置写错 = 功能消失"的坑本仓库已经踩过多次（见 core/safe_numeric.py）。
    """
    from core.safe_numeric import try_finite_float

    interval = try_finite_float(default_interval)
    if interval is None or interval <= 0:
        interval = 5.0

    factor_value = try_finite_float(factor)
    if factor_value is None or factor_value <= 0:
        factor_value = float(DEFAULT_STALE_FACTOR)

    return max(interval * factor_value, float(MIN_STALE_SECONDS))


def scan(database, *, now: datetime | None = None,
         threshold_seconds: float | None = None) -> dict[str, dict]:
    """扫描每台设备的最新数据时刻。

    Args:
        database: `存储层.database.Database`
        now: 参考时刻（便于测试）
        threshold_seconds: 失联阈值；None 时按 `collection` 配置算

    Returns:
        `{device_id: {'age': 秒 | None, 'stale': bool, 'last': str | None}}`
        只包含**曾经上报过**数据的设备 —— 从没上过报的不算失联。
    """
    from core.config_manager import load_collection_settings

    if threshold_seconds is None:
        settings = load_collection_settings()
        threshold_seconds = stale_threshold_seconds(settings.get('default_interval', 5))

    now = now or datetime.now()
    result: dict[str, dict] = {}

    with database.get_connection(readonly=True) as conn:
        rows = conn.execute(
            'SELECT device_id, MAX(timestamp) AS last FROM history_data '
            'GROUP BY device_id').fetchall()

    for row in rows:
        device_id = row['device_id'] if not isinstance(row, tuple) else row[0]
        last_raw = row['last'] if not isinstance(row, tuple) else row[1]
        last_dt = _parse_ts(last_raw)
        if last_dt is None:
            continue
        age = (now - last_dt).total_seconds()
        result[device_id] = {
            'age': age,
            'last': str(last_raw),
            'stale': age > threshold_seconds,
        }
    return result


def run_once(database, alarm_manager, *, now: datetime | None = None,
             threshold_seconds: float | None = None,
             known_devices: set[str] | None = None) -> dict:
    """扫一遍并对**状态变化**做报警/恢复（幂等：同一状态重复调用不会重复报警）。

    Args:
        known_devices: **当前仍然存在**的设备 id 集合。传了就启用"幽灵报警"清理
            （见下面的说明）；不传则只按历史数据判断。

    Returns:
        {'threshold': 秒, 'stale': [...], 'raised': [...], 'cleared': [...]}
    """
    from core.config_manager import load_collection_settings

    settings = load_collection_settings()
    if threshold_seconds is None:
        threshold_seconds = stale_threshold_seconds(settings.get('default_interval', 5))

    snapshot = scan(database, now=now, threshold_seconds=threshold_seconds)

    # round 277：**已删除设备的"幽灵报警"**。
    # `scan()` 按 history_data 分组，设备删了历史还在 → 它仍会被判为"陈旧"，
    # 而它再也不会产生新数据，所以那条 `data_stale_<dev>` 报警**永远不会恢复**：
    # 活跃报警列表里永久挂着一个不存在的设备，操作员点确认也没用。
    ghost_devices: list[str] = []
    if known_devices is not None:
        for dev in list(snapshot):
            if dev not in known_devices:
                ghost_devices.append(dev)
                snapshot.pop(dev, None)
        # 已经在报警、但设备已不存在 → 直接清掉
        for entry in list(alarm_manager.get_active_alarms()):
            alarm_id = entry.get('alarm_id') or ''
            if not alarm_id.startswith('data_stale_'):
                continue
            dev = entry.get('register_name') or ''
            if dev and dev not in known_devices and dev not in ghost_devices:
                ghost_devices.append(dev)

    stale_devices = [dev for dev, info in snapshot.items() if info['stale']]
    healthy_devices = [dev for dev, info in snapshot.items() if not info['stale']]

    raised, cleared = [], []
    for device_id in stale_devices:
        info = snapshot[device_id]
        alarm_id = f'data_stale_{device_id}'
        # 幂等：已经在报警的设备不再重复登记（raise_external_alarm 自身也去重，
        # 但这里提前判断可以少走一遍完整链路）
        already = any(a.get('alarm_id') == alarm_id
                      for a in alarm_manager.get_active_alarms())
        if already:
            continue
        alarm_manager.raise_external_alarm(
            alarm_id=alarm_id,
            message=(f'设备 {device_id} 采集中断：最新数据距今 {info["age"]:.0f}s'
                     f'（阈值 {threshold_seconds:.0f}s）'),
            level='critical',
            device_id=ALARM_DEVICE_ID,
            register_name=device_id,
        )
        raised.append(device_id)
        logger.warning("采集中断报警: %s（最新数据距今 %.0fs，阈值 %.0fs）",
                       device_id, info['age'], threshold_seconds)

    # 已删除设备：清掉它的报警（不列入 raised/cleared 的正常语义，单独返回）
    cleared_ghosts: list[str] = []
    for device_id in ghost_devices:
        alarm_id = f'data_stale_{device_id}'
        try:
            if alarm_manager.clear_external_alarm(alarm_id, ALARM_DEVICE_ID, device_id):
                cleared_ghosts.append(device_id)
                logger.info("设备已删除，清除其采集中断报警: %s", device_id)
        except Exception as e:      # noqa: BLE001
            logger.debug("清除幽灵报警失败 %s: %s", device_id, e)

    # 恢复：只清"曾经因本看门狗报过、现在又正常"的那些
    for device_id in healthy_devices:
        alarm_id = f'data_stale_{device_id}'
        if alarm_manager.clear_external_alarm(alarm_id, ALARM_DEVICE_ID, device_id):
            cleared.append(device_id)
            logger.info("采集已恢复: %s（最新数据距今 %.0fs）",
                        device_id, snapshot[device_id]['age'])

    return {
        'threshold': threshold_seconds,
        'scanned': len(snapshot),
        'stale': stale_devices,
        'raised': raised,
        'cleared': cleared,
        #: 因设备已删除而被清掉的报警（不是"采集恢复"，别混在一起统计）
        'cleared_ghosts': cleared_ghosts,
    }
