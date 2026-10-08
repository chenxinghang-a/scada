"""跨实现「设备状态形状」守卫：三套 IDeviceManager 实现必须给出同一套键。

为什么需要这条
-------------
`/api/devices` 直接返回 `device_manager.get_all_status()` 的元素，
而**前端读哪些字段**是写死的（见 `src/utils/dashboard.ts` / `Dashboard.vue`）：

    device_id / name / connected / stopped / device_category / protocol / host

三套实现里只要有**一套**少给某个字段，用它的那个模式下对应 UI 就会**静默失效**：

  * `device_category` 缺 → 「机械类」筛选/计数恒为 0，
    而且 `Dashboard.vue` 的**启停按钮**条件是
    `d.device_category === 'mechanical' && d.connected` → **按钮永不显示**
  * `stopped` 缺 → "已停止"状态永不出现、启停按钮文案永远错

实测（2026-10-08）就是这么发现形状不一致的：
  * `采集层/simulated_device_manager.py`  → 有 device_category / stopped / id / zone
  * `采集层/real_device_manager.py`       → 有 device_category / stopped
  * `采集层/device_manager.py`            → **没有**（只有 device_id/name/protocol/host/connected/…）

⚠️ 附带事实（本轮同时记录，不在本文件断言）：`采集层/device_manager.py` 的
`DeviceManager` **在生产代码里从未被实例化**（`run.py` 只用
`SimulatedDeviceManager` / `RealDeviceManager`），但它被 6 个以上测试文件使用 ——
即「大量测试在测一个产品不跑的类」。那条要去留属 P2-1 决策，本守卫只保证
**形状不再分叉**（万一将来切回它，不会静默打坏仪表盘）。

本守卫用 AST **静态**取三处 `status = {...}` 的字面量键，不实例化任何管理器
（实例化需要真实配置文件，属"依赖机器状态"的写法）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent

MANAGERS = {
    "simulated": BACKEND_ROOT / "采集层" / "simulated_device_manager.py",
    "real": BACKEND_ROOT / "采集层" / "real_device_manager.py",
    "legacy_device_manager": BACKEND_ROOT / "采集层" / "device_manager.py",
}

# 前端真实读取的字段（来源：src/utils/dashboard.ts 的 countDeviceStates/filterDevices、
# Dashboard.vue 的启停按钮与设备卡片）。
FRONTEND_REQUIRED = {
    "device_id",
    "name",
    "connected",
    "stopped",
    "device_category",
    "protocol",
    "host",
}


def _status_keys(path: Path, func_name: str = "get_device_status") -> set[str]:
    """从 `def <func_name>` 里取 `status = {...}` 字面量的键。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    func = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.FunctionDef) and n.name == func_name),
        None,
    )
    assert func is not None, f"{path.name} 里找不到 {func_name}"

    for node in ast.walk(func):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "status"
            and isinstance(node.value, ast.Dict)
        ):
            keys = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
            assert keys, f"{path.name} 的 {func_name} 里 status 字典没取到键"
            return keys

    raise AssertionError(f"{path.name} 的 {func_name} 里没找到 `status = {{...}}` 字面量")


@pytest.mark.parametrize("name,path", sorted(MANAGERS.items()))
def test_manager_exists(name, path):
    assert path.is_file(), f"缺少 {path}"


def test_all_managers_expose_frontend_required_fields():
    """每套实现的 get_device_status 都必须给出前端读的全部字段。"""
    missing = {}
    for name, path in MANAGERS.items():
        keys = _status_keys(path)
        gap = FRONTEND_REQUIRED - keys
        if gap:
            missing[name] = sorted(gap)

    assert not missing, (
        "以下实现的 get_device_status 缺前端要读的字段 —— "
        "用它的模式下对应 UI 会静默失效（筛选恒为 0 / 启停按钮永不显示）：\n"
        + "\n".join(f"  {n}: 缺 {v}" for n, v in missing.items())
    )


def test_managers_agree_on_frontend_required_subset():
    """三套实现都必须覆盖前端要读的字段（**这才是真正要守的不变量**）。"""
    shapes = {name: _status_keys(path) for name, path in MANAGERS.items()}
    gaps = {n: sorted(FRONTEND_REQUIRED - k) for n, k in shapes.items()}
    gaps = {n: v for n, v in gaps.items() if v}
    assert not gaps, f"以下实现缺前端要读的字段：{gaps}"


# 已知的、**刻意保留**的形状差异 —— 不是漏洞。
#
# 判据是「**并非三套实现都有**的字段」：
#   * `register_count`：只在 simulated 里算了，**全库无读取点**（"算了没人读"）
#   * `mode`（'real'/'simulated'）：无人读；前端 KPI 上的"模拟模式/真实设备"
#     来自 `stats.simulation_mode`，不是这里
# 所以**不**强迫三套都补齐 —— 逼形状完全相同只会到处塞死字段。
#
# 这条断言的真正作用是：**差异必须是"已知且写在这里的"**。
# 将来谁多给/少给一个字段，这里会红，逼一次有意识的决定
# （而不是像原先那样，形状悄悄分叉、只有前端某个 UI 静默失效）。
KNOWN_NON_UNIVERSAL = {"register_count", "mode"}


def test_shape_divergence_is_exactly_the_known_set():
    """「并非所有实现都有」的字段，只能等于 KNOWN_NON_UNIVERSAL 里登记的那些。"""
    shapes = {name: _status_keys(path) for name, path in MANAGERS.items()}
    union = set().union(*shapes.values())
    intersection = set.intersection(*shapes.values())
    non_universal = union - intersection

    unexpected = sorted(non_universal - KNOWN_NON_UNIVERSAL)
    stale = sorted(KNOWN_NON_UNIVERSAL - non_universal)

    assert not unexpected and not stale, (
        f"设备状态形状出现未登记的分叉：\n"
        f"  新增的差异（未登记）: {unexpected}\n"
        f"  登记了但已不存在    : {stale}\n"
        "  如果差异是刻意的（字段没人读），登记进 KNOWN_NON_UNIVERSAL 并写明理由；\n"
        "  如果字段**有人读**，那就是缺陷 —— 必须让所有实现都给。"
    )
