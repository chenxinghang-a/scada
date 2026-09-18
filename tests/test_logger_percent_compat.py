# -*- coding: utf-8 -*-
"""get_logger() 必须兼容 stdlib 的 %-格式化调用（round 168j）。

背景（实测确认）
----------------
`core/structured_logging.py` 的 `get_logger()` 原先直接返回
`loguru.logger.bind(...)`，而 loguru **只认 `{}` 风格占位符**：

    logger.warning("读取 %s 失败: %s", device, err)
    # loguru 实际输出：读取 %s 失败: %s      ← 参数被静默丢弃

本项目 1400+ 处日志调用（info 548 / error 477 / warning 283 / debug 171）
**全是 stdlib 的 %-风格**。于是所有带参数的日志都只剩字面 %s/%d ——
排障时看不到任何变量值：设备 ID、异常内容、配置项名称全部丢失，
日志里只有一句没有上下文的空话。

实测证据（修前）：
    18:47:25 | WARNING | <string>:6 | 带参数: %s | 数字=%d

本文件锁住修复后的行为。
"""

import pytest


@pytest.fixture
def cap():
    """收集日志文本的探针。"""
    from core.structured_logging import setup_logging, get_logger

    setup_logging(log_dir='logs', log_level='DEBUG')

    collected = []

    from loguru import logger as _lg

    sink_id = _lg.add(lambda m: collected.append(m), level='DEBUG')
    try:
        yield get_logger('probe'), collected
    finally:
        _lg.remove(sink_id)


def test_percent_args_are_formatted(cap):
    """**核心回归**：%s / %d 必须被实际参数替换。"""
    log, collected = cap
    log.warning('带参数: %s | 数字=%d', 'ABC', 42)

    text = ''.join(str(m) for m in collected)
    assert '带参数: ABC | 数字=42' in text, \
        f'参数没被格式化，日志里还是字面占位符: {text[-300:]}'


def test_no_args_message_unchanged(cap):
    """无参数的消息原样输出（不能因为包装把 % 吃掉）。"""
    log, collected = cap
    log.info('无参数: 普通消息')

    text = ''.join(str(m) for m in collected)
    assert '无参数: 普通消息' in text


def test_exception_object_is_stringified(cap):
    """异常对象要能被 %s 正常渲染，而不是打印对象 repr。"""
    log, collected = cap
    log.error('异常样式: %s', ValueError('boom'))

    text = ''.join(str(m) for m in collected)
    assert '异常样式: boom' in text


def test_arg_count_mismatch_does_not_lose_the_log(cap):
    """格式串与参数个数不匹配时，日志不能被丢掉。"""
    log, collected = cap
    log.debug('不匹配: %s %s', 'only-one')

    text = ''.join(str(m) for m in collected)
    assert '不匹配' in text, '日志整条丢了'
    assert 'only-one' in text, '参数信息丢了'


def test_line_number_points_to_caller(cap):
    """日志行号必须指向**调用方**，不是包装器内部。

    包装器若不加 `opt(depth=1)`，所有日志都会指向 structured_logging.py，
    日志里的 module:line 定位价值全失。
    """
    log, collected = cap
    log.warning('定位探针')

    rec = collected[-1]
    name = rec.record['name'] if hasattr(rec, 'record') else str(rec)
    assert 'structured_logging' not in name, \
        f'行号指向了包装器而不是调用方: {name}'
