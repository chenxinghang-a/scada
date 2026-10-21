"""
运行时配置接线守卫（round 241）

背景
----
`core/ops_tools.RuntimeConfigManager` 提供了一套「运行时配置热更新」：
`GET/PUT /api/ops/config` 可以读写 14 个键，`set()` 返回 True、
接口回 `changed_count: N`、审计日志记一笔 —— 看起来完全正常。

实测：**除了本轮接上的那一个键，其余全部没有任何消费方**。
`register_callback()` 全仓库零调用，也没有任何模块调
`runtime_config_manager.get(...)`。运维把 `device_poll_interval_sec`
从 5 改成 1，接口说「已变更」，采集器照旧按 yaml 里的每设备间隔跑。

更糟的三点：
1. `set()` 不校验键是否存在 —— `PUT /api/ops/config` 能往配置表里塞任意
   垃圾键，`GET` 还会把它当合法配置回显；
2. 配置只存内存，**重启即回默认值**，接口从不提这件事；
3. `reset(key)` 是无效实现（先刷默认值再 set 同值 → 判等返回 False）。

本轮做法：**只接一个能立即验证的键**（`websocket_push_interval_ms`），
其余 13 个如实标成 `not_wired`，并在 GET/PUT 的响应里报出来。
不假装全接 —— 那些键的真实来源是 `config.py` / `配置/*.yaml`，
硬接成第三套来源只会更乱。
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.ops_tools import (  # noqa: E402
    DEFAULT_RUNTIME_CONFIG,
    WIRED_KEYS,
    RuntimeConfigManager,
)
import 展示层.websocket as ws  # noqa: E402


@pytest.fixture
def mgr():
    """每个用例一个独立的 RuntimeConfigManager（不污染全局单例）"""
    return RuntimeConfigManager()


@pytest.fixture
def clean_global(monkeypatch):
    """API 测试要改全局单例，用完恢复"""
    from core.ops_tools import runtime_config_manager
    saved = dict(runtime_config_manager._config)
    saved_hist = list(runtime_config_manager._history)
    yield runtime_config_manager
    with runtime_config_manager._lock:
        runtime_config_manager._config = saved
        runtime_config_manager._history = saved_hist


# ============================================================
# 1. 接线表本身
# ============================================================

class TestWiredKeysRegistry:
    def test_every_wired_key_exists_in_defaults(self):
        for key in WIRED_KEYS:
            assert key in DEFAULT_RUNTIME_CONFIG, f"{key} 不在默认配置里"

    @pytest.mark.parametrize('key,target', sorted(WIRED_KEYS.items()))
    def test_consumer_file_and_symbol_exist(self, key, target):
        """登记的消费方必须真的有这个文件 + 这个函数 + 这个键名。

        这条防「表写了但代码没接」：谁把接线删了没改表，或者改了键名
        没改表，这里立刻红。
        """
        rel_path, func_name = target.split(':')
        full = os.path.join(PROJECT_ROOT, rel_path.replace('/', os.sep))
        assert os.path.isfile(full), f"消费方文件不存在: {rel_path}"

        with open(full, encoding='utf-8') as fh:
            source = fh.read()

        assert f'def {func_name}' in source, f"{rel_path} 里找不到函数 {func_name}"
        assert key in source, f"{rel_path} 里找不到配置键 {key!r}"

    def test_default_push_interval_matches_old_hardcoded_value(self):
        """默认值必须等于接线前硬编码的 2 秒，否则一上线就悄悄改了推送频率。"""
        assert DEFAULT_RUNTIME_CONFIG['websocket_push_interval_ms'] == 2000


# ============================================================
# 2. 诚实性：wired / not_wired 必须分开报
# ============================================================

class TestWiringStatus:
    def test_status_splits_wired_and_not_wired(self, mgr):
        st = mgr.get_wiring_status()

        assert st['wired'] == ['websocket_push_interval_ms']
        assert 'device_poll_interval_sec' in st['not_wired']
        assert 'max_concurrent_requests' in st['not_wired']
        assert set(st['wired']) | set(st['not_wired']) == set(DEFAULT_RUNTIME_CONFIG)
        assert not (set(st['wired']) & set(st['not_wired']))

    def test_status_reports_consumer_and_persistence(self, mgr):
        st = mgr.get_wiring_status()
        assert st['consumers']['websocket_push_interval_ms'].endswith('_base_push_interval')
        assert st['persistent'] is False, '只存内存，重启即丢 —— 必须如实说'

    def test_not_wired_count_is_reported_honestly(self, mgr):
        st = mgr.get_wiring_status()
        assert len(st['not_wired']) == len(DEFAULT_RUNTIME_CONFIG) - len(WIRED_KEYS)
        assert len(st['not_wired']) > 0, '如果全接完了，这条守卫该改'


# ============================================================
# 3. set / update_batch / reset 的行为
# ============================================================

class TestSetBehaviour:
    def test_unknown_key_is_rejected(self, mgr):
        """原实现会静默新增键 → GET 把它当合法配置回显。"""
        with pytest.raises(KeyError):
            mgr.set('totally_made_up_key', 1)
        assert 'totally_made_up_key' not in mgr.get_all()

    def test_known_key_is_accepted(self, mgr):
        assert mgr.set('websocket_push_interval_ms', 3000) is True
        assert mgr.get('websocket_push_interval_ms') == 3000

    def test_same_value_returns_false(self, mgr):
        assert mgr.set('websocket_push_interval_ms', 2000) is False

    def test_history_records_wired_flag(self, mgr):
        mgr.set('websocket_push_interval_ms', 3000)
        mgr.set('device_poll_interval_sec', 1)

        hist = mgr.get_history(10)
        by_key = {h['key']: h for h in hist}
        assert by_key['websocket_push_interval_ms']['wired'] is True
        assert by_key['device_poll_interval_sec']['wired'] is False

    def test_update_batch_reports_changed_and_rejected(self, mgr):
        result = mgr.update_batch({
            'websocket_push_interval_ms': 3000,
            'device_poll_interval_sec': 1,
            'nope_not_a_key': 1,
        })
        assert set(result['changed']) == {'websocket_push_interval_ms', 'device_poll_interval_sec'}
        assert result['rejected'] == ['nope_not_a_key']

    def test_reset_actually_resets(self, mgr):
        """原实现先刷默认值再 set 同值 → 判等返回 False，值虽然回去了但流程是错的。"""
        mgr.set('websocket_push_interval_ms', 5000)
        assert mgr.get('websocket_push_interval_ms') == 5000

        mgr.reset('websocket_push_interval_ms')
        assert mgr.get('websocket_push_interval_ms') == 2000

    def test_reset_is_recorded_in_history(self, mgr):
        """reset 必须走 `set()` 路径留痕 —— 原实现直接 `_load_defaults()`，
        值悄悄变了、history 一条不记、回调也不触发。运维事后查
        「谁把推送间隔改回去了」什么都查不到。"""
        mgr.set('websocket_push_interval_ms', 5000)
        before = len(mgr.get_history(100))

        mgr.reset('websocket_push_interval_ms')

        after = mgr.get_history(100)
        assert len(after) > before, 'reset 没有留下任何变更记录'
        assert after[-1]['key'] == 'websocket_push_interval_ms'
        assert after[-1]['new'] == 2000
        assert after[-1]['operator'] == 'system'

    def test_reset_all_records_history(self, mgr):
        mgr.set('websocket_push_interval_ms', 5000)
        before = len(mgr.get_history(100))

        mgr.reset()

        assert len(mgr.get_history(100)) > before, 'reset() 全量重置也必须留痕'

    def test_reset_all_restores_every_default(self, mgr):
        mgr.set('websocket_push_interval_ms', 5000)
        mgr.set('device_poll_interval_sec', 1)

        mgr.reset()

        assert mgr.get_all() == dict(DEFAULT_RUNTIME_CONFIG)

    def test_reset_unknown_key_raises(self, mgr):
        with pytest.raises(KeyError):
            mgr.reset('nope')


# ============================================================
# 4. 接线点真的读配置
# ============================================================

class TestPushIntervalWiring:
    def test_reads_configured_value(self, clean_global):
        clean_global.set('websocket_push_interval_ms', 3000)
        assert ws._base_push_interval() == pytest.approx(3.0)

    def test_default_is_two_seconds(self, clean_global):
        clean_global.reset()
        assert ws._base_push_interval() == pytest.approx(2.0)

    def test_clamped_to_sane_range(self, clean_global):
        clean_global.set('websocket_push_interval_ms', 1)          # 1ms
        assert ws._base_push_interval() == pytest.approx(0.5), '低于 0.5 秒等于忙等'

        clean_global.set('websocket_push_interval_ms', 600000)     # 10 分钟
        assert ws._base_push_interval() == pytest.approx(10.0), '高于 10 秒实时推送名存实亡'

    def test_invalid_values_fall_back(self, clean_global):
        for bad in (0, -5, float('nan'), float('inf'), 'abc', None):
            clean_global._config['websocket_push_interval_ms'] = bad
            assert ws._base_push_interval() == pytest.approx(2.0), f"{bad!r} 没兜底"

    def test_push_loop_uses_the_helper(self):
        """推送循环里必须用 `_base_push_interval()`，不能留硬编码的 2.0。

        静态守卫：把 `BASE_INTERVAL = _base_push_interval()` 改回
        `BASE_INTERVAL = 2.0` 时这里变红 —— 否则接线只是写了个没人用的函数。
        """
        import pathlib
        import re

        source = (pathlib.Path(PROJECT_ROOT) / '展示层' / 'websocket.py').read_text(encoding='utf-8')
        assert re.search(r'^\s+BASE_INTERVAL = _base_push_interval\(\)', source, flags=re.MULTILINE), \
            '推送循环里没有用 _base_push_interval()'
        assert not re.search(r'^\s+BASE_INTERVAL = 2\.0', source, flags=re.MULTILINE), \
            '推送循环里还留着硬编码的 BASE_INTERVAL = 2.0'


# ============================================================
# 5. API 层不再谎报「已生效」
# ============================================================

class TestOpsConfigApi:
    def test_get_reports_wiring(self, app, client, auth_headers, clean_global):
        r = client.get('/api/ops/config', headers=auth_headers)
        assert r.status_code == 200
        body = r.get_json()['data']
        assert 'config' in body and 'wiring' in body
        assert 'websocket_push_interval_ms' in body['wiring']['wired']
        assert 'device_poll_interval_sec' in body['wiring']['not_wired']

    def test_put_reports_not_wired_keys(self, app, client, auth_headers, clean_global):
        r = client.put('/api/ops/config', headers=auth_headers,
                       json={'config': {'device_poll_interval_sec': 1}})
        assert r.status_code == 200
        body = r.get_json()['data']
        assert body['changed'] == ['device_poll_interval_sec']
        assert body['not_wired'] == ['device_poll_interval_sec']
        assert '未接线' in (body.get('message') or ''), \
            '改了不生效的键必须明确告诉调用方，不能只回 changed_count'

    def test_put_reports_rejected_keys(self, app, client, auth_headers, clean_global):
        r = client.put('/api/ops/config', headers=auth_headers,
                       json={'config': {'nope': 1}})
        assert r.status_code == 200
        body = r.get_json()['data']
        assert body['rejected'] == ['nope']
        assert body['changed_count'] == 0
        assert '不存在' in (body.get('message') or '')

    def test_put_single_unknown_key_is_400(self, app, client, auth_headers, clean_global):
        """原实现会静默新增这个键，然后 GET 把它当合法配置回显。"""
        r = client.put('/api/ops/config/nope', headers=auth_headers, json={'value': 1})
        assert r.status_code == 400, f'期望 400，实际 {r.status_code}: {r.get_json()}'

    def test_put_single_known_key_reports_wired(self, app, client, auth_headers, clean_global):
        r = client.put('/api/ops/config/websocket_push_interval_ms',
                       headers=auth_headers, json={'value': 3000})
        assert r.status_code == 200
        body = r.get_json()['data']
        assert body['wired'] is True
        assert '未接线' not in (body.get('message') or '')

    def test_get_single_key_reports_wired_flag(self, app, client, auth_headers, clean_global):
        r = client.get('/api/ops/config/device_poll_interval_sec', headers=auth_headers)
        assert r.status_code == 200
        body = r.get_json()['data']
        assert body['wired'] is False
