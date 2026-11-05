# -*- coding: utf-8 -*-
"""动作结果判定口径（round 372）。

`bool({'success': False}) == True` 是仓库里最隐蔽的一类假成功：
写入失败被记成联锁动作执行成功，随后联锁状态锁死、停机指令永不重试。
"""
from __future__ import annotations

import pytest

from core.action_result import fail, is_failure, is_success, ok


@pytest.mark.parametrize('result', [False, {'success': False}])
def test_explicit_failures(result):
    assert is_failure(result) is True
    assert is_success(result) is False


@pytest.mark.parametrize('result', [
    True, None, {}, {'success': True}, {'status': 'ok'}, 1, 'done',
])
def test_everything_else_is_not_a_failure(result):
    """`None` 是"没有结论"（通知类回调），不能诬告成失败。"""
    assert is_failure(result) is False
    assert is_success(result) is True


def test_bool_of_failed_dict_is_the_trap():
    """钉住根因：直接 bool() 一个失败字典会得到 True。"""
    assert bool({'success': False}) is True
    assert is_failure({'success': False}) is True


def test_helpers_build_consistent_dicts():
    assert ok('保存成功') == {'success': True, 'message': '保存成功'}
    assert fail('磁盘满') == {'success': False, 'message': '磁盘满'}
    assert fail('x', code=500)['code'] == 500
