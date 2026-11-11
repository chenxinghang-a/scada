r"""破坏性按钮的安全验证：只点到确认框，然后取消。

安全红线：
  · 绝不点确认；
  · 每次点击前后对比**写类请求**（POST/PUT/DELETE），断言取消后一个都没发出去。
    只看弹窗消失是不够的 —— 有些实现会把请求挂在"打开弹窗"里。

用法：.\.venv\Scripts\python.exe tools\e2e_destructive_probe.py
产物：build/e2e/destructive/<按钮>.png + destructive_report.json
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
OUT = ROOT / "build" / "e2e" / "destructive"

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    OUT.mkdir(parents=True, exist_ok=True)

    cases = [
        # (页面, 按钮文案/选择器, 说明)
        ("/control", "#btn-estop", "紧急停机 E-STOP"),
        ("/control", "button:has-text('启动所有设备')", "批量启动所有设备"),
        ("/control", "button:has-text('停止所有设备')", "批量停止所有设备"),
        ("/alarm-output", "button:has-text('广播')", "报警广播"),
        ("/users", "button:has-text('添加用户')", "添加用户"),
    ]

    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        ctx = browser.new_context(viewport={"width": 1680, "height": 1050},
                                  storage_state=str(STATE))
        page = ctx.new_page()

        writes: list[str] = []
        page.on("request",
                lambda r: writes.append(f"{r.method} {r.url}")
                if r.method in WRITE_METHODS else None)

        for route, sel, label in cases:
            rec = {"page": route, "button": label, "selector": sel}
            try:
                page.goto(BASE + route, wait_until="load", timeout=30000)
                page.wait_for_timeout(2500)
                el = page.locator(sel).first
                if el.count() == 0:
                    rec["result"] = "button_not_found"
                    rows.append(rec)
                    print(f"  [SKIP] {label}: 找不到按钮 {sel}")
                    continue

                writes.clear()
                el.click(timeout=6000)
                page.wait_for_timeout(1200)

                # 三种可能：Bootstrap 弹窗 / 原生 confirm / 无弹窗
                modal = page.locator("#confirmModal.show, .modal.show").first
                has_modal = modal.count() > 0 and modal.is_visible()
                rec["modal_visible"] = has_modal
                if has_modal:
                    try:
                        rec["modal_text"] = page.locator(".modal.show").first.inner_text()[:300]
                    except Exception as e:  # noqa: BLE001
                        print(f"  [warn] 忽略异常 {type(e).__name__}: {e}")

                safe = "".join(c if c.isalnum() else "_" for c in label)
                shot = OUT / f"{safe}.png"
                page.screenshot(path=str(shot), full_page=False)
                rec["shot"] = str(shot)

                if has_modal:
                    # 只点"取消"：Bootstrap 里是 data-bs-dismiss 的 btn-secondary
                    cancel = page.locator(
                        ".modal.show .modal-footer button.btn-secondary, "
                        ".modal.show button[data-bs-dismiss='modal']").first
                    if cancel.count() > 0:
                        cancel.click(timeout=5000)
                        page.wait_for_timeout(900)
                        rec["cancelled"] = True
                        rec["modal_closed"] = page.locator(".modal.show").count() == 0
                    else:
                        # 找不到取消按钮就不动，交给人工
                        rec["cancelled"] = False
                        rec["note"] = "未找到取消按钮，已停止操作"
                else:
                    rec["note"] = "点击后没有出现确认弹窗"

                rec["write_requests_after_click"] = list(writes)
                rec["safe"] = len(writes) == 0
                print(f"  [{'PASS' if rec.get('safe') else 'FAIL'}] {label}: "
                      f"弹窗={has_modal} 取消={rec.get('cancelled')} "
                      f"写请求={len(writes)}")
            except Exception as e:  # noqa: BLE001
                rec["result"] = "error"
                rec["error"] = f"{type(e).__name__}: {e}"
                print(f"  [ERR ] {label}: {rec['error'][:120]}")
            rows.append(rec)

        browser.close()

    out = OUT / "destructive_report.json"
    out.write_text(json.dumps({"at": datetime.now().isoformat(), "cases": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    bad = [r for r in rows if r.get("safe") is False]
    print(f"\n合计 {len(rows)} 个破坏性按钮，发出写请求的 {len(bad)} 个 → {out}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
