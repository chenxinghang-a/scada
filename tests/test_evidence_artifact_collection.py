#!/usr/bin/env python3
"""证据包收集器的产物清单测试。

背景（真实缺陷）：
    ``write_manifest()`` 原先把 ``dist/`` 与 ``build/`` **整棵树**递归扫描，
    凡扩展名在 {.exe,.zip,.msi,.tar,.gz,.whl} 内就计入 ``artifacts``。后果：

    1. ``build/scada-backend/scada-backend.exe`` 被当成独立交付物 —— 它其实
       只是 PyInstaller 的中间产物，与 ``dist/`` 下的同名文件字节一致，
       列进交付清单会让人误以为有两个交付物；
    2. 为规避批量删除保护而改名的备份目录
       （``dist/scada-backend.prev-0918/``、``dist/scada-backend.prev-local/``）
       被当成当前版本产物 —— 上一世代的东西混进交付清单；
    3. 记录里没有 ``name`` 字段，无法按语义识别产物类型。

    该缺陷由 ``evidence/1.3.1031/build-manifest.json`` 的实际输出暴露：
    11 个 artifacts 中 8 个来自 ``build/`` 与 ``*.prev*``。

这些测试锁死修复后的行为，防止回归。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def collector_module():
    """加载 ``tools/collect_evidence.py``。

    它不在包内（``tools/`` 无 ``__init__.py``），必须按文件路径加载。
    """
    path = _REPO_ROOT / "tools" / "collect_evidence.py"
    if not path.is_file():
        pytest.skip("tools/collect_evidence.py 不存在")
    spec = importlib.util.spec_from_file_location("collect_evidence_mod", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["collect_evidence_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def fake_root(tmp_path, monkeypatch, collector_module):
    """构造一棵包含 onedir / onefile / 备份 / build 中间目录的假仓库树。

    返回 (root, 被测收集器实例)。
    """
    dist = tmp_path / "dist"
    build = tmp_path / "build"

    # onedir 主产物 + _internal
    onedir = dist / "scada-backend"
    onedir.mkdir(parents=True)
    (onedir / "scada-backend.exe").write_bytes(b"ONEDIR-EXE")
    internal = onedir / "_internal"
    internal.mkdir()
    (internal / "base_library.zip").write_bytes(b"ZIP1")
    (internal / "some.dll").write_bytes(b"DLL")

    # 备份目录：上一世代 onedir（必须被排除）
    prev = dist / "scada-backend.prev-0918"
    prev.mkdir(parents=True)
    (prev / "scada-backend.exe").write_bytes(b"PREV-EXE")
    (prev / "_internal").mkdir()
    (prev / "_internal" / "base_library.zip").write_bytes(b"ZIP-PREV")

    # 另一个备份目录变体
    prev_local = dist / "scada-backend.prev-local"
    prev_local.mkdir(parents=True)
    (prev_local / "scada-backend.exe").write_bytes(b"PREV-LOCAL-EXE")

    # onefile 主产物（应保留）
    (dist / "SCADA.exe").write_bytes(b"ONEFILE-EXE")

    # build/ 中间目录（必须被排除）
    build.mkdir()
    (build / "scada-backend.exe").write_bytes(b"ONEDIR-EXE")
    (build / "base_library.zip").write_bytes(b"ZIP1")

    monkeypatch.setattr(collector_module, "_ROOT", tmp_path)
    inst = collector_module.Collector(
        version="9.9.9",
        out_dir=tmp_path / "evidence" / "9.9.9",
        run_tests=False,
    )
    return tmp_path, inst


class TestArtifactCollection:
    """产物收集必须只含真正的交付物。"""

    def test_onedir_main_artifact_is_collected(self, fake_root):
        _, inst = fake_root
        names = {a["name"] for a in inst._collect_artifacts()}
        assert "backend_windows_onedir" in names

    def test_onefile_artifact_is_collected(self, fake_root):
        _, inst = fake_root
        names = {a["name"] for a in inst._collect_artifacts()}
        assert "backend_windows_onefile" in names

    def test_build_dir_intermediate_is_excluded(self, fake_root):
        """``build/`` 是 PyInstaller 的中间工作目录，不是交付物。

        它的 ``scada-backend.exe`` 与 ``dist/`` 下同名文件字节相同，
        列进清单会造成"有两个交付物"的错觉。
        """
        _, inst = fake_root
        paths = {a["path"] for a in inst._collect_artifacts()}
        offenders = [p for p in paths if p.replace("\\", "/").startswith("build/")]
        assert not offenders, f"build/ 中间产物不应出现在交付清单: {offenders}"

    def test_prev_backup_dirs_are_excluded(self, fake_root):
        """``*.prev*`` 目录是上一世代备份，不是当前版本产物。

        这些目录是为规避批量删除保护而改名保留的，混进清单会让
        ``stale`` 判断与交付物集合同时失真。
        """
        _, inst = fake_root
        paths = {a["path"] for a in inst._collect_artifacts()}
        offenders = [p for p in paths if ".prev" in p]
        assert not offenders, f"备份目录不应出现在交付清单: {offenders}"

    def test_internal_dir_binaries_are_not_top_level_entries(self, fake_root):
        """``_internal/`` 里的文件不能作为顶层产物记录。

        递归扫描会把 ``_internal/base_library.zip`` 当成交付物。
        正确做法是只记主产物，伴随目录用目录级指纹表达。
        """
        _, inst = fake_root
        paths = {a["path"] for a in inst._collect_artifacts()}
        offenders = [p for p in paths if "_internal" in p.replace("\\", "/")]
        assert not offenders, f"_internal 内文件不应作为顶层产物: {offenders}"

    def test_onedir_artifact_carries_companion_dir(self, fake_root):
        """onedir 主产物必须带伴随目录指纹。

        否则"只改了 _internal 未改 exe"这类变更是不可见的。
        """
        _, inst = fake_root
        entries = [a for a in inst._collect_artifacts() if a["name"] == "backend_windows_onedir"]
        assert len(entries) == 1
        cd = entries[0].get("companion_dir")
        assert cd is not None, "onedir 主产物缺少 companion_dir"
        assert cd["files"] == 2, f"伴随目录文件数应为 2，实际 {cd['files']}"
        assert cd["bytes"] > 0

    def test_every_artifact_has_name_and_sha256(self, fake_root):
        """每条产物记录必须有 name 与 sha256。

        原实现的记录只有 path/sha256/size_bytes，没有 name，
        无法按语义分辨产物类型。
        """
        _, inst = fake_root
        for a in inst._collect_artifacts():
            assert a.get("name"), f"产物缺少 name: {a}"
            assert len(a.get("sha256", "")) == 64, f"sha256 异常: {a}"

    def test_no_duplicate_paths(self, fake_root):
        _, inst = fake_root
        paths = [a["path"] for a in inst._collect_artifacts()]
        assert len(paths) == len(set(paths)), "产物路径重复"


class TestCandidateScanning:
    """候选扫描必须先覆盖备份目录，过滤才有意义。

    这是本文件最重要的一组测试。原因：若候选集直接写死
    ``dist/scada-backend/scada-backend.exe``，备份目录根本进不了候选，
    排除逻辑就成了**永远不执行的死代码**，而"清单里没有 .prev 路径"的
    断言会因候选集为空而**永远通过** —— 假绿。实测踩过这个坑。
    """

    def test_backup_dir_exe_is_a_candidate(self, fake_root):
        """备份目录里的 exe 必须进入候选集（之后再被过滤掉）。"""
        root, inst = fake_root
        dist = root / "dist"
        cands = {str(p.relative_to(dist)) for p in inst._iter_candidates(dist)}
        assert any(".prev" in c for c in cands), (
            f"备份目录未进入候选集，排除逻辑将成死代码。候选集={sorted(cands)}"
        )

    def test_build_dir_is_never_scanned(self, fake_root):
        """``build/`` 不应出现在候选里 —— 它根本不在扫描范围内。"""
        root, inst = fake_root
        dist = root / "dist"
        cands = [p.resolve() for p in inst._iter_candidates(dist)]
        # 用 resolve() 后按父路径精确判断，避免子串匹配误伤
        # （如 "base_library" 里含 "build" 字样）。
        offenders = [p for p in cands if (root / "build") in p.parents]
        assert not offenders, f"build/ 下的文件不应进入候选: {offenders}"

    def test_is_backup_entry_matches_prev_variants(self, fake_root):
        """``.prev`` 标记的各种命名变体都要被识别为备份。"""
        root, inst = fake_root
        dist = root / "dist"
        assert inst._is_backup_entry(dist / "scada-backend.prev-0918" / "scada-backend.exe", dist)
        assert inst._is_backup_entry(dist / "scada-backend.prev-local" / "scada-backend.exe", dist)
        assert not inst._is_backup_entry(dist / "scada-backend" / "scada-backend.exe", dist)

    def test_filter_actually_removes_backup_candidates(self, fake_root):
        """候选中有备份，最终产物中必须没有 —— 证明过滤真的生效。

        与 ``test_backup_dir_exe_is_a_candidate`` 配对使用：
        前者证明备份进了候选，后者证明它被剔除了。
        两条一起才能排除"死代码假绿"。
        """
        root, inst = fake_root
        dist = root / "dist"
        cand_count = len(inst._iter_candidates(dist))
        final = inst._collect_artifacts()
        assert len(final) < cand_count, (
            f"候选 {cand_count} 条、最终 {len(final)} 条 —— 过滤未生效"
        )


class TestManifestWriting:
    """清单落盘后的内容必须自洽。"""

    def test_manifest_excludes_backup_artifacts(self, fake_root):
        import json

        root, inst = fake_root
        inst.out_dir.mkdir(parents=True, exist_ok=True)
        inst.write_manifest()
        data = json.loads((inst.out_dir / "build-manifest.json").read_text(encoding="utf-8"))
        paths = [a["path"] for a in data["artifacts"]]
        assert not [p for p in paths if ".prev" in p or p.replace("\\", "/").startswith("build/")]
        assert data["version"] == "9.9.9"

    def test_missing_dist_dir_yields_empty_artifacts(self, tmp_path, monkeypatch, collector_module):
        """``dist/`` 不存在时必须返回空列表，而不是抛异常。

        证据收集器在干净仓库上运行是常见场景（CI 首次收集），
        不能因为没有构建产物就崩溃。
        """
        monkeypatch.setattr(collector_module, "_ROOT", tmp_path)
        inst = collector_module.Collector(
            version="9.9.9", out_dir=tmp_path / "evidence", run_tests=False
        )
        assert inst._collect_artifacts() == []
