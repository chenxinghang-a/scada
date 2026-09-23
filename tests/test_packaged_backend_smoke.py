# -*- coding: utf-8 -*-
"""打包后端运行时冒烟脚本（``.github/scripts/smoke_packaged_backend.py``）的测试。

背景（缺陷类别：fail-open 闸门）
--------------------------------
``build-backend`` job 原先只做 ``test -f dist/scada-backend/scada-backend.exe``
—— 只证明**文件存在**，与「产物能不能用」无关。PyInstaller 缺 ``hiddenimport``
时打包照样成功、exe 照样存在，只有进程启动那一刻才 ``ModuleNotFoundError``。
于是「构建成功 + exe 存在 + CI 全绿」和「产物一运行就崩」可以同时成立。

本测试锁死三件事：

1. **纯函数的边界行为** —— 端口解析 / ``runtime.json`` 定位与解析 / 日志截尾 /
   健康响应体判定；
2. **fail-closed 契约** —— 任何「测不出来」的情形都必须返回 1，不得静默放行。
   这里用**真实的假后端进程**（起真 HTTP 服务）验证，刻意不 mock：
   mock 掉的恰好就是「进程能不能起、端口能不能连」这件被测的事；
3. **接线** —— CI 必须真的调用这个脚本，否则脚本写得再好也没人跑。

⚠️ 本机环境说明（不影响 CI）
--------------------------------
本机注入了 WorkBuddy 的批量删除保护（safe-delete guard），删除额度**按轮累计**、
阈值 50；额度用完后删除会抛 ``SystemExit(1)`` 而**不是** ``OSError``。

所以在**同一个 turn 里反复跑全量套件 / 变异台**之后，本文件里**真正依赖删除**的
3 条用例会红：

    TestCleanupCreated::test_removes_created_files_and_keeps_existing
    TestCleanupCreated::test_keeps_preexisting_dir_but_removes_new_file_inside
    TestSmokeFailClosed::test_smoke_leaves_artifact_dir_untouched

报错原文含 ``[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]`` 且
``count`` 已等于 ``threshold`` —— 这是**额度饱和**，不是代码缺陷
（CI 上没有这个 guard，69 条全过）。
额度新鲜时本文件 69 条全过，已实测。

排查与规避见 skill ``safe-delete-guard-workarounds`` §6b。
"""

from __future__ import annotations

import importlib.util
import json
import locale
import os
import re
import socket
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SMOKE_SCRIPT = _REPO_ROOT / ".github" / "scripts" / "smoke_packaged_backend.py"
_CI_YML = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def smoke_mod():
    """按文件路径加载冒烟脚本（``.github/scripts`` 不是包，不能 import）。"""
    assert _SMOKE_SCRIPT.is_file(), f"冒烟脚本不存在: {_SMOKE_SCRIPT}"
    spec = importlib.util.spec_from_file_location("_smoke_packaged_backend", _SMOKE_SCRIPT)
    assert spec and spec.loader, f"无法加载 {_SMOKE_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["_smoke_packaged_backend"] = module
    spec.loader.exec_module(module)
    return module


# ==========================================================================
# 测试夹具：真实的假后端进程
# ==========================================================================

#: 假后端：起一个真 HTTP 服务，按给定状态码回给定 body。
#:
#: 同时在**工作目录**里造几个运行期文件 —— 真产物首次运行就会在 exe 旁边
#: 建库 / 写 ``runtime.json`` / 建 ``logs``，而 ``scada-backend.spec`` 的
#: ``datas`` 并**不含** ``data/``。这些文件用来验证「冒烟不得改动产物目录」。
_FAKE_SERVER = '''\
import http.server
import pathlib

PORT = {port}
HEALTH_PATH = {path!r}
STATUS = {status}
PAYLOAD = {payload!r}

pathlib.Path("runtime.json").write_text("{{}}", encoding="utf-8")
pathlib.Path("logs").mkdir(exist_ok=True)
pathlib.Path("logs", "backend.log").write_text("run", encoding="utf-8")
pathlib.Path("data").mkdir(exist_ok=True)
pathlib.Path("data", "scada_simulated.db").write_bytes(b"SQLite format 3\\x00")


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == HEALTH_PATH:
            body = PAYLOAD.encode("utf-8")
            self.send_response(STATUS)
        else:
            body = b"not found"
            self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


http.server.HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
'''


