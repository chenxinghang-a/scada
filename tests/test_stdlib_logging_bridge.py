# -*- coding: utf-8 -*-
"""标准 `logging` 必须被桥接进 loguru —— 否则它的 INFO 会被**静默丢弃**。

缺陷（2026-10-10，round 212；wb 在**部署版**上定位）
==================================================
`core/structured_logging.py::setup_logging()` 只配置了 **loguru** 自己的 sink；
而仓库里**大量模块**用的是**标准 logging**（`logging.getLogger(__name__)`）——
两者之间**没有任何桥**（全仓 `grep InterceptHandler` **零命中**）。
那些日志落进标准 logging 的 root logger，而 root **没有 handler**
→ Python 退回 `logging.lastResort`，它**只输出 WARNING 及以上**
→ **所有 INFO/DEBUG 被静默丢弃**。

实测证据（wb 查部署版日志）：`scada_2026-10-10.log` 305 行里 `module` 字段
只有 `[run×300, structured_logging×5]` —— 连 `config_manager` 的
「加载配置文件」（标准 logging INFO）都 **0 命中**。

**最严重的后果**：round 209 刚接上的**数据保留策略**（会删数据）用的正是标准 logging
→ 「调度已启动」/「执行：…删除 {…}」/「未启用」**一条都看不到** ——
跑没跑、删了什么、删了多少，运维**无从审计**。删除数据属高危行为，这不可接受。

本文件钉住三件事：
1. **桥接真的生效**（行为测试：标准 logging 的 INFO 必须被 loguru 的 sink 收到）；
2. **调用点归属正确**（不能指向 `logging/__init__.py` 或 `<unknown>`）；
3. **噪声被压住**（werkzeug 之类的 INFO 不许刷屏）+ **幂等**（重复 setup 不重复投递）。
"""

from __future__ import annotations

import logging
import pathlib

import pytest


@pytest.fixture
def logging_env(tmp_path):
    """跑一次 `setup_logging`，并**还原**全局 root logger（别污染别的用例）。"""
    from loguru import logger

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_noisy = {
        name: logging.getLogger(name).level
        for name in ("werkzeug", "engineio", "socketio", "urllib3", "pika",
                     "apscheduler", "asyncio", "matplotlib", "PIL")
    }
    try:
        from core.structured_logging import setup_logging
        log = setup_logging(log_dir=str(tmp_path), json_format=False, log_level="INFO")
        yield log
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        for name, lvl in saved_noisy.items():
            logging.getLogger(name).setLevel(lvl)
        try:
            logger.remove()
        except Exception:  # noqa: BLE001
            pass


def _capture(log):
    """给 loguru 挂一个临时 sink，返回 (records 列表, 卸载函数)。"""
    records: list[dict] = []
    sink_id = log.add(lambda m: records.append(dict(m.record)), level="DEBUG")
    return records, lambda: log.remove(sink_id)


class TestStdlibLoggingIsBridged:
    def test_bridge_handler_is_installed(self, logging_env):
        from core.structured_logging import InterceptHandler

        assert any(isinstance(h, InterceptHandler) for h in logging.getLogger().handlers), (
            "root logger 上没有 InterceptHandler —— 标准 logging 的日志又会被静默丢弃"
        )

    def test_root_level_allows_info(self, logging_env):
        assert logging.getLogger().level <= logging.INFO, (
            "root logger 级别高于 INFO —— INFO 会在到达 handler 之前就被丢掉"
        )

    def test_stdlib_info_reaches_loguru(self, logging_env):
        """★ 核心：标准 logging 的 INFO 必须被 loguru 的 sink 收到。"""
        records, stop = _capture(logging_env)
        try:
            logging.getLogger("core.retention_scheduler").info("数据保留策略执行：删除 5 行")
        finally:
            stop()
        msgs = [r["message"] for r in records]
        assert any("数据保留策略执行" in m for m in msgs), (
            "标准 logging 的 INFO 没被桥接进 loguru ——\n"
            "  这正是「数据保留策略在部署环境里隐身」的根因：\n"
            "  跑没跑、删了多少，运维一条都看不到。"
        )

    def test_stdlib_warning_and_error_also_bridged(self, logging_env):
        records, stop = _capture(logging_env)
        try:
            lg = logging.getLogger("core.some_module")
            lg.warning("warn-msg")
            lg.error("error-msg")
        finally:
            stop()
        levels = {r["level"].name for r in records}
        assert {"WARNING", "ERROR"} <= levels


class TestCallSiteAttribution:
    """调用点必须指向**真正的调用方**，不是 logging 内部或 `<unknown>`。"""

    def test_module_and_line_point_at_the_caller(self, logging_env):
        records, stop = _capture(logging_env)
        try:
            logging.getLogger("core.retention_scheduler").info("call-site-probe")
        finally:
            stop()
        hit = [r for r in records if r["message"] == "call-site-probe"]
        assert hit, "没收到探针日志"
        r = hit[0]
        # ⚠️ 踩过两次：`depth=2` 起手（loguru 官方写法）→ 指向 logging/__init__.py；
        #    「一路 f_back 走出 logging」→ 指向 <unknown>。
        #    现在改成直接用标准 record 自带的 module/funcName/lineno（零算术）。
        assert r["module"] == __name__.rsplit(".", 1)[-1], (
            f"module 归属错了：{r['module']!r}（应为本测试模块）——"
            " 日志会指到 logging 内部，排障时找不到真正打日志的地方"
        )
        assert "<unknown>" not in (r["module"], r["function"]), (
            f"调用点没解析出来：module={r['module']!r} function={r['function']!r}"
        )
        assert r["line"] > 0


