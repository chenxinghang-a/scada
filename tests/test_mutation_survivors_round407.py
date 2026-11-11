"""针对"变异存活"补的行为测试（round 407）。

来源：`tools/mutation_spotcheck.py` 对四个模块抽查，存活变异集中在：
* REST：`poll_interval` 非法值兜底、重复 connect 不起第二条轮询线程；
* 插件：插件名校验、语法错误文件的"无入口点"判定；
* Modbus：从站地址解析的浮点/越界/广播边界。

这些点此前没有任何用例，改坏实现测试也不会红 —— 本文件补上。
"""

import threading
from pathlib import Path

import pytest

from 采集层.modbus_client import ModbusClient
from 采集层.plugin_adapter import PluginError, _declares_entry_point, plugin_path
from 采集层.rest_client import RESTDeviceClient as RestClient


# --------------------------------------------------------------- REST
@pytest.mark.parametrize('bad', [0, -1, 'abc', None, float('nan'), float('inf')])
def test_rest_poll_interval_falls_back_to_10(bad):
    client = RestClient({'id': 'r1', 'base_url': 'http://127.0.0.1', 'poll_interval': bad})
    assert client.poll_interval == 10.0


def test_rest_poll_interval_keeps_valid_value():
    client = RestClient({'id': 'r1', 'base_url': 'http://127.0.0.1', 'poll_interval': 0.5})
    assert client.poll_interval == 0.5


def test_rest_connect_twice_reuses_single_thread(monkeypatch):
    client = RestClient({'id': 'r1', 'base_url': 'http://127.0.0.1',
                         'poll_interval': 30})
    # 不真正发请求：把轮询体替换成等待
    monkeypatch.setattr(client, '_poll_loop', lambda: client._stop_event.wait(5))
    client.connect()
    first = client._thread
    client.connect()
    try:
        assert client._thread is first, '第二次 connect 起了新线程（双轮询）'
        assert sum(1 for t in threading.enumerate() if t is first) == 1
    finally:
        client.disconnect()
    assert not first.is_alive()


# ------------------------------------------------------------- 插件路径
@pytest.mark.parametrize('bad', ['', '..', 'a/b', '../evil', 'a b', 123, None, '名字'])
def test_plugin_path_rejects_illegal_names(bad):
    with pytest.raises(PluginError):
        plugin_path(bad)


def test_plugin_path_accepts_plain_name(tmp_path, monkeypatch):
    monkeypatch.setenv('SCADA_PLUGIN_DIR', str(tmp_path))
    (tmp_path / 'my_plugin.py').write_text('Adapter = object\n', encoding='utf-8')
    assert plugin_path('my_plugin') == (tmp_path / 'my_plugin.py').resolve()


def test_declares_entry_point_false_on_syntax_error(tmp_path):
    broken = tmp_path / 'broken.py'
    broken.write_text('def oops(:\n', encoding='utf-8')
    assert _declares_entry_point(broken) is False


def test_declares_entry_point_true_for_adapter_class(tmp_path):
    ok = tmp_path / 'ok.py'
    ok.write_text('class Adapter:\n    pass\n', encoding='utf-8')
    assert _declares_entry_point(ok) is True


# ------------------------------------------------------------ Modbus 从站
@pytest.mark.parametrize('value', [1.5, 1.2, 'x', -1, 248, 999, True])
def test_modbus_resolve_slave_rejects_invalid(value):
    client = ModbusClient({'id': 'm1', 'host': '127.0.0.1', 'slave_id': 1})
    assert client._resolve_slave(value) is None


@pytest.mark.parametrize('value,expected', [(1, 1), (247, 247)])
def test_modbus_resolve_slave_accepts_boundaries(value, expected):
    client = ModbusClient({'id': 'm1', 'host': '127.0.0.1', 'slave_id': 1})
    assert client._resolve_slave(value) == expected


def test_modbus_zero_is_broadcast_not_default_slave():
    """0 是广播地址：不能被 `slave_id or self.slave_id` 静默替换成 1；
    默认也不放行（广播写危险），必须显式 allow_broadcast=True。"""
    client = ModbusClient({'id': 'm1', 'host': '127.0.0.1', 'slave_id': 7})
    assert client._resolve_slave(0) is None
    assert client._resolve_slave(0, allow_broadcast=True) == 0
    assert client._resolve_slave(None) == 7


