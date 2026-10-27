# -*- coding: utf-8 -*-
"""网关"起来了"必须能从返回值看出来（round 293）。

缺陷形态（同一族：**通道失败静默** —— 这次是"启动"这条通道）
----------------------------------------------------------
`BaseGateway.start()` 没有返回值，而它有三种失败方式，全是静默的：

| 失败方式 | 原行为 |
|---|---|
| `_connect_mqtt()` 抛异常 | 异常往上抛；`run_gateway.main()` 的 `try/except` 只打一行日志就继续 |
| `connect()` 返回 False | 这里只 `logger.error` 然后 `return`（`running` 仍 False） |
| 轮询线程起不来 | 无异常可捕，也无返回值 |

`run_gateway.main()` 随后照样打印「网关服务已启动」，进程进入 `while True: sleep(1)`。
于是外部看到的是：**进程活着、退出码 0、最后一行日志写着"已启动"** ——
systemd / 容器编排 / 运维都以为它在跑，而 MQTT 上一条数据都不会有。

判据
----
1. `start()` 的返回值 = **真的起来了**（MQTT 连上 + 设备全连上 + 轮询线程已起）；
2. 一个都没起来时，启动脚本必须**以非零码退出**，而不是空转；
3. 失败要留痕（`stats.errors` / `last_error`），并且部分失败要点名是哪几个。
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

import paho.mqtt.client as mqtt

import gateway.base_gateway as bg
from gateway.base_gateway import BaseGateway
from gateway.run_gateway import start_gateways


class _Gateway(BaseGateway):
    """可编程网关：分别指定"能连上的设备"与"连不上的设备"。"""

    def __init__(self, ok_devices=('d1',), fail_devices=(), mqtt_raises=False):
        self._ok_devices = tuple(ok_devices)
        self._fail_devices = tuple(fail_devices)
        self._mqtt_raises = mqtt_raises
        devices = [{'device_id': d} for d in (*self._ok_devices, *self._fail_devices)]
        super().__init__({'gateway_id': 'gw_t', 'devices': devices,
                          'poll_interval': 0.01})

    # ---- 覆盖 MQTT 连接：可控失败 ----
    def _connect_mqtt(self):
        if self._mqtt_raises:
            raise ConnectionRefusedError('broker 拒绝连接')
        self.mqtt_client = MagicMock()
        self.mqtt_client.publish.return_value = MagicMock(rc=mqtt.MQTT_ERR_SUCCESS)

    def connect(self) -> bool:
        """按配置逐台"连接"，并如实写 connected_devices（与真实网关同构）。"""
        for d in self._ok_devices:
            self.connected_devices[d] = True
        for d in self._fail_devices:
            self.connected_devices[d] = False
        return not self._fail_devices

    def disconnect(self):
        pass

    def read_device_data(self, device_id):
        return None

    def convert_to_telemetry(self, device_id, raw_data):
        raise NotImplementedError


@pytest.fixture
def clean_state():
    yield
    bg._active_gateways.clear()


# ---------------------------------------------------------------------------
# 1. start() 必须如实返回成败
# ---------------------------------------------------------------------------

def test_start_returns_false_when_mqtt_fails(clean_state):
    gw = _Gateway(mqtt_raises=True)

    ok = gw.start()

    assert ok is False, 'MQTT 连不上却返回成功 —— 调用方会以为网关在跑'
    assert gw.running is False, 'MQTT 没连上却把 running 置 True'
    assert gw.stats['errors'] >= 1, '启动失败没有记账'
    assert 'MQTT' in (gw.stats['last_error'] or '')


def test_start_returns_false_when_devices_fail(clean_state):
    gw = _Gateway(ok_devices=(), fail_devices=('d1',))

    ok = gw.start()

    assert ok is False, '设备全连不上却返回成功'
    assert gw.running is False


def test_start_returns_true_when_everything_ok(clean_state):
    """反向对照：真的起来了必须返回 True 且 running=True。"""
    gw = _Gateway()
    try:
        ok = gw.start()
        assert ok is True
        assert gw.running is True
        assert gw._poll_thread is not None and gw._poll_thread.is_alive()
    finally:
        gw.stop()


def test_start_is_idempotent_and_reports_true(clean_state):
    """已在运行时再调 start()：不应重复启动，也不应报失败。"""
    gw = _Gateway()
    try:
        assert gw.start() is True
        assert gw.start() is True
    finally:
        gw.stop()


# ---------------------------------------------------------------------------
# 2. 启动脚本必须按真实结果统计（并在全失败时退出）
# ---------------------------------------------------------------------------

def test_start_gateways_splits_ok_and_failed():
    ok1 = MagicMock(gateway_id='gw_ok')
    ok1.start.return_value = True
    bad = MagicMock(gateway_id='gw_bad')
    bad.start.return_value = False
    boom = MagicMock(gateway_id='gw_boom')
    boom.start.side_effect = RuntimeError('炸了')

    started, failed = start_gateways([ok1, bad, boom])

    assert started == ['gw_ok']
    assert failed == ['gw_bad', 'gw_boom']


def test_start_gateways_all_failed():
    gws = []
    for i in range(3):
        g = MagicMock(gateway_id=f'gw{i}')
        g.start.return_value = False
        gws.append(g)

    started, failed = start_gateways(gws)

    assert started == [], '一个都没起来，却报告有成功'
    assert len(failed) == 3


def test_main_exits_nonzero_when_no_gateway_started():
    """静态守卫：全失败必须真的 `sys.exit(1)`，不能继续空转。

    行为用例要跑整个 `main()`（argparse/配置/信号），成本远大于收益；
    这里改用 **AST** 盯结构 —— 字符串切片版本会被"函数后面另一个
    `sys.exit(1)`"骗过（实测：去掉那行后守卫仍然绿）。

    判据：存在一个 `if <not started>:` 分支，其**函数体里**含 `sys.exit(1)`。
    """
    import ast
    import inspect

    import gateway.run_gateway as rg

    src = inspect.getsource(rg.main)
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'main')

    def _is_not_started(test: ast.AST) -> bool:
        """识别 `not started` / `len(started) == 0` 这类判空条件。"""
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            return isinstance(test.operand, ast.Name) and test.operand.id == 'started'
        if isinstance(test, ast.Compare):
            left = test.left
            if isinstance(left, ast.Call) and getattr(left.func, 'id', '') == 'len':
                return True
            if isinstance(left, ast.Name) and left.id == 'started':
                return True
        return False

    def _exits(body: list[ast.stmt]) -> bool:
        for node in body:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)                         and sub.func.attr == 'exit'                         and isinstance(sub.func.value, ast.Name) and sub.func.value.id == 'sys':
                    return True
        return False

    hits = [n for n in ast.walk(fn) if isinstance(n, ast.If) and _is_not_started(n.test)]
    assert hits, 'main() 里没有"全部失败则退出"的判断分支'
    assert any(_exits(h.body) for h in hits), (
        '全部启动失败时没有 sys.exit(1) —— 进程会空转，外部以为网关在跑')

    assert 'start_gateways(' in src, 'main() 没有走统一的启动统计'


# ---------------------------------------------------------------------------
# 3. 部分成功也必须跑（round 294）
# ---------------------------------------------------------------------------

def test_start_continues_when_some_devices_fail(clean_state):
    """10 台只挂 1 台 → 网关必须照跑，另外 9 台的好数据不能一起丢。

    各协议网关的 `connect()` 返回的是"是否**全部**连上"，293 起 `start()` 把它
    当生死判据 —— 于是"少连一台"会让整个网关不启动。这是策略过严：
    现场更常见的是个别设备断线，而不是全部。
    """
    gw = _Gateway(ok_devices=('d1', 'd2', 'd3'), fail_devices=('d4',))
    try:
        ok = gw.start()
        assert ok is True, '一台连不上就把整个网关判死 —— 另外三台的数据全丢'
        assert gw.running is True
        assert gw.stats['devices_connected'] == 3
        assert gw.stats['devices_failed'] == 1
    finally:
        gw.stop()


def test_start_is_ok_when_no_device_configured(clean_state):
    """反向对照：没配设备的网关不该被"0 台连上"判死（那不是故障，是没有活干）。"""
    gw = _Gateway(ok_devices=(), fail_devices=())
    try:
        assert gw.start() is True
        assert gw.running is True
    finally:
        gw.stop()
