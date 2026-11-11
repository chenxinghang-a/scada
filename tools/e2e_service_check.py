r"""服务级检查：不经过页面，直接打接口 + 验 WebSocket + 验 404 分流。

用法：.\.venv\Scripts\python.exe tools\e2e_service_check.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:5000"

# 各页面实际调用的接口（从 JS/template 里 grep 出来的）
API_LIST = [
    ("GET", "/api/health/status", "健康探活"),
    ("GET", "/api/health/status/detail", "健康明细"),
    ("GET", "/api/system/status", "系统状态（仪表盘/大屏）"),
    ("GET", "/api/devices", "设备列表"),
    ("GET", "/api/data/realtime?limit=1000", "实时数据"),
    ("GET", "/api/alarms?limit=50", "报警列表"),
    ("GET", "/api/alarms/active", "活动报警"),
    ("GET", "/api/alarm-rules", "报警规则"),
    ("GET", "/api/industry40/overview", "工业4.0 总览"),
    ("GET", "/api/industry40/oee", "OEE"),
    ("GET", "/api/industry40/energy", "能耗"),
    ("GET", "/api/industry40/health", "健康度"),
    ("GET", "/api/devices/presets", "设备预设"),
    ("GET", "/api/auth/users", "用户列表"),
    ("POST", "/api/export/device/vibration_sensor_01", "单设备导出", {"start_time": "", "end_time": ""}),
]

PAGES = ["/dashboard", "/devices", "/control", "/history", "/alarms",
         "/alarm-output", "/industry40", "/config", "/users", "/screen"]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止自动跟随重定向。urllib 默认会把 302 → /login → 200 一路吃掉，
    于是"未登录被踢回登录页"看起来像"这个页面正常返回 200"。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def req(method, path, token=None, timeout=15, as_page=False, json_body=None):
    url = BASE + path
    data = None
    if json_body is not None:
        from datetime import datetime, timedelta
        now = datetime.now()
        json_body = dict(json_body)
        # 空串会被接口判成"缺少参数"，这里补成最近 1 小时
        json_body["start_time"] = json_body.get("start_time") or \
            (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        json_body["end_time"] = json_body.get("end_time") or \
            now.strftime("%Y-%m-%d %H:%M:%S")
        data = json.dumps(json_body).encode("utf-8")
    r = urllib.request.Request(url, method=method, data=data)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
        if as_page:
            r.add_header("Cookie", "token=" + token)
    t0 = time.time()
    try:
        with _opener.open(r, timeout=timeout) as resp:
            body = resp.read(2000)
            return resp.status, round((time.time() - t0) * 1000, 1), body
    except urllib.error.HTTPError as e:
        # 3xx 也走这里（_NoRedirect 让 redirect_request 返回 None）
        return e.code, round((time.time() - t0) * 1000, 1), e.read(500)
    except Exception as e:  # noqa: BLE001
        return -1, round((time.time() - t0) * 1000, 1), str(e).encode()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    token = os.environ.get("SCADA_TOKEN", "").strip()
    if not token:
        for cand in (ROOT / "build" / "e2e" / ".token",
                     Path(tempfile.gettempdir()) / "scada_token.txt"):
            if cand.exists():
                token = cand.read_text(encoding="utf-8").strip()
                break

    rows = []
    print("=" * 78)
    print("A. 接口重放（带 token）")
    print("=" * 78)
    for item in API_LIST:
        method, path, label = item[0], item[1], item[2]
        payload = item[3] if len(item) > 3 else None
        code, ms, body = req(method, path, token, json_body=payload)
        ok = 200 <= code < 400
        rows.append({"kind": "api", "path": path, "label": label,
                     "status": code, "ms": ms, "ok": ok,
                     "body_head": body[:160].decode("utf-8", "replace")})
        flag = "OK " if ok else "!! "
        print(f"  {flag}{code:>4} {ms:>8.1f}ms  {label:<20} {path}")

    print()
    print("=" * 78)
    print("B. 页面路由（带 cookie）")
    print("=" * 78)
    for p in PAGES:
        code, ms, body = req("GET", p, token, as_page=True)
        rows.append({"kind": "page", "path": p, "status": code, "ms": ms,
                     "ok": code in (200, 302)})
        print(f"  {code:>4} {ms:>8.1f}ms  {p}")

    print()
    print("=" * 78)
    print("C. 404 分流")
    print("=" * 78)
    for p, desc in [("/api/no-such-endpoint", "API 不存在 → JSON"),
                    ("/no-such-page", "页面不存在 → 应 HTML/302")]:
        code, ms, body = req("GET", p, token)
        is_json = body.strip().startswith(b"{")
        rows.append({"kind": "404", "path": p, "status": code,
                     "is_json": is_json, "body_head": body[:120].decode("utf-8", "replace")})
        print(f"  {code:>4} json={is_json!s:<5} {p}  ({desc})")

    out = ROOT / "build" / "e2e" / "service_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    bad = [r for r in rows if r.get("ok") is False or (r.get("status", 200) >= 500)]
    print()
    print(f"合计 {len(rows)} 项，异常 {len(bad)} 项 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
