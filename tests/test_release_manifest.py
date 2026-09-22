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

    def test_onedir_artifact_carries_companion_dir(self, manifest_module, tmp_path, monkeypatch):
        """onedir 产物必须带 `_internal/` 的目录级指纹。

        **测试自造产物布局**，不依赖开发机上是否构建过 `dist/scada-backend/`。
        原先是直接对仓库里的 `dist/` 断言 —— 本机有旧产物所以绿，
        而 CI 的 test job 是干净检出（根本没有 `dist/`）→ 必红。
        测试依赖机器状态就会「本机绿 / CI 红」，且红的原因与缺陷无关。
        """
        dist = tmp_path / 'dist' / 'scada-backend'
        (dist / '_internal').mkdir(parents=True)
        (dist / 'scada-backend.exe').write_bytes(b'MZ fake exe')
        (dist / '_internal' / 'python313.dll').write_bytes(b'x' * 128)
        (dist / '_internal' / 'base_library.zip').write_bytes(b'y' * 64)

        monkeypatch.setattr(manifest_module, 'BACKEND_ROOT', tmp_path)

        artifacts = manifest_module._collect_artifacts('9.9.9')
        onedir = next(a for a in artifacts if a['name'] == 'backend_windows_onedir')

        assert onedir['sha256'], '主产物存在时必须给出 sha256'
        assert 'companion_dir' in onedir, (
            'onedir 产物必须带 companion_dir（_internal/ 的目录级指纹）—— '
            '依赖代码全在 _internal/ 里，只记 exe 会严重低估交付内容'
        )
        cd = onedir['companion_dir']
        assert 'sha256' in cd and 'files' in cd and 'bytes' in cd, (
            f'companion_dir 结构不完整: {cd}'
        )
        assert cd['files'] == 2, f'应指纹化 _internal/ 下 2 个文件: {cd}'
        assert cd['bytes'] == 192
        assert len(cd['sha256']) == 64

    def test_missing_onedir_dir_has_no_companion_dir(self, manifest_module, tmp_path, monkeypatch):
        """反向：`_internal/` 不存在时不得给出可用的 companion_dir 指纹。

        两种情况必须区分开，否则「清单字段齐全」会掩盖产物不完整：
          - exe 不存在 → 该产物整条只有 sha256=None + note，**不带** companion_dir
          - exe 存在但 `_internal/` 缺失 → 带 companion_dir，但 sha256=None + 明确 note
        """
        dist = tmp_path / 'dist' / 'scada-backend'
        dist.mkdir(parents=True)
        monkeypatch.setattr(manifest_module, 'BACKEND_ROOT', tmp_path)

        # 情况一：连 exe 都没有（CI 的 test job 就是这种干净检出状态）
        artifacts = manifest_module._collect_artifacts('9.9.9')
        onedir = next(a for a in artifacts if a['name'] == 'backend_windows_onedir')
        assert onedir['sha256'] is None
        assert 'companion_dir' not in onedir, (
            '产物根本不存在，却给出了 companion_dir 结构 —— 清单会声称覆盖了不存在的内容'
        )

        # 情况二：exe 在，_internal/ 丢了（onedir 产物不完整）
        (dist / 'scada-backend.exe').write_bytes(b'MZ fake exe')
        artifacts = manifest_module._collect_artifacts('9.9.9')
        onedir = next(a for a in artifacts if a['name'] == 'backend_windows_onedir')
        assert onedir['sha256'], 'exe 存在时应有 sha256'
        cd = onedir.get('companion_dir')
        assert cd is not None, '_internal/ 缺失必须显式记录，不能当没这回事'
        assert cd['sha256'] is None, (
            f'_internal/ 不存在却给出了指纹 —— 清单会掩盖"依赖代码没打进去": {cd}'
        )
        assert 'note' in cd, '缺失原因必须写出来，否则运维只看到一个 null'


