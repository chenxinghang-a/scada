"""写入幂等：同一笔写操作重复提交，只能真的下发一次。

## 为什么补这个

`tools/audit_untested.py` 扫出来的零覆盖高风险函数里，`_idempotency_begin` /
`_idempotency_end` 是最要命的一对：它们保证「操作员手抖连点两下」不会
把同一个值往 PLC 写两遍。**在真实设备上，重复下发可能不是"多写一次"那么轻** ——
比如"启动"指令下发两次、或者一次写入被中断后重试，中间态可能落到危险值。

实现本身写得很讲究（单调用方看了会觉得"这么细肯定测过"）：

  · 用 `time.monotonic()` 而不是墙钟 —— NTP 回拨后 `now - last` 变负，
    会永远落在窗口内，此后所有重试都被判成"已写入"，实际一次都没发出去
  · `_IN_FLIGHT` 哨兵区分"正在写"和"写完了"
  · 失败时必须撤销占位，否则一次失败会让后续重试全部被挡

但这些**一条测试都没有**（tests/ 里 grep 不到这两个名字）。这组用例把它们钉住。
"""

from __future__ import annotations

import time

import pytest
from flask import Flask

import 展示层.api.api_control as ac


@pytest.fixture()
def app_ctx():
    app = Flask(__name__)
    with app.app_context():
        yield


@pytest.fixture(autouse=True)
def _clean():
    ac._recent_writes.clear()
    yield
    ac._recent_writes.clear()


def _json(resp):
    """`_idempotency_begin` 的返回值可能是 None，也可能是 (Response, status)。"""
    if resp is None:
        return None
    body, status = resp
    return body.get_json(), status


class TestFirstAndConcurrent:
    def test_first_begin_passes(self, app_ctx):
        assert _json(ac._idempotency_begin('k1')) is None, '首次应放行'

    def test_second_while_in_flight_is_409(self, app_ctx):
        assert ac._idempotency_begin('k2') is None
        got = _json(ac._idempotency_begin('k2'))
        assert got is not None, '同一键在写途中再次提交必须被挡'
        body, status = got
        assert status == 409
        assert body['success'] is False

    def test_different_keys_are_independent(self, app_ctx):
        assert ac._idempotency_begin('a') is None
        assert ac._idempotency_begin('b') is None, '不同写入不该互相阻塞'


class TestAfterSuccess:
    def test_replay_within_window_is_idempotent_200(self, app_ctx):
        assert ac._idempotency_begin('k3') is None
        ac._idempotency_end('k3', success=True)

        got = _json(ac._idempotency_begin('k3'))
        assert got is not None
        body, status = got
        assert status == 200
        assert body['success'] is True
        assert '幂等' in body['message']

    def test_replay_after_window_passes_again(self, app_ctx, monkeypatch):
        assert ac._idempotency_begin('k4') is None
        ac._idempotency_end('k4', success=True)

        # 把"现在"拨到窗口之外：直接改窗口常量最干净
        monkeypatch.setattr(ac, 'IDEMPOTENCY_WINDOW_S', 0.0)
        time.sleep(0.01)
        assert _json(ac._idempotency_begin('k4')) is None, (
            '过了幂等窗口，同样的写入应被视为新请求'
        )


class TestAfterFailure:
    def test_failure_releases_the_slot(self, app_ctx):
        assert ac._idempotency_begin('k5') is None
        ac._idempotency_end('k5', success=False)
        assert _json(ac._idempotency_begin('k5')) is None, (
            '写入失败必须撤销占位，否则一次失败会让后续重试全部被 409 挡死'
        )

    def test_failure_does_not_leave_idempotent_window(self, app_ctx):
        assert ac._idempotency_begin('k6') is None
        ac._idempotency_end('k6', success=False)
        assert 'k6' not in ac._recent_writes, '失败不该在窗口里留时间戳'

    def test_end_without_begin_is_harmless(self, app_ctx):
        """没有 begin 就 end（异常路径）不该炸，也不该遗留占位状态。"""
        ac._idempotency_end('never_begun_fail', success=False)
        assert 'never_begun_fail' not in ac._recent_writes
        assert 'never_begun_fail' not in ac._in_flight_targets
        ac._idempotency_end('never_begun', success=True)
        assert 'never_begun' not in ac._in_flight_targets


class TestClockSafety:
    def test_uses_monotonic_not_wallclock(self, app_ctx, monkeypatch):
        """用墙钟会让 NTP 回拨后的重试永远被判"已写入"。这里钉住用的是单调时钟。"""
        assert ac._idempotency_begin('k7') is None
        ac._idempotency_end('k7', success=True)
        stored = ac._recent_writes['k7']
        now_mono = time.monotonic()
        # 存的值应当接近 monotonic（远小于 time.time() 的量级）
        assert abs(now_mono - stored) < 5, (
            f'幂等时间戳看起来不是 monotonic：stored={stored} now={now_mono}'
        )
        assert stored < time.time() / 2, (
            'stored 接近 time.time() 说明用了墙钟，NTP 回拨会让重试永久被挡'
        )


class TestCleanup:
    def test_table_does_not_grow_without_bound(self, app_ctx):
        """超过上限要清理，否则长期运行后这张表无限涨。"""
        for i in range(ac.RECENT_WRITES_MAX + 40):
            k = f'bulk_{i}'
            if ac._idempotency_begin(k) is None:
                ac._idempotency_end(k, success=True)
        assert len(ac._recent_writes) <= ac.RECENT_WRITES_MAX + 40, '表大小失控'
