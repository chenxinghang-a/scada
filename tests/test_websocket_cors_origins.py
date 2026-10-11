# -*- coding: utf-8 -*-
"""WebSocket CORS 白名单必须含 `file://`（桌面版实时通道的命门）。

缺陷（2026-10-10，round 212；wb 在隔离副本上定位并实测）
======================================================
桌面版（Electron）页面是**用 `file://` 加载**的 —— 它的 `Origin` 就是字符串
`file://`。而 `展示层/websocket.py::_load_cors_origins()` 读
`配置/system.yaml` 的 `web.cors.origins`，里面**只有**
`http://localhost:5000` / `http://127.0.0.1:5000` → 不含 `file://`
→ engine.io 直接回 **`file:// is not an accepted origin.`**
→ **整条实时通道全废**：性能监控、数据大屏、报警输出**全部走 socket.io**。

实测（wb，隔离副本）：

| | 修前 | 修后 |
|---|---|---|
| `curl -H 'Origin: file://' …/socket.io/?EIO=4&transport=polling` | `Not an accepted origin.` | 200 + `Access-Control-Allow-Origin: file://` |
| 数据大屏 | 设备在线 **0/0**、吞吐 0 条/分 | **30/30**、**2615 条/分** |
| 性能监控 | 「未连接」 | 「已连接」 |

> ⚠️ 这条缺陷**在开发环境里看不出来**（开发时前端是 `http://localhost:5173`，
> 在允许列表里）。只有**打包成桌面版**之后，`file://` 这个 Origin 才出现 ——
> 典型的「本机绿、产物废」。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

BACKEND_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_YAML = BACKEND_ROOT / "配置" / "system.yaml"

#: 桌面版页面的 Origin。**不允许从允许列表里消失**。
DESKTOP_ORIGIN = "file://"


@pytest.fixture(scope="module")
def origins_from_config() -> list[str]:
    cfg = yaml.safe_load(SYSTEM_YAML.read_text(encoding="utf-8")) or {}
    return (cfg.get("web") or {}).get("cors", {}).get("origins") or []


class TestCorsOriginsConfig:
    def test_config_has_an_origins_list(self, origins_from_config):
        """防瞎：配置里必须真有一份非空列表，否则下面的断言是空的。"""
        assert isinstance(origins_from_config, list) and origins_from_config, (
            "配置/system.yaml 的 web.cors.origins 缺失或为空"
        )

    def test_desktop_origin_is_allowed(self, origins_from_config):
        """★ 核心：`file://` 必须在白名单里（否则桌面版实时通道全废）。"""
        assert DESKTOP_ORIGIN in origins_from_config, (
            f"web.cors.origins 不含 {DESKTOP_ORIGIN!r} ——\n"
            "  桌面版（Electron）页面是 file:// 加载的，Origin 就是 'file://'；\n"
            "  少了它 engine.io 会回「file:// is not an accepted origin.」，\n"
            "  性能监控/数据大屏/报警输出（全走 socket.io）会**全部不可用**。\n"
            f"  当前列表：{origins_from_config}"
        )

    def test_no_wildcard(self, origins_from_config):
        """禁止 `*` 通配 —— 那等于对任意来源开放（配置注释里也写了）。"""
        assert "*" not in origins_from_config, (
            "web.cors.origins 用了 '*' 通配 —— 等于允许任意来源，禁止"
        )


class TestLoaderActuallyReturnsIt:
    """不只看配置文件 —— 还要看**真正喂给 engine.io 的那个返回值**。"""

    def test_loader_returns_desktop_origin(self):
        from 展示层.websocket import _load_cors_origins

        origins = _load_cors_origins()
        assert isinstance(origins, list) and origins, "_load_cors_origins() 返回空"
        assert DESKTOP_ORIGIN in origins, (
            "_load_cors_origins() 的返回值不含 'file://' —— "
            "配置改了但加载路径没走到，或者被 fallback 覆盖了"
        )

    def test_fallback_also_allows_desktop_origin(self):
        """配置读不到时的**兜底列表**也必须含 `file://`。

        否则「配置丢了」这种降级场景会把实时通道悄悄打死 ——
        降级不能让功能静默消失。
        """
        import inspect
        from 展示层 import websocket as ws_mod

        src = inspect.getsource(ws_mod._load_cors_origins)
        # 兜底分支是最后那个 return（没有配置时的返回值）
        tail = src.rsplit("return", 1)[-1]
        assert DESKTOP_ORIGIN in tail, (
            f"_load_cors_origins() 的兜底返回值里没有 {DESKTOP_ORIGIN!r}：\n  {tail.strip()}\n"
            "  配置读不到时（降级路径）桌面版实时通道会再次全废。"
        )
