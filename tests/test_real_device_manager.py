"""`采集层/real_device_manager.py` 行为测试。

背景（2026-10-13）：这个模块 **350 行、0% 覆盖**，而它是 `run.py --real`
（真实设备模式）**唯一**的设备管理器 —— 接现场 PLC 的那条路。
CI 注释里早先记的是 22%，现在是 0%，说明当时覆盖它的测试已经不在（或退化成静态扫描）。

本轮在补测过程中挖出**两个真实缺陷**（都只在真实路径、都因为零覆盖而长期不可见）：

1. **找不到启停点位时回退写 `coil 0`**。
   本仓自己的注释已指出「地址 0 是 `relay_1`（输出线圈）与 `di_1` 共用的地址」
   （`智能层/device_control.py` 的联锁说明）—— 盲写就是**误动作**。
   更糟的是 `device_control.batch_control` 会**先按配置点位写一次**、再调用本类的
   `start_device()/stop_device()`，于是**每次启停都会额外多写一次 coil 0**。
2. **客户端不存在时返回 `True`**（`if client: ... ; return True` 的缩进错误）
   → `/api/devices/{id}/stop` 会把「什么都没做」报成「停止成功」。

两条都已修（真实模式不猜点位 → 拒绝执行；失败一律返回 False），本文件把行为钉住。
"""
import threading
from unittest.mock import MagicMock

import pytest
import yaml

MECH = {'device_category': 'mechanical'}


def _mgr(tmp_path, devices):
    from 采集层.real_device_manager import RealDeviceManager

    cfg = tmp_path / 'devices_real_test.yaml'
    cfg.write_text(yaml.safe_dump({'devices': devices}, allow_unicode=True),
                   encoding='utf-8')
    return RealDeviceManager(config_path=str(cfg))


def _fake_client(ok=True):
    """行为正确的假 PLC：写入生效、回读返回刚写入的值。

    round 380 起 `stop_device/start_device/adjust_device` 都要求 write-then-read-back，
    假客户端必须像真 PLC 一样「读得到刚写的东西」，否则测的就不是真实路径。
    测试仍可用 `.write_single_*.return_value = False` 模拟写失败 —— side_effect
    每次都从 `return_value` 取当前值，写入失败时不更新回读缓存。
    """
    c = MagicMock()
    c.connected = True
    c._regs: dict[int, int] = {}
    c._coils: dict[int, bool] = {}

    def _write_register(address, value, *args, **kwargs):
        result = c.write_single_register.return_value
        if result:
            c._regs[address] = int(value) & 0xFFFF
        return result

    def _write_coil(address, value, *args, **kwargs):
        result = c.write_single_coil.return_value
        if result:
            c._coils[address] = bool(value)
        return result

    c.write_single_register.return_value = ok
    c.write_single_coil.return_value = ok
    c.write_single_register.side_effect = _write_register
    c.write_single_coil.side_effect = _write_coil
    c.read_holding_registers.side_effect = (
        lambda address, count=1, *a, **k: [c._regs.get(address + i, 0)
                                          for i in range(count)])
    c.read_coils.side_effect = (
        lambda address, count=1, *a, **k: [c._coils.get(address + i, False)
                                          for i in range(count)])
    return c


def _mech_mgr(tmp_path, control=None, extra=None):
    dev = {'id': 'plc_a', 'name': 'A', 'protocol': 'modbus_tcp', **MECH}
    if control is not None:
        dev['control'] = control
    if extra:
        dev.update(extra)
    m = _mgr(tmp_path, [dev])
    client = _fake_client()
    m.clients['plc_a'] = client
    return m, client


# ---------------------------------------------------------------------------
# 1. 配置加载与基本状态
# ---------------------------------------------------------------------------

