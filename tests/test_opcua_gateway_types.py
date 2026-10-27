# -*- coding: utf-8 -*-
"""`gateway/opcua_gateway.py` 的类型转换与容忍策略测试（gateway 短板第三站）。

与 s7 那站不同：OPC UA 的解码逻辑（`_read_device_data_async` 的类型转换）
**自包含** —— client 是注入的假对象、asyncio 是标准库，不依赖 asyncua 包
（asyncua 在模块级是 try/except 可选依赖，本 venv 未装）→ **本文件全部真跑，
无需 skip**。

覆盖：
* `_read_device_data_async` 的六种类型转换（double/float/int 族/bool/未知）
  + 单节点失败容忍（warning 继续、不拖垮其它节点）；
* `read_device_data` 的 None 路径（无 client / 无节点配置）；
* `write_node` 的无 client 早退。
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
from unittest.mock import MagicMock

import pytest

_BACKEND = pathlib.Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _gw():
    from gateway.opcua_gateway import OPCUAGateway
    import logging
    g = OPCUAGateway.__new__(OPCUAGateway)
    g.logger = logging.getLogger("test_opcua_gateway")
    return g


def _client(values: dict):
    """假 OPC UA client：get_node(node_id) → read_value() 返回设定值。

    values: {node_id: value}；抛异常的节点用 Exception 实例作值。
    """
    c = MagicMock()

    def get_node(node_id):
        node = MagicMock()
        v = values.get(node_id)
        if isinstance(v, Exception):
            fut = asyncio.Future()
            fut.set_exception(v)
            node.read_value = MagicMock(return_value=fut)
        else:
            fut = asyncio.Future()
            fut.set_result(v)
            node.read_value = MagicMock(return_value=fut)
        return node

    c.get_node = get_node
    return c


class TestReadDeviceDataAsync:
    def _run(self, g, client, node_configs):
        return asyncio.run(g._read_device_data_async(client, node_configs))

    def test_double_and_float(self):
        g = _gw()
        out = self._run(g, _client({"ns=2;i=1": 3.5, "ns=2;i=2": 7.0}),
                        [{"name": "a", "node_id": "ns=2;i=1", "type": "double"},
                         {"name": "b", "node_id": "ns=2;i=2", "type": "float"}])
        assert out == {"a": 3.5, "b": 7.0}

    def test_int_family_goes_through_int(self):
        """int 族必须走 int() 再 float()：20.9 变 20.0（截断，不是四舍五入）。"""
        g = _gw()
        out = self._run(g, _client({"n1": 16, "n2": 20.9, "n3": -5}),
                        [{"name": "i32", "node_id": "n1", "type": "int32"},
                         {"name": "u16", "node_id": "n2", "type": "uint16"},
                         {"name": "i16", "node_id": "n3", "type": "int16"}])
        assert out == {"i32": 16.0, "u16": 20.0, "i16": -5.0}

    def test_int_family_with_float_string_is_dropped(self):
        """⚠️ 钉住现状边界（不是缺陷声明，是类型契约）：

        int 族走 `float(int(value))`，而 ``int('16.0')`` 在 Python 里抛
        ValueError → 该节点被**静默丢弃**（只留 warning）；double 族
        `float('16.0')` 反而能解析。即 **int 族对字符串脏值比 double 脆**。
        OPC UA 服务端按声明类型返回值时不会出现这种情况，但现场若配错
        数据类型（把整型节点标成别的），表现就是「这个点莫名没有」——
        故钉住：脏值 → 丢节点而不是猜一个值。
        """
        g = _gw()
        out = self._run(g, _client({"n1": "16.0", "ok": 1.0}),
                        [{"name": "i32", "node_id": "n1", "type": "int32"},
                         {"name": "okv", "node_id": "ok", "type": "double"}])
        assert out == {"okv": 1.0}, f"脏值节点应被丢弃、好节点保留: {out}"

    def test_bool_becomes_one_zero(self):
        g = _gw()
        out = self._run(g, _client({"b1": True, "b2": False}),
                        [{"name": "on", "node_id": "b1", "type": "bool"},
                         {"name": "off", "node_id": "b2", "type": "bool"}])
        assert out == {"on": 1.0, "off": 0.0}
        assert isinstance(out["on"], float)

    def test_unknown_type_falls_back_to_float(self):
        g = _gw()
        out = self._run(g, _client({"x": 2.5}),
                        [{"name": "v", "node_id": "x", "type": "datetime"}])
        assert out == {"v": 2.5}

    def test_failing_node_does_not_kill_others(self):
        """单节点失败只 warning，其余节点的值必须保留（OPC UA 半活的典型场景）。"""
        g = _gw()
        out = self._run(g, _client({"good": 1.5, "bad": Exception("node stale")}),
                        [{"name": "g", "node_id": "good", "type": "double"},
                         {"name": "b", "node_id": "bad", "type": "double"}])
        assert out == {"g": 1.5}, f"坏节点拖垮了好节点: {out}"

    def test_all_nodes_failing_returns_empty(self):
        g = _gw()
        out = self._run(g, _client({"a": Exception("x"), "b": Exception("y")}),
                        [{"name": "a", "node_id": "a", "type": "double"},
                         {"name": "b", "node_id": "b", "type": "double"}])
        assert out == {}

    def test_missing_node_id_in_value(self):
        """node_config 没有 node_id → get_node(None) 应走异常路径而不是 KeyError。"""
        g = _gw()
        out = self._run(g, _client({}), [{"name": "x", "type": "double"}])
        assert out == {}


class TestReadDeviceDataNonePaths:
    """同步外层的 None 路径（不依赖 asyncua，也不真的起事件循环）。"""

    def _gw2(self):
        g = _gw()
        g._clients = {}
        g._node_configs = {}
        return g

    def test_no_client_returns_none(self):
        g = self._gw2()
        g._clients = {}
        assert g.read_device_data("d1") is None

    def test_no_node_config_returns_none(self):
        g = self._gw2()
        g._clients = {"d1": MagicMock()}
        assert g.read_device_data("d1") is None

    def test_exception_on_future_returns_none(self):
        g = self._gw2()
        g._clients = {"d1": MagicMock()}
        g._node_configs = {"d1": [{"name": "a", "node_id": "n", "type": "double"}]}
        # _loop 是 MagicMock → run_coroutine_threadsafe 抛异常 → except 分支。
        # round 296：生产代码在 except 里 `coro.close()`，所以这里不会再出现
        # `RuntimeWarning: coroutine ... was never awaited`（原来每次跑都刷一条）。
        g._loop = MagicMock()
        assert g.read_device_data("d1") is None


class TestWriteNode:
    def test_no_client_returns_false(self):
        g = _gw()
        g._clients = {}
        assert g.write_node("d1", "ns=2;i=3", 1.0) is False
