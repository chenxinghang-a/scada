# -*- coding: utf-8 -*-
"""设备生命周期事件（round 312）：设备**真的被删掉**之后，通知关心它的模块。

## 为什么需要这个模块

round 310 给 9 个模块加了 `forget_device()`，并在 `api_devices.delete_device()`
里通过 `_forget_device_everywhere()` 调用 —— 但那**只覆盖了「运维在界面上删设备」**
这一条路径。设备还可能因为**配置文件热重载**而消失：

    POST /api/devices/reload   →  DeviceManager.reload_config()
    core/device_config_watch   →  维护任务每 10s 检查 devices.yaml 的 mtime

这两条路径走的是 `reload_config()` 的差集（`removed`），**根本不经过**
`api_devices`，所以智能层的状态照样残留 —— 表现为：
「yaml 里删掉一台设备 → 配置表没了、采集停了，但 OEE 报表里它还在，
重新加回同名设备时继承旧的连续失败计数」。

## 为什么用模块级注册表而不是 `DeviceManager` 的实例方法

三个设备管理器是**平行实现**（`DeviceManager` / `RealDeviceManager` /
`SimulatedDeviceManager`），各有自己的 `remove_device()`。把监听器做成实例字段，
就得在三个类的 `__init__` 里各初始化一次、在三处 `remove_device` 里各通知一次 ——
漏一个就是同一个 bug 换个类再来一遍（round 302 的 `_save_config` 就是这么漏的）。

模块级注册表只有**一个**状态、**一个**通知函数，调用点仍是 4 处（3 个
`remove_device` + 1 个 `reload_config`），但漏了会被
`tests/test_device_lifecycle_events.py` 的源码守卫抓到。

## 约束

* 监听器异常**必须被收住**：删设备是主操作，清理是附带动作，
  清理失败不能让删除返回失败（删不掉比清不掉严重得多）。
* 注册表本身是进程级的、只增不减 —— 但监听器只在启动时注册一次（`run.py`），
  数量是常量，不是"无界增长"。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_listeners: list[Callable[[str], None]] = []


def add_device_remove_listener(fn: Callable[[str], None]) -> None:
    """注册一个「设备被删除」监听器。重复注册同一个函数会被忽略（幂等）。"""
    if not callable(fn):
        raise TypeError(f'监听器必须可调用，收到 {type(fn).__name__}')
    with _lock:
        if fn in _listeners:
            return
        _listeners.append(fn)
    logger.debug("注册设备删除监听器: %s", getattr(fn, '__qualname__', fn))


def notify_device_removed(device_id: str) -> dict[str, str]:
    """设备**确认删除成功之后**调用，通知所有监听器。

    Returns:
        dict: `{监听器名: '清理异常: ...'}`，只含出错的；全部成功时为空字典。

    ⚠️ 调用时机：必须在**落盘成功之后**。落盘失败会回滚设备，
    那时清掉状态反而让一台仍在运行的设备丢掉健康数据/失败计数。
    """
    with _lock:
        targets = list(_listeners)

    failures: dict[str, str] = {}
    for fn in targets:
        name = getattr(fn, '__qualname__', repr(fn))
        try:
            fn(device_id)
        except Exception as e:          # noqa: BLE001 —— 清理失败不许影响删除结果
            failures[name] = f'清理异常: {e}'
            logger.error("设备删除监听器执行失败 [%s] device=%s: %s", name, device_id, e)

    if targets:
        logger.info("设备 %s 已删除，已通知 %d 个监听器（失败 %d 个）",
                    device_id, len(targets), len(failures))
    return failures


def clear_device_remove_listeners() -> None:
    """清空监听器。**只给测试用** —— 生产代码里监听器是启动时注册一次、永不撤销。"""
    with _lock:
        _listeners.clear()


def get_device_remove_listener_count() -> int:
    """当前监听器数量（供测试与排障确认"到底注册上了没有"）。"""
    with _lock:
        return len(_listeners)
