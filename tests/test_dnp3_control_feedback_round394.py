# -*- coding: utf-8 -*-
"""DNP3 遥控反馈策略（round 394，方案 C）。

背景：原来的实现「没有反馈点就跳过回读、直接返回成功」，与闭环确认
共用同一个"成功"语义 —— 值班员无法区分「断路器已到位」和「指令发出去了」。
本轮改为显式配置：

  · ``feedback_index``   指定回读的遥信索引（**不默认等于输出索引**）
  · ``require_feedback`` 为 True 时必须有 feedback_index，否则下发前拒绝

三种结果：VERIFIED / SENT_UNVERIFIED / FAILED。通信失败、超时、读回不一致
一律 FAILED（fail-closed），绝不因为"命令发出去了"就算成功。
"""
from __future__ import annotations

import time

import pytest

from gateway.dnp3_gateway import (
    ControlOutcome, ControlStatus, DNP3Gateway, DNP3ObjectGroup,
)

DEVICE = 'rtu1'


class _FakeClient:
    """最小 DNP3 客户端替身：只实现写路径用到的方法。"""

    def __init__(self, *, send_ok: bool = True, raise_on_read: bool = False):
        self.connected = True
        self.send_ok = send_ok
        self.raise_on_read = raise_on_read
        self.sent: list[tuple[int, int]] = []
        self.read_calls = 0
        self.data: dict[tuple[int, int], dict] = {}

    def send_control(self, index: int, value: int) -> bool:
        self.sent.append((index, value))
        return self.send_ok

    def request_data(self) -> None:
        self.read_calls += 1
        if self.raise_on_read:
            raise OSError('link down')

    def read_data(self) -> dict:
        return dict(self.data)


class _ReadbackClient(_FakeClient):
    """每次请求数据都刷新一个反馈点（时间戳取当前时刻）。"""

    def __init__(self, feedback_index: int, feedback_value: int, **kwargs):
        super().__init__(**kwargs)
        self.feedback_index = feedback_index
        self.feedback_value = feedback_value

    def request_data(self) -> None:
        super().request_data()
        self.data = {
            (DNP3ObjectGroup.BI, self.feedback_index): {
                'value': self.feedback_value,
                'time': time.time(),
            }
        }


def _gateway(registers: list[dict]) -> DNP3Gateway:
    return DNP3Gateway({
        'gateway_id': 'gw_test',
        'devices': [{'device_id': DEVICE, 'registers': registers}],
    })


def _attach(gw: DNP3Gateway, client: _FakeClient) -> _FakeClient:
    gw._clients[DEVICE] = client          # noqa: SLF001 —— 测试替身注入
    gw.connected_devices[DEVICE] = True
    return client


# ---------------------------------------------------------------------------
# 普通遥控点：没有 feedback_index、require_feedback=False
# ---------------------------------------------------------------------------

def test_plain_control_returns_sent_unverified_not_timeout():
    """验收判据：无反馈点 + 从站回执 0 → SENT_UNVERIFIED（不是超时失败）。"""
    gw = _gateway([
        {'name': 'breaker_cmd', 'index': 5, 'group': 'bo'},
    ])
    client = _attach(gw, _FakeClient(send_ok=True))

    result = gw.write_register(DEVICE, 5, 1)

    assert isinstance(result, ControlOutcome)
    assert result['success'] is True
    assert result['status'] == ControlStatus.SENT_UNVERIFIED
    assert result['verified'] is None
    assert result['message'] == '已下发（未验证）'
    assert bool(result) is True
    assert client.sent == [(5, 1)]
    # 没配反馈点就不该去读回
    assert client.read_calls == 0


def test_feedback_index_is_not_inferred_from_same_index():
    """同索引恰好有一个 BI 点，也不能拿它当反馈 —— 必须显式配置。"""
    gw = _gateway([
        {'name': 'breaker_cmd', 'index': 5, 'group': 'bo'},
        {'name': 'breaker_status', 'index': 5, 'group': 'bi'},
    ])
    client = _attach(gw, _FakeClient(send_ok=True))
    # 同索引的 BI 点值正好等于命令值（1）
    client.data = {
        (DNP3ObjectGroup.BI, 5): {'value': 1, 'time': time.time() + 10},
    }

    result = gw.write_register(DEVICE, 5, 1)

    assert result['status'] == ControlStatus.SENT_UNVERIFIED
    assert result['verified'] is None
    assert client.read_calls == 0, '未配置 feedback_index 却去读回了'


# ---------------------------------------------------------------------------
# 配置了 feedback_index：3 秒内读回一致 → VERIFIED
# ---------------------------------------------------------------------------

