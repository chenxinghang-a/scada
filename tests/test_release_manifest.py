"""发布清单（release-manifest.json）生成器的回归测试。

背景：``release-manifest.json`` 是"一个版本源、一个 commit、一个安装包"
口径的机器可读落点。它最容易出的问题不是字段缺失，而是**字段齐全但撒谎**：
sha256 真实、路径真实，可那个产物其实来自旧世代源码，与清单里的
version/commit_sha 不对应。这类问题在人工核对时几乎必然漏掉。

本测试锁死三条不变量：
    1. 清单内 version / commit_sha 必须与仓库当前真源一致（不能是手写常量）。
    2. 产物缺失时必须记 null + note，**不得导致脚本失败**（构建前属正常态）。
    3. 产物早于 HEAD 提交时间必须被标记 ``stale``（防止旧包冒充新版本）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(BACKEND_ROOT))


def _load_manifest_module():
    """以模块方式加载 tools/gen_release_manifest.py（tools 不是包）。"""
    path = BACKEND_ROOT / "tools" / "gen_release_manifest.py"
    spec = importlib.util.spec_from_file_location("_gen_release_manifest", path)
    assert spec and spec.loader, f"无法加载 {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def manifest_module():
    return _load_manifest_module()


class TestManifestTruthSource:
    """清单必须复用版本唯一真源，不得自行硬编码。"""

    def test_version_matches_version_file(self, manifest_module):
        from core.version import get_version

        manifest = manifest_module.generate_manifest()
        assert manifest["version"] == get_version(), (
            "清单版本号与 core.version.get_version() 不一致："
            "清单必须复用唯一真源 VERSION 文件，不得另起一套。"
        )

    def test_commit_sha_matches_head(self, manifest_module):
        import subprocess

        manifest = manifest_module.generate_manifest()
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(BACKEND_ROOT),
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not head:
            pytest.skip("非 git 仓库环境，跳过 commit_sha 校验")
        assert manifest["commit_sha"] == head, (
            "清单 commit_sha 与 git HEAD 不一致：清单会指向错误的源码版本。"
        )

    def test_required_keys_present(self, manifest_module):
        manifest = manifest_module.generate_manifest()
        for key in (
            "version",
            "commit_sha",
            "build_time",
            "python_version",
            "node_version",
            "artifacts",
        ):
            assert key in manifest, f"清单缺少必需字段 {key}"


class TestMissingArtifactIsNotFailure:
    """产物缺失是构建前的正常状态，绝不能抛异常。"""

    def test_missing_artifact_recorded_as_null(self, manifest_module, tmp_path, monkeypatch):
        manifest = manifest_module.generate_manifest()
        missing = [
            a for a in manifest["artifacts"]
            if a.get("sha256") is None
        ]
        for art in missing:
            assert art.get("note"), (
                f"产物 {art['name']} 的 sha256 为 null 但没有 note 说明原因："
                "缺失产物必须被显式解释，不能留空。"
            )

    def test_generate_never_raises(self, manifest_module):
        # 连续两次生成都必须成功（幂等且不依赖产物存在）
        first = manifest_module.generate_manifest()
        second = manifest_module.generate_manifest()
        assert first["version"] == second["version"]
        assert [a["name"] for a in first["artifacts"]] == [
            a["name"] for a in second["artifacts"]
        ]


class TestStaleArtifactDetection:
    """产物早于 HEAD 提交时间必须被标记 stale —— 防止旧包冒充新版本。"""

    def test_stale_detected_when_mtime_predates_head(self, manifest_module, tmp_path):
        # 构造一个 mtime 明确早于 HEAD 的文件
        old_file = tmp_path / "SCADA.exe"
        old_file.write_bytes(b"fake exe payload")
        ancient = time.time() - 86400 * 365  # 一年前
        import os

        os.utime(old_file, (ancient, ancient))

        head_ts = manifest_module._git_head_commit_time()
        if head_ts is None:
            pytest.skip("无法读取 HEAD 提交时间，跳过")
        note = manifest_module._stale_note(old_file, head_ts)
        assert note and "STALE" in note, (
            "产物 mtime 早于 HEAD 提交时间时未标记 stale："
            "这会允许旧世代产物被记录为本版本交付物。"
        )

    def test_fresh_artifact_not_flagged(self, manifest_module, tmp_path):
        import os

        fresh = tmp_path / "fresh.exe"
        fresh.write_bytes(b"x")
        future = time.time() + 3600
        os.utime(fresh, (future, future))

        head_ts = manifest_module._git_head_commit_time()
        if head_ts is None:
            pytest.skip("无法读取 HEAD 提交时间，跳过")
        assert manifest_module._stale_note(fresh, head_ts) is None, (
            "比 HEAD 更新的产物不应被误报为 stale（避免狼来了）。"
        )

    def test_unknown_head_time_does_not_flag(self, manifest_module, tmp_path):
        """取不到 HEAD 时间时宁可不判断，也不误报。"""
        f = tmp_path / "any.exe"
        f.write_bytes(b"x")
        assert manifest_module._stale_note(f, None) is None, (
            "无法判断新鲜度时不应标记 stale：误报会让标记失去意义。"
        )


class TestManifestFileOnDisk:
    """仓库内已提交的清单文件本身必须是可解析且自洽的。"""

    def test_committed_manifest_parses(self):
        path = BACKEND_ROOT / "release-manifest.json"
        if not path.is_file():
            pytest.skip("release-manifest.json 尚未生成")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data.get("artifacts"), list)
        for art in data["artifacts"]:
            assert "name" in art and "sha256" in art, (
                f"产物条目结构不完整: {art}"
            )

    def test_committed_manifest_version_matches_source(self):
        from core.version import get_version

        path = BACKEND_ROOT / "release-manifest.json"
        if not path.is_file():
            pytest.skip("release-manifest.json 尚未生成")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["version"] == get_version(), (
            "已提交的 release-manifest.json 版本号已过期，请重新运行 "
            "tools/gen_release_manifest.py"
        )