class TestConfigAndBasics:
    def test_loads_devices(self, tmp_path):
        m = _mgr(tmp_path, [
            {'id': 'd1', 'name': '一', 'protocol': 'modbus_tcp'},
            {'id': 'd2', 'name': '二', 'protocol': 'opcua'},
        ])
        assert set(m.devices) == {'d1', 'd2'}

    def test_missing_config_file_is_not_fatal(self, tmp_path):
        """配置不存在只告警，不能抛（否则真实模式启动就崩）"""
        from 采集层.real_device_manager import RealDeviceManager
        m = RealDeviceManager(config_path=str(tmp_path / 'nope.yaml'))
        assert m.devices == {}

    def test_never_simulation(self, tmp_path):
        """真实设备管理器**永远**不是模拟模式（这是它存在的意义）"""
        m = _mgr(tmp_path, [{'id': 'd1', 'protocol': 'modbus_tcp'}])
        assert m.simulation_mode is False

    def test_device_status_shape(self, tmp_path):
        m = _mgr(tmp_path, [{'id': 'd1', 'name': '一', 'protocol': 'modbus_tcp',
                             'host': '10.0.0.1', 'port': 502, **MECH}])
        st = m.get_device_status('d1')
        for k in ('device_id', 'name', 'protocol', 'host', 'port', 'connected',
                  'stopped', 'device_category', 'mode'):
            assert k in st, f'设备状态缺字段 {k}'
        assert st['mode'] == 'real'
        assert st['connected'] is False

    def test_unknown_device_status_returns_error(self, tmp_path):
        m = _mgr(tmp_path, [])
        assert 'error' in m.get_device_status('nope')

    def test_estop_marks_all_stopped(self, tmp_path):
        m = _mgr(tmp_path, [{'id': 'd1', 'protocol': 'modbus_tcp', **MECH}])
        m.set_estop_override(True)
        assert m.get_device_status('d1')['stopped'] is True
        m.set_estop_override(False)
        assert m.get_device_status('d1')['stopped'] is False

    def test_protocol_summary(self, tmp_path):
        m = _mgr(tmp_path, [
            {'id': 'a', 'protocol': 'modbus_tcp'},
            {'id': 'b', 'protocol': 'modbus_tcp'},
            {'id': 'c', 'protocol': 'opcua'},
        ])
        assert m.get_protocol_summary() == {'modbus_tcp': 2, 'opcua': 1}


# ---------------------------------------------------------------------------
# 2. 客户端工厂
# ---------------------------------------------------------------------------

class TestClientFactory:
    def test_dispatch_by_protocol(self, tmp_path, monkeypatch):
        import 采集层.real_device_manager as rdm

        seen = []
        monkeypatch.setattr(rdm, 'ModbusClient', lambda cfg: seen.append(('modbus', cfg)) or 'M')
        monkeypatch.setattr(rdm, 'OPCUAClient', lambda cfg: seen.append(('opcua', cfg)) or 'O')
        monkeypatch.setattr(rdm, 'MQTTClient', lambda cfg: seen.append(('mqtt', cfg)) or 'Q')
        monkeypatch.setattr(rdm, 'RESTDeviceClient', lambda cfg: seen.append(('rest', cfg)) or 'R')

        m = _mgr(tmp_path, [])
        assert m._create_real_client({'protocol': 'modbus_tcp'}) == 'M'
        assert m._create_real_client({'protocol': 'modbus_rtu'}) == 'M'
        assert m._create_real_client({'protocol': 'opcua'}) == 'O'
        assert m._create_real_client({'protocol': 'mqtt'}) == 'Q'
        assert m._create_real_client({'protocol': 'rest'}) == 'R'
        assert [x[0] for x in seen] == ['modbus', 'modbus', 'opcua', 'mqtt', 'rest']

    def test_missing_protocol_defaults_to_modbus(self, tmp_path, monkeypatch):
        import 采集层.real_device_manager as rdm
        monkeypatch.setattr(rdm, 'ModbusClient', lambda cfg: 'M')
        m = _mgr(tmp_path, [])
        assert m._create_real_client({}) == 'M'

    def test_unsupported_protocol_returns_none(self, tmp_path):
        """不支持的协议必须返回 None（调用方据此报错），不能抛也不能瞎建"""
        m = _mgr(tmp_path, [])
        assert m._create_real_client({'protocol': 'iec104'}) is None
        assert m._create_real_client({'protocol': '不存在的协议'}) is None

    def test_get_client_is_lazy_and_cached(self, tmp_path, monkeypatch):
        import 采集层.real_device_manager as rdm
        calls = []
        monkeypatch.setattr(rdm, 'ModbusClient',
                            lambda cfg: calls.append(cfg) or MagicMock())
        m = _mgr(tmp_path, [{'id': 'd1', 'protocol': 'modbus_tcp'}])
        c1 = m.get_client('d1')
        c2 = m.get_client('d1')
        assert c1 is c2 and len(calls) == 1, '客户端应只创建一次（懒创建 + 缓存）'

    def test_get_client_unknown_device_returns_none(self, tmp_path):
        m = _mgr(tmp_path, [])
        assert m.get_client('nope') is None


