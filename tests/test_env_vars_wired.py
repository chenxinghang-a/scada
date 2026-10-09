# -*- coding: utf-8 -*-
"""「声明了、但没有任何代码读」的环境变量守卫。

缺陷类别：**接线状态（声明了，但不通电）**
==========================================
和 round 193-205 查的「写了但没接线」同族，只是对象换成了**环境变量**。

    Dockerfile / docker-compose / .env.example / 启动脚本 / 文档表格
    声明了 ``SCADA_MODE=real``，而**代码里没有一处读它** ——
    于是「我切到真实设备模式了」是个**假信念**，系统照旧跑仿真数据。

在 SCADA 场景里这比普通配置项失效更危险：操作员以为在看真实设备。

本轮（round 206，2026-10-09）实测到的 8 个
------------------------------------------
| 变量 | 谁声明的 | 后果 |
|---|---|---|
| ``SCADA_MODE`` | README / Dockerfile / docker-compose / .env.example | 设了不生效 |
| ``SCADA_HOST`` | Dockerfile / .env.example | 容器里只监听 127.0.0.1 → 端口发布打不到；HEALTHCHECK 却是绿的 |
| ``SCADA_PORT`` | Dockerfile / .env.example / tools/multi_instance.py | 多实例全挤 5000 |
| ``SCADA_DATA_DIR`` | tools/multi_instance.py | 多实例共享同一个 SQLite 库 |
| ``SCADA_CONFIG_DIR`` | tools/multi_instance.py | 多实例共享同一份 ``配置/`` |
| ``SCADA_INSTANCE`` | tools/multi_instance.py | 无任何用途 |
| ``SCADA_DB_PATH`` | .env.example | 设了不生效 |
| ``DEBUG`` | .env.example | 真名是 ``FLASK_DEBUG`` |

处理：能接的接上（HOST/PORT/REAL_PORT/DATA_DIR/CONFIG_DIR），
没意义的删掉（MODE/INSTANCE/DB_PATH/DEBUG 改名 FLASK_DEBUG）。

为什么必须有「扫描器自己有没有牙齿」那组用例
--------------------------------------------
round 194 的教训：扫描器第一版对**已知有缺陷的代码**也报「干净」——
`**x` 展开在 `node.values` 不在 `node.keys`、只认 `Assign` 不认 `AnnAssign`。
**是拿修复前的代码回放才发现工具没牙齿的。** 所以这里：

* 用**合成夹具**证明它抓得到（Dockerfile 形态 / .env 形态 / compose 形态 /
  脚本形态 / 文档表格形态各一条）；
* 用**规模下限**证明它不是「因为一个都没解析出来」才通过
  （``collect_declared`` 返回空字典时，``unread == {}`` 是**平凡真**）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

_SCANNER_PATH = BACKEND_ROOT / "tools" / "scan_doc_env_vars.py"

#: 本轮修好、必须保持「既被声明、又有读取点」的变量。
#: 少一条就说明有人把接线拆了。
MUST_BE_WIRED = (
    "SCADA_HOST",
    "SCADA_PORT",
    "SCADA_REAL_PORT",
    "SCADA_DATA_DIR",
    "SCADA_CONFIG_DIR",
    "SCADA_LOG_DIR",
    "SCADA_ENV",
)

#: 本轮删掉的死变量：不得在任何声明源里复活。
MUST_NOT_RETURN = ("SCADA_MODE", "SCADA_DB_PATH", "SCADA_INSTANCE")


@pytest.fixture(scope="module")
def scanner():
    """以模块方式加载 tools/scan_doc_env_vars.py（tools 不是包）。"""
    spec = importlib.util.spec_from_file_location("_scan_env_vars", _SCANNER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def declared(scanner) -> dict[str, set[str]]:
    return scanner.collect_declared(BACKEND_ROOT)


# ---------------------------------------------------------------------------
# 主断言
# ---------------------------------------------------------------------------

class TestNoDeclaredButUnreadEnvVars:
    def test_no_declared_but_unread(self, scanner):
        unread, _allowed = scanner.find_unread(BACKEND_ROOT)
        if unread:
            lines = [
                f"  {var}  <- 声明于 {', '.join(sorted(where))}"
                for var, where in sorted(unread.items())
            ]
            pytest.fail(
                "以下环境变量被声明了，但**代码里没有任何读取点** —— "
                "设了不会生效（运维会以为自己切换了模式/路径/端口）：\n"
                + "\n".join(lines)
                + "\n\n二选一：① 在代码里接上它；"
                "② 从声明源删掉；③ 若确实是**别的进程**读的，"
                "写进 tools/scan_doc_env_vars.py 的 EXTERNAL_ALLOW 并给出可复核的理由。"
            )


class TestScannerIsNotBlind:
    """守卫自己也要验：一个「什么都解析不出来」的扫描器会平凡通过。"""

    def test_declared_set_is_not_trivially_empty(self, declared):
        assert len(declared) >= 40, (
            f"只解析出 {len(declared)} 个被声明的环境变量 —— "
            "声明源解析（Dockerfile/compose/.env/脚本/文档表格）多半坏了，"
            "此时 unread=={{}} 是平凡真，主断言毫无意义"
        )

    def test_known_declarations_are_seen(self, declared):
        """抽查几个**必然存在**的声明，钉住各个解析分支。"""
        for var in ("SECRET_KEY", "JWT_SECRET", "SCADA_HOST", "SCADA_LOG_LEVEL"):
            assert var in declared, f"{var} 没被解析出来 —— 对应解析分支坏了"

    def test_read_corpus_is_not_empty(self, scanner):
        corpus = scanner.collect_read_corpus(BACKEND_ROOT)
        assert len(corpus) > 200_000, (
            f"读取语料只有 {len(corpus)} 字符 —— 源码/YAML 没扫进来"
        )
        # 抽查一个必然存在的读取点，钉住语料确实含源码
        assert "SCADA_LOG_LEVEL" in corpus


class TestScannerDetectsSyntheticDeclarations:
    """合成夹具：五种声明形态各来一条，且证明「加了读取点就不报」。"""

    def _scan(self, scanner, tmp_path: Path, files: dict[str, str]):
        for name, content in files.items():
            p = tmp_path / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return scanner.find_unread(tmp_path)

    def test_dockerfile_env_detected(self, scanner, tmp_path):
        unread, _ = self._scan(scanner, tmp_path, {
            "Dockerfile": "FROM python:3.12-slim\nENV SYNTH_MODE=real\n",
            "a.py": "print('no read here')\n",
        })
        assert "SYNTH_MODE" in unread

    def test_env_file_detected(self, scanner, tmp_path):
        unread, _ = self._scan(scanner, tmp_path, {
            ".env.example": "SYNTH_DB_PATH=data/x.db\n",
            "a.py": "print('no read here')\n",
        })
        assert "SYNTH_DB_PATH" in unread

    def test_compose_assignment_and_interpolation_detected(self, scanner, tmp_path):
        unread, _ = self._scan(scanner, tmp_path, {
            "docker-compose.yml": (
                "services:\n  a:\n    environment:\n"
                "      - SYNTH_A=1\n"
                "      - SYNTH_B=${SYNTH_B:-x}\n"
            ),
            "a.py": "print('no read here')\n",
        })
        assert {"SYNTH_A", "SYNTH_B"} <= set(unread)

    def test_script_env_assignment_detected(self, scanner, tmp_path):
        unread, _ = self._scan(scanner, tmp_path, {
            "tools/launcher.py": "env = {}\nenv['SYNTH_INSTANCE'] = 'x'\n",
            "a.py": "print('no read here')\n",
        })
        assert "SYNTH_INSTANCE" in unread

    def test_doc_table_detected(self, scanner, tmp_path):
        unread, _ = self._scan(scanner, tmp_path, {
            "README.md": "| 变量 | 默认值 |\n|---|---|\n| `SYNTH_MODE` | `simulated` | 运行模式 |\n",
            "a.py": "print('no read here')\n",
        })
        assert "SYNTH_MODE" in unread

    def test_ci_workflow_env_detected(self, scanner, tmp_path):
        """CI 工作流里的 ``env:`` 也是声明源 —— 最容易在将来被加进来。"""
        unread, _ = self._scan(scanner, tmp_path, {
            ".github/workflows/ci.yml": (
                "jobs:\n  t:\n    env:\n      SYNTH_CI_MODE: strict\n    steps:\n      - run: x\n"
            ),
            "a.py": "print('no read here')\n",
        })
        assert "SYNTH_CI_MODE" in unread

    @pytest.mark.parametrize("reader,label", [
        ("import os\nMODE = os.environ.get('SYNTH_MODE', 'sim')\n", "os.environ.get"),
        ("import os\nMODE = os.environ['SYNTH_MODE']\n", "os.environ[]"),
        ("MODE = process.env.SYNTH_MODE\n", "process.env."),
        ("MODE = _get_int('SYNTH_MODE', 5000)\n", "_get_int 助手"),
        ("MODE = _env_path('SYNTH_MODE', None)\n", "_env_path 助手"),
    ])
    def test_read_sites_silence_the_finding(self, scanner, tmp_path, reader, label):
        """每种读取姿势都要能把 finding 消掉 —— 否则守卫会假红，最后被人关掉。"""
        unread, _ = self._scan(scanner, tmp_path, {
            "Dockerfile": "FROM x\nENV SYNTH_MODE=real\n",
            "a.py": reader,
        })
        assert "SYNTH_MODE" not in unread, f"{label} 这种读取姿势没被认出来"

    def test_yaml_interpolation_counts_as_read(self, scanner, tmp_path):
        """``配置/*.yaml`` 里的 ``${VAR}`` 是**真读**（采集层会代入）。"""
        unread, _ = self._scan(scanner, tmp_path, {
            ".env.example": "PLC_HOST_01=10.0.0.1\n",
            "配置/devices_real.yaml": "devices:\n  - host: ${PLC_HOST_01}\n",
        })
        assert "PLC_HOST_01" not in unread

    def test_comment_alone_does_not_satisfy_the_guard(self, scanner, tmp_path):
        """注释里出现变量名**不算**读取点（round 191/197/199/203 被喂饱过 5 次）。"""
        unread, _ = self._scan(scanner, tmp_path, {
            "Dockerfile": "FROM x\nENV SYNTH_MODE=real\n",
            "a.py": "# SYNTH_MODE 以后再说\nprint('x')\n",
        })
        assert "SYNTH_MODE" in unread, "一句注释就把守卫喂饱了"

    def test_constant_indirection_counts_as_read(self, scanner, tmp_path):
        """``_ENV = "VAR"`` + ``os.environ.get(_ENV)`` 是**真读**。

        round 206 实测踩到：``tests/test_frontend_contract.py`` 就是这么写的，
        第一版扫描器没认出来，把 ``SCADA_FRONTEND_API_DIR`` 误报成死变量。
        """
        unread, _ = self._scan(scanner, tmp_path, {
            ".github/workflows/ci.yml": "jobs:\n  t:\n    env:\n      SYNTH_API_DIR: /x\n",
            "a.py": (
                "_SYNTH_ENV = \"SYNTH_API_DIR\"\n"
                "import os\n"
                "def f():\n"
                "    return os.environ.get(_SYNTH_ENV)\n"
            ),
        })
        assert "SYNTH_API_DIR" not in unread, "常量间接读取没被认出来（会假红）"

    def test_constant_without_a_reader_is_still_flagged(self, scanner, tmp_path):
        """反向对照：光把变量名存进常量、没拿去读，**仍然要报**。

        没有这条，「认常量」就会退化成「文件里出现过就算读」——
        守卫会重新变成瞎的。
        """
        unread, _ = self._scan(scanner, tmp_path, {
            ".github/workflows/ci.yml": "jobs:\n  t:\n    env:\n      SYNTH_API_DIR: /x\n",
            "a.py": "_SYNTH_ENV = \"SYNTH_API_DIR\"\nprint('never read')\n",
        })
        assert "SYNTH_API_DIR" in unread, "只要出现过常量就放行 —— 守卫被绕过了"


class TestAllowListHygiene:
    """白名单不能变成垃圾桶。"""

    def test_every_allow_entry_has_a_reviewable_reason(self, scanner):
        for var, reason in scanner.EXTERNAL_ALLOW.items():
            assert reason and len(reason) >= 10, f"{var} 的白名单理由太短，无法复核"
            assert any(k in reason for k in ("容器", "进程", "解释器")), (
                f"{var} 的理由没说清是「谁」在读它（容器/进程/解释器）"
            )

    def test_allow_entries_are_actually_declared(self, scanner, declared):
        """陈旧的允许项 = 白名单在无声地放行一个已不存在的变量。"""
        stale = [v for v in scanner.EXTERNAL_ALLOW if v not in declared]
        assert not stale, f"白名单里的 {stale} 在任何声明源里都找不到，应删掉"

    def test_allow_entries_are_really_unread_by_us(self, scanner):
        """允许项不该出现在「我们真的会读」的名单里 —— 那说明它已经接上了。"""
        corpus = scanner.collect_read_corpus(BACKEND_ROOT)
        indirect = scanner.collect_indirectly_read(BACKEND_ROOT)
        import re as _re
        wrongly_allowed = []
        for var in scanner.EXTERNAL_ALLOW:
            if var in indirect:
                wrongly_allowed.append(var)
                continue
            for pat in scanner.READ_PATTERNS:
                if _re.search(pat.replace("%s", _re.escape(var)), corpus):
                    wrongly_allowed.append(var)
                    break
        assert not wrongly_allowed, (
            f"{wrongly_allowed} 已在代码里接上，却还挂在 EXTERNAL_ALLOW 里 —— 应移出白名单"
        )


class TestThisRoundsVarsStayWired:
    def test_fixed_vars_are_declared_and_read(self, scanner, declared):
        corpus = scanner.collect_read_corpus(BACKEND_ROOT)
        import re as _re
        for var in MUST_BE_WIRED:
            assert var in declared, f"{var} 的声明没了（Dockerfile/.env.example/README 被改？）"
            assert any(
                _re.search(pat.replace("%s", _re.escape(var)), corpus)
                for pat in scanner.READ_PATTERNS
            ), f"{var} 被声明了但读取点没了 —— 接线被拆"

    def test_dead_vars_do_not_come_back(self, declared):
        back = [v for v in MUST_NOT_RETURN if v in declared]
        assert not back, (
            f"{back} 是 round 206 删掉的死变量（设了不生效），"
            "又在声明源里复活了 —— 要么接上它，要么别声明"
        )

    def test_multi_instance_tool_still_declares_isolation_vars(self, tmp_path, monkeypatch):
        """多实例工具的「隔离四件套」不得被删 —— **真跑一遍生成物**，不读源码散文。

        变异验证时发现的**守卫盲区**：上面的扫描只查「声明了没人读」，
        管不住「该声明的被删掉了」—— 把工具里的 ``SCADA_DATA_DIR`` 删掉，
        扫描器只会少看见一个变量，**不会变红**，而多实例退回共享库。

        所以这里真的调用 ``create()`` / ``start()``，断言**生成出来的**
        ``start.bat`` / ``start.sh`` / 传给子进程的 env。
        刻意不 grep 源码：`_create_start_script` 的 docstring 里就写着这几个变量名，
        grep 源码会被自己的说明文字喂饱（round 191/197/199/203 踩过 5 次）。
        """
        spec = importlib.util.spec_from_file_location(
            "_multi_instance_probe", BACKEND_ROOT / "tools" / "multi_instance.py")
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # 先改模块级常量再构造 —— ``MultiInstanceManager.__init__`` 会用
        # 模块级 ``INSTANCES_DIR`` 去 mkdir，构造完再覆盖就已经在仓库里建出目录了。
        monkeypatch.setattr(mod, "INSTANCES_DIR", tmp_path)
        mgr = mod.MultiInstanceManager()
        mgr.instances_dir = tmp_path          # 不往仓库里写 instances/
        assert mgr.create("probe", port=5199) is True

        inst = tmp_path / "probe"
        bat = (inst / "start.bat").read_text(encoding="utf-8")
        sh = (inst / "start.sh").read_text(encoding="utf-8")
        cfg = json.loads((inst / "instance.json").read_text(encoding="utf-8"))

        for var in ("SCADA_PORT", "SCADA_DATA_DIR", "SCADA_CONFIG_DIR", "SCADA_LOG_DIR"):
            assert f"set {var}=" in bat, f"start.bat 没设置 {var} —— 手动启动的实例会共享资源"
            assert f"export {var}=" in sh, f"start.sh 没设置 {var}"

        # 语义：DATA_DIR 是**目录**，不是数据库文件
        assert cfg["data_dir"].endswith("data")
        assert not cfg["data_dir"].endswith(".db"), (
            "又把 database（文件路径）当 SCADA_DATA_DIR 传了 —— 后端要的是目录"
        )

        # start() 传给子进程的 env 必须带齐这四项
        captured: dict = {}

        class _FakeProc:
            pid = 4242

        def _fake_popen(*args, **kwargs):
            captured.update(kwargs)
            return _FakeProc()

        monkeypatch.setattr(mod.subprocess, "Popen", _fake_popen)
        assert mgr.start("probe") is True
        env = captured.get("env") or {}
        for var in ("SCADA_PORT", "SCADA_DATA_DIR", "SCADA_CONFIG_DIR", "SCADA_LOG_DIR"):
            assert var in env, f"start() 的 env 里没有 {var} —— 多实例会共享资源"
        assert env["SCADA_DATA_DIR"] == cfg["data_dir"]


# ---------------------------------------------------------------------------
# 功能证明：静态扫描说「接上了」，运行时必须真的生效
# ---------------------------------------------------------------------------

_PROBE = textwrap.dedent(
    """
    import json, os, sys
    from pathlib import Path

    repo = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(repo))

    import paths
    import config

    print("__PROBE__" + json.dumps({
        "data_dir": str(paths.DATA_DIR),
        "config_dir": str(paths.CONFIG_DIR),
        "log_dir": str(paths.LOG_DIR),
        "config_log_dir": str(config.LogConfig.LOG_DIR),
        "host": config.FlaskConfig.HOST,
        "port": config.FlaskConfig.PORT,
        "real_port": config.FlaskConfig.REAL_PORT,
        "db_path": str(config.DatabaseConfig.DB_PATH),
    }, ensure_ascii=False))
    """
)


def _probe(env_overrides: dict[str, str]) -> dict:
    """在**子进程**里导入 paths/config 并回报取值。

    必须子进程：这些是模块级常量，同进程内改环境变量再 import 不会重算。
    """
    env = dict(os.environ)
    for key in (
        "SCADA_DATA_DIR", "SCADA_CONFIG_DIR", "SCADA_LOG_DIR",
        "SCADA_HOST", "SCADA_PORT", "SCADA_REAL_PORT",
    ):
        env.pop(key, None)
    env.update(env_overrides)

    probe_file = BACKEND_ROOT / "tests" / "_env_probe_tmp.py"
    probe_file.write_text(_PROBE, encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, str(probe_file), str(BACKEND_ROOT)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, cwd=str(BACKEND_ROOT), env=env,
        )
    finally:
        probe_file.unlink(missing_ok=True)

    assert proc.returncode == 0, (
        f"探针自身跑挂了（先修探针，不是被测代码的问题）\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("__PROBE__")), None)
    assert line, f"探针没打出结果行\nstdout:\n{proc.stdout}"
    return json.loads(line[len("__PROBE__"):])


class TestEnvVarsActuallyTakeEffect:
    """正向对照 + 反向对照：证明「接上了」不是纸面结论。"""

    def test_baseline_defaults(self):
        """不设任何变量时，取值必须是历史默认值（接上≠改变默认行为）。"""
        out = _probe({})
        assert out["host"] == "127.0.0.1"
        assert out["port"] == 5000
        assert out["real_port"] == 5001
        assert out["data_dir"].endswith("data")
        assert out["config_dir"].endswith("配置")
        assert out["log_dir"].endswith("logs")

    def test_host_and_ports_take_effect(self):
        out = _probe({
            "SCADA_HOST": "0.0.0.0",
            "SCADA_PORT": "5100",
            "SCADA_REAL_PORT": "5101",
        })
        assert out["host"] == "0.0.0.0"
        assert out["port"] == 5100
        assert out["real_port"] == 5101

    def test_bad_port_falls_back_to_default(self):
        """端口有范围校验：非法值不能把服务绑到一个荒谬的端口上。"""
        out = _probe({"SCADA_PORT": "99999"})
        assert out["port"] == 5000
        out = _probe({"SCADA_PORT": "not-a-number"})
        assert out["port"] == 5000

    def test_data_and_config_dirs_take_effect(self, tmp_path):
        d = tmp_path / "inst-a" / "data"
        c = tmp_path / "inst-a" / "配置"
        out = _probe({
            "SCADA_DATA_DIR": str(d),
            "SCADA_CONFIG_DIR": str(c),
            "SCADA_LOG_DIR": str(tmp_path / "inst-a" / "logs"),
        })
        assert out["data_dir"] == str(d)
        assert out["config_dir"] == str(c)
        # 库路径必须**跟着数据目录走**（否则「多实例隔离」只隔离了日志）
        assert out["db_path"] == str(d / "scada.db")
        # config 与 paths 必须一致 —— 不能再出现「两个真源」（round 173 的教训）
        assert out["config_log_dir"] == out["log_dir"]

    def test_relative_dir_resolves_against_project_root(self):
        """相对路径按项目根解析，不按 CWD —— 否则从别处启动会静默换目录。"""
        out = _probe({"SCADA_DATA_DIR": "data"})
        assert Path(out["data_dir"]).is_absolute()
        assert Path(out["data_dir"]) == BACKEND_ROOT / "data"

    def test_relative_log_dir_does_not_split_paths_and_config(self):
        """`SCADA_LOG_DIR=logs` 这种相对值下，config 与 paths **不能分叉**。

        修复前的写法是 config 自己再读一遍环境变量（拿到相对串 ``"logs"``），
        而 paths 按项目根解析（拿到绝对路径）→
        「日志写在一处、排障工具看另一处」。这条用例专门钉住那个分叉。
        """
        out = _probe({"SCADA_LOG_DIR": "logs"})
        assert Path(out["log_dir"]).is_absolute()
        assert out["config_log_dir"] == out["log_dir"], (
            "config.LogConfig.LOG_DIR 与 paths.LOG_DIR 分叉了 —— "
            "说明 config 又在自己读 SCADA_LOG_DIR，而不是委托 paths"
        )


# ---------------------------------------------------------------------------
# 反方向：配置里**引用了**、但任何声明源都没声明的环境变量（round 207）
# ---------------------------------------------------------------------------

class TestConfigRefsAreDeclared:
    """`配置/*.yaml` 里真实存在的 `${VAR}`，必须在某个声明源里有对应声明。

    为什么这是独立的一类缺陷
    ------------------------
    `expand_env_vars`（core/config_manager.py）对「未设置且无默认值」的占位符
    是**保留原文 + 打 warning**。于是：

      * 该配置项的值就是字面量 `${VAR}` —— **看着像配置、其实是坏的**；
      * 而且**每次加载配置都打一条永远消除不掉的 warning**。
        一条无法消除的告警会把所有告警一起训练成噪音（MEMORY 25）。

    实测到的那个（round 207）：`配置/system.yaml` 有
    `secret_key: ${SCADA_SECRET_KEY}`，而 `SCADA_SECRET_KEY` 在**整仓只出现在那两行** ——
    `.env` / `.env.example` / README / docker-compose / Dockerfile / CI 全都没有它。
    同时那个 `web.secret_key` 键**没有任何代码读它**
    （唯一提及是 `api_system.py` 的**脱敏名单** —— 给一个没人读的键做脱敏）。
    """

    def test_no_undeclared_config_refs(self, scanner):
        undeclared = scanner.find_undeclared_config_refs(BACKEND_ROOT)
        if undeclared:
            lines = [
                f"  ${{{var}}}  <-  {', '.join(sorted(where))}"
                for var, where in sorted(undeclared.items())
            ]
            pytest.fail(
                "以下环境变量被**配置 YAML 引用**了，但**任何声明源里都没有** —— "
                "部署方无从知道要设它，而配置项会停在字面量 `${VAR}` 上"
                "（并且每次加载配置都打一条消除不掉的 warning）：\n"
                + "\n".join(lines)
                + "\n\n三选一：① 在 .env.example 里补上它；"
                "② 给占位符写默认值 `${VAR:-默认}`；③ 若这个键根本没人读，直接删掉它。"
            )

    def test_config_corpus_is_not_trivially_empty(self, scanner):
        """防瞎：真仓库里**确实**有一批 `${VAR}` 引用，解析器坏掉时不能平凡通过。"""
        import re as _re
        refs = set()
        for p in (BACKEND_ROOT / "配置").glob("*.y*ml"):
            txt = scanner.strip_comments(p.read_text(encoding="utf-8", errors="replace"))
            refs.update(m.group(1) for m in scanner.CONFIG_REF_RE.finditer(txt))
        assert len(refs) >= 5, (
            f"只从 配置/ 解析出 {len(refs)} 个 ${{VAR}} 引用 —— "
            "解析器多半坏了，此时「未声明集合为空」是平凡真"
        )
        # 抽查一个必然存在的真实引用（它在 .env.example 里有声明）
        assert "PLC_HOST_01" in refs, "PLC_HOST_01 没被解析出来 —— 配置引用解析分支坏了"

    # -- 合成夹具 ---------------------------------------------------------

    def _refs(self, scanner, tmp_path, files: dict) -> dict:
        for name, content in files.items():
            p = tmp_path / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return scanner.find_undeclared_config_refs(tmp_path)

    def test_undeclared_ref_detected(self, scanner, tmp_path):
        out = self._refs(scanner, tmp_path, {
            "配置/system.yaml": "web:/n  secret_key: ${SYNTH_NOT_DECLARED}\n",
        })
        assert "SYNTH_NOT_DECLARED" in out

    def test_declared_ref_is_silent(self, scanner, tmp_path):
        out = self._refs(scanner, tmp_path, {
            "配置/system.yaml": "web:/n  secret_key: ${SYNTH_DECLARED}\n",
            ".env.example": "SYNTH_DECLARED=CHANGE_ME\n",
        })
        assert "SYNTH_DECLARED" not in out

    def test_ref_with_default_is_still_reported(self, scanner, tmp_path):
        """写了 `${VAR:-默认}` 也**照样要报** —— 有默认值只说明不会停在字面量上，
        部署方仍然不知道这个旋钮存在。要豁免就显式写进白名单，别靠「有没有默认值」猜。"""
        out = self._refs(scanner, tmp_path, {
            "配置/system.yaml": "web:/n  secret_key: ${SYNTH_WITH_DEFAULT:-x}\n",
        })
        assert "SYNTH_WITH_DEFAULT" in out

    def test_yaml_comment_does_not_create_a_dependency(self, scanner, tmp_path):
        """**注释里举例的 `${VAR}` 不是依赖。** 不剥注释就会多报。"""
        out = self._refs(scanner, tmp_path, {
            "配置/devices_real.yaml": (
                "#   host   设备地址（建议 ${SYNTH_IN_COMMENT} 引用）\n"
                "devices:/n  - host: 10.0.0.1\n"
            ),
        })
        assert "SYNTH_IN_COMMENT" not in out, "注释里的示例被当成了真实依赖"

    def test_comment_in_a_declaration_source_is_not_a_declaration(self, scanner, tmp_path):
        """⚠️ **本轮自己踩到的那个坑**：解释性注释不能算「声明」。

        我在 `.env.example` 里写了一句
        「system.yaml 当时引用的是 ${SCADA_SECRET_KEY}」，
        结果 `INTERP_RE` 把注释里的占位符当成了一条声明，
        守卫立刻对刚刚修掉的真实缺陷**视而不见**（变异验证时才发现）。
        """
        out = self._refs(scanner, tmp_path, {
            "配置/system.yaml": "web:/n  secret_key: ${SYNTH_ONLY_IN_PROSE}\n",
            ".env.example": "# 这里解释一下：以前用过 ${SYNTH_ONLY_IN_PROSE}\nOTHER=1\n",
        })
        assert "SYNTH_ONLY_IN_PROSE" in out, (
            "注释里的占位符被当成了声明 —— 一句解释就能让守卫瞎掉"
        )


class TestStripComments:
    """剥注释本身也要验 —— 它是上面所有判据的地基。"""

    def test_hash_style(self, scanner):
        got = scanner.strip_comments("a: 1  # ${FOO}\nb: 2\n", "hash")
        assert "${FOO}" not in got
        assert "b: 2" in got

    def test_js_block_and_line_style(self, scanner):
        got = scanner.strip_comments(
            "/* ${FOO} */\nconst x = 1 // ${BAR}\nconst y = 2\n", "code_js"
        )
        assert "${FOO}" not in got and "${BAR}" not in got
        assert "const y = 2" in got

    def test_does_not_eat_url_scheme(self, scanner):
        """`https://…` 里的 `//` 不能被当成行注释（否则会把后面全吃掉）。"""
        got = scanner.strip_comments("const u = 'https://x/y' // 注释\nconst v = 2\n", "code_js")
        assert "https://x/y" in got
        assert "const v = 2" in got

    def test_kind_for_maps_extensions(self, scanner):
        assert scanner._kind_for(Path("a.js")) == "code_js"
        assert scanner._kind_for(Path("a.mjs")) == "code_js"
        assert scanner._kind_for(Path("a.yaml")) == "hash"
        assert scanner._kind_for(Path("a.py")) == "hash"
