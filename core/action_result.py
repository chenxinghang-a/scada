# -*- coding: utf-8 -*-
"""动作/写入结果的**唯一**判定口径。

为什么需要这个模块（round 372）
--------------------------------
仓库里的动作回调有三种返回约定，混着用：

* `bool`（`write_single_register` / `release_device_lock`）；
* `dict`（`{'success': False, 'message': ...}`，API 与 DeviceControl 用）；
* `None`（通知类回调，本来就不返回结论）。

调用方各自写判断，于是出现两类 bug：

1. `bool(write_res)` —— `bool({'success': False})` 是 **True**，
   写入失败被记成"联锁动作执行成功"；
2. `result is not None` —— `False is not None` 为 True，硬件没响也记成功。

统一到这里：**明确的失败只有两种** —— `False` 或 `{'success': False}`。
`None` 表示"没有结论"，不算失败（否则每个通知类回调都会被记成失败）。
"""

from __future__ import annotations

from typing import Any


def is_failure(result: Any) -> bool:
    """返回值是否明确表示失败（`False` / `{'success': False}`）。"""
    return result is False or (
        isinstance(result, dict) and result.get('success') is False)


def is_success(result: Any) -> bool:
    """返回值是否**不构成**失败（`None` 也算成功：没有结论，不能诬告）。"""
    return not is_failure(result)


def ok(message: str = '', **extra: Any) -> dict[str, Any]:
    """构造统一成功结果。"""
    return {'success': True, 'message': message, **extra}


def fail(message: str, **extra: Any) -> dict[str, Any]:
    """构造统一失败结果。"""
    return {'success': False, 'message': message, **extra}
