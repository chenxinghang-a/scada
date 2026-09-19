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


class TestDirectoryDigest:
    """onedir 产物必须带目录级指纹。

    为什么这条重要：PyInstaller onedir 交付物 = exe + ``_internal/``，
    依赖代码全在 ``_internal/`` 里。只记 exe 的 sha256 会漏掉 95% 的内容 ——
    装错版本、文件被替换、打包不完整都不会在清单上体现。
    """

    def test_digest_is_deterministic(self, manifest_module, tmp_path):
        d = tmp_path / "bundle"
        (d / "sub").mkdir(parents=True)
        (d / "a.pyc").write_bytes(b"aaa")
        (d / "sub" / "b.pyc").write_bytes(b"bbb")

        first = manifest_module._dir_digest(d)
        second = manifest_module._dir_digest(d)
        assert first == second, "同一目录两次计算必须得到相同指纹（确定性）"
        assert first["files"] == 2
        assert first["bytes"] == 6

    def test_digest_is_order_independent(self, manifest_module, tmp_path):
        """摘要计算必须与输入顺序无关。

        为什么直接测 ``_digest_entries`` 而不是"两棵不同创建顺序的目录"：
        后者**测不出顺序泄漏** —— `rglob` 在同一进程内对目录树返回的顺序
        相当稳定，构造正序/逆序两棵树得到的遍历顺序仍可能一样。
        实测：移除 `_digest_entries` 里的 sorted()，那种写法依然全绿（假绿）。

        真正的风险场景是跨文件系统（NTFS 与 ext4 的目录项顺序不同），
        而在本机无法构造。直接喂乱序列表是唯一可靠的验证方式。
        """
        base = [
            ("a/mod.pyc", 100, "aa" * 32),
            ("b/mod.pyc", 200, "bb" * 32),
            ("c/mod.pyc", 300, "cc" * 32),
        ]
        forward = manifest_module._digest_entries(list(base))
        reversed_ = manifest_module._digest_entries(list(reversed(base)))
        shuffled = manifest_module._digest_entries(
            [base[1], base[2], base[0]]
        )

        assert forward == reversed_ == shuffled, (
            "同样的文件集合因传入顺序不同得出不同摘要 —— "
            "说明排序没生效，换文件系统后会误报产物变化"
        )

    def test_digest_entries_detects_content_change(self, manifest_module):
        """单文件摘要（或大小）变化必须体现在结果上。"""
        a = manifest_module._digest_entries([("x.pyc", 10, "aa" * 32)])
        b = manifest_module._digest_entries([("x.pyc", 10, "bb" * 32)])
        c = manifest_module._digest_entries([("x.pyc", 11, "aa" * 32)])
        assert a != b, "文件内容变化未体现在摘要上"
        assert a != c, "文件大小变化未体现在摘要上"

    def test_digest_entries_detects_added_file(self, manifest_module):
        """集合里多一个文件（上一世代残留）必须改变摘要。"""
        base = [("x.pyc", 10, "aa" * 32)]
        with_extra = base + [("stale_leftover.pyc", 5, "cc" * 32)]
        assert manifest_module._digest_entries(base) != manifest_module._digest_entries(
            with_extra
        ), "混入额外文件（如上一世代残留）时摘要必须变化"

    def test_digest_detects_content_change(self, manifest_module, tmp_path):
        d = tmp_path / "bundle"
        d.mkdir()
        (d / "mod.pyc").write_bytes(b"original")
        before = manifest_module._dir_digest(d)

        (d / "mod.pyc").write_bytes(b"tampered")
        after = manifest_module._dir_digest(d)

        assert before["sha256"] != after["sha256"], (
            "文件内容被替换后目录指纹必须变化，否则清单无法发现产物被篡改"
        )

    def test_digest_detects_extra_file(self, manifest_module, tmp_path):
        """多出一个文件（如上一世代残留）也必须体现在指纹上。"""
        d = tmp_path / "bundle"
        d.mkdir()
        (d / "mod.pyc").write_bytes(b"x")
        before = manifest_module._dir_digest(d)

        (d / "stale_leftover.pyc").write_bytes(b"old")
        after = manifest_module._dir_digest(d)

        assert before["sha256"] != after["sha256"], (
            "目录里混入上一世代残留文件时指纹必须变化 —— "
            "这正是 Stage 步骤要防止的产物污染"
        )

    def test_missing_dir_returns_none(self, manifest_module, tmp_path):
        assert manifest_module._dir_digest(tmp_path / "nope") is None


class TestOnedirArtifactRecorded:
    """清单必须记录 onedir 主产物（含 _internal/），而不是历史 onefile。"""

    def test_main_backend_artifact_is_onedir(self, manifest_module):
        manifest = manifest_module.generate_manifest()
        names = [a["name"] for a in manifest["artifacts"]]
        assert "backend_windows_onedir" in names, (
            "主后端产物必须记为 onedir 布局（dist/scada-backend/scada-backend.exe）："
            "它才是 CI 与前端 extraResources 实际消费的产物。"
        )
        assert "backend_windows_exe" not in names, (
            "不应再记录 dist/SCADA.exe（launcher.py onefile 历史配方）："
            "它不参与安装包组装，记录它会让清单夸大实际交付内容。"
        )

    def test_onedir_artifact_carries_companion_dir(self, manifest_module):
        manifest = manifest_module.generate_manifest()
        onedir = next(
            (a for a in manifest["artifacts"] if a["name"] == "backend_windows_onedir"),
            None,
        )
        assert onedir is not None
        assert "companion_dir" in onedir, (
            "onedir 产物必须带 companion_dir（_internal/ 的目录级指纹）"
        )
        cd = onedir["companion_dir"]
        assert "sha256" in cd and "files" in cd and "bytes" in cd, (
            f"companion_dir 结构不完整: {cd}"
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
