# -*- coding: utf-8 -*-
"""测试卫生守卫（round 301）：不许出现「没执行也算通过」的空测。

背景
----
这一轮的重灾区不是产品代码，而是**测试本身**：

· `assert len(data) >= 0` —— 恒真；
· `try: <断言> except: pass` —— 断言抛异常时被一起吞掉；
· `if write_cb.called: <断言>` —— 回调没被调用时整块跳过；
· 整条测试只有一行调用 + 「不抛即通过」—— 空实现也能过。

这类测试的共同危害：**它们让后续每一轮的变异验证都失去意义**。
守卫逻辑在 `tools/_scan_empty_asserts.py`（也能单独跑，打印报告）。

允许豁免：函数体内写 `empty-test-ok`（要配理由）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / 'tests'


def _scanner():
    path = REPO / 'tools' / '_scan_empty_asserts.py'
    spec = importlib.util.spec_from_file_location('_scan_empty_asserts', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _scan():
    return _scanner().scan(TESTS)


def test_scanner_actually_scans():
    """守卫自检：扫描器必须真的读到测试文件，否则下面四条断言全是空的。

    round 297 的教训（排除规则写太宽，把自己仓库也跳过了）：守卫必须
    验证「目标仍在集合内」——这里用「已知的 2 处豁免必须被识别」当锚点。
    """
    issues = _scan()
    assert len(issues['D_declared']) >= 2, (
        '已知的两处 empty-test-ok 豁免没被识别出来 —— 扫描器或标记识别坏了：'
        f'{issues["D_declared"]}')


def test_no_assertion_swallowed_by_condition():
    issues = _scan()
    assert not issues['A'], (
        '断言被 `if <mock>.called:` 包住 —— 回调没被调用时整块跳过，测试恒绿：\n  '
        + '\n  '.join(issues['A']))


def test_no_assertion_swallowed_by_except_pass():
    issues = _scan()
    assert not issues['B'], (
        '断言被 `try/except: pass` 包住 —— 断言自己抛异常也会被当成通过：\n  '
        + '\n  '.join(issues['B']))


def test_no_always_true_assertion():
    issues = _scan()
    assert not issues['C'], (
        '恒真断言（len(...) >= 0 / x 与 x 自己比）：\n  ' + '\n  '.join(issues['C']))


def test_no_undeclared_empty_test():
    """整条测试没有任何失败信号时，必须显式声明 empty-test-ok 并写明理由。"""
    issues = _scan()
    assert not issues['D'], (
        '这些测试函数既没有断言、也没有 pytest.raises / mock.assert_*，'
        '空实现也能通过。要么补上判据，要么在函数体内写 empty-test-ok 说明理由：\n  '
        + '\n  '.join(issues['D']))