def test_feedback_match_returns_verified():
    gw = _gateway([
        {'name': 'breaker_cmd', 'index': 5, 'group': 'bo',
         'feedback_index': 20},
    ])
    client = _attach(gw, _ReadbackClient(20, 1))

    result = gw.write_register(DEVICE, 5, 1)

    assert result['success'] is True
    assert result['status'] == ControlStatus.VERIFIED
    assert result['verified'] is True
    assert bool(result) is True
    assert client.read_calls >= 1


def test_feedback_mismatch_is_failed_and_not_verified():
    """反馈不一致必须 FAILED —— 不能把"发出去了"报成成功。"""
    gw = _gateway([
        {'name': 'breaker_cmd', 'index': 5, 'group': 'bo',
         'feedback_index': 20},
    ])
    gw.readback_timeout = 0.2
    _attach(gw, _ReadbackClient(20, 0))     # 命令 1，反馈 0

    result = gw.write_register(DEVICE, 5, 1)

    assert result['success'] is False
    assert result['status'] == ControlStatus.FAILED
    assert result['verified'] is False
    assert bool(result) is False, '失败结果不能是真值（bool(dict) 陷阱）'


def test_no_feedback_data_times_out_as_failed():
    gw = _gateway([
        {'name': 'breaker_cmd', 'index': 5, 'group': 'bo',
         'feedback_index': 20},
    ])
    gw.readback_timeout = 0.15
    _attach(gw, _FakeClient(send_ok=True))   # 永远读不到反馈点

    result = gw.write_register(DEVICE, 5, 1)

    assert result['status'] == ControlStatus.FAILED
    assert result['success'] is False


def test_stale_cached_feedback_is_not_accepted():
    """缓存里命令前的旧值即使一致，也不能算验证通过。"""
    gw = _gateway([
        {'name': 'breaker_cmd', 'index': 5, 'group': 'bo',
         'feedback_index': 20},
    ])
    gw.readback_timeout = 0.15
    client = _attach(gw, _FakeClient(send_ok=True))
    client.data = {
        (DNP3ObjectGroup.BI, 20): {'value': 1, 'time': time.time() - 60},
    }

    result = gw.write_register(DEVICE, 5, 1)

    assert result['status'] == ControlStatus.FAILED


def test_communication_error_during_readback_is_failed():
    gw = _gateway([
        {'name': 'breaker_cmd', 'index': 5, 'group': 'bo',
         'feedback_index': 20},
    ])
    _attach(gw, _FakeClient(send_ok=True, raise_on_read=True))

    result = gw.write_register(DEVICE, 5, 1)

    assert result['status'] == ControlStatus.FAILED
    assert result['success'] is False


# ---------------------------------------------------------------------------
# require_feedback：配置缺失必须在下发前拒绝
# ---------------------------------------------------------------------------

def test_require_feedback_without_index_rejected_before_send():
    gw = _gateway([
        {'name': 'breaker_cmd', 'index': 5, 'group': 'bo',
         'require_feedback': True},
    ])
    client = _attach(gw, _FakeClient(send_ok=True))

    result = gw.write_register(DEVICE, 5, 1)

    assert result['status'] == ControlStatus.FAILED
    assert result['success'] is False
    assert client.sent == [], '配置错误必须在下发前拒绝'


@pytest.mark.parametrize('bad_value', ['20', 1.5, True, -1])
def test_invalid_feedback_index_rejected_before_send(bad_value):
    gw = _gateway([
        {'name': 'breaker_cmd', 'index': 5, 'group': 'bo',
         'feedback_index': bad_value},
    ])
    client = _attach(gw, _FakeClient(send_ok=True))

    result = gw.write_register(DEVICE, 5, 1)

    assert result['status'] == ControlStatus.FAILED
    assert client.sent == []


# ---------------------------------------------------------------------------
# 其它失败路径 / 三分类
# ---------------------------------------------------------------------------

def test_send_rejected_by_outstation_is_failed():
    gw = _gateway([{'name': 'breaker_cmd', 'index': 5, 'group': 'bo'}])
    _attach(gw, _FakeClient(send_ok=False))

    result = gw.write_register(DEVICE, 5, 1)

    assert result['status'] == ControlStatus.FAILED
    assert result['success'] is False


def test_disconnected_is_failed():
    gw = _gateway([{'name': 'breaker_cmd', 'index': 5, 'group': 'bo'}])
    client = _attach(gw, _FakeClient())
    client.connected = False

    result = gw.write_register(DEVICE, 5, 1)

    assert result['status'] == ControlStatus.FAILED


def test_non_control_point_is_rejected():
    gw = _gateway([{'name': 'voltage', 'index': 0, 'group': 'ai'}])
    client = _attach(gw, _FakeClient())

    result = gw.write_register(DEVICE, 0, 1)

    assert result['status'] == ControlStatus.FAILED
    assert client.sent == []


def test_three_statuses_are_distinct():
    assert len({ControlStatus.VERIFIED, ControlStatus.SENT_UNVERIFIED,
                ControlStatus.FAILED}) == 3
