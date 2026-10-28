# -*- coding: utf-8 -*-
"""设备配置文件（`配置/devices.yaml`）的**自动**热重载（round 285）。

背景
----
round 283 给了 `POST /api/devices/reload`（重载配置 + 同步采集任务），
round 284 给它加了 fail-closed 守卫（写坏的配置不许清空设备表）。
但那仍然要**人去点一下**。现场改设备清单（加一台仪表、停一台旧设备）
本质上是"改配置 → 生效"，不该要求运维记得去调接口、更不该重启进程。

为什么现在才敢做成自动
----------------------
自动重载的前提是"坏输入不会造成破坏"。round 284 之前不是：
一次编辑器中间态保存就会把整张设备表清空、停掉全部采集。
现在 `reload_config()` 会拒绝解析失败与"解析成 0 台"，
所以**最坏情况只是这次改动不生效**，不会比不改更糟。

判据
----
· 首次调用只**建立 mtime 基线**，不重载 —— 否则服务一启动就会立刻
  reload 一遍，日志里全是噪音，还会与启动流程抢设备表；
· 同一个 mtime 只处理一次（处理后立即更新基线），坏文件不会每 10 秒
  刷一次错误日志；修好文件后 mtime 变化 → 自然重试；
· 重载被拒绝时**不同步采集任务**（差集会是"全部移除"）。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: `DeviceManager` 没给出 config_path 时的兜底路径
DEFAULT_CONFIG_REL = '配置/devices.yaml'
#: 安全联锁规则文件（round 318）。改它同样必须热重载 ——
#: `DeviceControlSafety._load_preset_interlocks()` 只在构造时跑一次。
DEFAULT_INTERLOCK_REL = '配置/interlocks.yaml'


def _resolve_path(device_manager) -> Any:
    """取设备配置文件的绝对路径（与 `DeviceManager` 用同一套解析）。"""
    raw = getattr(device_manager, 'config_path', None) or DEFAULT_CONFIG_REL
    try:
        import paths
        return paths.resolve(raw)
    except Exception as e:      # noqa: BLE001 —— 解析失败就按原样用
        logger.debug("解析设备配置路径失败，按原样使用 %s: %s", raw, e)
        from pathlib import Path
        return Path(raw)


def _empty_result(reason: str) -> dict[str, Any]:
    return {
        'changed': False,
        'reloaded': False,
        'rejected': None,
        'diff': None,
        'sync': None,
        'reason': reason,
    }


def watch_once(device_manager, data_collector=None, *,
               state: dict | None = None) -> dict[str, Any]:
    """检查设备配置文件的 mtime，变了就重载 + 同步采集任务。

    Args:
        device_manager: `采集层.device_manager.DeviceManager`
        data_collector: `采集层.data_collector.DataCollector`（可为 None ——
            只重载配置、不同步任务）
        state: 跨调用保存的基线，形如 `{'mtime': 1234.5}`。
            传 None 表示本次调用是独立的（基线不保留）。

    Returns:
        dict: `{'changed', 'reloaded', 'rejected', 'diff', 'sync', 'reason'}`
        · `changed`  —— mtime 与基线不同
        · `reloaded` —— 配置表**真的**被替换了
        · `rejected` —— 被 fail-closed 守卫拒绝的原因（None 表示没拒绝）
        · `reason`   —— 未处理时的人类可读说明
    """
    if state is None:
        state = {}

    path = _resolve_path(device_manager)
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return _empty_result(f'设备配置文件不存在: {path}')
    except OSError as e:
        return _empty_result(f'无法读取设备配置文件 {path}: {e}')

    previous = state.get('mtime')
    if previous is None:
        # 首次：只建立基线。服务刚启动时配置已经由 `load_config()` 加载过，
        # 这里再 reload 一遍纯属重复劳动。
        state['mtime'] = mtime
        return _empty_result('首次建立 mtime 基线（不重载）')

    if mtime == previous:
        return _empty_result('配置文件未变化')

    # 先更新基线：同一个坏文件不该每轮都重试并刷日志。
    # 文件被修好 → mtime 会再变一次 → 自然重试。
    state['mtime'] = mtime

    try:
        diff = device_manager.reload_config() or {}
    except Exception as e:      # noqa: BLE001 —— 重载本身炸了不能让维护任务挂
        logger.error("设备配置自动重载异常: %s", e)
        return {
            'changed': True, 'reloaded': False, 'rejected': str(e),
            'diff': None, 'sync': None,
            'reason': f'重载异常: {e}',
        }

    rejected = diff.get('rejected')
    if rejected:
        # 守卫已经保留了原配置表；**绝不能**再去同步任务
        # （差集会是"全部移除"，等于把现场采集全停掉）。
        logger.error("设备配置自动重载被拒绝，未做任何变更: %s", rejected)
        return {
            'changed': True, 'reloaded': False, 'rejected': rejected,
            'diff': diff, 'sync': None, 'reason': '被 fail-closed 守卫拒绝',
        }

    sync = None
    if data_collector is not None:
        try:
            sync = data_collector.sync_device_tasks()
        except Exception as e:      # noqa: BLE001
            logger.error("设备配置已重载，但同步采集任务失败: %s", e)
            return {
                'changed': True, 'reloaded': True, 'rejected': None,
                'diff': diff, 'sync': None,
                'reason': f'采集任务同步失败: {e}',
            }

    added = diff.get('added') or []
    removed = diff.get('removed') or []
    started = (sync or {}).get('started') or []
    stopped = (sync or {}).get('stopped') or []

    if added or removed or started or stopped:
        logger.warning(
            "设备配置自动热重载: 新增 %s，移除 %s；采集启动 %s，停止 %s",
            added, removed, started, stopped)
    else:
        logger.info("设备配置文件 mtime 变化，但设备清单内容无差异（不重启任何任务）")

    return {
        'changed': True,
        'reloaded': True,
        'rejected': None,
        'diff': diff,
        'sync': sync,
        'reason': '已应用',
    }


def watch_interlocks_once(device_control, *, state: dict | None = None) -> dict[str, Any]:
    """检查 `配置/interlocks.yaml` 的 mtime，变了就热重载联锁规则（round 318）。

    Args:
        device_control: `智能层.device_control.DeviceControlSafety`
        state: 跨调用保存的基线，形如 `{'mtime': 1234.5}`。

    Returns:
        dict: `{'changed', 'reloaded', 'diff', 'reason'}`

    ## 为什么需要

    `_load_preset_interlocks()` **只在 `__init__` 里调用一次** ——
    改 `interlocks.yaml`（调阈值、加一条、停用一条）必须**重启进程**才生效，
    而这是 24/7 的现场系统。且**没有任何提示**：运维改完文件就以为生效了。

    方向虽然"安全"（旧规则继续保护），但两种误判都很危险：
    · 改**高**阈值想减少误跳闸 → 实际没生效 → 继续误跳闸；
    · 改**低**阈值想更严格 → 实际仍是旧阈值 → **保护不足而无人知**。

    ## 与 `watch_once` 同样的三条纪律

    · 首次调用只建基线（构造时已经加载过，不重复加载）；
    · mtime 未变什么都不做；
    · **处理完立刻推进基线** —— 同一个坏文件不每 10 秒刷一次错误日志，
      修好后 mtime 再变 → 自然重试。
    """
    if state is None:
        state = {}

    try:
        import paths
        path = paths.resolve(DEFAULT_INTERLOCK_REL)
    except Exception as e:      # noqa: BLE001
        from pathlib import Path
        logger.debug("解析联锁配置路径失败，按原样使用: %s", e)
        path = Path(DEFAULT_INTERLOCK_REL)

    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return {'changed': False, 'reloaded': False, 'diff': None,
                'reason': f'联锁配置文件不存在: {path}'}
    except OSError as e:
        return {'changed': False, 'reloaded': False, 'diff': None,
                'reason': f'无法读取联锁配置文件 {path}: {e}'}

    previous = state.get('mtime')
    if previous is None:
        state['mtime'] = mtime
        return {'changed': False, 'reloaded': False, 'diff': None,
                'reason': '首次建立 mtime 基线（不重载）'}

    if mtime == previous:
        return {'changed': False, 'reloaded': False, 'diff': None,
                'reason': '联锁配置文件未变化'}

    # 先推进基线：坏文件不该每轮都重试并刷日志
    state['mtime'] = mtime

    reload_fn = getattr(device_control, 'reload_interlocks', None)
    if not callable(reload_fn):
        return {'changed': True, 'reloaded': False, 'diff': None,
                'reason': 'device_control 不提供 reload_interlocks()，无法热重载'}

    try:
        diff = reload_fn() or {}
    except Exception as e:      # noqa: BLE001 —— 重载炸了不能让维护任务挂
        logger.error("联锁配置自动重载异常: %s", e)
        return {'changed': True, 'reloaded': False, 'diff': None,
                'reason': f'重载异常: {e}'}

    if diff.get('added') or diff.get('removed') or diff.get('changed'):
        logger.warning(
            "联锁配置已自动热重载：新增 %s / 移除 %s / 变更 %s",
            diff.get('added'), diff.get('removed'), diff.get('changed'))
    else:
        logger.info("联锁配置已自动热重载（规则内容无变化）")

    return {'changed': True, 'reloaded': True, 'diff': diff, 'reason': '已重载'}
