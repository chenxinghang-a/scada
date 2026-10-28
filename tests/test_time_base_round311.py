# -*- coding: utf-8 -*-
"""round 311：时间基准错的第二族 —— 「窗口/节流/过期用墙钟」，外加一个无界表。

## 为什么单独一批

上一轮（310）扫的是「按 key 存储但无清理点」。这一轮换维度扫
**`time.time()` 参与判定**：10 条规则里唯一有产出的是这条（4 处真错）。
其余规则（可变默认参数 / `is` 比较字面量 / `assert` 用于生产）在本仓库**全空**。

## 判据（写在这里，下次直接套）

**表达「时长」的量必须用 `time.monotonic()`；表达「时刻」的量才用 `time.time()`。**

| 语义 | 正确基准 | 本仓库的实例 |
|---|---|---|
| "最近 60 秒内几次" | monotonic | 限流窗口（本轮修） |
| "每 10 秒最多推一次" | monotonic | WebSocket 状态推送（本轮修） |
| "旁路 30 分钟后自动恢复" | monotonic | 联锁旁路（本轮修） |
| "已超时多少秒" | monotonic | 升级通知里的 elapsed（本轮修） |
| "旁路到 14:30 结束" | 墙钟 | 对外展示字段（保留） |
| "每天 8 点执行" | 墙钟 | 定时任务 cron 语义（保留） |
| 防重放签名的客户端时间戳 | 墙钟 | `request_signature`（协议要求，保留） |

⚠️ 判定用 monotonic 之后，**初值必须 `-inf`**，不能写 `0`：
`time.monotonic()` 在 Windows 上是开机以来的秒数，写 `0` 等于断言
"在开机第 0 秒执行过"，于是任何开机不足阈值的机器上**首次执行被吞**。
本轮的 `websocket._last_status_time` 就是原样踩到了这个坑（原来写 `0`）。
"""

import time
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# 1. 限流窗口：校时不许让用户被误限流 / 不许绕过
# ---------------------------------------------------------------------------

@pytest.fixture
def limiter_env(monkeypatch):
    """隔离 core.user_rate_limiter 的全局状态，并暴露它。"""
    import core.user_rate_limiter as mod

    monkeypatch.setattr(mod, '_user_limits', {}, raising=True)
    return mod


def test_rate_limit_window_uses_monotonic(app, limiter_env, monkeypatch):
    """窗口判定必须读单调时钟 —— 墙钟被拨动时窗口不能变形。

    做法：把 `time.time()` 拨后一小时，模拟 NTP 校时。若判定用墙钟，
    历史时间戳会"永不过期"，用户被误限流。
    """
    mod = limiter_env
    fake = {'mono': 1000.0, 'wall': 1_700_000_000.0}

    class _FakeTime:
        """只替换**本模块**对 time 的引用。

        ⚠️ 不能 `monkeypatch.setattr(mod.time, 'time', ...)` —— `mod.time`
        就是全局 `time` 模块本身，那样会把整个进程的 `time.time()` 都改成常量，
        pytest 自己的超时/日志计时跟着失真（实测会把变异验证跑挂死）。
        """
        @staticmethod
        def monotonic():
            return fake['mono']

        @staticmethod
        def time():
            return fake['wall']

    monkeypatch.setattr(mod, 'time', _FakeTime)

    calls = []
    fn = mod.user_rate_limit(calls=2, period=60)(lambda: calls.append(1) or 'ok')

    with app.test_request_context('/api/x'):
        from flask import g
        g.current_user = {'username': 'u1', 'role': 'engineer'}

        # 窗口内 2 次 → 都放行
        assert fn() == 'ok'
        assert fn() == 'ok'
        # 第 3 次触发限流
        resp = fn()
        assert getattr(resp, 'status_code', 200) == 429

        # 时钟往后拨 1 小时（NTP 校时），单调时钟**不动**
        fake['wall'] += 3600
        resp = fn()
        assert getattr(resp, 'status_code', 200) == 429, (
            '墙钟被拨后仍应是限流状态（窗口还没过去）—— '
            '若这里放行，说明判定读的是墙钟')

        # 单调时钟走过窗口 → 恢复
        fake['mono'] += 61
        assert fn() == 'ok', '窗口已过却仍被限流 —— 单调时钟没被用上'


def test_rate_limit_reset_header_is_wall_clock(app, limiter_env, monkeypatch):
    """`X-RateLimit-Reset` 是给人看的**绝对时刻**，必须换算成墙钟。"""
    mod = limiter_env
    fake = {'mono': 1000.0, 'wall': 1_700_000_000.0}

    class _FakeTime:
        @staticmethod
        def monotonic():
            return fake['mono']

        @staticmethod
        def time():
            return fake['wall']

    monkeypatch.setattr(mod, 'time', _FakeTime)

    fn = mod.user_rate_limit(calls=1, period=60)(lambda: 'ok')

    with app.test_request_context('/api/y'):
        from flask import g
        g.current_user = {'username': 'u2', 'role': 'engineer'}

        fn()
        resp = fn()
        reset = int(resp.headers['X-RateLimit-Reset'])
        assert reset >= int(fake['wall']), (
            'Reset 头用的是单调时钟（一个很小的数）—— 客户端倒计时会错乱')


# ---------------------------------------------------------------------------
# 2. WebSocket 状态推送节流
# ---------------------------------------------------------------------------

def test_status_push_throttle_initial_value_is_neg_inf():
    """节流初值必须 `-inf`：写 `0` 会让开机不足 10 秒的机器**吞掉首帧推送**。"""
    import inspect

    from 展示层 import websocket as ws

    src = inspect.getsource(ws)
    assert "_last_status_time = float('-inf')" in src, (
        '节流初值又写回 0 了 —— 开机不足 10 秒时首帧状态推送会被吞掉')
    assert 'now = time.monotonic()' in src, '节流判定又用回墙钟了'