# ---------------------------------------------------------------------------
# 3. 连接退避
# ---------------------------------------------------------------------------

class TestBackoff:
    def test_no_backoff_initially(self, tmp_path):
        m = _mgr(tmp_path, [{'id': 'd1', 'protocol': 'modbus_tcp'}])
        assert m._should_skip_connect('d1') is False

    def test_backoff_after_max_retries(self, tmp_path, monkeypatch):
        m = _mgr(tmp_path, [{'id': 'd1', 'protocol': 'modbus_tcp'}])
        for _ in range(m.MAX_RETRY_COUNT - 1):
            m._record_connection_failure('d1')
        assert m._should_skip_connect('d1') is False, '未达上限不应退避'
        m._record_connection_failure('d1')
        assert m._should_skip_connect('d1') is True, '达到上限后应进入退避'

    def test_backoff_expires(self, tmp_path, monkeypatch):
        """退避窗口过后应重新允许连接（否则设备永不恢复）"""
        import 采集层.real_device_manager as rdm
        m = _mgr(tmp_path, [{'id': 'd1', 'protocol': 'modbus_tcp'}])
        for _ in range(m.MAX_RETRY_COUNT):
            m._record_connection_failure('d1')
        assert m._should_skip_connect('d1') is True
        # 把「上次失败时刻」回拨到远超退避窗口
        m._connection_failures['d1']['last_retry'] -= (m.MAX_RETRY_INTERVAL + 1)
        assert m._should_skip_connect('d1') is False

    def test_backoff_is_capped(self, tmp_path):
        """指数退避必须有上限：超过上限后等待时间不再增长。

        判据（有判别力）：`retry_count` 很大时，
        * 距上次失败 301 秒 → **不再跳过**（说明 wait 被限幅到 300 秒）；
        * 距上次失败 299 秒 → 仍跳过。
        若没有 `min(..., MAX_RETRY_INTERVAL)` 限幅，wait = 30 × 2^97 秒，
        301 秒那一条也会被判成「还要等」→ 设备**永不重连**。
        """
        import time
        m = _mgr(tmp_path, [{'id': 'd1', 'protocol': 'modbus_tcp'}])

        # round 369：退避基准是单调时钟
        m._connection_failures['d1'] = {'retry_count': 100,
                                        'last_retry': time.monotonic() - 301}
        assert m._should_skip_connect('d1') is False, \
            '等待时间没被限幅 —— 指数退避会无限增长，设备永不重连'

        m._connection_failures['d1']['last_retry'] = time.monotonic() - 299
        assert m._should_skip_connect('d1') is True

    def test_success_resets_counter(self, tmp_path):
        m = _mgr(tmp_path, [{'id': 'd1', 'protocol': 'modbus_tcp'}])
        m._record_connection_failure('d1')
        m._record_connection_success('d1')
        assert 'd1' not in m._connection_failures
        assert m._should_skip_connect('d1') is False

    def test_connect_device_records_result(self, tmp_path):
        m, client = _mech_mgr(tmp_path)
        client.connect.return_value = False
        assert m.connect_device('plc_a') is False
        assert m._connection_failures['plc_a']['retry_count'] == 1
        client.connect.return_value = True
        assert m.connect_device('plc_a') is True
        assert 'plc_a' not in m._connection_failures


# ---------------------------------------------------------------------------
# 4. 启停点位解析（本轮修复的核心）
# ---------------------------------------------------------------------------

