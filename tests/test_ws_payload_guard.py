# -*- coding: utf-8 -*-
"""WebSocket 事件处理器的 payload 类型防护（round 168h）。

背景
----
`展示层/websocket.py` 的三个事件处理器原先这样取字段：

    device_id = (data or {}).get('device_id')      # subscribe / unsubscribe
    'client_timestamp': data.get('timestamp') if data else None   # heartbeat

`(data or {})` 只防住了 **None 和空值**，防不住**类型错误**：
客户端发 `socket.emit('heartbeat', 'abc')` → `"abc".get(...)` → AttributeError。

在 Socket.IO 事件处理器里，这个异常的后果是**服务端刷堆栈、客户端收不到
任何回应，而连接不会断** —— 客户端只会觉得"卡住了"，比直接报错更难排查。

修复：统一用模块级 `_payload()`，非 dict 一律规整成 `{}`。
"""

import inspect

import pytest


# ---------------------------------------------------------------- 单元行为

@pytest.mark.parametrize('bad', ['abc', None, 123, 4.5, ['a', 'b'], b'bytes', object()])
def test_payload_normalizes_non_dict_to_empty(bad):
    """**核心回归**：任何非 dict 输入都必须规整成空 dict，不能抛异常。"""
    from 展示层.websocket import _payload

    result = _payload(bad)
    assert result == {}, f'_payload({bad!r}) 应返回空 dict，实际 {result!r}'
    assert isinstance(result, dict)


def test_payload_passes_dict_through():
    """dict 必须原样通过（不能丢字段）。"""
    from 展示层.websocket import _payload

    src = {'device_id': 'dev1', 'extra': [1, 2]}
    assert _payload(src) is src


def test_payload_is_module_level():
    """必须是模块级函数 —— 闭包版本无法被单独测试。"""
    import 展示层.websocket as ws

    assert hasattr(ws, '_payload'), '缺少模块级 _payload'
    assert inspect.isfunction(ws._payload)
    # 不能是某个外层函数里的局部定义
    assert ws._payload.__qualname__ == '_payload', \
        f'_payload 应是模块级函数，实际 {ws._payload.__qualname__}'


# ---------------------------------------------------------------- 静态守卫

def test_no_unsafe_payload_pattern_remains():
    """静态守卫：handler 里不得再出现 `(data or {})` 这类只防 None 的写法。

    用 AST 结构匹配而不是字符串匹配 —— 后者会误伤 docstring / 注释里
    对这个历史模式的说明文字（本文件的第一版就踩了这个坑）。
    """
    import ast

    import 展示层.websocket as ws

    tree = ast.parse(inspect.getsource(ws))

    or_with_dict = []     # `data or {}`
    ifexp_on_data = []    # `... if data else None`

    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            vals = node.values
            if (len(vals) == 2
                    and isinstance(vals[0], ast.Name) and vals[0].id == 'data'
                    and isinstance(vals[1], ast.Dict)):
                or_with_dict.append(node.lineno)
        if (isinstance(node, ast.IfExp)
                and isinstance(node.test, ast.Name) and node.test.id == 'data'):
            ifexp_on_data.append(node.lineno)

    assert not or_with_dict, \
        f'仍在使用 `data or {{}}`（只防 None，非字典会抛 AttributeError），行号 {or_with_dict}'
    assert not ifexp_on_data, \
        f'仍在用 `... if data else None` 判断 payload，行号 {ifexp_on_data}'


def test_all_handlers_use_payload_helper():
    """三个取 payload 的 handler 都必须走 _payload()。"""
    import ast

    import 展示层.websocket as ws

    tree = ast.parse(inspect.getsource(ws))
    calls = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == '_payload'
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == 'data'):
            calls.append(node.lineno)

    # subscribe + unsubscribe 取 device_id，heartbeat 取 timestamp
    assert len(calls) == 3, \
        f'应有 3 处 _payload(data) 调用（subscribe/unsubscribe/heartbeat），实际 {len(calls)} 处，行号 {calls}'
