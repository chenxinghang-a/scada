"""用 OSV.dev 漏洞库审计当前 venv 的依赖（无需安装任何额外包）。"""
import json
import subprocess
import sys
import urllib.request

PROXY = "http://127.0.0.1:10808"
VENV_PY = r"C:/Users/cxx/WorkBuddy/Claw/industrial_scada/.venv/Scripts/python.exe"


def installed():
    out = subprocess.check_output(
        [VENV_PY, "-m", "pip", "list", "--format=json"], text=True
    )
    return [(p["name"], p["version"]) for p in json.loads(out)]


def query(pkgs):
    queries = [
        {"package": {"name": n, "ecosystem": "PyPI"}, "version": v}
        for n, v in pkgs
    ]
    body = json.dumps({"queries": queries}).encode()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    )
    req = urllib.request.Request(
        "https://api.osv.dev/v1/querybatch",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with opener.open(req, timeout=120) as r:
        return json.loads(r.read())


def main():
    pkgs = installed()
    print(f"扫描 {len(pkgs)} 个已安装包 ...")
    res = query(pkgs)

    hits = []
    for (name, ver), item in zip(pkgs, res.get("results", [])):
        for v in item.get("vulns", []) or []:
            sev = "?"
            for s in v.get("severity", []) or []:
                sev = s.get("score", sev)
            # 从 CVSS 向量粗判等级
            level = "UNKNOWN"
            vec = str(sev)
            if "CVSS" in vec:
                try:
                    score = float(v.get("severity", [{}])[0].get("score", "").split("/")[-1])
                except Exception:
                    score = None
                level = "?" if score is None else str(score)
            hits.append((name, ver, v.get("id"), level, (v.get("summary") or "")[:90]))

    if not hits:
        print("未发现已知漏洞。")
        return 0

    print(f"\n发现 {len(hits)} 条已知漏洞：\n")
    for name, ver, vid, level, summ in sorted(hits):
        print(f"  {name}=={ver}\n    {vid}  {summ}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