class TestControlPointResolution:
    def test_refuses_when_no_control_config(self, tmp_path):
        """⚠️ 核心回归：未配置 control → 拒绝，**绝不回退写 coil 0**"""
        m, client = _mech_mgr(tmp_path, control=None)
        assert m.stop_device('plc_a') is False
        assert m.start_device('plc_a') is False
        client.write_single_coil.assert_not_called()
        client.write_single_register.assert_not_called()

    def test_refuses_when_control_is_empty_dict(self, tmp_path):
        m, client = _mech_mgr(tmp_path, control={})
        assert m.stop_device('plc_a') is False
        client.write_single_coil.assert_not_called()

    def test_schema_a_action_dict(self, tmp_path):
        """写法 A（与 device_control 同形）：control.start / control.stop"""
        m, client = _mech_mgr(tmp_path, control={
            'start': {'type': 'coil', 'address': 7, 'value': True},
            'stop': {'type': 'coil', 'address': 9, 'value': False},
        })
        assert m.start_device('plc_a') is True
        client.write_single_coil.assert_called_once_with(7, True)
        client.reset_mock()
        assert m.stop_device('plc_a') is True
        client.write_single_coil.assert_called_once_with(9, False)

    def test_schema_a_register_point(self, tmp_path):
        m, client = _mech_mgr(tmp_path, control={
            'start': {'type': 'register', 'address': 321, 'value': 1},
        })
        assert m.start_device('plc_a') is True
        client.write_single_register.assert_called_once_with(321, 1)
        client.write_single_coil.assert_not_called()

    def test_schema_b_legacy_coil(self, tmp_path):
        """写法 B（本模块历史写法）必须继续可用，否则旧配置会突然失效"""
        m, client = _mech_mgr(tmp_path, control={'start_coil': 5, 'stop_coil': 6})
        assert m.start_device('plc_a') is True
        client.write_single_coil.assert_called_once_with(5, True)
        client.reset_mock()
        assert m.stop_device('plc_a') is True
        client.write_single_coil.assert_called_once_with(6, False)

    def test_schema_b_legacy_register(self, tmp_path):
        m, client = _mech_mgr(tmp_path, control={'start_register': 400, 'stop_register': 401})
        assert m.start_device('plc_a') is True
        client.write_single_register.assert_called_once_with(400, 1)
        client.reset_mock()
        assert m.stop_device('plc_a') is True
        client.write_single_register.assert_called_once_with(401, 0)

    def test_schema_c_flat_shared_address(self, tmp_path):
        """写法 C：扁平共用 type/address，各动作取自己的 value"""
        m, client = _mech_mgr(tmp_path, control={
            'type': 'coil', 'address': 3, 'start_value': True, 'stop_value': False,
        })
        assert m.start_device('plc_a') is True
        client.write_single_coil.assert_called_once_with(3, True)
        client.reset_mock()
        assert m.stop_device('plc_a') is True
        client.write_single_coil.assert_called_once_with(3, False)

    @pytest.mark.parametrize('bad', [
        {'start': {'type': 'pwm', 'address': 1}},        # 类型非法
        {'start': {'type': 'coil', 'address': 'x'}},     # 地址非法
        {'start': {'type': 'coil'}},                     # 缺地址
    ])
    def test_invalid_point_refused(self, tmp_path, bad):
        m, client = _mech_mgr(tmp_path, control=bad)
        assert m.start_device('plc_a') is False
        client.write_single_coil.assert_not_called()
        client.write_single_register.assert_not_called()


# ---------------------------------------------------------------------------
# 5. 失败语义（本轮修复的核心之二）
# ---------------------------------------------------------------------------