def test_rest_connect_returns_true(monkeypatch):
    """connect() 的返回值是调用方判断成败的唯一依据。"""
    client = RestClient({'id': 'r1', 'base_url': 'http://127.0.0.1',
                         'poll_interval': 30})
    monkeypatch.setattr(client, '_poll_loop', lambda: client._stop_event.wait(5))
    try:
        assert client.connect() is True
    finally:
        client.disconnect()


def test_rest_connect_restarts_when_thread_died(monkeypatch):
    """connected=True 但线程已死（崩溃/异常退出）时，connect() 必须重起。"""
    client = RestClient({'id': 'r1', 'base_url': 'http://127.0.0.1',
                         'poll_interval': 30})
    monkeypatch.setattr(client, '_poll_loop', lambda: client._stop_event.wait(0.2))
    client.connect()
    dead = client._thread
    dead.join(timeout=3)
    assert not dead.is_alive()

    # 模拟"标志仍是 True，但轮询线程已经不在了"
    client.connected = True
    monkeypatch.setattr(client, '_poll_loop', lambda: client._stop_event.wait(5))
    try:
        client.connect()
        assert client._thread is not dead, '线程已死却复用了旧对象'
        assert client._thread.is_alive()
    finally:
        client.disconnect()


def test_plugin_dir_ignores_blank_env(tmp_path, monkeypatch):
    """`SCADA_PLUGIN_DIR='   '` 必须回落到默认目录（不能解析成空白路径）。"""
    from 采集层 import plugin_adapter

    default_dir = plugin_adapter.paths.PROJECT_ROOT.joinpath(
        *plugin_adapter.DEFAULT_PLUGIN_SUBDIR)
    monkeypatch.setenv('SCADA_PLUGIN_DIR', '   ')
    assert plugin_adapter.get_plugin_dir() == default_dir

    monkeypatch.setenv('SCADA_PLUGIN_DIR', str(tmp_path))
    assert plugin_adapter.get_plugin_dir() == tmp_path.resolve()


def test_rest_second_connect_returns_true(monkeypatch):
    """已连接时 connect() 也要返回 True（调用方靠返回值判成败）。"""
    client = RestClient({'id': 'r1', 'base_url': 'http://127.0.0.1',
                         'poll_interval': 30})
    monkeypatch.setattr(client, '_poll_loop', lambda: client._stop_event.wait(5))
    try:
        assert client.connect() is True
        assert client.connect() is True
    finally:
        client.disconnect()


# --------------------------------------------------------- Modbus 缓存/协议
def test_modbus_cache_not_evicted_before_limit():
    """条目数 == 上限时**不能**淘汰（`>` 改成 `>=` 会提前砍半）。"""
    from 采集层.modbus_client import _MAX_CACHED_RANGES

    client = ModbusClient({'id': 'm1', 'host': '127.0.0.1'})
    for i in range(_MAX_CACHED_RANGES):
        client._mark_read_fresh((i, 1), [i])

    assert len(client._last_good_values) == _MAX_CACHED_RANGES
    assert len(client._last_good_at) == _MAX_CACHED_RANGES


def test_modbus_cache_evicts_when_over_limit():
    from 采集层.modbus_client import _MAX_CACHED_RANGES

    client = ModbusClient({'id': 'm1', 'host': '127.0.0.1'})
    for i in range(_MAX_CACHED_RANGES + 1):
        client._mark_read_fresh((i, 1), [i])

    # 两张表必须同步收缩，且不超过上限
    assert len(client._last_good_values) <= _MAX_CACHED_RANGES
    assert len(client._last_good_values) == len(client._last_good_at)


def test_modbus_protocol_branch_picks_connection_fields():
    tcp = ModbusClient({'id': 't1', 'protocol': 'modbus_tcp',
                        'host': '10.0.0.5', 'port': 1502})
    assert (tcp.host, tcp.port) == ('10.0.0.5', 1502)

    rtu = ModbusClient({'id': 'r1', 'protocol': 'modbus_rtu',
                        'serial_port': 'COM3', 'baudrate': 19200})
    assert rtu.serial_port == 'COM3'
    assert rtu.baudrate == 19200
