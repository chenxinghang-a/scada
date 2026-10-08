# -*- coding: utf-8 -*-
"""协议**允许集**守卫：校验器放行的协议，必须真的有人实现。

缺陷类别：**校验放行 = 通过校验，然后运行时被拒**
----------------------------------------------
`core/config_validator.py` 里设备配置的 schema 是：

    "protocol": {"type": "string",
                 "enum": ["modbus_tcp", "modbus_rtu", "opcua", "mqtt", "rest",
                          "s7", "iec104", "fins", "mc", "dnp3"]}

**这 10 个值被混在同一个枚举里，但它们不属于同一条路径**：

| 路径 | 谁在用 | 支持哪些 |
|---|---|---|
| **采集层**（设备配置走这条） | `device_manager.py`（模拟）/ `real_device_manager.py`（真实） | 5 种 / 7 种 |
| **gateway/**（独立部署的进程） | `python -m gateway.run_gateway --protocol …` | modbus / opcua / s7 / iec104 / dnp3 |

于是 `protocol: s7` 写在**设备配置**里时：
**校验通过** → 到 `create_client()` 走 `else` 分支 →
`logger.error("不支持的协议类型: s7")` → `return None` → **设备加不进去**。
用户看到的只是「设备没出现」，而配置校验当初是放行的。

实测（2026-10-09，round 202）：
* 校验枚举 **10** 种；
* `device_manager.py`（模拟模式）**5** 种 → 放行但**不支持 5 种**（dnp3/fins/iec104/mc/s7）；
* `real_device_manager.py`（真实模式）**7** 种 → 放行但**不支持 3 种**（dnp3/iec104/s7）。

本守卫盯两件事
--------------
1. **校验枚举里的每个协议都必须有人实现** —— 要么 `采集层/` 有对应客户端，
   要么 `gateway/` 有对应网关文件。防止「枚举里有、实现里没有」；
2. **两套设备管理器的协议集必须与已声明的表一致**（双向）——
   防止再冒出第三套清单（round 184 就吃过「三套设备管理器形状不一致」的亏）。

本守卫**不**要求把两条路径的枚举拆开 —— 那会改变配置校验的行为，属产品口径（见 D20）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent

VALIDATOR = BACKEND_ROOT / "core" / "config_validator.py"
DEVICE_MANAGERS = [
    BACKEND_ROOT / "采集层" / "device_manager.py",
    BACKEND_ROOT / "采集层" / "real_device_manager.py",
]
GATEWAY_DIR = BACKEND_ROOT / "gateway"
COLLECT_DIR = BACKEND_ROOT / "采集层"


#: 两套设备管理器的协议集（已核实，**双向**断言）。
#:
#: ⚠️ 这两套**不一样**是事实：模拟模式不支持 mc/fins（没有对应的模拟客户端），
#: 真实模式才支持。这**不是**本守卫要修的缺陷 —— 它只是要求「差异是显式的」。
DECLARED_MANAGER_PROTOCOLS: dict[str, set[str]] = {
    "采集层/device_manager.py": {"modbus_tcp", "modbus_rtu", "opcua", "mqtt", "rest"},
    "采集层/real_device_manager.py": {
        "modbus_tcp", "modbus_rtu", "opcua", "mqtt", "rest", "mc", "fins",
    },
}


def _validator_protocols() -> set[str]:
    """取校验器里设备协议的枚举值。"""
    text = VALIDATOR.read_text(encoding="utf-8")
    m = re.search(
        r'"protocol"\s*:\s*\{[^}]*"enum"\s*:\s*\[([^\]]+)\]', text
    )
    assert m, "没在 config_validator.py 里找到 protocol 的 enum —— 扫描口径坏了"
    return {x.strip().strip('"\'') for x in m.group(1).split(",") if x.strip()}


def _manager_protocols(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"SUPPORTED_PROTOCOLS\s*=\s*\[([^\]]+)\]", text)
    assert m, f"{path.name} 里没找到 SUPPORTED_PROTOCOLS"
    return {x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()}


def _collector_protocols() -> set[str]:
    """采集层**实际支持**的协议 = 两套设备管理器 `SUPPORTED_PROTOCOLS` 之并。

    ⚠️ 第一版按 `*_client.py` 的**文件名**推 —— 那是错的：
    `modbus_rtu` 没有单独的客户端文件（由同一个 `ModbusClient` 处理，
    靠 `if protocol in ('modbus_tcp', 'modbus_rtu')` 分发），
    于是它被误报成「没有实现」（假红）。
    **判据要落在「谁在做分发」上，不是文件名。**
    """
    out: set[str] = set()
    for path in DEVICE_MANAGERS:
        out |= _manager_protocols(path)
    return out


def _gateway_protocols() -> set[str]:
    """gateway 目录下**实际有网关文件**的协议（按 `*_gateway.py` 的文件名推）。"""
    return {
        f.stem.replace("_gateway", "")
        for f in GATEWAY_DIR.glob("*_gateway.py")
    }


# ---------------------------------------------------------------------------
# 1. 校验枚举里的每个协议都必须有人实现
# ---------------------------------------------------------------------------


def test_validator_enum_is_nonempty():
    """元守卫：枚举扫不出来时，下面的断言会退化成空断言。"""
    protos = _validator_protocols()
    assert len(protos) >= 5, f"只扫到 {len(protos)} 个协议 —— 口径可能坏了"


def test_every_validated_protocol_has_an_implementation():
    """校验器放行的每个协议，必须至少有一条实现路径。

    实现路径有两条：
      * `采集层/*_client.py`（设备配置走这条）；
      * `gateway/*_gateway.py`（独立部署的进程走这条）。
    """
    validator = _validator_protocols()
    implemented = _collector_protocols() | _gateway_protocols()

    missing = sorted(validator - implemented)
    assert not missing, (
        "以下协议在**配置校验里被放行**，但采集层和 gateway 里都没有实现 —— "
        "写进配置会通过校验、然后运行时被拒（`不支持的协议类型`）：\n  "
        + "\n  ".join(missing)
        + "\n\n要么实现它，要么把它从 config_validator 的 enum 里删掉。"
    )


# ---------------------------------------------------------------------------
# 2. 两套设备管理器的协议集必须与已声明的表一致（双向）
# ---------------------------------------------------------------------------


def test_manager_protocol_sets_match_declaration():
    """两套管理器的 `SUPPORTED_PROTOCOLS` 必须与 `DECLARED_MANAGER_PROTOCOLS` 一致。

    ⚠️ 两套不一样**是事实**（模拟模式没有 mc/fins 的模拟客户端）。
    本断言只要求「差异是显式的」—— 新增/删除协议时必须同步改表，
    免得又冒出一套没人知道的清单。
    """
    for rel, declared in DECLARED_MANAGER_PROTOCOLS.items():
        actual = _manager_protocols(BACKEND_ROOT / rel)
        assert actual == declared, (
            f"{rel} 的 SUPPORTED_PROTOCOLS 与已声明的表不一致：\n"
            f"  实际:   {sorted(actual)}\n"
            f"  已声明: {sorted(declared)}\n"
            f"  多出: {sorted(actual - declared)}  缺少: {sorted(declared - actual)}\n"
            f"请同步更新 DECLARED_MANAGER_PROTOCOLS（并想清楚差异是有意的还是疏漏）。"
        )


def test_declared_managers_exist():
    """表里的文件必须存在（防改名后表变成僵尸）。"""
    for rel in DECLARED_MANAGER_PROTOCOLS:
        assert (BACKEND_ROOT / rel).is_file(), f"{rel} 不存在"


# ---------------------------------------------------------------------------
# 3. 记录「校验放行但设备路径不支持」的差集（**只报告不失败**）
# ---------------------------------------------------------------------------


def test_report_protocols_not_usable_in_device_config(capsys):
    """**只报告**：校验放行、但**设备配置这条路径**用不了的协议。

    这些协议（s7 / iec104 / dnp3）是给 `gateway/` 那条独立路径用的，
    写进设备配置会被 `add_device` 拒绝。把它们从校验枚举里摘掉会**改变校验行为**，
    属产品口径（见决策简报 D20），所以这里只把差集打出来。
    """
    validator = _validator_protocols()
    collector = _collector_protocols()

    unusable = sorted(validator - collector)
    with capsys.disabled():
        print(f"\n[协议报告] 校验放行 {len(validator)} 种；采集层有实现的 {len(collector)} 种。")
        print(f"[协议报告] 校验放行但**设备配置用不了**的 {len(unusable)} 种：{unusable}")
        for rel in DECLARED_MANAGER_PROTOCOLS:
            sup = _manager_protocols(BACKEND_ROOT / rel)
            bad = sorted(validator - sup)
            print(f"[协议报告]   {rel} 不支持其中 {len(bad)} 种：{bad}")
    assert isinstance(unusable, list)