class TestFailureSemantics:
    def test_missing_client_returns_false_not_true(self, tmp_path):
        """⚠️ 核心回归：客户端不存在时必须返回 False。

        历史实现是 `if client: ...` 然后**无条件 `return True`** ——
        `/api/devices/{id}/stop` 会把「什么都没做」报成「停止成功」。

        ⚠️ 构造要点：必须让 `get_client()` **真的返回 None**。
        直接 `m.clients.clear()` 不行 —— `get_client` 会**懒创建**一个真客户端
        （协议受支持时），于是根本走不到 `client is None` 那条分支，判据会假绿。
        这里用**不支持的协议**（`iec104` 不在本类 `SUPPORTED_PROTOCOLS` 里）
        让 `_create_real_client` 返回 None。
        """
        m = _mgr(tmp_path, [{'id': 'plc_x', 'name': 'X', 'protocol': 'iec104',
                             'device_category': 'mechanical',
                             'control': {'stop_coil': 9, 'start_coil': 7}}])
        assert m.get_client('plc_x') is None, '构造前提不成立：客户端不该被创建出来'

        assert m.stop_device('plc_x') is False
        assert m.start_device('plc_x') is False

    def test_write_failure_returns_false(self, tmp_path):
        m, client = _mech_mgr(tmp_path, control={'stop_coil': 9})
        client.write_single_coil.return_value = False
        assert m.stop_device('plc_a') is False

    def test_stopped_flag_only_after_success(self, tmp_path):
        """写失败不得把设备标成「已停止」（历史实现在写之前就标记）"""
        m, client = _mech_mgr(tmp_path, control={'stop_coil': 9})
        client.write_single_coil.return_value = False
        m.stop_device('plc_a')
        assert m.get_device_status('plc_a')['stopped'] is False

        client.write_single_coil.return_value = True
        assert m.stop_device('plc_a') is True
        assert m.get_device_status('plc_a')['stopped'] is True

    def test_start_clears_stopped_flag(self, tmp_path):
        m, client = _mech_mgr(tmp_path, control={'start_coil': 7, 'stop_coil': 9})
        m.stop_device('plc_a')
        assert m.get_device_status('plc_a')['stopped'] is True
        m.start_device('plc_a')
        assert m.get_device_status('plc_a')['stopped'] is False

    def test_unknown_device_returns_false(self, tmp_path):
        m, _ = _mech_mgr(tmp_path, control={'start_coil': 7})
        assert m.start_device('nope') is False
        assert m.stop_device('nope') is False

    def test_non_mechanical_device_refused(self, tmp_path):
        """仪表/安全设备不可启停（get_device_category 判定）"""
        m = _mgr(tmp_path, [{'id': 'inst', 'name': '温度计', 'protocol': 'modbus_tcp',
                             'device_category': 'instrument',
                             'control': {'start_coil': 7}}])
        client = _fake_client()
        m.clients['inst'] = client
        assert m.start_device('inst') is False
        client.write_single_coil.assert_not_called()

    def test_client_without_write_methods_returns_false(self, tmp_path):
        """客户端不支持任何写入方法 → False（不能静默当成功）"""
        m, _ = _mech_mgr(tmp_path, control={'start_coil': 7})
        m.clients['plc_a'] = object()
        assert m.start_device('plc_a') is False


# ---------------------------------------------------------------------------
# 6. round 380：写后回读 / 急停语义 / 调参校验 / 设备级互斥
# ---------------------------------------------------------------------------

