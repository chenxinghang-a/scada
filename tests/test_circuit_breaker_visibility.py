# -*- coding: utf-8 -*-
"""熔断状态可见性：韧性看板不许显示"一切正常"而设备早已熔断（round 251）。

背景
----
采集链的熔断保护是 `采集层/data_collector.py` 的**内联实现**
（`_circuit_state` / `_failure_counts` / `_handle_circuit_breaker()`）：
按设备、连续失败 5 次打开、冷却 30 秒后半开试探。

而 `core/circuit_breaker.py` 那套通用熔断器**生产环境从未创建过实例** ——
`/api/resilience/circuit-breaker` 和健康详情的 `circuit_breakers` 段读的
正是它，于是**永远返回空**。

净效果：运维在韧性看板上看到「0 个熔断器、一切正常」，
而某台设备可能早已熔断打开、正在按 30 秒周期空转 ——
**真实状态没有任何接口能看到**。

本轮修法：
1. `DataCollector.get_circuit_breaker_states()` 暴露内联熔断器的真实状态
   （开/闭、连续失败数、已熔断多久、冷却剩余、当前阶段）；
2. `get_stats()` 带上汇总（`/api/system/status` 与健康检查因此可见）；
3. 韧性 API 与健康详情改以采集器为真源，通用管理器数据作为 `generic`
   附带返回（空，但"有两套实现"这件事在接口层可见）；
4. `core/circuit_breaker.py` 的 docstring 如实标注未被使用 + 两种抽象的
   区别，避免下一个人以为设备靠它保护。

本文件把这些钉死。
"""

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _make_collector():
    """构造一个只带熔断状态字段的最小采集器（不起线程、不连数据库）。"""
    import threading
    from 采集层.data_collector import DataCollector
    dc = object.__new__(DataCollector)
    dc._circuit_state = {}
    dc._failure_counts = {}
    dc._stats_lock = threading.Lock()
    return dc


# ===========================================================================
# 1. 真实状态可读
# ===========================================================================

class TestStatesReadable:
    def test_healthy_collector_reports_empty(self):
        dc = _make_collector()
        out = dc.get_circuit_breaker_states()
        assert out['devices'] == {}
        assert out['summary']['open'] == 0
        assert out['summary']['threshold'] == 5

    def test_open_breaker_is_visible_with_cooldown(self):
        dc = _make_collector()
        dc._circuit_state['dev1'] = {'opened_at': time.monotonic() - 10, 'opened_at_wall': time.time() - 10}
        dc._failure_counts['dev1'] = 7

        out = dc.get_circuit_breaker_states()
        d = out['devices']['dev1']
        assert d['state'] == 'open'
        assert d['consecutive_failures'] == 7
        assert d['phase'] == 'cooldown'
        assert 19 < d['cooldown_remaining_sec'] <= 20, d
        assert out['summary']['open'] == 1

    def test_half_open_phase_after_cooldown(self):
        """冷却结束后进入半开试探阶段 —— 运维能看到"正在试探恢复"。"""
        dc = _make_collector()
        dc._circuit_state['dev1'] = {'opened_at': time.monotonic() - 31, 'opened_at_wall': time.time() - 31}
        out = dc.get_circuit_breaker_states()
        assert out['devices']['dev1']['phase'] == 'half_open_probe'
        assert out['devices']['dev1']['cooldown_remaining_sec'] == 0

    def test_failures_without_open_are_visible_too(self):
        """还没到阈值也有连续失败 —— 那是"正在恶化"的早期信号。"""
        dc = _make_collector()
        dc._failure_counts['dev2'] = 3
        out = dc.get_circuit_breaker_states()
        assert out['devices']['dev2']['state'] == 'closed'
        assert out['devices']['dev2']['consecutive_failures'] == 3
        assert out['summary']['open'] == 0

    def test_stats_includes_summary(self):
        """`get_stats()` 必须带汇总 —— /api/system/status 与健康检查读它。"""
        dc = _make_collector()
        dc._circuit_state = {'dev1': {'opened_at': time.monotonic(), 'opened_at_wall': time.time()}}
        dc._failure_counts = {'dev1': 5}
        dc.running = True
        dc.stats = {}
        dc.data_queue = MagicMock()
        dc.data_queue.qsize.return_value = 0
        dc.data_queue.persist_failures = 0

        stats = dc.get_stats()
        assert stats['circuit_breakers']['open'] == 1
        assert 'dev1' in stats['circuit_breaker_devices']


# ===========================================================================
# 2. 韧性 API 报真实状态
# ===========================================================================

