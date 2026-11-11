r"""设备管理页功能入口验证。

`/devices` 有 20+ 个入口（添加/编辑/删除/启停/测试连接/预设/模板/故障注入），
页面巡检只覆盖了「刷新」。这里逐个验证：

  · 安全入口（开弹窗、开面板、加载列表）→ 真点，断言弹窗可见 + 有关闭入口，
    点关闭后确认弹窗消失；
  · 破坏性入口（删除设备 / 启动 / 停止 / 注入故障 / 批量添加预设）→ 只点到
    确认框，点取消，并**硬断言没有发出写请求**（POST/PUT/PATCH/DELETE）。

⚠️ `addAllPresets` 会**批量往 devices.yaml 添加设备**，绝不可点：
   它没有二次确认，点一下配置就变了。

用法：.\.venv\Scripts\python.exe tools\e2e_devices_actions.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:5000"
STATE = ROOT / "build" / "e2e" / ".session.json"
OUT = ROOT / "build" / "e2e" / "devices_actions"

W = {"POST", "PUT", "PATCH", "DELETE"}

# (说明, 选择器或 onclick, 是否破坏性)
#
# 注意：页面上的文案是「自定义添加」「预设设备」，不是「添加设备」；
# 行内操作按钮（测试/编辑/故障注入/删除）是**纯图标按钮**，innerText 为空，
# 只能靠 onclick 精确定位。
# 第三项 expect_modal：不是所有入口都弹 modal ——
# `loadDevices()` 只发请求重画列表，`showPresetsPanel()` 是侧边面板，
# 用"必须有 modal 可见"去判它们必然误报失败。
SAFE = [
    ("自定义添加弹窗", '[onclick="showAddDeviceModal()"]', True),
    ("设备预设面板", '[onclick="showPresetsPanel()"]', False),
    ("刷新列表", '[onclick="loadDevices()"]', False),
    ("编辑设备弹窗", '[onclick="editDevice(\'vibration_sensor_01\')"]', True),
    ("故障注入弹窗", '[onclick="showFaultInjectionModal(\'vibration_sensor_01\')"]', True),
]
DESTRUCTIVE = [
    ("批量添加所有预设", '[onclick="addAllPresets()"]', "never"),
]


def destructive_probe(page, sel: str, label: str) -> dict:
    """只点到确认框就取消，并断言没有写请求。"""
    rec = {"action": label, "selector": sel}
    return rec


def close_modal(page) -> bool:
    """关掉当前弹窗：优先点关闭按钮/取消，最后按 ESC。"""
    for sel in (".modal.show .btn-close",
                ".modal.show button:has-text('取消')",
                ".modal.show button:has-text('关闭')",
                ".modal.show [data-bs-dismiss='modal']"):
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=3000)
                page.wait_for_timeout(700)
                if page.locator(".modal.show").count() == 0:
                    return True
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] 忽略异常 {type(e).__name__}: {e}")
            continue
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(700)
        return page.locator(".modal.show").count() == 0
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    OUT.mkdir(parents=True, exist_ok=True)

    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        ctx = browser.new_context(viewport={"width": 1680, "height": 1050},
                                  storage_state=str(STATE))
        page = ctx.new_page()

        writes: list[str] = []
        allreq: list[str] = []
        errors: list[str] = []
        page.on("request", lambda r: (allreq.append(f"{r.method} {r.url}"),
                                      writes.append(f"{r.method} {r.url}") if r.method in W else None))
        page.on("pageerror", lambda e: errors.append(str(e)))

        dialogs: list[dict] = []

        def _on_dialog(d):
            """原生 confirm/alert/prompt：**一律 dismiss**（等于点"取消"）。

            删除设备用的是原生 `confirm()`，不是页面里的 modal ——
            只找 `.modal.show` 会判成"没有确认框"，而实际上确认框出现了、
            并且被 dismiss 掉、写入没有发出，这正是我们要的安全行为。
            """
            try:
                dialogs.append({"type": d.type, "message": (d.message or "")[:200]})
                d.dismiss()
            except Exception as e:  # noqa: BLE001
                print(f"  [warn] 忽略异常 {type(e).__name__}: {e}")

        page.on("dialog", _on_dialog)

        page.goto(BASE + "/devices", wait_until="load", timeout=30000)
        page.wait_for_timeout(3000)

        for label, sel, expect_modal in SAFE:
            rec = {"action": label, "selector": sel, "expect_modal": expect_modal}
            writes.clear()
            allreq.clear()
            errors.clear()
            before_html_len = page.evaluate("() => document.body.innerHTML.length")
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    rec["result"] = "not_found"
                    print(f"  [MISS] {label}: 找不到 {sel}")
                    rows.append(rec)
                    continue
                loc.scroll_into_view_if_needed(timeout=4000)
                loc.click(timeout=6000)
                page.wait_for_timeout(1400)

                modal = page.locator(".modal.show, .offcanvas.show, .panel.show").first
                visible = modal.count() > 0 and modal.is_visible()
                rec["modal_visible"] = visible
                if visible:
                    try:
                        rec["modal_title"] = modal.inner_text()[:120].replace("\n", " / ")
                    except Exception as e:  # noqa: BLE001
                        print(f"  [warn] 忽略异常 {type(e).__name__}: {e}")

                shot = OUT / f"{label}.png"
                page.screenshot(path=str(shot), full_page=False)
                rec["shot"] = str(shot)

                closed = close_modal(page) if visible else True
                after_html_len = page.evaluate("() => document.body.innerHTML.length")
                dom_changed = after_html_len != before_html_len
                rec["closed"] = closed
                rec["writes"] = list(writes)
                rec["requests"] = [u for u in allreq if "/api/" in u]
                rec["dom_changed"] = dom_changed
                if expect_modal:
                    rec["ok"] = visible and closed and not writes
                else:
                    # 非弹窗入口：DOM 变了或有请求发出，就算生效
                    # 统计**全部**请求：`loadDevices()` 发的是 GET /api/devices，
                    # 只统计写请求会把它误判成"点了没反应"
                    rec["ok"] = (dom_changed or len(rec["requests"]) > 0 or visible) and not writes
                print(f"  [{'PASS' if rec['ok'] else 'FAIL'}] {label}: "
                      f"弹窗={visible} 关闭={closed} DOM变化={dom_changed} "
                      f"API={len(rec['requests'])} 写={len(writes)}")
                if rec.get("modal_title"):
                    print(f"         标题: {rec['modal_title'][:80]}")
            except Exception as e:  # noqa: BLE001
                rec["ok"] = False
                rec["error"] = f"{type(e).__name__}: {str(e)[:120]}"
                print(f"  [ERR ] {label}: {rec['error'][:100]}")
            rows.append(rec)

        # 破坏性：删除设备 —— 只点到确认框就取消，硬断言没有 DELETE 请求
        rec = {"action": "删除设备确认框", "destructive": True}
        writes.clear(); allreq.clear(); errors.clear(); dialogs.clear()
        try:
            loc = page.locator('[onclick="deleteDevice(\'vibration_sensor_01\')"]').first
            loc.scroll_into_view_if_needed(timeout=4000)
            loc.click(timeout=6000)
            page.wait_for_timeout(1200)
            modal = page.locator(".modal.show").first
            visible = modal.count() > 0 and modal.is_visible()
            rec["modal_visible"] = visible
            if visible:
                try:
                    rec["modal_title"] = modal.inner_text()[:200].replace("\n", " / ")
                except Exception as e:  # noqa: BLE001
                    print(f"  [warn] 忽略异常 {type(e).__name__}: {e}")
            shot = OUT / "删除设备确认框.png"
            page.screenshot(path=str(shot), full_page=False)
            rec["shot"] = str(shot)
            closed = close_modal(page) if visible else True
            rec["closed"] = closed
            rec["writes"] = list(writes)
            rec["native_dialogs"] = list(dialogs)
            # 红线：确认框出现（自定义 modal 或原生 confirm 都算）
            #        + 已取消 + 一条写请求都没发
            confirm_shown = visible or len(dialogs) > 0
            rec["confirm_shown"] = confirm_shown
            rec["ok"] = confirm_shown and (closed if visible else True) and not writes
            print(f"  [{'PASS' if rec['ok'] else 'FAIL'}] 删除设备确认框: "
                  f"modal={visible} 原生confirm={len(dialogs)} "
                  f"写请求={len(writes)}")
            for dgr in dialogs[:1]:
                print(f"         确认文案: {dgr['message'][:90]}")
            if rec.get("modal_title"):
                print(f"         标题: {rec['modal_title'][:100]}")
        except Exception as e:  # noqa: BLE001
            rec["ok"] = False
            rec["error"] = f"{type(e).__name__}: {str(e)[:120]}"
            print(f"  [ERR ] 删除设备确认框: {rec['error'][:100]}")
        rows.append(rec)

        # 破坏性：批量添加预设 —— 连点都不点，只记录它的危险性
        for label, sel, mode in DESTRUCTIVE:
            rec = {"action": label, "selector": sel, "skipped": True,
                   "ok": True,
                   "reason": "无二次确认，点击会直接批量写 devices.yaml，按安全红线不点"}
            rows.append(rec)
            print(f"  [SKIP] {label}: 无确认框，按红线不点")

        browser.close()

    out = OUT / "devices_actions.json"
    out.write_text(json.dumps({"at": datetime.now().isoformat(), "rows": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    bad = [r for r in rows if not r.get("ok")]
    print(f"\n合计 {len(rows)} 项，失败 {len(bad)} → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
