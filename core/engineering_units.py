# -*- coding: utf-8 -*-
"""工程量 <-> 原始寄存器值的**唯一**换算入口（scale / offset）。

为什么需要这个模块（round 372）
--------------------------------
同一行换算 `raw = (value - offset) / scale` 在本仓库里出现过 **5 份**实现：

* `智能层/device_control.py` 的 `_encode_engineering_write`；
* `智能层/device_control.py` 的 `write_with_verification`（第二份，逻辑重复）；
* `gateway/modbus_gateway.py` 的读取分支（**漏了换算**，报原始值）；
* `模拟服务端/device_data_hub.py` 的写回分支（**多算一次**）；
* `采集层/base_client.py` 的解码路径（另一套写法）。

结果就是审计报告里"scale 双算 / 漏算"反复出现：修一处、其余照错。
SCADA 行业把这层叫 **tag scaling engine** —— 一个点位一个换算，顺序固定、
全系统只有一条通路（raw → engineering = raw*scale + offset）。

本模块只做两件事，且都带有限值校验：

* :func:`from_raw` —— 原始值 → 工程值（读方向）；
* :func:`to_raw` —— 工程值 → 原始值（写方向）。

⚠️ 不要再在别处写 `value * scale + offset` / `(value - offset) / scale`。
`tests/test_engineering_units.py` 会扫源码。
"""

from __future__ import annotations

from typing import Any, Mapping

from core.safe_numeric import to_finite_float

DEFAULT_SCALE = 1.0
DEFAULT_OFFSET = 0.0


def _scale_offset(scale: Any, offset: Any) -> tuple[float, float]:
    """归一化 (scale, offset)：缺省用 1/0，非有限值或 scale=0 直接报错。"""
    s = DEFAULT_SCALE if scale is None else to_finite_float(scale, 'scale')
    o = DEFAULT_OFFSET if offset is None else to_finite_float(offset, 'offset')
    if s == 0:
        # scale=0 意味着整个量程被压成一个点，读出来恒等于 offset ——
        # 这不是"没配置"，是配置错误；静默当成 1.0 会让量级错 10 倍。
        raise ValueError('Invalid scale: 0 会导致除零/量程坍缩')
    return s, o


def from_raw(raw: Any, scale: Any = DEFAULT_SCALE,
             offset: Any = DEFAULT_OFFSET) -> float:
    """原始寄存器值 → 工程值：`raw * scale + offset`（读方向）。"""
    value = to_finite_float(raw, 'raw')
    s, o = _scale_offset(scale, offset)
    return value * s + o


def to_raw(value: Any, scale: Any = DEFAULT_SCALE,
           offset: Any = DEFAULT_OFFSET) -> float:
    """工程值 → 原始寄存器值：`(value - offset) / scale`（写方向）。"""
    engineering = to_finite_float(value, 'value')
    s, o = _scale_offset(scale, offset)
    return (engineering - o) / s


def scale_offset_of(config: Mapping[str, Any] | None) -> tuple[float, float]:
    """从点位配置里取 `(scale, offset)`；缺省 1/0，非法值抛 `ValueError`。"""
    cfg = config or {}
    return _scale_offset(cfg.get('scale'), cfg.get('offset'))
