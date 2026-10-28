# -*- coding: utf-8 -*-
"""round 301：智能模块失败静默 + 测试卫生守卫。

这一轮的主战场是**测试自己**（见 tests/test_empty_assert_guard.py 与
tools/_scan_empty_asserts.py）—— 因为空测会让后续每一轮的变异验证失去意义。
产品代码只补了一处：`_safe_intel` 的失败不计数。
"""

from __future__ import annotations

from unittest.mock import MagicMock


def _collector():
    from 采集层.data_collector import DataCollector
    return DataCollector(MagicMock(), MagicMock(), MagicMock())


def test_intel_failure_is_counted_and_reported():
    """模块抛异常必须计数，并在 get_stats 里可见。

    原来 `_safe_intel` 只打一行 `logger.error` —— 某个模块持续崩溃时
    `/stats` 上一切正常，而它本该产出的判异 / 健康分 / OEE 一直是空的。
    """
    c = _collector()

    def boom(*a, **k):
        raise RuntimeError('spc 炸了')

    c._safe_intel('spc_analyzer', boom)
    c._safe_intel('spc_analyzer', boom)
    c._safe_intel('oee_calculator', boom)

    stats = c.get_stats()
    assert stats.get('intel_failures') == {'spc_analyzer': 2, 'oee_calculator': 1}, (
        f'失败次数没按模块分开统计：{stats.get("intel_failures")}')
    assert 'spc 炸了' in stats.get('intel_last_error', {}).get('spc_analyzer', ''), (
        '最近一次错误没暴露出来 —— 运维只能去翻日志')


def test_intel_failure_does_not_stop_other_modules():
    """反向对照：一个模块崩了，其余模块照旧执行（这是 _safe_intel 的本职）。"""
    c = _collector()
    calls = []

    def boom(*a, **k):
        raise RuntimeError('boom')

    c._safe_intel('bad', boom)
    c._safe_intel('good', lambda *a, **k: calls.append('ok'))

    assert calls == ['ok'], '一个模块抛异常把后续模块带走了'


def test_stats_omit_intel_failures_when_healthy():
    """一切正常时不要往 /stats 里塞空字典（保持接口干净、不制造噪声）。"""
    c = _collector()
    stats = c.get_stats()
    assert 'intel_failures' not in stats
    assert 'intel_last_error' not in stats
