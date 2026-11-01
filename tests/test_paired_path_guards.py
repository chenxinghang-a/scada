"""成对路径的结构守卫（round 338）。

为什么需要这个文件
------------------
本项目反复出现同一个形状：**同一件事有两条路径，只修了其中一条。**

已经发生过的实例（每一条都是真实事故，不是假设）：

1. `write-register` 在「安全模块不可用」时 **503 拒绝**，
   而 `write-coil` 和 `batch_control` **降级成直接写 PLC**
   （还带硬编码地址 线圈 0 / 寄存器 100）——
   于是「挑一条闸门少的路径」就绕过了整个安全层。

2. `restore_interlock`（手动恢复联锁）只改内存、**不落库**，
   而 `_auto_restore_bypass`（超时自动恢复）落库 ——
   重启后「已手动恢复的联锁复活」，检修现场以为保护回来了。

3. `change_password` 写 `password_changed_at` + 清 `must_change_password`，
   而 `force_change_password` 曾经漏了后者 → 改密死循环。

4. 9 个 Modbus 读写方法各自的从站地址解析（`slave_id or self.slave_id`）——
   只改一个，其余 8 条路径仍然「0 被替换成默认值」。

5. 三个客户端的 `write_endpoint` 签名不一致（2 参 vs 3 参），
   而 API 无条件用关键字传 `method=` → 真实设备每次写入都 TypeError。

靠人记得「还有另一条」是不可靠的。本文件把「成对」这件事变成**结构断言**：
用 AST / 正则扫源码，直接断言「所有 X 都必须做 Y」。
新增一条路径时，只要漏了 Y，这里就会红。
"""

import ast
import inspect
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
API_DIR = REPO / "展示层" / "api"
DC_PATH = REPO / "智能层" / "device_control.py"
MB_PATH = REPO / "采集层" / "modbus_client.py"


