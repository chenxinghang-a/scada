"""版本信息单一真源 - 工业 SCADA 系统

整个项目（后端运行时、Prometheus 指标、Swagger 文档、API 头、发布清单）
统一从这里读取版本号，**不要在任何地方硬编码版本号**。

唯一真源是项目根目录的 ``VERSION`` 文件，内容形如 ``1.3.1028``。
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# 本文件位于 core/version.py，上两级即为项目根目录（VERSION 所在）。
_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = _ROOT / "VERSION"

# 语义化版本号基本格式：1.3.1028 / 1.3.1028-beta.1 / 1.3.1028+20260918 等。
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+].+)?$")

# 读取失败 / 文件缺失 / 内容非法时的明确标记。
# 绝不返回空串——空串会让上游把它当成“版本未设置”而静默带病运行。
UNKNOWN_VERSION = "0.0.0-unknown"


def get_version() -> str:
    """读取并返回版本号（已 strip 并做格式校验）。

    返回规则：
        * 文件存在且内容合法 -> 返回 strip 后的版本号
        * 文件缺失 / 读取失败 -> ``"0.0.0-unknown"``
        * 文件存在但内容为空或不合法 -> ``"0.0.0-unknown"``

    设计要点：任何异常路径都返回一个**能暴露问题的显式标记**，
    而不是空串或静默回退到某个“看似正常”的版本。
    """
    try:
        raw = VERSION_FILE.read_text(encoding="utf-8").strip()
    except (OSError, FileNotFoundError):
        return UNKNOWN_VERSION

    if not raw:
        return UNKNOWN_VERSION

    if not _VERSION_RE.match(raw):
        # 内容存在但不合法（例如误写入了分支名/commit）。
        # 仍返回标记以便问题暴露，而不是假装成正常版本号。
        return UNKNOWN_VERSION

    return raw


def _git_commit_sha() -> str:
    """返回当前 HEAD 的 commit SHA；非 git 仓库或命令失败时返回 'unknown'。"""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            sha = proc.stdout.strip()
            return sha or UNKNOWN_VERSION
        logger.debug(
            "git rev-parse HEAD 返回非零退出码 %s，stderr=%s",
            proc.returncode,
            proc.stderr.strip(),
        )
    except Exception as e:
        # git 不可用 / 超时 / 非仓库目录：属可接受的降级路径，
        # 但必须留痕，避免 commit_sha 静默变 unknown 而无人察觉。
        logger.debug("读取 git commit sha 失败，降级为 unknown: %s", e, exc_info=True)
    return "unknown"


def _node_version() -> str:
    """返回 node --version；node 不可用或命令失败时返回 'unknown'。"""
    try:
        proc = subprocess.run(
            ["node", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            return proc.stdout.strip() or "unknown"
        logger.debug(
            "node --version 返回非零退出码 %s，stderr=%s",
            proc.returncode,
            proc.stderr.strip(),
        )
    except Exception as e:
        # node 未安装属正常场景（后端可独立运行），但同样需要留痕。
        logger.debug("读取 node 版本失败，降级为 unknown: %s", e, exc_info=True)
    return "unknown"


def get_build_info() -> dict:
    """返回构建信息字典。

    字段：
        version      - 应用版本号（来自 VERSION 文件）
        commit_sha   - git HEAD SHA（非 git 仓库时为 'unknown'）
        build_time   - 构建时间，UTC，ISO8601 格式
        python_version - 运行环境 Python 版本
        node_version - 运行环境 Node 版本（不可用时为 'unknown'）
    """
    return {
        "version": get_version(),
        "commit_sha": _git_commit_sha(),
        "build_time": datetime.now(timezone.utc).isoformat(),
        "python_version": sys.version.split()[0],
        "node_version": _node_version(),
    }


if __name__ == "__main__":
    import json

    print(get_version())
    print(json.dumps(get_build_info(), indent=2, ensure_ascii=False))
