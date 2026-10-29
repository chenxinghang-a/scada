# -*- coding: utf-8 -*-
"""数值解析的唯一入口：把「非有限值」当成非法输入挡在业务逻辑之外。

为什么需要这个模块（round 224）
--------------------------------
同一类漏洞在仓库里出现过**五次**，写法一模一样：

    try:
        v = int(raw)
    except (TypeError, ValueError):
        <回退默认 / 报 400>

`int(float('inf'))` 抛的是 **OverflowError** —— 不在那个元组里，于是直接漏成
未捕获异常（HTTP 500 / 启动崩溃）。而 `inf` 是**能从正常输入路径进来的**：

* JSON：Python 的 `json.loads` 把 `1e400` 解析成 `inf`（float 上溢不报错）；
  裸 `NaN` / `Infinity` 字面量也照收（不合 JSON 规范，但 Flask 就这么解）；
* YAML：`.inf` / `-.inf` / `.nan` 是**规范内**的字面量，配置文件里写得出。

更麻烦的是 `NaN` 不会抛任何异常 —— 它与任何数比较都返回 False，所以
「范围校验」`v < min or v > max` 对 NaN **完全失效**，NaN 会被当成合法值
一路下发到现场设备（float32 寄存器写进一个 NaN 位型）。

本模块把这些规则收在一处：
* `to_finite_float` / `to_int` —— 解析失败抛 `ValueError`（给「参数非法」用）；
* `try_finite_float` / `try_int` —— 解析失败返回 `None`（给「回退默认值」用）。

⚠️ 不要在别处再写裸 `int()` / `float()` + `except (TypeError, ValueError)`。
`tests/test_non_finite_input_guard.py` 会扫出来。
"""

from __future__ import annotations

import math
from typing import Any


def to_finite_float(value: Any, name: str = 'value') -> float:
    """转成有限浮点数；NaN / ±Infinity / 非数值一律 `ValueError`。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'Invalid {name}: must be number') from None
    except OverflowError:
        # 自定义 `__float__` 之类的极端实现才会走到这里，一并收口。
        raise ValueError(f'Invalid {name}: must be a finite number') from None
    if not math.isfinite(num):
        raise ValueError(f'Invalid {name}: must be a finite number (NaN/Infinity 不被接受)')
    return num


def to_int(value: Any, name: str = 'value') -> int:
    """转成整数；非有限值 / 超范围 / 非数值一律 `ValueError`。

    ⚠️ **必须保留 `int()` 的原语义**，不能先 `float()` 再取整。
    `int("12.7")` 原本抛 ValueError（拒绝拼错的地址），若改成
    `int(float("12.7"))` 就会静默变成 12 —— 地址错一位写到别的寄存器上，
    这种「悄悄修好」比报错危险得多。

    这里只补两件 `int()` 本身不管的事：
    * `int(float('inf'))` 抛的是 **OverflowError**，调用方的
      `except (TypeError, ValueError)` 接不住 → 漏成 500；
    * `int(float('nan'))` 虽然抛 ValueError，但报错文案看不出是「非有限值」。
    """
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f'Invalid {name}: must be a finite number (NaN/Infinity 不被接受)')
    try:
        return int(value)
    except OverflowError:
        raise ValueError(f'Invalid {name}: must be a finite integer') from None
    except (TypeError, ValueError):
        raise ValueError(f'Invalid {name}: must be integer') from None


def try_finite_float(value: Any) -> float | None:
    """同 `to_finite_float`，失败返回 `None`（用于「回退默认值」的分支）。"""
    try:
        return to_finite_float(value)
    except ValueError:
        return None


def try_int(value: Any) -> int | None:
    """同 `to_int`，失败返回 `None`。"""
    try:
        return to_int(value)
    except ValueError:
        return None


def require_positive_int(value: Any, name: str = 'value', minimum: int = 1) -> int:
    """把「保留天数 / 保留份数」一类入参收敛成 >= ``minimum`` 的整数。

    为什么必须有这道校验（round 248 实测）：
    清理类接口的入参直接来自 HTTP body，而它们是**删除操作的方向** ——

    - ``retention_days=0`` → cutoff = 现在 → ``timestamp < now``
      命中**全部**历史（实测 201 万行 100% 被删）；
    - ``retention_days=-1`` → cutoff 在未来 → 同样全删；
    - ``keep_count=0`` → ``backups[0:]`` → 全部备份被删（实测 7/7）。

    定时路径（``core/retention_scheduler.py``）早就把 ``days <= 0``
    回退成默认值，但**手动清理接口与 Database 的保留策略方法都没有校验** ——
    运维手滑填个 0、前端传个未初始化的 ``undefined``，就是一次全表删除。

    删数据的方向上宁可保守：非法入参一律**抛 ValueError**
    （调用方 bug，不是环境故障），由 API 层转成 400，而不是吞进
    ``{'status': 'error'}`` 显示成 500「服务器坏了」。

    ``bool`` 是 ``int`` 的子类，必须显式排除 —— ``True`` 会变成 1，
    看起来"合法"，实则把 90 天策略改成 1 天。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f'Invalid {name}: must be an integer '
            f'(收到 {type(value).__name__}: {value!r})'
        )
    if value < minimum:
        raise ValueError(
            f'Invalid {name}: must be >= {minimum} (收到 {value}) —— '
            f'删数据的方向上不接受 0/负数，那等于删光'
        )
    return value