class TestNoiseAndIdempotency:
    def test_noisy_third_party_info_is_suppressed(self, logging_env):
        """werkzeug 每个请求一行 INFO —— 放进来会把有价值的行淹掉。"""
        records, stop = _capture(logging_env)
        try:
            logging.getLogger("werkzeug").info("GET /x 200")
        finally:
            stop()
        assert not any("GET /x" in r["message"] for r in records), (
            "werkzeug 的 INFO 被放进来了 —— 日志会被请求行刷满，等于又不可读"
        )

    def test_noisy_third_party_warning_still_passes(self, logging_env):
        records, stop = _capture(logging_env)
        try:
            logging.getLogger("werkzeug").warning("werkzeug-warn")
        finally:
            stop()
        assert any("werkzeug-warn" in r["message"] for r in records), (
            "噪声抑制过头了 —— WARNING 及以上必须放行"
        )

    def test_setup_is_idempotent_no_duplicate_delivery(self, tmp_path, logging_env):
        """重复 `setup_logging` 不能让同一条日志投递多次。"""
        from core.structured_logging import setup_logging
        setup_logging(log_dir=str(tmp_path), json_format=False, log_level="INFO")
        records, stop = _capture(logging_env)
        try:
            logging.getLogger("core.dup_probe").info("dup-probe")
        finally:
            stop()
        n = sum(1 for r in records if r["message"] == "dup-probe")
        assert n == 1, f"同一条日志投递了 {n} 次 —— handler 被重复安装了"

    def test_bridge_never_raises(self, logging_env):
        """日志桥自己绝不能把应用搞挂。

        round 301：这是**有意为之的 smoke**，判据就是「4 种畸形输入都不抛」。
        桥接器出错只会让日志少一行，加业务断言反而会把日志实现钉死。
        标记 empty-test-ok 让测试卫生守卫放行（见 tools/_scan_empty_asserts.py）。
        """
        lg = logging.getLogger("core.weird")
        lg.info("普通")
        lg.info("带参数 %s 与 %d", "x", 1)
        lg.info("没有参数的百分号 %")
        lg.info("多行\n第二行")
        # empty-test-ok：不抛即通过，见 docstring


# ---------------------------------------------------------------------------
# 全局日志状态的泄漏 —— 本轮（round 212）真实踩到的那条红
# ---------------------------------------------------------------------------
#
# 形状：`setup_logging()` 有**全局副作用**（装桥 / 改 root level / 压噪声 logger）。
# 上游用例调用它而不还原 → 下游用例活在「桥已装」的状态里 →
# 日志真的落盘 → 墙钟断言被日志 I/O 撞破。
# 失败出现在**下游**用例上，看起来与被调用者完全无关。
#
# 修法分两层，这里钉住的是**系统性那层**（conftest 的每用例自动还原）。

CONFTEST = pathlib.Path(__file__).resolve().parent / "conftest.py"


class TestGlobalLoggingStateDoesNotLeak:
    def test_conftest_has_an_autouse_logging_restore(self):
        """★ 防回退：conftest 里那个「每用例还原全局日志状态」的 autouse fixture 不能被删。

        判别方式用 **AST**（不是 grep）—— 本文件里就有一处注释提到
        `setup_logging()`，文本匹配会把注释算成代码（同一个坑本轮踩了三次）。
        """
        import ast

        tree = ast.parse(CONFTEST.read_text(encoding="utf-8"))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decos = [ast.unparse(d) for d in node.decorator_list]
            if not any("fixture" in d for d in decos):
                continue
            if not any("autouse" in d for d in decos):
                continue
            body = ast.unparse(node)
            if "handlers" in body and "setLevel" in body:
                found.append(node.name)
        assert found, (
            "tests/conftest.py 里找不到「autouse 且还原 root handlers/level」的 fixture ——\n"
            "  删掉它，`setup_logging()` 的全局副作用就会在用例之间泄漏：\n"
            "  上游用例装桥 → 下游用例的日志真的落盘 → 墙钟型断言被日志 I/O 撞破，\n"
            "  而失败会出现在**下游**用例上，看起来完全无关（round 212 实测过）。"
        )

    def test_setup_logging_really_has_global_side_effects(self, tmp_path):
        """前提守卫：上面那条防回退只在「`setup_logging` 真有全局副作用」时才有意义。

        如果哪天它改成纯局部配置，这条会红 —— 提醒复核 conftest 那个 fixture 是否还需要。
        """
        from core.structured_logging import InterceptHandler, setup_logging

        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        try:
            # ⚠️ 日志目录必须用 tmp_path —— 第一版写成了 `__file__` 所在目录，
            #    结果往 **tests/** 里落了 3 个 .log（`git status` 立刻多出未跟踪文件）。
            setup_logging(log_dir=str(tmp_path), log_level="INFO")
            assert any(isinstance(h, InterceptHandler) for h in root.handlers), (
                "setup_logging 没有装 stdlib→loguru 桥 —— 前提变了，请复核 conftest 的还原 fixture"
            )
            assert root.level == logging.INFO
        finally:
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)