# ---------------------------------------------------------------------------
# 3. 联锁旁路过期：双基准
# ---------------------------------------------------------------------------

def _dc():
    from 智能层.device_control import DeviceControlSafety
    dc = DeviceControlSafety(MagicMock(), config={})
    dc._load_preset_interlocks = lambda: None
    return dc


def test_bypass_request_has_monotonic_expiry():
    """新建的旁路请求必须同时带墙钟与单调时钟两个到期时间。"""
    dc = _dc()
    dc.add_interlock({
        'id': 'il1', 'name': 'il1',
        'condition': {'type': 'threshold', 'device_id': 'd1',
                      'register': 'temp', 'operator': '>', 'value': 90},
        'action': {'type': 'write_register', 'device_id': 'd1',
                   'register': 'status', 'value': 0},
    })
    rid = dc.request_bypass('il1', 'op1', 'maintenance', 30)

    req = dc._bypass_requests[rid]
    assert req.expires_at > 0, '墙钟到期时间没设'
    assert req.expires_at_mono > 0, '单调时钟到期时间没设 —— 判定会回退墙钟'


def test_bypass_expiry_ignores_wall_clock_jump(monkeypatch):
    """墙钟被拨后，旁路**不该**因此提前过期或延长。"""
    from 智能层.device_control import BypassRequest

    # 构造一个"墙钟已过期、单调时钟还没到"的请求
    req = BypassRequest(
        request_id='r1', interlock_id='il1', requested_by='op',
        requested_at=time.time(), expires_at=time.time() - 3600,   # 墙钟看起来已过期
        reason='x', expires_at_mono=time.monotonic() + 600,        # 单调时钟还有 10 分钟
    )
    assert req.is_expired is False, (
        '判定读了墙钟 —— NTP 往前拨会让维护中的旁路突然失效')


def test_bypass_expiry_falls_back_for_legacy_records():
    """没有单调时钟副本的旧记录回退墙钟（不能因为新字段缺失就永不过期）。"""
    from 智能层.device_control import BypassRequest

    req = BypassRequest(
        request_id='r2', interlock_id='il1', requested_by='op',
        requested_at=time.time(), expires_at=time.time() - 10,
        reason='x',      # expires_at_mono 默认 0.0
    )
    assert req.is_expired is True


def test_bypass_requests_table_is_bounded(caplog):
    """旁路请求表必须有上限，且**只淘汰已结束的**。

    `request_id` 是 uuid，每次申请都新增一条，而审批只改 status 不删除 ——
    原来的表是无界增长的。
    """
    import logging

    dc = _dc()
    dc.add_interlock({
        'id': 'il1', 'name': 'il1',
        'condition': {'type': 'threshold', 'device_id': 'd1',
                      'register': 'temp', 'operator': '>', 'value': 90},
        'action': {'type': 'write_register', 'device_id': 'd1',
                   'register': 'status', 'value': 0},
    })

    # 造一批"已结束"的请求，塞满到上限以上
    cap = dc.MAX_BYPASS_REQUESTS_IN_MEMORY
    for i in range(cap + 20):
        rid = dc.request_bypass('il1', f'op{i}', 'maintenance', 30)
        dc._bypass_requests[rid].status = 'rejected'      # 直接标结束，跳过审批

    with caplog.at_level(logging.INFO):
        # 再申请一条触发裁剪
        dc.request_bypass('il1', 'op_last', 'maintenance', 30)

    assert len(dc._bypass_requests) <= cap + 1, (
        f'旁路请求表没有被裁剪：{len(dc._bypass_requests)} 条')


def test_trim_never_drops_pending(caplog):
    """全是待审批时不许淘汰 —— 那会丢掉正在走流程的安全申请。"""
    import logging

    dc = _dc()
    dc.add_interlock({
        'id': 'il1', 'name': 'il1',
        'condition': {'type': 'threshold', 'device_id': 'd1',
                      'register': 'temp', 'operator': '>', 'value': 90},
        'action': {'type': 'write_register', 'device_id': 'd1',
                   'register': 'status', 'value': 0},
    })
    cap = dc.MAX_BYPASS_REQUESTS_IN_MEMORY
    dc.MAX_BYPASS_REQUESTS_IN_MEMORY = 3

    with caplog.at_level(logging.ERROR):
        for i in range(6):
            dc.request_bypass('il1', f'op{i}', 'maintenance', 30)

    assert len(dc._bypass_requests) == 6, '待审批请求被淘汰了'
    assert any('无法淘汰' in r.getMessage() for r in caplog.records), (
        '待审批堆积到上限却没有任何告警 —— 审批流程卡住没人知道')


# ---------------------------------------------------------------------------
# 4. 升级通知里的 elapsed 与判定同口径
# ---------------------------------------------------------------------------

def test_escalation_elapsed_uses_monotonic():
    """升级回调里的 `elapsed_seconds` 必须与判定同基准（monotonic）。

    否则校时后通知里会出现负数（"已超时 -3598 秒"），
    而这句会直接进打给三级主管的电话话术。
    """
    import inspect

    from 报警层 import alarm_escalation as mod

    src = inspect.getsource(mod)
    assert "'elapsed_seconds': time.monotonic() - state.first_trigger_mono" in src, (
        'elapsed 又用回墙钟了 —— 与 check_escalations 的判定口径不一致')
    assert "'elapsed_seconds': time.time() - state.first_trigger_time" not in src
