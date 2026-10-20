# -*- coding: utf-8 -*-
"""`tools/scan_config_readers.py` 的**假阳性**回归测试。

背景
----
这个扫描器的用途是找「配置算了但没人读」的字段（MEMORY 硬规则 17）。
它的可信度完全取决于**它报出来的东西是不是真的没人读**。

2026-10-19 实测发现：它把 `FlaskConfig.HOST / PORT / REAL_PORT` 报成「无人读」，
而 `run.py:510` 明明在读 `WebConfig.HOST` —— 因为 `config.py:258` 有一行
类别名 `WebConfig = FlaskConfig`，旧实现只认字面类名，认不出别名。

为什么这不是「多几行输出」的小事
--------------------------------
56 项里混进 7 项假阳性之后，这份清单就**没法逐条核对**了，人的处理方式会变成
「大概看看」→ 真正的死配置被淹掉 → 扫描器事实上失效。
这与本项目反复修的「闸门喊狼来了」（fail-open 闸门）是同一类病。

本测试钉三件事：
  ① `config.py` 里的别名确实存在（否则测试前提消失，要显式红）；
  ② 别名下的读取被计入**规范类名**；
  ③ 不解析别名时该读取点会丢 —— 即这条测试真的能抓到回归。
"""

import ast
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools import scan_config_readers as scr  # noqa: E402


def test_config_module_still_has_class_alias():
    """前提：config.py 里存在 `WebConfig = FlaskConfig` 这类别名。

    如果哪天别名被删掉、run.py 改成直接读 `FlaskConfig.HOST`，
    本文件的前提就不成立了 —— 那时应当显式失败提醒维护者，
    而不是让后面的断言在「没有别名可测」的情况下空转通过。
    """
    aliases = scr.collect_class_aliases(scr.collect_config_classes())
    assert aliases, (
        "config.py 里已经没有类别名了。若确实删掉了别名，"
        "请同步删除本测试并确认 scan_config_readers 不再需要别名解析。"
    )
    assert aliases.get("WebConfig") == "FlaskConfig", (
        f"类别名解析结果异常: {aliases!r}（期望 WebConfig -> FlaskConfig）"
    )


def test_run_py_really_reads_through_alias():
    """前提：生产代码确实通过别名读取（否则别名解析没有意义）。"""
    run_src = (REPO / "run.py").read_text(encoding="utf-8")
    tree = ast.parse(run_src)
    alias_reads = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "WebConfig"
        ):
            alias_reads.add(node.attr)
    assert {"HOST", "PORT"} <= alias_reads, (
        f"run.py 里没有通过 WebConfig 读 HOST/PORT，实际读到: {sorted(alias_reads)}"
    )


def test_alias_reads_count_toward_canonical_class():
    """核心断言：`WebConfig.HOST` 必须算作 `FlaskConfig.HOST` 的读取点。"""
    classes = scr.collect_config_classes()
    resolve = {name: name for name in classes}
    resolve.update(scr.collect_class_aliases(classes))

    readers, _dynamic = scr.scan_readers(resolve, [REPO / "run.py"])

    for attr in ("HOST", "PORT"):
        hits = readers.get(("FlaskConfig", attr), [])
        assert hits, (
            f"FlaskConfig.{attr} 经别名 WebConfig 被 run.py 读取，"
            f"但扫描器没记到规范类名下（别名解析失效）"
        )
        assert any(h.startswith("run.py:") for h in hits), hits


def test_without_alias_resolution_the_read_is_lost():
    """变异检查：不传别名时读取点确实消失 —— 证明上一条测试不是空转。

    这条同时也说明**旧实现的行为**：只看字面类名就会漏掉别名读取，
    也就是假阳性的来源。
    """
    classes = scr.collect_config_classes()
    plain = {name: name for name in classes}  # 故意不合并别名

    readers, _ = scr.scan_readers(plain, [REPO / "run.py"])

    assert not readers.get(("FlaskConfig", "HOST"), []), (
        "不解析别名时不该有 FlaskConfig.HOST 的读取点；"
        "若这里非空，说明 run.py 里出现了直接读 FlaskConfig.HOST 的代码，"
        "本变异检查需要重新设计。"
    )


def test_scan_covers_alias_in_config_dot_form():
    """`config.WebConfig.HOST` 形态（带 config 前缀）也要能解析。"""
    classes = scr.collect_config_classes()
    resolve = {name: name for name in classes}
    resolve.update(scr.collect_class_aliases(classes))

    tmp = REPO / ".pytest_scan_alias_probe.py"
    tmp.write_text(
        "import config\nVALUE = config.WebConfig.HOST\n",
        encoding="utf-8",
    )
    try:
        readers, _ = scr.scan_readers(resolve, [tmp])
        assert readers.get(("FlaskConfig", "HOST")), (
            "config.WebConfig.HOST 形态没被解析到 FlaskConfig.HOST"
        )
    finally:
        tmp.unlink(missing_ok=True)


def test_no_known_false_positive_for_web_config():
    """端到端：对真实仓库跑一遍，FlaskConfig 的 HOST/PORT/REAL_PORT 不得报未读。"""
    classes = scr.collect_config_classes()
    resolve = {name: name for name in classes}
    resolve.update(scr.collect_class_aliases(classes))

    prod_roots = [REPO / d for d in scr.PROD_DIRS if (REPO / d).exists()]
    prod_roots += [REPO / f for f in scr.PROD_FILES if (REPO / f).exists()]
    readers, _ = scr.scan_readers(resolve, prod_roots)

    for attr in ("HOST", "PORT", "REAL_PORT", "SECRET_KEY"):
        assert readers.get(("FlaskConfig", attr)), (
            f"FlaskConfig.{attr} 被 run.py / routes.py 读取，"
            f"扫描器却报「无人读」—— 假阳性回归"
        )
