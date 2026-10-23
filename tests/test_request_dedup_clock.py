# -*- coding: utf-8 -*-
"""请求去重的过期时刻必须按**单调时钟**算（round 258）。

缺陷形态
--------
`core/request_dedup.py` 给每个写请求记一个"过期时刻"（`now + 2s`），
2 秒内的同指纹请求回 409。原实现用 `time.time()`。墙钟被回拨一次
（NTP 校时、现场机无电池开机后校时、手工改时间），所有已登记的过期时刻
都变成"远在未来" →

* 这些请求**永久**被判成重复提交，每个写接口一直回 409；
* 唯一解法是重启进程（内存表没有别的失效路径）；
* 表现是「界面点什么都说"请勿重复操作"」，而日志里只有一行 debug。

判据同 alarm/circuit_breaker：**时长与过期时刻用 monotonic，展示用墙钟**。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

DEDUP_SRC = Path(__file__).resolve().parents[1] / "core" / "request_dedup.py"

#: 真实墙钟函数（被 monkeypatch 之前的原版）—— 用来模拟「回拨 8 小时」
_REAL_TIME = time.time


def _make_app():
    from flask import Flask, jsonify
    from core.request_dedup import init_request_dedup

    app = Flask(__name__)
    app.config['TESTING'] = True
    init_request_dedup(app)

    @app.route('/api/write-probe', methods=['POST'])
    def _probe():
        return jsonify({'ok': True})

    return app


def test_duplicate_is_rejected_within_window():
    """基线：窗口内的同指纹请求必须 409（这条不红才说明后面的用例有意义）。"""
    client = _make_app().test_client()
    assert client.post('/api/write-probe', json={'a': 1}).status_code == 200
    assert client.post('/api/write-probe', json={'a': 1}).status_code == 409


def test_retry_allowed_after_window_despite_backward_clock(monkeypatch):
    """墙钟回拨 8 小时后，过了窗口的合法重试仍必须放行。"""
    from core.request_dedup import DEDUP_WINDOW

    client = _make_app().test_client()
    assert client.post('/api/write-probe', json={'a': 1}).status_code == 200
    assert client.post('/api/write-probe', json={'a': 1}).status_code == 409

    # 墙钟被回拨 8 小时（单调时钟不受影响）
    monkeypatch.setattr(time, 'time', lambda: _REAL_TIME() - 8 * 3600)

    time.sleep(DEDUP_WINDOW + 0.3)      # 真实时间已过窗口
    assert client.post('/api/write-probe', json={'a': 1}).status_code == 200, (
        '窗口早已过期却仍回 409 —— 说明过期时刻用的是墙钟，'
        '一次校时之后所有写接口会永久返回「请勿重复操作」')


def test_dedup_table_uses_monotonic_values():
    """指纹表里的过期时刻必须是单调时钟（与墙钟差 > 1 天即用错）。"""
    app = _make_app()
    client = app.test_client()
    client.post('/api/write-probe', json={'b': 2})

    state = app.extensions['request_dedup']
    expiry = max(state['fingerprints'].values())
    assert abs(time.monotonic() - expiry) < 60, (
        f'过期时刻 {expiry!r} 看起来是墙钟（与 monotonic 差 '
        f'{abs(time.monotonic() - expiry):.0f}s）')


def test_window_math_does_not_use_wall_clock():
    """静态守卫：去重中间件里不许再出现 `time.time()`。"""
    code = re.sub(r"#.*$", "", DEDUP_SRC.read_text(encoding='utf-8'), flags=re.M)
    assert 'time.time()' not in code, (
        'request_dedup 里又出现了 time.time() —— 墙钟回拨会让所有写接口永久 409')