def _free_port() -> int:
    """要一个当前空闲的端口（bind 0 让内核分配，随即释放）。

    有极小的竞态窗口，但 pytest 是单线程跑，本机不存在别的进程抢。
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _make_fake_exe(tmp_path: Path, body: str, *, suffix: str = "") -> Path:
    """把一段 shell 命令包装成一个「可执行文件」。

    Windows 用 ``.bat``（``subprocess.Popen`` 能直接执行批处理），
    POSIX 用带 shebang 的 ``sh`` 脚本。两种都**真实启动子进程** ——
    被测的正是「进程能不能起、端口能不能连」。

    批处理按系统 ANSI 代码页编码（``cmd.exe`` 就是这么解释它的），
    用 UTF-8 写会让含非 ASCII 的临时路径变成乱码。
    """
    script = tmp_path / f"fake_body{suffix}.py"
    script.write_text(body, encoding="utf-8")

    if os.name == "nt":
        exe = tmp_path / f"fake-backend{suffix}.bat"
        text = ("@echo off\r\n"
                f'"{sys.executable}" "{script}"\r\n'
                "exit /b %ERRORLEVEL%\r\n")
        exe.write_bytes(text.encode(locale.getpreferredencoding(False), "replace"))
    else:
        exe = tmp_path / f"fake-backend{suffix}.sh"
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n', encoding="utf-8")
        exe.chmod(0o755)
    return exe


def _serving_exe(tmp_path: Path, payload: str, *, status: int = 200,
                 path: str = "/api/health/status", suffix: str = "") -> tuple[Path, int]:
    """造一个「能起来、能连上、按指定状态码回指定 body」的假后端。

    返回 ``(可执行文件, 它实际监听的端口)`` —— 端口**必须回传**给调用方。
    曾经这里让调用方自己再取一次空闲端口，于是「服务监听 A、探针探 B」，
    正向用例必然假红；负向用例反而因为「什么都没测到」而全绿。
    """
    port = _free_port()
    exe = _make_fake_exe(
        tmp_path,
        _FAKE_SERVER.format(port=port, path=path, status=status, payload=payload),
        suffix=suffix)
    return exe, port


def _exiting_exe(tmp_path: Path, code: int) -> Path:
    """造一个「起来就立刻退出」的假后端 —— 模拟缺 hiddenimport 的产物。"""
    return _make_fake_exe(tmp_path, f"raise SystemExit({code})\n")


def _good_payload() -> dict:
    """一份真实形状的健康响应（对照 2026-09-22 打包产物实测输出）。"""
    return {
        "success": True,
        "data": {
            "version": "1.3.1039",
            "global_status": "healthy",
            "modules": {
                "alarm_manager": {"status": "initialized"},
                "data_collector": {"status": "initialized"},
                "database": {"status": "initialized"},
                "device_manager": {"status": "initialized"},
            },
            "checks": {
                "global_status": "healthy",
                "total_checks": 2,
                "checks": {
                    "database": {"status": "healthy",
                                 "last_check": "2026-09-22T17:00:00"},
                    "disk": {"status": "healthy",
                             "last_check": "2026-09-22T17:00:00"},
                },
            },
            "unhealthy_modules": [],
            "unhealthy_checks": [],
        },
    }


# ==========================================================================
# 纯函数
# ==========================================================================

class TestCandidatePorts:
    """端口串解析。"""

    def test_parses_comma_separated(self, smoke_mod):
        assert smoke_mod.candidate_ports("5000,5001") == [5000, 5001]

    def test_strips_whitespace(self, smoke_mod):
        assert smoke_mod.candidate_ports(" 5000 , 5001 ") == [5000, 5001]

    def test_dedupes_preserving_order(self, smoke_mod):
        assert smoke_mod.candidate_ports("5001,5000,5001") == [5001, 5000]

    def test_skips_empty_segments(self, smoke_mod):
        assert smoke_mod.candidate_ports("5000,,5001,") == [5000, 5001]

    def test_accepts_single_port(self, smoke_mod):
        assert smoke_mod.candidate_ports("8080") == [8080]

    @pytest.mark.parametrize("raw", ["", "   ", ",", ",,"])
    def test_rejects_empty_list(self, smoke_mod, raw):
        with pytest.raises(ValueError, match="空"):
            smoke_mod.candidate_ports(raw)

    @pytest.mark.parametrize("raw", ["abc", "5000,abc", "50.5", "5000;"])
    def test_rejects_non_numeric(self, smoke_mod, raw):
        with pytest.raises(ValueError, match="非法端口"):
            smoke_mod.candidate_ports(raw)

    @pytest.mark.parametrize("raw", ["0", "65536", "-1", "99999"])
    def test_rejects_out_of_range(self, smoke_mod, raw):
        with pytest.raises(ValueError, match="端口越界"):
            smoke_mod.candidate_ports(raw)

    def test_accepts_range_boundaries(self, smoke_mod):
        assert smoke_mod.candidate_ports("1,65535") == [1, 65535]


class TestRuntimeJsonCandidates:
    """``runtime.json`` 的候选位置。"""

    def test_onedir_layout_comes_first(self, tmp_path, smoke_mod):
        exe = tmp_path / "scada-backend" / "scada-backend.exe"
        cands = smoke_mod.runtime_json_candidates(exe)
        assert cands == [
            tmp_path / "scada-backend" / "_internal" / "data" / "runtime.json",
            tmp_path / "scada-backend" / "data" / "runtime.json",
        ]

    def test_matches_paths_layout_frozen_rule(self, tmp_path, smoke_mod):
        """与 ``paths._get_project_root()`` 的 frozen 分支保持一致。

        frozen + 存在 ``_internal/`` → ``_BASE = exe_dir/_internal``
        → ``DATA_DIR = exe_dir/_internal/data``。
        """
        exe_dir = tmp_path / "dist" / "scada-backend"
        (exe_dir / "_internal").mkdir(parents=True)
        exe = exe_dir / "scada-backend.exe"
        exe.touch()
        first = smoke_mod.runtime_json_candidates(exe)[0]
        assert first.parent.parent.name == "_internal"
        assert first.parent.name == "data"


class TestReadRuntimePort:
    """``runtime.json`` 解析 —— 只做辅助发现，任何异常都返回 None。"""

    def test_reads_valid_port(self, tmp_path, smoke_mod):
        f = tmp_path / "runtime.json"
        f.write_text(json.dumps({"port": 5000, "pid": 123}), encoding="utf-8")
        assert smoke_mod.read_runtime_port(f) == 5000

    def test_missing_file_returns_none(self, tmp_path, smoke_mod):
        assert smoke_mod.read_runtime_port(tmp_path / "nope.json") is None

    def test_invalid_json_returns_none(self, tmp_path, smoke_mod):
        f = tmp_path / "runtime.json"
        f.write_text("{ not json", encoding="utf-8")
        assert smoke_mod.read_runtime_port(f) is None

    def test_non_dict_json_returns_none(self, tmp_path, smoke_mod):
        f = tmp_path / "runtime.json"
        f.write_text("[5000]", encoding="utf-8")
        assert smoke_mod.read_runtime_port(f) is None

    def test_bool_port_returns_none(self, tmp_path, smoke_mod):
        """``True`` 是 ``int`` 的子类 —— 不显式排除就会变成端口 1。"""
        f = tmp_path / "runtime.json"
        f.write_text(json.dumps({"port": True}), encoding="utf-8")
        assert smoke_mod.read_runtime_port(f) is None

    def test_string_port_returns_none(self, tmp_path, smoke_mod):
        f = tmp_path / "runtime.json"
        f.write_text(json.dumps({"port": "5000"}), encoding="utf-8")
        assert smoke_mod.read_runtime_port(f) is None

    @pytest.mark.parametrize("port", [0, -1, 65536])
    def test_out_of_range_returns_none(self, tmp_path, smoke_mod, port):
        f = tmp_path / "runtime.json"
        f.write_text(json.dumps({"port": port}), encoding="utf-8")
        assert smoke_mod.read_runtime_port(f) is None

    def test_missing_port_key_returns_none(self, tmp_path, smoke_mod):
        f = tmp_path / "runtime.json"
        f.write_text(json.dumps({"pid": 1}), encoding="utf-8")
        assert smoke_mod.read_runtime_port(f) is None


class TestTail:
    """日志截尾。"""

    def test_short_text_unchanged(self, smoke_mod):
        assert smoke_mod.tail("abc", 10) == "abc"

    def test_long_text_keeps_tail_with_marker(self, smoke_mod):
        out = smoke_mod.tail("0123456789", 4)
        assert out.endswith("6789")
        assert "前略" in out

    def test_empty_text_placeholder(self, smoke_mod):
        assert smoke_mod.tail("", 10) == "(空)"


class TestFindOpenPort:
    """端口探测。"""

    def test_finds_listening_port(self, smoke_mod):
        port = _free_port()
        with socket.socket() as server:
            server.bind(("127.0.0.1", port))
            server.listen(1)
            assert smoke_mod.find_open_port([port]) == port

    def test_returns_none_when_closed(self, smoke_mod):
        assert smoke_mod.find_open_port([_free_port()]) is None

    def test_respects_order(self, smoke_mod):
        port = _free_port()
        with socket.socket() as server:
            server.bind(("127.0.0.1", port))
            server.listen(1)
            assert smoke_mod.find_open_port([_free_port(), port]) == port


class TestCleanupCreated:
    """「冒烟不得改动产物」—— 按运行前后清单求差，只删本次新建的。"""

    def test_removes_created_files_and_keeps_existing(self, tmp_path, smoke_mod):
        keep = tmp_path / "keep.txt"
        keep.write_text("k", encoding="utf-8")
        before = smoke_mod._snapshot_tree(tmp_path)

        (tmp_path / "new.txt").write_text("n", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "deep.txt").write_text("d", encoding="utf-8")

        assert smoke_mod.cleanup_created(tmp_path, before) == []
        assert keep.is_file()
        assert not (tmp_path / "new.txt").exists()
        assert not (tmp_path / "sub").exists(), "新建的空目录也要清掉"

    def test_keeps_preexisting_dir_but_removes_new_file_inside(self, tmp_path, smoke_mod):
        """已有目录里新增的文件要删，但目录本身（及其原有内容）要留着。

        这条是「不要按目录名删固定几个」的守卫：spec 哪天真的开始随包携带
        ``data/`` 预置内容，按目录名删会把它一起删掉。
        """
        data = tmp_path / "data"
        data.mkdir()
        (data / "seed.db").write_text("seed", encoding="utf-8")
        before = smoke_mod._snapshot_tree(tmp_path)

        (data / "runtime.json").write_text("{}", encoding="utf-8")

        smoke_mod.cleanup_created(tmp_path, before)
        assert (data / "seed.db").is_file()
        assert not (data / "runtime.json").exists()
        assert data.is_dir()

    def test_noop_when_nothing_created(self, tmp_path, smoke_mod):
        f = tmp_path / "a.txt"
        f.write_text("a", encoding="utf-8")
        before = smoke_mod._snapshot_tree(tmp_path)
        assert smoke_mod.cleanup_created(tmp_path, before) == []
        assert f.is_file()

    def test_guard_systemexit_is_recorded_not_propagated(self, tmp_path, smoke_mod,
                                                         monkeypatch):
        """本机 safe-delete 守卫拒绝删除时抛的是 ``SystemExit``，**不是** ``OSError``。

        只 catch ``OSError`` 会让异常穿出 ``smoke()``，把一次干净的成功判成崩溃
        （本机实测：8 条冒烟用例连带失败）。这里锁住「记进 failed、不向上抛」。

        ⚠️ 这不是绕过安全控制 —— 文件确实没被删掉，只是由调用方打 WARN。
        """
        (tmp_path / "created.txt").write_text("x", encoding="utf-8")
        before = smoke_mod._snapshot_tree(tmp_path)
        (tmp_path / "runtime.json").write_text("{}", encoding="utf-8")

        def _refuse(self, *args, **kwargs):
            raise SystemExit(1)

        monkeypatch.setattr(Path, "unlink", _refuse)
        assert smoke_mod.cleanup_created(tmp_path, before) == ["runtime.json"]


class TestAssessHealth:
    """健康响应体判定 —— 只对确定的负面信号判负。"""

    def test_healthy_payload_ok(self, smoke_mod):
        result = smoke_mod.assess_health(_good_payload())
        assert result["ok"] is True
        assert result["resolved"] is True
        assert result["global_status"] == "healthy"
        assert result["modules"]["data_collector"] == "initialized"
        assert set(result["checks"]) == {"database", "disk"}

    def test_unresolved_checks_reported_not_failed(self, smoke_mod):
        """巡检还没跑第一轮 → ok 但 resolved=False（调用方继续等，不判死）。"""
        payload = _good_payload()
        for info in payload["data"]["checks"]["checks"].values():
            info["last_check"] = None
            info["status"] = "unknown"
        result = smoke_mod.assess_health(payload)
        assert result["ok"] is True
        assert result["resolved"] is False

    def test_rejects_non_dict(self, smoke_mod):
        assert smoke_mod.assess_health(["nope"])["ok"] is False

    def test_rejects_success_false(self, smoke_mod):
        payload = _good_payload()
        payload["success"] = False
        result = smoke_mod.assess_health(payload)
        assert result["ok"] is False
        assert "success" in result["reason"]

    def test_rejects_missing_data(self, smoke_mod):
        result = smoke_mod.assess_health({"success": True})
        assert result["ok"] is False

    def test_rejects_global_unhealthy(self, smoke_mod):
        payload = _good_payload()
        payload["data"]["global_status"] = "unhealthy"
        result = smoke_mod.assess_health(payload)
        assert result["ok"] is False
        assert "global_status" in result["reason"]

    def test_rejects_checks_global_unhealthy(self, smoke_mod):
        payload = _good_payload()
        payload["data"]["checks"]["global_status"] = "unhealthy"
        result = smoke_mod.assess_health(payload)
        assert result["ok"] is False

    @pytest.mark.parametrize("bad", ["error", "disabled", "unavailable"])
    def test_rejects_bad_module_status(self, smoke_mod, bad):
        payload = _good_payload()
        payload["data"]["modules"]["data_collector"] = {"status": bad}
        result = smoke_mod.assess_health(payload)
        assert result["ok"] is False
        assert "data_collector" in result["reason"]

    def test_rejects_unhealthy_check(self, smoke_mod):
        payload = _good_payload()
        payload["data"]["checks"]["checks"]["data_freshness"] = {
            "status": "unhealthy", "last_check": "2026-09-22T17:00:00"}
        result = smoke_mod.assess_health(payload)
        assert result["ok"] is False
        assert "data_freshness" in result["reason"]

    @pytest.mark.parametrize("tolerated", ["unknown", "degraded", "healthy"])
    def test_tolerates_non_fatal_check_status(self, smoke_mod, tolerated):
        """``unknown``（启动瞬态）与 ``degraded``（新鲜度临界）都不判负。

        判负会让闸门在 CI 上随 runner 快慢抖动 —— 会抖的闸门早晚被人关掉。
        """
        payload = _good_payload()
        payload["data"]["checks"]["checks"]["data_freshness"] = {
            "status": tolerated, "last_check": "2026-09-22T17:00:00"}
        assert smoke_mod.assess_health(payload)["ok"] is True

    def test_does_not_trust_unhealthy_modules_field(self, smoke_mod):
        """``unhealthy_modules`` 是派生字段，一律从 ``modules`` 重算。

        两个方向都要覆盖：派生字段说好而实况坏 → 判负；
        派生字段说坏而实况好 → 不判负（否则一个写错的派生字段就能伪造红灯）。
        """
        lying_ok = _good_payload()
        lying_ok["data"]["unhealthy_modules"] = ["data_collector"]
        assert smoke_mod.assess_health(lying_ok)["ok"] is True

        lying_bad = _good_payload()
        lying_bad["data"]["modules"]["database"] = {"status": "error"}
        lying_bad["data"]["unhealthy_modules"] = []
        assert smoke_mod.assess_health(lying_bad)["ok"] is False

    def test_tolerates_missing_optional_sections(self, smoke_mod):
        """``modules`` / ``checks`` 缺失不该崩，但也不算 resolved。"""
        result = smoke_mod.assess_health({"success": True, "data": {"global_status": "healthy"}})
        assert result["ok"] is True
        assert result["resolved"] is False
        assert result["modules"] == {} and result["checks"] == {}


# ==========================================================================
# fail-closed 契约（真进程）
# ==========================================================================

class TestSmokeFailClosed:
    """``smoke()`` 必须对任何「测不出来」的情形返回 1。"""

    def test_missing_exe_fails(self, tmp_path, smoke_mod):
        """产物不存在是闸门失败，不是「跳过」。"""
        rc = smoke_mod.smoke(tmp_path / "nope.exe", [5000], "/api/health/status",
                             timeout=5.0, checks_timeout=5.0, log_tail=500)
        assert rc == 1

    def test_process_exits_before_port_fails(self, tmp_path, smoke_mod):
        """起来就退出的产物（缺 hiddenimport 的典型症状）必须判负。"""
        exe = _exiting_exe(tmp_path, 3)
        rc = smoke_mod.smoke(exe, [_free_port()], "/api/health/status",
                             timeout=20.0, checks_timeout=5.0, log_tail=500)
        assert rc == 1

    def test_non_200_health_fails(self, tmp_path, smoke_mod):
        """端口起来了但健康端点非 200 —— 不能算通过。"""
        exe, port = _serving_exe(tmp_path, "boom", status=500)
        rc = smoke_mod.smoke(exe, [port], "/api/health/status",
                             timeout=20.0, checks_timeout=3.0, log_tail=500)
        assert rc == 1

    def test_unhealthy_payload_fails(self, tmp_path, smoke_mod):
        """HTTP 200 但 body 里有确定负面信号 —— 这正是「只看状态码」会漏掉的。"""
        payload = _good_payload()
        payload["data"]["global_status"] = "unhealthy"
        exe, port = _serving_exe(tmp_path, json.dumps(payload))
        rc = smoke_mod.smoke(exe, [port], "/api/health/status",
                             timeout=20.0, checks_timeout=10.0, log_tail=500)
        assert rc == 1

    def test_unresolved_checks_time_out(self, tmp_path, smoke_mod):
        """HTTP 200、无负面信号，但巡检线程一直没跑 → 超时判负。"""
        payload = _good_payload()
        for info in payload["data"]["checks"]["checks"].values():
            info["last_check"] = None
        exe, port = _serving_exe(tmp_path, json.dumps(payload))
        rc = smoke_mod.smoke(exe, [port], "/api/health/status",
                             timeout=20.0, checks_timeout=3.0, log_tail=500)
        assert rc == 1

    def test_healthy_backend_passes(self, tmp_path, smoke_mod):
        """正向对照：真起进程、真连端口、真解析 body → 必须 0。

        没有这条，前面所有 assert rc == 1 都可以靠「函数永远返回 1」通过 ——
        而「永远返回 1 的闸门」和「永远返回 0 的闸门」一样没用。
        """
        exe, port = _serving_exe(tmp_path, json.dumps(_good_payload()))
        rc = smoke_mod.smoke(exe, [port], "/api/health/status",
                             timeout=20.0, checks_timeout=10.0, log_tail=500)
        assert rc == 0

    def test_smoke_leaves_artifact_dir_untouched(self, tmp_path, smoke_mod):
        """冒烟必须**不改动产物目录**。

        真产物首次运行会在自己旁边建库 / 写 ``runtime.json`` / 建 ``logs``，
        而 ``scada-backend.spec`` 的 ``datas`` **不含** ``data/`` ——
        那些都是纯运行期产物。若不清掉，紧接着的 ``upload-artifact``
        会把它们一起打包出去（本机手动跑还会在 `dist-scada-<版本>/` 里留垃圾）。
        """
        pre_existing = tmp_path / "preexisting.txt"
        pre_existing.write_text("keep me", encoding="utf-8")
        exe, port = _serving_exe(tmp_path, json.dumps(_good_payload()))
        before = smoke_mod._snapshot_tree(tmp_path)

        rc = smoke_mod.smoke(exe, [port], "/api/health/status",
                             timeout=20.0, checks_timeout=10.0, log_tail=500)
        assert rc == 0, "正向对照必须通过，否则这条断言什么也没验证"

        after = smoke_mod._snapshot_tree(tmp_path)
        assert after == before, (
            f"冒烟改动了产物目录："
            f"新增文件 {sorted(after[0] - before[0])}、丢失文件 {sorted(before[0] - after[0])}、"
            f"新增目录 {sorted(after[1] - before[1])}、丢失目录 {sorted(before[1] - after[1])}"
        )
        assert pre_existing.read_text(encoding="utf-8") == "keep me"

    def test_custom_health_path_is_used(self, tmp_path, smoke_mod):
        """``--health-path`` 必须真的参与请求，否则「换个端点」是空操作。"""
        exe, port = _serving_exe(tmp_path, json.dumps(_good_payload()),
                                 path="/api/health/status/detail")
        rc = smoke_mod.smoke(exe, [port], "/api/health/status/detail",
                             timeout=20.0, checks_timeout=10.0, log_tail=500)
        assert rc == 0

        # 换成错的端点必须判负（证明路径确实被用了，不是碰巧都通）
        exe2, port2 = _serving_exe(tmp_path, json.dumps(_good_payload()),
                                   path="/api/health/status/detail", suffix="-b")
        rc2 = smoke_mod.smoke(exe2, [port2], "/api/health/status",
                              timeout=20.0, checks_timeout=3.0, log_tail=500)
        assert rc2 == 1


# ==========================================================================
# 接线：CI 必须真的调用它
# ==========================================================================

def _job_block(ci_text: str, job: str) -> str:
    """取出 ci.yml 里某个 job 的文本块（到下一个同级 job 名为止）。"""
    lines = ci_text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if line.rstrip("\r\n") == f"  {job}:":
            start = i
            break
    assert start is not None, f"ci.yml 里找不到 job {job!r}"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^  [A-Za-z0-9_-]+:\s*$", lines[j]):
            end = j
            break
    return "".join(lines[start:end])


def _step_block(job_text: str, needle: str) -> str:
    """取出包含 ``needle`` 的那个 YAML step 的文本块。"""
    lines = job_text.splitlines(keepends=True)
    hit = next(i for i, line in enumerate(lines) if needle in line)
    start = hit
    for i in range(hit, -1, -1):
        if re.match(r"^\s*- name:", lines[i]):
            start = i
            break
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"^\s*- name:", lines[j]):
            end = j
            break
    return "".join(lines[start:end])


class TestCiWiring:
    """脚本写了就得有人跑 —— 否则只是仓库里的一个装饰品。"""

    def test_build_backend_job_invokes_smoke(self):
        ci = _CI_YML.read_text(encoding="utf-8")
        block = _job_block(ci, "build-backend")
        assert "smoke_packaged_backend.py" in block, (
            "build-backend job 没有调用冒烟脚本 —— "
            "「只验证 exe 存在」的 fail-open 闸门又回来了"
        )
        assert "dist/scada-backend/scada-backend.exe" in block, (
            "冒烟步骤没有指向构建产物路径"
        )

    def test_smoke_runs_before_artifact_upload(self):
        """冒烟必须在打包产物上传**之前** —— 上传了坏产物，闸门再红也晚了。"""
        ci = _CI_YML.read_text(encoding="utf-8")
        block = _job_block(ci, "build-backend")
        smoke_at = block.find("smoke_packaged_backend.py")
        upload_at = block.find("Upload backend artifact")
        assert smoke_at != -1 and upload_at != -1
        assert smoke_at < upload_at

    def test_smoke_step_fails_the_job(self):
        """步骤不能带 ``continue-on-error`` —— 否则闸门红了 job 还是绿的。"""
        ci = _CI_YML.read_text(encoding="utf-8")
        step = _step_block(_job_block(ci, "build-backend"),
                           "smoke_packaged_backend.py")
        assert "continue-on-error" not in step, (
            "冒烟步骤带了 continue-on-error —— 闸门形同虚设"
        )
        assert "if:" not in step, "冒烟步骤带了 if 条件 —— 可能被跳过"

    def test_fail_open_gate_is_gone(self):
        """那条「只验证 exe 存在」的 fail-open 断言不得再作为**可执行行**存在。

        留着它会让后来的人以为它就是闸门。（注释里提到它是允许的 ——
        本步骤的注释本来就在解释它为什么被换掉。）
        """
        ci = _CI_YML.read_text(encoding="utf-8")
        block = _job_block(ci, "build-backend")
        code = "\n".join(line for line in block.splitlines()
                         if not line.lstrip().startswith("#"))
        assert "test -f dist/scada-backend/scada-backend.exe" not in code, (
            "「test -f exe」又回来了 —— 它是 fail-open 闸门，只证明文件存在"
        )

    def test_checks_timeout_covers_periodic_interval(self, smoke_mod):
        """``--checks-timeout`` 必须覆盖后台巡检的**至少两轮**。

        巡检线程是「先 sleep 再跑第一轮」，所以闸门至少要等一个完整间隔才能
        看到检查项从 ``unknown`` 变成结论。这是两处独立常量（脚本里的默认超时
        与 ``run.py`` 里的 ``start_periodic_checks(interval=…)``），
        哪天有人把间隔调大而忘了调超时，闸门就会开始假红 —— 而假红的闸门
        早晚会被人关掉。
        """
        run_py = (_REPO_ROOT / "run.py").read_text(encoding="utf-8")
        match = re.search(r"start_periodic_checks\(\s*interval\s*=\s*(\d+)", run_py)
        assert match, "run.py 里找不到 start_periodic_checks(interval=…)"
        interval = int(match.group(1))
        assert smoke_mod.DEFAULT_CHECKS_TIMEOUT >= interval * 2, (
            f"--checks-timeout 默认 {smoke_mod.DEFAULT_CHECKS_TIMEOUT:.0f}s "
            f"不足以覆盖巡检间隔 {interval}s 的两轮 —— 闸门会假红"
        )
