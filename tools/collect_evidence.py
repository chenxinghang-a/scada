#!/usr/bin/env python3
"""证据包收集器 — 审计 9.5 节自动化部分

将"机器可产出"的证据收集到 ``evidence/<version>/``：

自动生成：
    * build-manifest.json   版本/commit/构建时间/环境/产物 sha256
    * coverage.xml          仓库根目录存在时复制
    * pytest-report.xml     仅当 --run-tests 显式开启时运行 pytest 生成
    * frontend-test-report.xml  前端 scada-app 产物存在时复制
    * sbom.spdx.json        基于 requirements/pip 的简化 SBOM（SPDX 2.3 结构）

占位模板（带"待人工/待脚本回填"标记）：
    scope-matrix.md, requirements-traceability.csv, risk-register.md,
    api-contract-report.json, protocol-e2e-report.md, soak-metrics.csv,
    security-scan.json, install-smoke-report.md, backup-restore-report.md,
    sat-record.md

健壮性：缺 git、缺 pytest、缺前端、缺 coverage 均不崩溃，
对应项在控制台与 build-manifest.json 的 evidence_status 中记为
``skipped`` 并注明原因——绝不静默成功。

用法：
    python tools/collect_evidence.py --version 1.3.1028 [--run-tests] [--output evidence/]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# 允许从仓库根目录直接运行：把根目录加入 sys.path 以便 import core.version
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from core.version import get_build_info
except Exception:  # pragma: no cover - 防御：core 不可导入时退化为最小实现
    def get_build_info() -> dict:
        return {
            "version": "0.0.0-unknown",
            "commit_sha": "unknown",
            "build_time": datetime.now(timezone.utc).isoformat(),
            "python_version": platform.python_version(),
            "node_version": "unknown",
        }


TEMPLATE_DIR = _ROOT / "evidence" / "_template"

# 15 份必备证据文件
ALL_EVIDENCE_FILES = [
    "scope-matrix.md",
    "requirements-traceability.csv",
    "risk-register.md",
    "api-contract-report.json",
    "protocol-e2e-report.md",
    "pytest-report.xml",
    "frontend-test-report.xml",
    "coverage.xml",
    "soak-metrics.csv",
    "security-scan.json",
    "sbom.spdx.json",
    "build-manifest.json",
    "install-smoke-report.md",
    "backup-restore-report.md",
    "sat-record.md",
]

# 由脚本自动生成（或从已有产物复制）的文件
AUTO_FILES = {
    "build-manifest.json",
    "coverage.xml",
    "pytest-report.xml",
    "frontend-test-report.xml",
    "sbom.spdx.json",
}

# 前端测试报告候选位置（按优先级）
FRONTEND_REPORT_CANDIDATES = [
    _ROOT / "scada-app" / "test-results" / "junit.xml",
    _ROOT / "scada-app" / "reports" / "junit.xml",
    _ROOT / "scada-app" / "coverage" / "junit.xml",
    _ROOT / "scada-app" / "frontend-test-report.xml",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Collector:
    """逐项收集证据；每项产出 (status, reason)，status ∈ generated/copied/template/skipped。"""

    def __init__(self, version: str, out_dir: Path, run_tests: bool):
        self.version = version
        self.out_dir = out_dir
        self.run_tests = run_tests
        self.status: dict[str, dict] = {}
        self.started_at = utc_now()
        self.build_info = get_build_info()

    # ------------------------------------------------------------------ util
    def _record(self, name: str, status: str, reason: str = "") -> None:
        self.status[name] = {"status": status, "reason": reason}
        tag = {"generated": "生成", "copied": "复制", "template": "占位模板", "skipped": "跳过"}[status]
        line = f"[{tag:>4}] {name}"
        if reason:
            line += f"  —— {reason}"
        print(line)

    def _copy_template(self, name: str) -> None:
        """从 evidence/_template 复制占位模板，并回填版本/commit 占位符。"""
        src = TEMPLATE_DIR / name
        dst = self.out_dir / name
        if src.exists():
            text = src.read_text(encoding="utf-8")
        else:
            text = ""
        text = text.replace("<VERSION>", self.version).replace(
            "<COMMIT_SHA>", self.build_info.get("commit_sha", "unknown")
        )
        dst.write_text(text, encoding="utf-8")
        self._record(name, "template", "占位模板已生成，待人工/待脚本回填")

    def _sha256(self, path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    # ------------------------------------------------------------- collectors
    def collect_coverage(self) -> None:
        src = _ROOT / "coverage.xml"
        if not src.exists():
            self._copy_template("coverage.xml")
            self.status["coverage.xml"] = {
                "status": "skipped",
                "reason": "根目录不存在 coverage.xml，已放置占位模板，待 pytest --cov 生成后复制",
            }
            print("[跳过] coverage.xml  —— 根目录 coverage.xml 不存在，占位模板已生成")
            return
        shutil.copy2(src, self.out_dir / "coverage.xml")
        self._record("coverage.xml", "copied", "从仓库根目录 coverage.xml 复制")

    def collect_pytest(self) -> None:
        if not self.run_tests:
            self._copy_template("pytest-report.xml")
            self.status["pytest-report.xml"] = {
                "status": "skipped",
                "reason": "未指定 --run-tests，跳过 pytest 执行（避免慢）；占位模板已生成",
            }
            print("[跳过] pytest-report.xml  —— 未指定 --run-tests，占位模板已生成")
            return
        dst = self.out_dir / "pytest-report.xml"
        cmd = [
            sys.executable, "-m", "pytest", "tests/", "-q",
            f"--junitxml={dst}",
        ]
        print(f"[运行] pytest-report.xml  —— 执行: {' '.join(cmd)}")
        try:
            proc = subprocess.run(cmd, cwd=str(_ROOT), timeout=3600)
        except FileNotFoundError:
            self.status["pytest-report.xml"] = {
                "status": "skipped",
                "reason": "当前环境无 pytest 模块（ModuleNotFoundError）",
            }
            print("[跳过] pytest-report.xml  —— pytest 不可用")
            return
        except subprocess.TimeoutExpired:
            self.status["pytest-report.xml"] = {
                "status": "skipped",
                "reason": "pytest 执行超时（>3600s）",
            }
            print("[跳过] pytest-report.xml  —— 执行超时")
            return
        if dst.exists():
            self._record(
                "pytest-report.xml", "generated",
                f"pytest 退出码 {proc.returncode}（非 0 表示存在失败用例，详见 XML）",
            )
        else:
            self.status["pytest-report.xml"] = {
                "status": "skipped",
                "reason": f"pytest 退出码 {proc.returncode} 且未产出 JUnit XML",
            }
            print(f"[跳过] pytest-report.xml  —— pytest 未产出报告（退出码 {proc.returncode}）")

    def collect_frontend(self) -> None:
        for cand in FRONTEND_REPORT_CANDIDATES:
            if cand.exists():
                shutil.copy2(cand, self.out_dir / "frontend-test-report.xml")
                self._record("frontend-test-report.xml", "copied", f"从 {cand.relative_to(_ROOT)} 复制")
                return
        self._copy_template("frontend-test-report.xml")
        reason = "未找到前端 scada-app 测试报告（候选: " + ", ".join(
            str(p.relative_to(_ROOT)) for p in FRONTEND_REPORT_CANDIDATES) + "）；占位模板已生成"
        self.status["frontend-test-report.xml"] = {"status": "skipped", "reason": reason}
        print(f"[跳过] frontend-test-report.xml  —— {reason}")

    def collect_sbom(self) -> None:
        """生成简化 SBOM：优先 requirements.txt，其次 pip freeze。结构为 SPDX 2.3。"""
        deps: list[dict] = []
        source = ""
        req = _ROOT / "requirements.txt"
        if req.exists():
            source = "requirements.txt"
            for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith(("#", "-")):
                    continue
                if "==" in line:
                    name, ver = line.split("==", 1)
                elif ">=" in line:
                    name, ver = line.split(">=", 1)
                    ver = ">=" + ver
                else:
                    name, ver = line, "unspecified"
                deps.append({"name": name.strip(), "version": ver.strip()})
        else:
            # 回退 pip freeze
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "pip", "freeze"],
                    capture_output=True, text=True, timeout=60,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    source = "pip freeze"
                    for line in proc.stdout.splitlines():
                        if "==" in line:
                            name, ver = line.split("==", 1)
                            deps.append({"name": name.strip(), "version": ver.strip()})
            except Exception as exc:
                self.status["sbom.spdx.json"] = {
                    "status": "skipped",
                    "reason": f"requirements.txt 缺失且 pip freeze 失败: {exc}",
                }
                print(f"[跳过] sbom.spdx.json  —— {exc}")
                return
        if not deps:
            self.status["sbom.spdx.json"] = {
                "status": "skipped",
                "reason": "requirements.txt 与 pip freeze 均无依赖，未生成 SBOM",
            }
            print("[跳过] sbom.spdx.json  —— 无依赖数据")
            return
        doc = {
            "spdxVersion": "SPDX-2.3",
            "dataLicense": "CC0-1.0",
            "SPDXID": "SPDXRef-DOCUMENT",
            "name": f"industrial-scada-sbom-{self.version}",
            "documentNamespace": f"https://example.invalid/spdx/industrial-scada/{self.version}",
            "creationInfo": {
                "created": utc_now(),
                "creators": ["Tool: collect_evidence.py (simplified SBOM)"],
            },
            "evidence_meta": {
                "note": "简化 SBOM：仅含依赖名与版本，来源于 "
                        f"{source}；如需完整 SPDX（license/哈希），请安装 cyclonedx-bom 重新生成",
                "version": self.version,
                "commit_sha": self.build_info.get("commit_sha", "unknown"),
                "environment": f"{platform.system()} {platform.release()} / Python {self.build_info.get('python_version')}",
                "command": f"python tools/collect_evidence.py --version {self.version}",
                "result": "partial",
                "failures": [],
                "witness": {"name": "<待人工见证人签字>", "date": "<YYYY-MM-DD>"},
            },
            "packages": [
                {
                    "SPDXID": f"SPDXRef-Package-{i}",
                    "name": d["name"],
                    "versionInfo": d["version"],
                    "downloadLocation": "NOASSERTION",
                    "copyrightText": "NOASSERTION",
                    "licenseConcluded": "NOASSERTION",
                }
                for i, d in enumerate(deps)
            ],
        }
        (self.out_dir / "sbom.spdx.json").write_text(
            json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self._record("sbom.spdx.json", "generated", f"简化 SBOM，{len(deps)} 个依赖，来源 {source}")

    def collect_templates(self) -> None:
        for name in ALL_EVIDENCE_FILES:
            if name in AUTO_FILES or name == "build-manifest.json":
                continue
            dst = self.out_dir / name
            if dst.exists():
                self._record(name, "template", "已存在，保留原内容（未覆盖）")
                continue
            self._copy_template(name)

    # ------------------------------------------------------------- manifest
    def write_manifest(self) -> None:
        artifacts = []
        for d in (_ROOT / "dist", _ROOT / "build"):
            if d.is_dir():
                for p in sorted(d.rglob("*")):
                    if p.is_file() and p.suffix.lower() in {".exe", ".zip", ".msi", ".tar", ".gz", ".whl"}:
                        artifacts.append({
                            "path": str(p.relative_to(_ROOT)),
                            "sha256": self._sha256(p),
                            "size_bytes": p.stat().st_size,
                        })
        manifest = {
            "version": self.version,
            "commit_sha": self.build_info.get("commit_sha", "unknown"),
            "build_time": self.build_info.get("build_time", utc_now()),
            "environment": {
                "os": f"{platform.system()} {platform.release()} ({platform.machine()})",
                "python_version": self.build_info.get("python_version", "unknown"),
                "node_version": self.build_info.get("node_version", "unknown"),
            },
            "command": " ".join(sys.argv),
            "started_at": self.started_at,
            "finished_at": utc_now(),
            "config_summary": {
                "version_source": "VERSION 文件（core/version.py）",
                "tests_executed": self.run_tests,
                "output_dir": str(self.out_dir.relative_to(_ROOT) if self.out_dir.is_relative_to(_ROOT) else self.out_dir),
            },
            "artifacts": artifacts,
            "result": "partial",
            "failures": [],
            "evidence_status": self.status,
            "witness": {"name": "<待人工见证人签字>", "date": "<YYYY-MM-DD>"},
        }
        (self.out_dir / "build-manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self._record("build-manifest.json", "generated", f"含 {len(artifacts)} 个产物哈希")

    # ------------------------------------------------------------------ run
    def run(self) -> int:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        print(f"证据包目录: {self.out_dir}")
        print(f"版本: {self.version}  commit: {self.build_info.get('commit_sha')}")
        print("-" * 60)
        self.collect_coverage()
        self.collect_pytest()
        self.collect_frontend()
        self.collect_sbom()
        self.collect_templates()
        self.write_manifest()
        print("-" * 60)
        self.print_backfill_checklist()
        # 有任何 skipped 不算脚本失败（缺项是合法状态），返回 0
        return 0

    def print_backfill_checklist(self) -> None:
        print("\n=== 回填清单（须人工/脚本补齐后方可过审）===")
        for name in ALL_EVIDENCE_FILES:
            st = self.status.get(name, {"status": "missing", "reason": "未处理"})
            mark = {
                "generated": "[OK]", "copied": "[OK]",
                "template": "[待回填]", "skipped": "[跳过]",
            }.get(st["status"], "[缺失]")
            line = f"  {mark:<8} {name}"
            if st["status"] in ("template", "skipped") and st.get("reason"):
                line += f"  —— {st['reason']}"
            print(line)
        print("\n提醒：每份文件须补齐 9 项必备字段（版本/commit/环境/命令/起止时间/"
              "配置摘要/结果/失败项/人工见证人），详见 evidence/README.md 第 4 节。")


def main() -> int:
    ap = argparse.ArgumentParser(description="收集审计 9.5 节证据包到 evidence/<version>/")
    ap.add_argument("--version", required=True, help="版本号，如 1.3.1028（应与 VERSION 文件一致）")
    ap.add_argument("--run-tests", action="store_true",
                    help="显式运行 pytest 生成 pytest-report.xml（默认不跑，避免慢）")
    ap.add_argument("--output", default="evidence",
                    help="证据根目录（默认 evidence/，相对仓库根目录）")
    args = ap.parse_args()

    out_root = Path(args.output)
    if not out_root.is_absolute():
        out_root = _ROOT / out_root
    out_dir = out_root / args.version

    collector = Collector(args.version, out_dir, args.run_tests)
    return collector.run()


if __name__ == "__main__":
    sys.exit(main())
