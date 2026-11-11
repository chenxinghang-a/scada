r"""Web E2E 巡检：登录 → 逐页打开 → 点安全按钮 → 截图 → 收集缺陷。

用法：
    .\.venv\Scripts\python.exe tools\e2e_web_walkthrough.py
    .\.venv\Scripts\python.exe tools\e2e_web_walkthrough.py --headful

产物：
    build/e2e/<时间戳>/*.png     截图
    build/e2e/<时间戳>/report.json
    build/e2e/<时间戳>/console.log
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:5000"
USER, PWD = "admin", "admin123"

# 页面清单：(路由, 中文名, 安全按钮选择器列表)
PAGES = [
    ("/dashboard", "仪表盘", []),
    ("/devices", "设备管理", []),
    ("/control", "控制面板", []),
    ("/history", "历史数据", []),
    ("/alarms", "报警管理", []),
    ("/alarm-output", "报警输出", []),
    ("/industry40", "工业4.0", []),
    ("/config", "系统配置", []),
    ("/users", "用户管理", []),
    ("/screen", "大屏", []),
]


class Recorder:
    def __init__(self, outdir: Path):
        self.outdir = outdir
        self.pages: list[dict] = []
        self.console: list[str] = []
        self.http_errors: list[dict] = []
        self.t0 = time.time()
        self.req_count = 0        # 累计请求数：用来判断"点按钮有没有发请求"

    def log(self, msg: str):
        line = f"[{time.time() - self.t0:7.2f}s] {msg}"
        self.console.append(line)
        print(line, flush=True)

    def page_entry(self, name: str) -> dict:
        e = {
            "name": name,
            "url": "",
            "load_ok": False,
            "console_errors": [],
            "page_errors": [],
            "http_4xx_5xx": [],
            "bad_text": [],
            "buttons_tried": [],
            "shots": [],
        }
        self.pages.append(e)
        return e

    def dump(self):
        (self.outdir / "console.log").write_text("\n".join(self.console), encoding="utf-8")
        (self.outdir / "report.json").write_text(
            json.dumps(
                {
                    "started": datetime.fromtimestamp(time.time() - (time.time() - self.t0)).isoformat(),
                    "base": BASE,
                    "pages": self.pages,
                    "http_errors": self.http_errors,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def shot(page, rec: Recorder, name: str, full: bool = True):
    safe = re.sub(r"[^\w\-]+", "_", name)
    path = rec.outdir / f"{safe}.png"
    try:
        page.screenshot(path=str(path), full_page=full)
        rec.log(f"    截图 {path.name}")
        return str(path)
    except Exception as e:  # noqa: BLE001
        rec.log(f"    !! 截图失败 {name}: {e}")
        return ""


def attach_listeners(page, rec: Recorder, entry: dict):
    """把监听器绑到 page 上；entry 用可变容器包住，导航后仍记到"当前页面"。

    Playwright 的 page 对象在 goto 换页时不变，如果直接闭包捕获 entry，
    后续页面的报错会被记到第一个页面的账上（上一轮就出现过：
    工业4.0 的 `exportI40Data is not defined` 记到了"登录页"）。
    """
    holder = {"entry": entry}

    def cur():
        return holder["entry"]

    def on_console(msg):
        if msg.type == "error":
            cur()["console_errors"].append(msg.text)
            rec.log(f"    !! console error: {msg.text[:200]}")

    def on_pageerror(err):
        txt = str(err)
        cur()["page_errors"].append(txt)
        rec.log(f"    !! pageerror: {txt[:300]}")

    def on_request(r):
        rec.req_count += 1
        try:
            if "/api/" in r.url:
                cur().setdefault("api_calls", []).append(r.url.split("?")[0])
        except Exception:  # noqa: BLE001
            pass

    def on_response(resp):
        # 先把记录建好再抓 body：resp.text() 对已消费/无体的响应会抛异常，
        # 若把它和 append 放在同一个 try 里，4xx/5xx 会整条丢失（脚本自身的坑）。
        try:
            status = resp.status
            if status < 400:
                return
            item = {"url": resp.url, "status": status,
                    "method": resp.request.method, "body": ""}
            cur()["http_4xx_5xx"].append(item)
            rec.http_errors.append({**item, "page": cur()["name"]})
            rec.log(f"    !! HTTP {status} {resp.request.method} {resp.url}")
        except Exception:  # noqa: BLE001
            return
        try:
            ct = resp.headers.get("content-type", "")
            if "json" in ct or "text" in ct:
                item["body"] = resp.text()[:200]
        except Exception:  # noqa: BLE001
            pass

    page.on("request", on_request)
    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("response", on_response)
    return holder


def check_bad_text(page, entry: dict, rec: Recorder):
    """检查可见文本里的 undefined / NaN / null。"""
    try:
        txt = page.inner_text("body", timeout=5000)
    except Exception:  # noqa: BLE001
        return
    hits = []
    for pat in [r"\bundefined\b", r"\bNaN\b", r"\bnull\b"]:
        for m in re.finditer(pat, txt):
            s = max(0, m.start() - 40)
            hits.append(txt[s:m.end() + 40].replace("\n", " "))
    if hits:
        entry["bad_text"] = hits[:8]
        for h in hits[:3]:
            rec.log(f"    !! 疑似坏文本: {h[:120]}")


def login(ctx, rec: Recorder, reuse_state: Path | None = None):
    """走真实登录表单。

    `/api/auth/login` 有 10 次/小时的硬限流，反复跑巡检很容易把自己锁在门外
    （实测跑第 6 轮就 429）。所以登录成功一次后把会话存成 storage_state，
    下一轮直接复用；`reuse_state` 命中时不碰登录接口。

    注意：复用会话时不再验证登录页本身，登录页的巡检结果取自最近一次
    真实登录的那一轮（report.json 里保留）。
    """
    page = ctx.new_page()
    entry = rec.page_entry("登录页")
    holder = attach_listeners(page, rec, entry)

    if reuse_state and reuse_state.exists():
        rec.log(f"[登录] 复用已保存会话 {reuse_state.name}")
        page.goto(BASE + "/dashboard", wait_until="load", timeout=30000)
        page.wait_for_timeout(2000)
        entry["url"] = page.url
        entry["login_ok"] = "/dashboard" in page.url
        entry["reused_session"] = True
        rec.log(f"[登录] 复用结果 url={page.url} ok={entry['login_ok']}")
        if not entry["login_ok"]:
            rec.log("[登录] 会话失效，回落到表单登录")
        else:
            return page, holder

    page.goto(BASE + "/login", wait_until="load", timeout=30000)
    page.wait_for_timeout(1200)
    entry["url"] = page.url
    entry["load_ok"] = page.locator("#username").count() > 0
    rec.log(f"[登录] title={page.title()!r} url={page.url}")
    entry["shots"].append(shot(page, rec, "00_登录页"))
    check_bad_text(page, entry, rec)

    page.fill("#username", USER)
    page.fill("#password", PWD)
    shot(page, rec, "01_登录页_已填表")
    page.click("#loginBtn")
    try:
        page.wait_for_url("**/dashboard", timeout=20000)
    except Exception:  # noqa: BLE001
        rec.log(f"[登录] 未跳转 dashboard，当前 {page.url}")
    page.wait_for_timeout(2500)
    entry["after_login_url"] = page.url
    entry["login_ok"] = "/dashboard" in page.url
    entry["shots"].append(shot(page, rec, "02_登录后首页"))
    rec.log(f"[登录] 结果 url={page.url} ok={entry['login_ok']}")

    if entry["login_ok"] and reuse_state:
        try:
            ctx.storage_state(path=str(reuse_state))
            rec.log(f"[登录] 会话已保存到 {reuse_state}")
        except Exception as e:  # noqa: BLE001
            rec.log(f"[登录] 会话保存失败: {e}")

    return page, holder


def collect_nav(page, rec: Recorder) -> list[dict]:
    """抓导航栏所有链接。"""
    items = []
    try:
        links = page.locator("nav a.nav-link, .navbar a.nav-link, aside a, .sidebar a")
        for i in range(links.count()):
            el = links.nth(i)
            try:
                href = el.get_attribute("href") or ""
                text = (el.inner_text() or "").strip()
                if href and not href.startswith("#") and href not in ("/logout",):
                    items.append({"href": href, "text": text})
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        rec.log(f"导航抓取失败: {e}")
    seen, out = set(), []
    for it in items:
        k = it["href"]
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


def probe_buttons(page, rec: Recorder, entry: dict, limit: int = 12):
    """枚举页面上可点的安全按钮并逐个点击，断言 DOM 有变化。"""
    safe_keywords = ["刷新", "查询", "搜索", "筛选", "重置", "翻页", "上一页", "下一页",
                     "导出", "导出CSV", "导出Excel", "确定查询", "应用", "概览", "统计"]
    skip_keywords = ["删除", "清空", "急停", "复位", "停机", "登出", "注销", "写入",
                     "下发", "启动", "停止", "修改", "保存"]
    try:
        btns = page.locator("button:visible, a.btn:visible")
        n = min(btns.count(), 40)
    except Exception:  # noqa: BLE001
        return

    for i in range(n):
        if len(entry["buttons_tried"]) >= limit:
            break
        el = btns.nth(i)
        try:
            if not el.is_visible() or not el.is_enabled():
                continue
            text = (el.inner_text() or "").strip()
            if not text or len(text) > 20:
                continue
            if any(k in text for k in skip_keywords):
                entry["buttons_tried"].append({"text": text, "action": "skip_destructive"})
                continue
            if not any(k in text for k in safe_keywords):
                continue

            before = page.inner_text("body")[:4000]
            before_url = page.url
            before_reqs = rec.req_count
            before_nodes = page.evaluate("() => document.body.getElementsByTagName('*').length")
            el.click(timeout=4000)
            page.wait_for_timeout(1200)
            after = page.inner_text("body")[:4000]
            after_nodes = page.evaluate("() => document.body.getElementsByTagName('*').length")
            # "有反应"的三种证据：文本变了 / URL 变了 / 打了接口 / DOM 节点数变了。
            # 只看 innerText 会误判「刷新」：数据没变时文本一模一样，
            # 但请求确实发了、表格也确实重绘了（节点数是变的）。
            fired = rec.req_count - before_reqs
            changed = (before != after) or (page.url != before_url) or fired > 0 \
                or (after_nodes != before_nodes)
            entry["buttons_tried"].append(
                {"text": text, "changed": changed, "requests": fired,
                 "text_changed": before != after, "nodes_delta": after_nodes - before_nodes})
            rec.log(f"    按钮[{text}] 变化={changed} 请求={fired} 节点差={after_nodes - before_nodes}")
            shot(page, rec, f"{entry['name']}_btn_{text}")
            if not changed:
                rec.log(f"    !! 按钮无反应: {text}")
        except Exception as e:  # noqa: BLE001
            rec.log(f"    按钮点击异常: {type(e).__name__}: {str(e)[:150]}")


def visit(page, rec: Recorder, route: str, name: str, holder=None):
    entry = rec.page_entry(name)
    if holder is not None:
        holder["entry"] = entry
    rec.log(f"[页面] {name} {route}")
    try:
        page.goto(BASE + route, wait_until="load", timeout=30000)
    except Exception as e:  # noqa: BLE001
        rec.log(f"    !! 打开失败: {e}")
        return entry
    # 等网络空闲，最多 8s；卡住退化为 load + 1s
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:  # noqa: BLE001
        page.wait_for_timeout(1000)
    page.wait_for_timeout(1500)

    entry["url"] = page.url
    entry["load_ok"] = route in page.url
    entry["title"] = page.title()
    entry["shots"].append(shot(page, rec, f"{name}_整页"))
    check_bad_text(page, entry, rec)
    probe_buttons(page, rec, entry, limit=10)
    return entry


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headful", action="store_true")
    ap.add_argument("--only", default="", help="只跑某个页面名")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = ROOT / "build" / "e2e" / stamp
    outdir.mkdir(parents=True, exist_ok=True)
    rec = Recorder(outdir)
    rec.log(f"证据目录 {outdir}")

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=not args.headful,
                                    args=["--no-sandbox", "--disable-dev-shm-usage"])
        state_file = ROOT / "build" / "e2e" / ".session.json"
        ctx_kwargs = {"viewport": {"width": 1680, "height": 1050},
                      "ignore_https_errors": True}
        # 会话文件既用于"跳过登录"的判断，也直接喂给 context —— 否则
        # localStorage 里没有 auth_token，页面虽然有 cookie 能过页面级鉴权，
        # 前端 fetch 却仍是未登录态（会成片 401）。
        if state_file.exists():
            ctx_kwargs["storage_state"] = str(state_file)
        ctx = browser.new_context(**ctx_kwargs)
        ctx.set_default_timeout(15000)

        try:
            page, holder = login(ctx, rec, reuse_state=state_file)
            nav = collect_nav(page, rec)
            rec.log(f"[导航] {len(nav)} 项: " + ", ".join(f"{x['text']}→{x['href']}" for x in nav))
            (outdir / "nav.json").write_text(json.dumps(nav, ensure_ascii=False, indent=2),
                                             encoding="utf-8")

            for route, name, _ in PAGES:
                if args.only and args.only not in name:
                    continue
                visit(page, rec, route, name, holder)

            # 异常路径
            rec.log("[异常] 未登录访问受保护页")
            ctx2 = browser.new_context(viewport={"width": 1280, "height": 900})
            p2 = ctx2.new_page()
            p2.goto(BASE + "/devices", wait_until="load", timeout=20000)
            p2.wait_for_timeout(1200)
            e = rec.page_entry("未登录访问_devices")
            e["url"] = p2.url
            e["redirected_to_login"] = "/login" in p2.url
            rec.log(f"    结果 url={p2.url}")
            shot(p2, rec, "90_未登录访问devices")

            rec.log("[异常] 访问不存在的路由（已登录态）")
            # 必须在**已登录**的 context 里测：未登录时 before_request 会先把
            # 任何页面路由 302 到 /login，永远看不到 404 页 —— 那测的是登录拦截，
            # 不是 404 处理。这里复用主 context 新开一个标签页。
            p3 = ctx.new_page()
            resp = p3.goto(BASE + "/no-such-page-xyz", wait_until="load", timeout=20000)
            p3.wait_for_timeout(800)
            e2 = rec.page_entry("404页")
            e2["url"] = p3.url
            e2["http_status"] = resp.status if resp else None
            e2["status_text"] = p3.inner_text("body")[:300]
            e2["is_html_404"] = (resp is not None and resp.status == 404
                                 and "404" in e2["status_text"])
            rec.log(f"    结果 status={e2['http_status']} url={p3.url} "
                    f"html404={e2['is_html_404']}")
            shot(p3, rec, "91_404页")
            p3.close()
            ctx2.close()

        except Exception:  # noqa: BLE001
            rec.log("!!! 巡检异常\n" + traceback.format_exc())
        finally:
            rec.dump()
            browser.close()

    rec.log(f"完成，报告 {outdir / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