class TestResilienceApi:
    def test_reports_collector_state_not_empty_manager(self, app, client,
                                                       auth_headers):
        """修复前这里永远回 {'breakers': {}, 'total': 0}。"""
        dc = _make_collector()
        dc._circuit_state = {'dev1': {'opened_at': time.monotonic() - 5, 'opened_at_wall': time.time() - 5}}
        dc._failure_counts = {'dev1': 6}
        app.data_collector = dc

        r = client.get('/api/resilience/circuit-breaker', headers=auth_headers)
        assert r.status_code == 200, r.get_json()
        body = r.get_json()['data']
        assert body['source'] == 'data_collector'
        assert body['total'] == 1
        assert body['breakers']['dev1']['state'] == 'open'
        assert body['breakers']['dev1']['consecutive_failures'] == 6

    def test_healthy_system_reports_zero_not_silently_empty(self, app, client,
                                                            auth_headers):
        """全健康时也要能区分"真的健康"与"接口坏了"—— source 字段就是为此。"""
        app.data_collector = _make_collector()
        r = client.get('/api/resilience/circuit-breaker', headers=auth_headers)
        body = r.get_json()['data']
        assert body['source'] == 'data_collector'
        assert body['total'] == 0
        assert body['breakers'] == {}

    def test_generic_manager_data_still_attached(self, app, client, auth_headers):
        """通用管理器的数据要附带返回（当前为空）—— 两套实现不能在接口层隐形。"""
        app.data_collector = _make_collector()
        r = client.get('/api/resilience/circuit-breaker', headers=auth_headers)
        body = r.get_json()['data']
        assert 'generic' in body
        assert 'note' in body and '内联' in body['note']


# ===========================================================================
# 3. 健康详情同样可见
# ===========================================================================

class TestHealthDetail:
    def test_health_detail_shows_real_breakers(self, app, client, auth_headers):
        dc = _make_collector()
        dc._circuit_state = {'dev9': {'opened_at': time.monotonic() - 40, 'opened_at_wall': time.time() - 40}}
        dc._failure_counts = {'dev9': 9}
        app.data_collector = dc

        r = client.get('/api/health/status/detail', headers=auth_headers)
        assert r.status_code == 200, r.get_json()
        cb = r.get_json()['data']['circuit_breakers']
        assert cb['source'] == 'data_collector'
        assert cb['summary']['open'] == 1
        assert cb['devices']['dev9']['phase'] == 'half_open_probe'

    def test_probe_shape_mismatch_degrades_not_500(self, app, client, auth_headers):
        """探测返回值不符预期时必须降级带 reason，**不能 500**。

        第一版直接把 `real['summary']` 嵌进响应，撞上 MagicMock 采集器
        （测试夹具就是这样）→ jsonify 炸 → **连同 database/websocket 等
        其它组件的状态一起 500**。这个接口的契约（round 168e）恰恰是
        「单个组件探不到不该让整个诊断接口 500」。
        """
        app.data_collector = MagicMock()      # 返回值不是预期形状

        r = client.get('/api/health/status/detail', headers=auth_headers)
        assert r.status_code == 200, (
            f'熔断探测形状不符却让整个诊断接口 {r.status_code} 了')
        data = r.get_json()['data']
        cb = data['circuit_breakers']
        assert cb['status'] == 'unknown'
        assert 'reason' in cb and cb['reason'], '降级了但没带原因'
        # 其它组件不能被连累
        assert data['websocket']['status'] in ('ok', 'unknown')

    def test_resilience_api_shape_mismatch_degrades_not_500(self, app, client,
                                                            auth_headers):
        """韧性接口自己更不能 500。"""
        app.data_collector = MagicMock()
        r = client.get('/api/resilience/circuit-breaker', headers=auth_headers)
        assert r.status_code == 200, r.get_json()
        body = r.get_json()['data']
        assert body['source'] == 'generic_manager'
        assert 'degraded' in body and body['degraded']['reason']


# ===========================================================================
# 4. 静态守卫：模块标注 + 接口不许再读空管理器当真源
# ===========================================================================

def test_circuit_breaker_module_documents_being_unused():
    """`core/circuit_breaker.py` 必须写明"生产未使用 + 真源在哪"。

    否则下一个人看到韧性 API 有个熔断器模块，就以为设备靠它保护。
    """
    source = (PROJECT_ROOT / 'core' / 'circuit_breaker.py').read_text(encoding='utf-8')
    assert '没有被使用' in source or '未使用' in source, (
        'core/circuit_breaker.py 没标注"生产未使用"')
    assert 'data_collector' in source, (
        'core/circuit_breaker.py 没指出真正生效的熔断实现在哪')


def test_resilience_api_does_not_use_generic_as_sole_source():
    """韧性 API 不许把通用管理器的空数据当唯一真源。"""
    source = (PROJECT_ROOT / '展示层' / 'api' / 'api_resilience.py').read_text(encoding='utf-8')
    assert 'get_circuit_breaker_states' in source, (
        '韧性 API 没有读采集器的真实熔断状态 —— 看板又会变回"永远健康"')
    # generic 只能作为附带字段
    assert "'generic': generic" in source, (
        '通用管理器的数据应作为 generic 附带字段返回，而不是主真源')