class TestWriteVerificationRound380:
    def test_stop_fails_when_read_back_disagrees(self, tmp_path):
        """帧发出去了但设备没停 → 必须 False，且不得标记 stopped（假成功致命）。"""
        m, client = _mech_mgr(tmp_path, control={'stop_coil': 9})
        client.read_coils.side_effect = (
            lambda address, count=1, *a, **k: [True])   # 读回仍为 ON
        assert m.stop_device('plc_a') is False
        assert m.get_device_status('plc_a')['stopped'] is False

    def test_start_fails_when_read_back_disagrees(self, tmp_path):
        m, client = _mech_mgr(tmp_path, control={'start_coil': 7})
        client.read_coils.side_effect = (
            lambda address, count=1, *a, **k: [False])
        assert m.start_device('plc_a') is False

    def test_stale_cache_is_not_success(self, tmp_path):
        """Modbus 读失败会返回陈旧缓存 —— 回读非 fresh 一律算失败。"""
        m, client = _mech_mgr(tmp_path, control={'stop_coil': 9})
        client.last_read_source = 'cache'
        assert m.stop_device('plc_a') is False
        assert m.get_device_status('plc_a')['stopped'] is False

    def test_client_without_read_back_is_not_success(self, tmp_path):
        """客户端没有回读能力 → 无法确认写入，不能报成功。"""
        class WriteOnly:
            connected = True

            def write_single_coil(self, address, value):
                return True

        m, _ = _mech_mgr(tmp_path, control={'stop_coil': 9})
        m.clients['plc_a'] = WriteOnly()
        assert m.stop_device('plc_a') is False

    def test_estop_only_affects_mechanical(self, tmp_path):
        """红线：E-STOP 只归零 mechanical；仪表与安全设备继续工作。"""
        m = _mgr(tmp_path, [
            {'id': 'mech', 'name': 'pump', 'protocol': 'modbus_tcp',
             'device_category': 'mechanical'},
            {'id': 'inst', 'name': '温度计', 'protocol': 'modbus_tcp',
             'device_category': 'instrument'},
            {'id': 'safe', 'name': '报警灯', 'protocol': 'modbus_tcp',
             'device_category': 'safety'},
        ])
        m.set_estop_override(True)
        assert m.get_device_status('mech')['stopped'] is True
        assert m._get_brief_status('mech')['status'] == 'fault'

        for did in ('inst', 'safe'):
            full = m.get_device_status(did)
            brief = m._get_brief_status(did)
            assert full['stopped'] is False, f'{did} 被 E-STOP 误标为 stopped'
            assert brief['stopped'] is False, f'{did} 被 E-STOP 误标为 stopped'
            assert brief['status'] != 'fault', f'{did} 被 E-STOP 误标为 fault'

    def test_adjust_rejects_nan_and_inf(self, tmp_path):
        m, client = _mech_mgr(tmp_path, extra={
            'registers': [{'name': 'speed', 'address': 10, 'data_type': 'uint16'}]})
        for bad in (float('nan'), float('inf'), float('-inf')):
            r = m.adjust_device('plc_a', 'speed', bad)
            assert r['success'] is False, f'{bad!r} 被当成合法设定值'
        client.write_single_register.assert_not_called()

    def test_adjust_rejects_out_of_range_instead_of_wrapping(self, tmp_path):
        """65536 / -1 写 uint16 不能 `& 0xFFFF` 变成 0 / 65535。"""
        m, client = _mech_mgr(tmp_path, extra={
            'registers': [{'name': 'speed', 'address': 10, 'data_type': 'uint16'}]})
        for bad in (65536, -1):
            r = m.adjust_device('plc_a', 'speed', bad)
            assert r['success'] is False, f'{bad} 超出 uint16 却被接受'
        client.write_single_register.assert_not_called()

    def test_adjust_read_back_mismatch_is_failure(self, tmp_path):
        m, client = _mech_mgr(tmp_path, extra={
            'registers': [{'name': 'speed', 'address': 10, 'data_type': 'uint16'}]})
        client.read_holding_registers.side_effect = (
            lambda address, count=1, *a, **k: [4242])
        r = m.adjust_device('plc_a', 'speed', 1500)
        assert r['success'] is False
        assert '回读' in r['message']

    def test_concurrent_commands_are_serialized_per_device(self, tmp_path):
        """同一台设备的启停/调参不得重叠进入写临界区。"""
        import threading
        import time

        m, client = _mech_mgr(tmp_path, control={'start_coil': 7, 'stop_coil': 9})
        base_coil = client.write_single_coil.side_effect
        state = {'active': 0, 'max': 0, 'lock': threading.Lock()}

        def _tracked(address, value, *args, **kwargs):
            with state['lock']:
                state['active'] += 1
                state['max'] = max(state['max'], state['active'])
            time.sleep(0.02)
            try:
                return base_coil(address, value, *args, **kwargs)
            finally:
                with state['lock']:
                    state['active'] -= 1

        client.write_single_coil.side_effect = _tracked

        def _work(i):
            m.start_device('plc_a') if i % 2 == 0 else m.stop_device('plc_a')

        threads = [threading.Thread(target=_work, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert state['max'] == 1, '同一设备的写操作发生了重叠'


# ---------------------------------------------------------------------------
# 7. 重连循环生命周期
# ---------------------------------------------------------------------------

class TestReconnectLoop:
    def test_start_and_stop_loop(self, tmp_path):
        m = _mgr(tmp_path, [])
        m.start_reconnect_loop(interval=1)
        assert m._reconnect_running is True
        m.stop_reconnect_loop()
        assert m._reconnect_running is False
        assert m._reconnect_stop_event.is_set()

    def test_start_loop_is_idempotent(self, tmp_path):
        m = _mgr(tmp_path, [])
        m.start_reconnect_loop(interval=1)
        first = m._reconnect_stop_event
        m.start_reconnect_loop(interval=1)
        assert m._reconnect_stop_event is first, '重复调用不应重建线程/事件'
        m.stop_reconnect_loop()