class TestFrontendVersionLockstep:
    """前后端版本必须同步 —— 否则清单会记下**永不产生**的前端产物路径。

    背景：本清单里前端产物路径是用**后端 VERSION** 拼的
    （``SmartSCADA Setup {version}.exe``），而安装包名实际由前端
    ``package.json`` 的 version 决定（electron-builder 读它）。
    生成器原先从不读前端 package.json，于是两端版本一旦不同步，
    清单就会写出一条字段齐全、看起来正常、但**永远不可能存在**的路径 ——
    这正是本项目一直在清的那类「静默失效」。
    """

    @staticmethod
    def _fake_frontend(tmp_path, version):
        """造一个最小可识别的假前端仓库（定位靠 package-lock.json）。"""
        fe = tmp_path / 'scada-app'
        fe.mkdir()
        (fe / 'package.json').write_text(
            json.dumps({'name': 'smartscada', 'version': version}),
            encoding='utf-8',
        )
        (fe / 'package-lock.json').write_text('{}', encoding='utf-8')
        return fe

    def test_frontend_version_is_read_from_package_json(self, manifest_module,
                                                        tmp_path, monkeypatch):
        monkeypatch.setattr(
            manifest_module, 'FRONTEND_ROOT', self._fake_frontend(tmp_path, '7.7.7'))
        assert manifest_module._frontend_version() == '7.7.7', (
            '必须从**前端自己的** package.json 读版本；'
            '拿后端 VERSION 当答案就永远发现不了不同步'
        )

    def test_frontend_version_none_when_unavailable(self, manifest_module,
                                                    tmp_path, monkeypatch):
        monkeypatch.setattr(manifest_module, 'FRONTEND_ROOT', tmp_path / 'nope')
        assert manifest_module._frontend_version() is None
        assert manifest_module._lockstep_state('1.0.0', None) == 'unknown', (
            '前端仓库不在场时状态必须是 unknown，不能猜成 synced'
        )

    def test_lockstep_state_classification(self, manifest_module):
        assert manifest_module._lockstep_state('1.2.3', '1.2.3') == 'synced'
        assert manifest_module._lockstep_state('1.2.3', '1.2.2') == 'skewed'
        assert manifest_module._lockstep_state('1.2.3', None) == 'unknown'

    def test_skew_annotates_missing_frontend_artifact(self, manifest_module,
                                                      tmp_path, monkeypatch):
        """不同步 + 产物不存在 → note 必须点明「这路径造不出来」。"""
        monkeypatch.setattr(
            manifest_module, 'FRONTEND_ROOT', self._fake_frontend(tmp_path, '1.0.0'))

        artifacts = manifest_module._collect_artifacts('1.0.1', '1.0.0')
        fe_arts = [a for a in artifacts if a['owner'] == 'frontend']
        assert fe_arts, '应当有前端产物条目'
        for a in fe_arts:
            note = a.get('note') or ''
            assert '不同步' in note, (
                f'前后端版本不同步却没有标注，运维只会看到"还没构建"：{a}'
            )
            assert '1.0.0' in note and '1.0.1' in note, (
                f'note 必须写出两端各自的实际版本，否则无法定位改哪边：{note}'
            )

    def test_skew_marks_existing_frontend_artifact_stale(self, manifest_module,
                                                         tmp_path, monkeypatch):
        """不同步 + 磁盘上**真有**同名文件 → 仍须标 stale（它不可能是本次源码的产物）。"""
        fe = self._fake_frontend(tmp_path, '1.0.0')
        (fe / 'release').mkdir()
        # 注意文件名用的是**后端**版本（生成器的拼法），所以它会「找得到」
        (fe / 'release' / 'SmartSCADA Setup 1.0.1.exe').write_bytes(b'MZ fake')
        monkeypatch.setattr(manifest_module, 'FRONTEND_ROOT', fe)

        artifacts = manifest_module._collect_artifacts('1.0.1', '1.0.0')
        inst = next(a for a in artifacts if a['name'] == 'frontend_installer')
        assert inst['sha256'], '文件在就该有 sha256'
        assert inst.get('stale') is True, (
            '版本不同步却把文件当有效交付证据 —— 清单会撒谎'
        )
        assert '不同步' in (inst.get('note') or '')

    def test_synced_does_not_annotate_frontend_artifacts(self, manifest_module,
                                                         tmp_path, monkeypatch):
        """反向对照：版本一致时不得误标（否则标注会被当成噪音忽略）。"""
        monkeypatch.setattr(
            manifest_module, 'FRONTEND_ROOT', self._fake_frontend(tmp_path, '1.0.1'))

        assert manifest_module._lockstep_state('1.0.1', '1.0.1') == 'synced'
        artifacts = manifest_module._collect_artifacts('1.0.1', '1.0.1')
        # 只看前端条目：后端产物可能因本机 dist/ 早于 HEAD 而**合法地**带 stale，
        # 那是既有行为，与版本同步无关。
        fe_arts = [a for a in artifacts if a['owner'] == 'frontend']
        assert fe_arts, '应当有前端产物条目'
        for a in fe_arts:
            assert 'stale' not in a, f'版本已同步却标了 stale: {a}'
            assert '不同步' not in (a.get('note') or ''), f'版本已同步却报了不同步: {a}'

    def test_candidate_list_covers_ci_layout(self, manifest_module):
        """候选列表必须覆盖 CI 的布局：前端在后端工作区的**子目录**里。

        CI 的 test job 把前端 checkout 到 ``<workspace>/scada-app``，
        而后端就在 ``<workspace>`` 根 —— 也就是 ``BACKEND_ROOT / 'scada-app'``。
        少了这条候选，CI 上 FRONTEND_ROOT 指向不存在的路径，
        frontend_deps_lock_digest 会静默变成 null（清单少字段但无人报错）。
        """
        cands = manifest_module._frontend_root_candidates()
        assert manifest_module.BACKEND_ROOT / 'scada-app' in cands, (
            f'候选路径缺少 CI 布局 BACKEND_ROOT/scada-app，实际候选={cands}'
        )

    def test_resolve_picks_first_existing_candidate(self, manifest_module,
                                                    tmp_path, monkeypatch):
        """搜索逻辑：命中第一个「有 package-lock.json」的候选。"""
        good = tmp_path / 'a' / 'scada-app'
        good.mkdir(parents=True)
        (good / 'package-lock.json').write_text('{}', encoding='utf-8')
        empty = tmp_path / 'b' / 'scada-app'
        empty.mkdir(parents=True)  # 没有 package-lock.json，应被跳过

        monkeypatch.setattr(
            manifest_module, '_frontend_root_candidates',
            lambda: [empty, good],
        )
        assert manifest_module._resolve_frontend_root() == good, (
            '应跳过没有 package-lock.json 的候选'
        )

    def test_resolve_falls_back_when_nothing_matches(self, manifest_module,
                                                     tmp_path, monkeypatch):
        """一个候选都不命中时返回默认位置（让下游字段记 null 而不是抛异常）。"""
        monkeypatch.setattr(
            manifest_module, '_frontend_root_candidates',
            lambda: [tmp_path / 'nope1', tmp_path / 'nope2'],
        )
        monkeypatch.setattr(manifest_module, 'BACKEND_ROOT', tmp_path / 'be')
        # 兜底 = BACKEND_ROOT.parent / 'scada-app'
        assert manifest_module._resolve_frontend_root() == tmp_path / 'scada-app'

    def test_generate_manifest_exposes_lockstep_fields(self, manifest_module):
        manifest = manifest_module.generate_manifest()
        assert 'frontend_version' in manifest, (
            '清单必须暴露前端实际版本，否则无法从清单判断同步与否'
        )
        assert manifest['version_lockstep'] in ('synced', 'skewed', 'unknown'), (
            f"version_lockstep 取值非法: {manifest['version_lockstep']!r}"
        )

    def test_committed_manifest_lockstep_is_self_consistent(self, manifest_module):
        """磁盘上那份清单的同步状态必须与它自己的两个版本字段自洽。

        刻意**不断言**等于 synced：本机/CI 的前端检出状态可能不同，
        写死就会变成"依赖机器状态"的用例（本机绿 CI 红）。
        自洽性才是真正要守的不变量。
        """
        on_disk = json.loads(
            (BACKEND_ROOT / 'release-manifest.json').read_text(encoding='utf-8'))
        state = on_disk.get('version_lockstep')
        fv = on_disk.get('frontend_version')
        assert state in ('synced', 'skewed', 'unknown'), (
            f'已提交的清单缺少/非法 version_lockstep={state!r}；'
            '重新运行 tools/gen_release_manifest.py'
        )
        if state == 'synced':
            assert fv == on_disk['version'], (
                f'清单自称 synced，但 version={on_disk["version"]} != frontend_version={fv}'
            )
        elif state == 'skewed':
            assert fv != on_disk['version'], (
                f'清单自称 skewed，两个版本却相同（{fv}）'
            )
        else:
            assert fv is None, f'unknown 状态下 frontend_version 应为 null，实为 {fv!r}'


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
