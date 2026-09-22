"""生成发布清单 release-manifest.json

把"一个版本源、一个 commit、一个安装包"的口径固化成机器可读的清单：
    version                 - 应用版本号（来自唯一真源 VERSION 文件）
    commit_sha              - git HEAD SHA（非 git 仓库时为 unknown）
    build_time              - 构建时间（UTC，ISO8601）
    python_version          - 运行环境 Python 版本
    node_version            - 运行环境 Node 版本（不可用时 unknown）
    backend_deps_lock_digest  - requirements.txt 的 sha256（文件缺失则 null）
    frontend_deps_lock_digest - scada-app/package-lock.json 的 sha256（缺失则 null）
    frontend_version        - 前端 package.json 的 version（读不到则 null）
    version_lockstep        - 前后端版本同步状态：synced / skewed / unknown
    artifacts               - 交付产物清单（路径 + sha256）；产物缺失记 null 并注明

设计要点：
    * 产物（安装包/归档）尚未构建时是常态，脚本**必须仍能成功运行**，
      缺失产物记为 null 而非抛异常退出。
    * 依赖锁文件缺失同样记为 null，不影响其余字段生成。
    * 前端产物路径是用**后端 VERSION** 拼的，所以必须读前端 package.json
      核对版本；不同步时显式标注（否则清单会记下永不存在的路径）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# 仓库根（本脚本位于 tools/，上两级为后端根）。
BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _frontend_root_candidates() -> list[Path]:
    """前端仓库根的候选路径，**按优先级**排列（抽成独立函数是为了可测）。

    对应两种真实布局：
      1. 开发机：前端与后端是同级目录
         （``.../Claw/industrial_scada`` + ``.../Claw/scada-app``）
      2. CI：前端被 checkout 到**后端工作区的子目录** ``<workspace>/scada-app``
         （见 .github/workflows/ci.yml 的 "Checkout frontend sources"）
    漏掉第 2 条时 CI 上 ``FRONTEND_ROOT`` 指向不存在的路径，
    ``frontend_deps_lock_digest`` 会**静默变成 null** —— 清单少了一个字段
    却没人报错，属于典型的静默失效。
    """
    return [
        BACKEND_ROOT.parent / "scada-app",
        BACKEND_ROOT / "scada-app",
        Path("C:/Users/cxx/scada-app"),
        BACKEND_ROOT.parent.parent / "scada-app",
    ]


def _resolve_frontend_root() -> Path:
    """定位前端仓库根（不同环境可能不在后端根的同级目录）。

    只要某候选目录下存在 package-lock.json 即视为前端根；
    都找不到则返回 BACKEND_ROOT.parent / 'scada-app'（让产物/锁文件字段记 null）。
    """
    for cand in _frontend_root_candidates():
        if (cand / "package-lock.json").is_file():
            return cand
    return BACKEND_ROOT.parent / "scada-app"


FRONTEND_ROOT = _resolve_frontend_root()

# 确保能 import core.version（版本唯一真源）。
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from core.version import get_version, _git_commit_sha, _node_version  # noqa: E402


def _sha256_of_file(path: Path) -> str | None:
    """返回文件 sha256（小写 hex）；文件不存在/读取失败返回 None。"""
    if not path.is_file():
        return None
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _lock_digest(rel_path: Path) -> str | None:
    """依赖锁文件 digest：存在则 sha256，缺失则 None（调用方据实记录）。"""
    return _sha256_of_file(rel_path)


def _frontend_version() -> str | None:
    """读前端仓库 package.json 的 version；读不到返回 None。

    为什么必须读它：本清单里前端产物路径是用**后端 VERSION** 拼的
    （``SmartSCADA Setup {version}.exe``，见 ``_collect_artifacts``），
    而安装包名由前端自己的 package.json 决定。两端版本不同步时，
    清单会记下一个实际构建**永远产生不了**的路径 —— 字段齐全、
    看起来正常，但内容是错的。这类"静默失效"正是本清单要消灭的东西，
    所以这里把前端真实版本也读出来，不同步就显式标注。
    """
    pkg = FRONTEND_ROOT / "package.json"
    if not pkg.is_file():
        return None
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("读取前端 package.json 失败，版本同步状态未知: %s", e)
        return None
    v = data.get("version")
    return v if isinstance(v, str) and v else None


def _lockstep_state(backend_version: str, frontend_version: str | None) -> str:
    """前后端版本同步状态：``synced`` / ``skewed`` / ``unknown``。"""
    if frontend_version is None:
        return "unknown"
    return "synced" if frontend_version == backend_version else "skewed"


def _git_head_commit_time() -> float | None:
    """返回 HEAD 提交的 Unix 时间戳；非 git 仓库/命令失败返回 None。

    用途：判断构建产物是否早于当前源码——否则清单会记录一个
    "属于本版本、实际来自旧世代" 的产物，属于虚假证据。
    """
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%ct"],
            cwd=str(BACKEND_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return float(proc.stdout.strip())
    except Exception as e:  # noqa: BLE001 - 需覆盖所有异常以保证清单仍能生成
        logger.debug("读取 HEAD 提交时间失败: %s", e, exc_info=True)
    return None


def _stale_note(path: Path, head_ts: float | None) -> str | None:
    """产物是否早于 HEAD 提交。

    返回 None 表示新鲜（或无法判断）；否则返回可读的陈旧说明。
    **宁可不判断也不误报**：取不到 HEAD 时间时一律视为无法判断。
    """
    if head_ts is None:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if mtime < head_ts:
        import datetime as _dt

        return (
            "STALE: artifact mtime "
            f"({_dt.datetime.fromtimestamp(mtime).isoformat()}) "
            "predates HEAD commit "
            f"({_dt.datetime.fromtimestamp(head_ts).isoformat()}); "
            "must rebuild from current source before release"
        )
    return None


def _digest_entries(entries: list[tuple[str, int, str]]) -> str:
    """把 (相对路径, 大小, 文件sha256) 列表汇总成单个 sha256。

    **排序在这里做，而不是依赖调用方**：摘要必须与遍历顺序无关，
    否则同一个目录在 NTFS 与 ext4 上会得出不同指纹（两者目录项顺序不同），
    清单会在换机器后误报"产物变了"。

    抽成独立函数是为了可测：直接传乱序列表进来断言结果不变，
    比"在同一目录上算两次"可靠得多（后者在同一进程内 rglob 顺序稳定，
    根本测不出顺序泄漏 —— 实测过，是假绿）。
    """
    h = hashlib.sha256()
    for rel, size, digest in sorted(entries, key=lambda x: x[0]):
        h.update(f"{rel}\0{size}\0{digest}\n".encode("utf-8"))
    return h.hexdigest()


def _dir_digest(path: Path, max_files: int = 20000) -> dict | None:
    """计算目录的确定性指纹。

    为什么需要：PyInstaller onedir 产物 = ``scada-backend.exe`` + ``_internal/``。
    只记 exe 的 sha256 会严重低估交付内容 —— 依赖代码全在 ``_internal/`` 里，
    而那部分才是"装错版本"最容易出问题的地方。

    算法：把「相对路径 + 文件大小 + 文件 sha256」按相对路径排序后拼接，
    整体再取一次 sha256（排序在 ``_digest_entries`` 内完成）。
    单个文件内容参与哈希，所以文件被替换会在摘要上体现。

    返回 ``{"files": n, "bytes": total, "sha256": digest}``；目录不存在返回 None。
    """
    if not path.is_dir():
        return None
    entries: list[tuple[str, int, str]] = []
    total = 0
    truncated = False
    try:
        for f in path.rglob("*"):
            if not f.is_file():
                continue
            rel = f.relative_to(path).as_posix()
            size = f.stat().st_size
            total += size
            entries.append((rel, size, _sha256_of_file(f) or ""))
            if len(entries) >= max_files:
                # 超出上限：明确标记，避免摘要看起来"完整"实则截断
                truncated = True
                break
    except OSError as e:
        logger.debug("目录指纹计算失败 %s: %s", path, e, exc_info=True)
        return None

    result = {
        "files": len(entries),
        "bytes": total,
        "sha256": _digest_entries(entries),
    }
    if truncated:
        # 截断时必须显式暴露，否则清单会声称"完整覆盖"一个只算了一半的目录
        result["truncated"] = True
        result["note"] = f"目录文件数超过上限 {max_files}，指纹仅覆盖前 {max_files} 个"
    return result


def _collect_artifacts(version: str, frontend_version: str | None = None) -> list[dict]:
    """收集交付产物。

    列出"期望存在"的产物路径；存在则给 sha256，缺失则 sha256=null 并注明原因。
    产物缺失是构建前/构建中的正常状态，不应导致脚本失败。

    额外做**陈旧性校验**：产物 mtime 早于 HEAD 提交时间时附加 ``stale`` 说明。
    否则清单会把旧世代的包记录成当前版本的产物——这是最危险的
    "证据撒谎"：字段齐全、sha256 真实，但内容与源码不对应。

    额外做**版本同步校验**：前端产物路径是用后端 VERSION 拼的，
    若 ``frontend_version`` 与之不符，该路径不可能被构建出来，
    因此给前端产物条目加 ``stale`` + 说明（见 ``_frontend_version``）。
    """
    frontend_skewed = frontend_version is not None and frontend_version != version
    skew_note = (
        f"前后端版本不同步：后端 VERSION={version}，前端 package.json="
        f"{frontend_version}；清单中的前端产物路径按**后端版本**拼出，"
        f"实际构建不会产生该文件"
        if frontend_skewed else ""
    )
    # (展示名, 路径, 归属, 伴随目录)
    #
    # 主后端产物是 **onedir 布局的 dist/scada-backend/scada-backend.exe**，
    # 而不是 dist/SCADA.exe：
    #   - scada-backend.spec 是 CI 与前端共同依赖的配方（onedir，入口 run.py）；
    #   - 前端 Electron 的 extraResources 期望 <resources>/backend/scada-backend.exe；
    #   - dist/SCADA.exe 来自 SCADA.spec（launcher.py onefile），是"双击即用"的
    #     历史产物，不参与安装包组装，因此不列入交付物清单。
    # 早期版本这里记的是 dist/SCADA.exe，且只记 exe 单文件，
    # 导致清单既夸大了交付内容、又漏掉了真正的依赖代码（_internal/）。
    candidates = [
        (
            "backend_windows_onedir",
            BACKEND_ROOT / "dist" / "scada-backend" / "scada-backend.exe",
            "backend",
            BACKEND_ROOT / "dist" / "scada-backend" / "_internal",
        ),
        (
            "frontend_installer",
            FRONTEND_ROOT / "release" / f"SmartSCADA Setup {version}.exe",
            "frontend",
            None,
        ),
        (
            "frontend_archive",
            FRONTEND_ROOT / "release" / f"smartscada-{version}-x64.nsis.7z",
            "frontend",
            None,
        ),
    ]

    head_ts = _git_head_commit_time()
    artifacts: list[dict] = []
    for name, path, owner, companion_dir in candidates:
        digest = _sha256_of_file(path)
        if digest is None:
            note = "artifact not built yet / not found"
            if owner == "frontend" and skew_note:
                note = f"{note}；{skew_note}"
            artifacts.append(
                {
                    "name": name,
                    "owner": owner,
                    "path": str(path),
                    "sha256": None,
                    "note": note,
                }
            )
            continue

        entry = {
            "name": name,
            "owner": owner,
            "path": str(path),
            "sha256": digest,
        }

        # onedir 产物：附带 _internal/ 的目录级指纹
        if companion_dir is not None:
            d = _dir_digest(companion_dir)
            if d is not None:
                entry["companion_dir"] = {
                    "path": str(companion_dir),
                    "files": d["files"],
                    "bytes": d["bytes"],
                    "sha256": d["sha256"],
                }
            else:
                entry["companion_dir"] = {
                    "path": str(companion_dir),
                    "sha256": None,
                    "note": "companion dir not found —— onedir 产物不完整",
                }

        # 文件名里写死版本号的产物：若版本与当前清单不符，同样属陈旧。
        stale = _stale_note(path, head_ts)
        version_tokens = [
            t for t in path.name.replace("-", " ").replace("_", " ").split()
            if t.count(".") == 2
        ]
        if any(t != version for t in version_tokens):
            stale = (
                stale
                or f"STALE: artifact filename version {version_tokens} "
                f"!= manifest version {version}"
            )
        if stale:
            entry["stale"] = True
            entry["note"] = stale
        if owner == "frontend" and skew_note:
            # 即便磁盘上真有这么个文件（比如人为改名），版本不同步也说明
            # 它不是本次源码的产物，不能当作有效交付证据。
            entry["stale"] = True
            entry["note"] = (
                f"{entry['note']}；{skew_note}" if entry.get("note") else skew_note
            )
        artifacts.append(entry)
    return artifacts


def generate_manifest() -> dict:
    version = get_version()
    build_info = {
        "version": version,
        "commit_sha": _git_commit_sha(),
        "build_time": datetime.now(timezone.utc).isoformat(),
        "python_version": sys.version.split()[0],
        "node_version": _node_version(),
    }

    backend_lock = _lock_digest(BACKEND_ROOT / "requirements.txt")
    frontend_lock = _lock_digest(FRONTEND_ROOT / "package-lock.json")
    frontend_version = _frontend_version()

    manifest = {
        **build_info,
        "frontend_version": frontend_version,
        "version_lockstep": _lockstep_state(version, frontend_version),
        "backend_deps_lock_digest": backend_lock,
        "frontend_deps_lock_digest": frontend_lock,
        "artifacts": _collect_artifacts(version, frontend_version),
        # 生成清单的工具自身版本，便于追溯清单格式
        "manifest_generator": "tools/gen_release_manifest.py",
    }
    return manifest


def main() -> int:
    manifest = generate_manifest()
    out_path = BACKEND_ROOT / "release-manifest.json"
    out_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"release-manifest.json 已生成: {out_path}")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