def _source(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """去掉行注释。

    ⚠️ 静态守卫必须做这一步，否则**注释里的字样会造成假绿**：
    实测把 `self._persist_bypasses()` 注释掉（`# self._persist_bypasses()`）后，
    `"_persist_bypasses()" in body` 仍然为真 —— 守卫放行了一个真实的违规。
    这正是本项目反复踩的「静态检查被注释骗过」。
    """
    return re.sub(r"#[^\n]*", "", text)


# ===========================================================================
# 1. 展示层不许直接写 PLC
# ===========================================================================

class TestApiLayerNeverWritesPlcDirectly:
    """**所有寄存器/线圈写入必须经过 `device_control`。**

    这是「写路径收敛」的核心断言。历史上 `write-coil` 与 `batch_control`
    都在「安全模块不可用」时降级成 `client.write_single_coil(...)` 直写，
    而 `write-register` 是 503 —— 挑闸门少的那条就绕过了安全层。

    唯一例外是 `client.write_endpoint(...)`：端点写入没有寄存器地址/量程
    语义，由 api 层自己做三项前置闸门（安全模块 / 急停 / 联锁 + 审计落盘），
    见 `api_control.py::write_endpoint`。
    """

    #: 允许出现在展示层的写方法（必须各有对应的前置闸门）
    ALLOWED = {"write_endpoint"}

    FORBIDDEN_RE = re.compile(
        r"\bclient\.(write_single_register|write_single_coil|"
        r"write_multiple_registers|write_multiple_coils)\s*\(")

    def test_no_register_or_coil_writes_in_api_layer(self):
        hits = []
        for py in sorted(API_DIR.rglob("*.py")):
            text = _source(py)
            for m in self.FORBIDDEN_RE.finditer(text):
                lineno = text[:m.start()].count("\n") + 1
                hits.append(f"{py.name}:{lineno}  {m.group(1)}")
        assert not hits, (
            "展示层直接写了 PLC，绕过 device_control 的安全闸门：\n  "
            + "\n  ".join(hits)
            + "\n所有寄存器/线圈写入必须走 device_control.write_with_verification"
              " 或 device_control.batch_control。")

    def test_allowed_exceptions_are_still_used(self):
        """反向对照：白名单里的方法必须真的还在用（防止白名单腐化）。"""
        text = "\n".join(_source(p) for p in API_DIR.rglob("*.py"))
        for name in self.ALLOWED:
            assert f"client.{name}(" in text, (
                f"白名单里的 {name} 在 api 层已不存在 —— 请从 ALLOWED 移除")


# ===========================================================================
# 2. 所有「恢复旁路」的路径都必须落库
# ===========================================================================

class TestBypassRestorePathsAreEquivalent:
    """手动恢复与超时自动恢复，对持久化状态的写入必须**等价**。

    历史上 `restore_interlock` 漏了 `_persist_bypasses()`，而
    `_auto_restore_bypass` 有 —— 重启后已手动恢复的联锁复活。
    """

    #: 所有会「解除旁路」的方法名（新增一条路径时**必须**加到这里）
    RESTORE_METHODS = ("restore_interlock", "_auto_restore_bypass")

    def test_each_restore_path_persists(self):
        src = _source(DC_PATH)
        tree = ast.parse(src)
        missing = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in self.RESTORE_METHODS:
                continue
            body = _strip_comments(ast.get_source_segment(src, node) or "")
            if "_persist_bypasses()" not in body:
                missing.append(node.name)
        assert not missing, (
            f"这些恢复旁路的方法没有落库：{missing} —— "
            "重启后已恢复的联锁会复活，检修中的设备被联锁动作。")

    def test_restore_methods_exist(self):
        """反向对照：名单里的方法必须真的存在（防止改名后守卫静默失效）。"""
        src = _source(DC_PATH)
        for name in self.RESTORE_METHODS:
            assert f"def {name}(" in src, (
                f"守卫名单里的 {name} 已不存在 —— 请更新 RESTORE_METHODS")


# ===========================================================================
# 3. 所有 Modbus 读写方法的从站地址解析必须统一
# ===========================================================================

class TestModbusSlaveResolutionIsUniform:
    """有 `slave_id` 形参的方法，必须走 `_resolve_slave()`。

    历史上 9 处都是 `slave_id or self.slave_id` —— 0（广播地址）被静默
    替换成默认从站。只改一两条的话，其余路径仍然打错设备。
    """

    def test_all_methods_use_resolve_slave(self):
        src = _source(MB_PATH)
        tree = ast.parse(src)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not re.match(r"(read|write)_\w+", node.name):
                continue
            args = [a.arg for a in node.args.args]
            args += [a.arg for a in node.args.kwonlyargs]
            if "slave_id" not in args:
                continue
            body = _strip_comments(ast.get_source_segment(src, node) or "")
            if "_resolve_slave(" not in body:
                offenders.append(node.name)
        assert not offenders, (
            f"这些方法有 slave_id 形参但没走 _resolve_slave()：{offenders}\n"
            "`slave_id or self.slave_id` 会把 0（广播地址）静默替换成默认从站。")

    def test_no_truthiness_fallback(self):
        """静态兜底：源码里不许再出现 `slave = slave_id or ...` 的赋值。"""
        src = _source(MB_PATH)
        bad = [ln.strip() for ln in src.splitlines()
               if ln.strip().startswith("slave = slave_id or")]
        assert not bad, f"仍有真值回退写法: {bad}"


# ===========================================================================
# 4. 所有客户端的 write_endpoint 签名必须一致
# ===========================================================================

class TestWriteEndpointSignaturesAreUniform:
    """三个客户端的 `write_endpoint` 都必须能接受 `method`。

    历史上 `rest_client` / `simulated_client` 是 2 参，而 API 无条件用
    关键字传 `method=` —— **真实设备每次写入都 TypeError → 通用 500**，
    这个功能从来没成功过，而且没有一条正向测试覆盖。
    """

    TARGETS = (
        ("采集层.rest_client", "RESTDeviceClient"),
        ("采集层.simulated_client", "SimulatedRESTClient"),
        ("采集层.enhanced_simulated_client", "EnhancedSimulatedRESTClient"),
    )

    def test_all_implementations_accept_method(self):
        import importlib
        problems = []
        for mod_name, cls_name in self.TARGETS:
            mod = importlib.import_module(mod_name)
            cls = getattr(mod, cls_name, None)
            if cls is None:
                problems.append(f"{mod_name}.{cls_name} 不存在")
                continue
            fn = getattr(cls, "write_endpoint", None)
            if fn is None:
                problems.append(f"{cls_name} 没有 write_endpoint")
                continue
            params = inspect.signature(fn).parameters
            accepts = ("method" in params
                       or any(p.kind == inspect.Parameter.VAR_KEYWORD
                              for p in params.values()))
            if not accepts:
                problems.append(f"{cls_name}.write_endpoint 不接受 method")
        assert not problems, (
            "客户端 write_endpoint 签名不一致 —— API 层用关键字传 method=，"
            "这些实现会 TypeError：\n  " + "\n  ".join(problems))
