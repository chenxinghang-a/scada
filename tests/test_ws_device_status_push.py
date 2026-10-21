"""
WebSocket 设备状态推送接线（round 240）

背景
----
`emit_device_status()` 一直是**零调用**的死函数：前端 Dashboard 里
`socket.on('device_status', …)` 监听着一个永不到来的事件，设备的
在线/离线状态实际只能靠 2~5 秒的 HTTP 轮询兜底 —— 也就是「实时推送」
这个卖点在这条数据上根本不存在。

本轮把它接在推送线程里：每轮拿 `device_manager.get_all_status(brief=True)`，
与上一轮基线比对，**只推变化**。

几条刻意的设计取舍（都由本文件锁死）：
- 首轮只建基线不推 —— 启动瞬间 100 台设备全「从无到有」，全推一遍
  等于把一次 HTTP 轮询复制到 WebSocket 上，纯噪声；
- 状态不变不推 —— 设备状态几小时不变是常态；
- 设备被删要清基线 —— 否则同 id 设备重新加回来时判不出变化，永远不推；
- 数据源必须是 `get_all_status(brief=True)`，**不是** `get_device_status()`：
  后者会尝试自动重连并写状态缓存，在推送线程里每轮调 100 次
  等于把「查状态」变成「反复发起连接」。
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import 展示层.websocket as ws  # noqa: E402


@pytest.fixture(autouse=True)
def clean_baseline(monkeypatch):
    """每个用例一份干净基线（不串味、不影响真实模块状态）"""
    monkeypatch.setattr(ws, '_device_status_cache', {})


def _st(device_id, connected=True, status='online', stopped=False):
    return {
        'device_id': device_id,
        'connected': connected,
        'status': status,
        'stopped': stopped,
    }


# ============================================================
# 1. 差分逻辑
# ============================================================

class TestDiffDeviceStatus:
    def test_first_round_only_builds_baseline(self):
        cache = {}
        changes = ws._diff_device_status([_st('D1'), _st('D2')], cache)

        assert changes == [], '首轮不该推 —— 启动瞬间全推等于把轮询搬到 WS 上'
        assert cache == {'D1': (True, 'online', False), 'D2': (True, 'online', False)}

    def test_unchanged_state_produces_no_change(self):
        cache = {}
        ws._diff_device_status([_st('D1')], cache)
        assert ws._diff_device_status([_st('D1')], cache) == []

    def test_connection_drop_is_reported(self):
        cache = {}
        ws._diff_device_status([_st('D1')], cache)

        changes = ws._diff_device_status([_st('D1', connected=False, status='offline')], cache)

        assert changes == [('D1', {'connected': False, 'status': 'offline', 'stopped': False})]

    def test_stopped_flag_change_is_reported(self):
        """设备「运行中 → 已停止」不改变 connected，但必须推。"""
        cache = {}
        ws._diff_device_status([_st('D1')], cache)

        changes = ws._diff_device_status([_st('D1', stopped=True)], cache)

        assert len(changes) == 1
        assert changes[0][1]['stopped'] is True

    def test_only_changed_devices_are_reported(self):
        cache = {}
        ws._diff_device_status([_st('D1'), _st('D2'), _st('D3')], cache)

        changes = ws._diff_device_status(
            [_st('D1'), _st('D2', connected=False, status='offline'), _st('D3')], cache)

        assert [c[0] for c in changes] == ['D2']

    def test_removed_device_clears_baseline(self):
        """设备删除后基线必须清掉，否则同 id 重新加回来时永远判不出变化。"""
        cache = {}
        ws._diff_device_status([_st('D1'), _st('D2')], cache)
        ws._diff_device_status([_st('D1')], cache)      # D2 消失

        assert 'D2' not in cache

        # D2 以不同状态重新加入 → 因为基线已清，按首轮处理（不推）
        changes = ws._diff_device_status([_st('D1'), _st('D2', connected=False, status='offline')], cache)
        assert changes == []
        assert cache['D2'] == (False, 'offline', False)

        # 再变一次就能推了
        changes = ws._diff_device_status([_st('D1'), _st('D2')], cache)
        assert [c[0] for c in changes] == ['D2']

    def test_malformed_entries_are_skipped(self):
        """坏条目（非 dict / 缺 device_id）不能让整轮推送炸掉。"""
        cache = {}
        changes = ws._diff_device_status(
            [None, 'garbage', {'device_id': ''}, {'no_id': 1}, _st('D1')], cache)

        assert changes == []
        assert list(cache) == ['D1']

    def test_none_input_is_tolerated(self):
        cache = {'D1': (True, 'online', False)}
        assert ws._diff_device_status(None, cache) == []
        assert cache == {}, '空输入时旧基线也要清掉'


# ============================================================
# 2. 推送接线
# ============================================================

class _FakeDeviceManager:
    def __init__(self, statuses, raises=None):
        self._statuses = statuses
        self._raises = raises
        self.brief_args = []

    def get_all_status(self, brief=False):
        # 记录**实际传进来的值**而不是断言：断言抛出的异常会被
        # `_push_device_status_changes` 的 except 吞掉，测试照样绿 ——
        # 那就成了「守卫看着在守、其实什么都没守住」。
        self.brief_args.append(brief)
        if self._raises:
            raise self._raises
        return self._statuses


class _FakeCollector:
    def __init__(self, device_manager=None):
        if device_manager is not None:
            self.device_manager = device_manager


class TestPushWiring:
    def test_pushes_changes_via_emit_device_status(self, monkeypatch):
        sent = []
        monkeypatch.setattr(ws, 'emit_device_status', lambda d, s: sent.append((d, s)))

        dm = _FakeDeviceManager([_st('D1'), _st('D2')])
        collector = _FakeCollector(dm)

        assert ws._push_device_status_changes(collector) == 0, '首轮不推'
        assert sent == []

        dm._statuses = [_st('D1'), _st('D2', connected=False, status='offline')]
        assert ws._push_device_status_changes(collector) == 1
        assert sent == [('D2', {'connected': False, 'status': 'offline', 'stopped': False})]

    def test_missing_device_manager_is_noop(self, monkeypatch):
        monkeypatch.setattr(ws, 'emit_device_status', lambda d, s: pytest.fail('不该推'))
        assert ws._push_device_status_changes(_FakeCollector()) == 0

    def test_status_query_failure_is_contained(self, monkeypatch):
        """状态查询抛异常时不能把推送线程带崩（否则实时通道整条停掉）。"""
        monkeypatch.setattr(ws, 'emit_device_status', lambda d, s: pytest.fail('不该推'))

        dm = _FakeDeviceManager([], raises=RuntimeError('device manager broken'))
        assert ws._push_device_status_changes(_FakeCollector(dm)) == 0

    def test_uses_brief_status_not_full_status(self, monkeypatch):
        """必须用 brief=True：全量状态带 registers/nodes，100 台设备每轮序列化太重。"""
        monkeypatch.setattr(ws, 'emit_device_status', lambda d, s: None)
        dm = _FakeDeviceManager([])
        ws._push_device_status_changes(_FakeCollector(dm))
        assert dm.brief_args == [True], f'实际传的是 {dm.brief_args}，应为 [True]'


# ============================================================
# 3. 静态守卫
# ============================================================

def test_emit_device_status_has_a_caller():
    """`emit_device_status` 不能退回「定义了但没人调用」。

    修复前它零调用，而前端一直在监听 —— 这种「函数在、事件不来」
    的死链从代码上看不出来，只能靠这条守卫盯着。
    """
    import pathlib
    import re

    source = (pathlib.Path(PROJECT_ROOT) / '展示层' / 'websocket.py').read_text(encoding='utf-8')

    call_sites = re.findall(r'^\s+emit_device_status\(', source, flags=re.MULTILINE)
    assert call_sites, 'emit_device_status() 没有任何调用方 —— 前端会一直等一个不来的事件'


def test_push_loop_actually_calls_status_push():
    """推送循环里必须真的调用 `_push_device_status_changes(data_collector)`。

    ⚠️ 正则必须带 `^\\s+` 锚点：函数**定义**处
    `def _push_device_status_changes(data_collector) -> int:` 也包含同样的
    子串，不带行首锚点的话，把调用处改成 `(None)` 这条守卫照样通过
    —— 变异验证实测抓到过（M6 未变红）。
    """
    import pathlib
    import re

    source = (pathlib.Path(PROJECT_ROOT) / '展示层' / 'websocket.py').read_text(encoding='utf-8')

    assert re.search(r'^\s+_push_device_status_changes\(data_collector\)', source, flags=re.MULTILINE), \
        '推送线程里没有调用 _push_device_status_changes(data_collector)'
