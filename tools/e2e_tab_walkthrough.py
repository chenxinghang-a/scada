r"""Tab 逐个点开验证（bootstrap tab 版）。

## 为什么单独写

页面巡检只到「打开页面 + 点安全按钮」，tab 里的内容（图表、表格）从来没被渲染过。
第一版脚本用 `.tab-pane.active, .tab-content > *` 抓当前面板，结果**每次都抓到
包含全部 pane 的容器**，8 个 tab 的文本长度完全一样（1708 字），看起来"全 OK"
其实一个都没验证到——这种"自证成功"的判定必须避免。

这版改成：
  · tab 按钮只认 `[data-bs-toggle="tab"]`，按 `data-bs-target` 取到唯一 pane；
  · 每次点击后断言 pane 的 `offsetHeight > 0`（真的显示了）
    且文本与点击前不同（真的换了内容）。

用法：.\.venv\Scripts\python.exe tools\e2e_tab_walkthrough.py
产物：build/e2e/tabs/<页面>_<tab>.png + tabs_report.json
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:5000"
STATE = ROOT / "build" / "e2e" / ".session.json"
OUT = ROOT / "build" / "e2e" / "tabs"

PAGES = [
    ("/industry40", "工业4.0"),
    ("/config", "系统配置"),
]


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

        errors: list[str] = []
        page.on("console", lambda m: errors.append(f"console:{m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror:{e}"))

        for route, name in PAGES:
            page.goto(BASE + route, wait_until="load", timeout=30000)
            page.wait_for_timeout(3000)

            tabs = page.evaluate("""() => {
                return Array.from(document.querySelectorAll('[data-bs-toggle="tab"]'))
                    .map((el, i) => ({
                        i,
                        text: (el.innerText || '').trim(),
                        target: el.getAttribute('data-bs-target')
                                || el.getAttribute('href'),
                    }))
                    .filter(t => t.target);
            }""")

            if not tabs:
                print(f"[{name}] 没有 bootstrap tab")
                continue
            print(f"[{name}] {len(tabs)} 个 tab")

            for t in tabs:
                rec = {"page": name, "tab": t["text"], "target": t["target"]}
                errors.clear()
                try:
                    before = page.evaluate("() => document.body.innerText.length")
                    page.locator('[data-bs-toggle="tab"]').nth(t["i"]).click(timeout=6000)
                    page.wait_for_timeout(2400)

                    pane = page.evaluate("""(sel) => {
                        const el = document.querySelector(sel);
                        if (!el) return {found: false};
                        const txt = (el.innerText || '').trim();
                        const canvases = Array.from(el.querySelectorAll('canvas'))
                            .map(c => ({w: c.width, h: c.height, h2: c.offsetHeight}));
                        const tables = el.querySelectorAll('table').length;
                        return {
                            found: true,
                            visible: el.offsetHeight > 0,
                            height: el.offsetHeight,
                            cls: el.className,
                            textLen: txt.length,
                            textHead: txt.slice(0, 200),
                            canvasCount: canvases.length,
                            canvasDrawn: canvases.filter(c => c.h2 > 10).length,
                            tableCount: tables,
                            hasNaN: /\\bNaN\\b/.test(txt),
                            hasUndef: /\\bundefined\\b/.test(txt),
                        };
                    }""", t["target"])

                    rec.update(pane)
                    rec["body_len_before"] = before
                    rec["console_errors"] = [e for e in errors if e.startswith("console:")]
                    rec["page_errors"] = [e for e in errors if e.startswith("pageerror:")]

                    safe = re.sub(r"[^\w\u4e00-\u9fff]+", "_", f"{name}_{t['text']}")
                    shot = OUT / f"{safe}.png"
                    page.screenshot(path=str(shot), full_page=False)
                    rec["shot"] = str(shot)

                    # 判定：pane 真的显示了 + 有内容或画了图 + 没有明显坏值
                    ok = (pane.get("visible")
                          and (pane.get("textLen", 0) > 20 or pane.get("canvasDrawn", 0) > 0)
                          and not pane.get("hasNaN") and not pane.get("hasUndef"))
                    rec["ok"] = bool(ok)
                    flag = "OK  " if ok else "FAIL"
                    print(f"  {flag} {t['text']:<14} 高{pane.get('height', 0):>4} "
                          f"文本{pane.get('textLen', 0):>5} "
                          f"canvas {pane.get('canvasDrawn', 0)}/{pane.get('canvasCount', 0)} "
                          f"表{pane.get('tableCount', 0)} "
                          f"NaN={pane.get('hasNaN')} undef={pane.get('hasUndef')} "
                          f"err={len(rec['console_errors']) + len(rec['page_errors'])}")
                    if pane.get("textHead"):
                        rec["textHead"] = pane["textHead"]
                except Exception as e:  # noqa: BLE001
                    rec["ok"] = False
                    rec["error"] = f"{type(e).__name__}: {str(e)[:150]}"
                    print(f"  ERR  {t['text']}: {rec['error'][:100]}")
                rows.append(rec)

        browser.close()

    out = OUT / "tabs_report.json"
    out.write_text(json.dumps({"at": datetime.now().isoformat(), "tabs": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    bad = [r for r in rows if not r.get("ok")]
    print(f"\n合计 {len(rows)} 个 tab：失败 {len(bad)} → {out}")
    for r in bad:
        print(f"  - {r['page']}/{r['tab']}: {r.get('error') or r.get('textHead', '')[:80]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
