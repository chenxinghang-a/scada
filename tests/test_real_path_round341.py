"""真实设备路径（round 341）。

## 为什么需要这个文件

前面 340 轮的工作几乎全在**模拟模式**上 —— 模拟器做得再真，真实设备路径
一行没跑过也没用。本轮审计发现真实路径上有一批「模拟能过、真机不行」的缺陷，
这个文件把它们逐条锁住。

四条是**静默失败**（不报错、不告警，只是数据不对或功能不动）：

1. `${VAR}` 不展开 —— `devices_real.yaml` 里 `host: ${PLC_HOST_01}`，
   三个加载器都用 `yaml.safe_load` 直接读，于是客户端拿
   `"${PLC_HOST_01}"` 当主机名去连，**42 台设备一台都连不上**，
   日志只说「连接失败」。而配置文件头部还写着「加载方需支持 ${VAR} 展开」。

2. WebSocket 压缩包没人解 —— 服务端把 >1KB 的载荷换成
   `{"__compressed": true, "data": "..."}`，前端没有任何解压实现，
   读 `msg.device_id` 得到 undefined，**界面静默不更新**。

3. 回读读的是客户端缓存 —— `ModbusClient` 读失败时返回最后已知良好值，
   写后回读不判 `last_read_source`，于是「写失败+缓存恰好同值」判通过、
   「写成功+回读超时」判失败（触发重复下发）。

4. `/start` `/stop` `/adjust` 绕过安全层 —— 急停/联锁/量程/审计/回读全没有，
   而 `/api/control/*` 有。同一件事两条路，挑闸门少的那条就绕过去了。

另外两条是**一崩全崩**：
5. 单台设备的 `byte_order` 配错 → `ByteOrder()` 抛 ValueError →
   穿出 `connect_all()` → `run.py` 的启动流程整块中断。
6. REST 的 `self._session.timeout = (3, 5)` —— `requests.Session` 没有这个
   属性，等于**所有 REST 请求无超时**，对端不响应时永久阻塞。
"""

import copy
import inspect
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO = Path(__file__).resolve().parent.parent

#: 本来就无量纲的物理量（功率因数、pH、比率、状态码）。
#: 它们没有 unit 是对的 —— 强行填「1」或「%」反而是错的。
_DIMENSIONLESS = __import__('re').compile(
    r'power_factor|factor|ratio|ph\b|ph_value|status|state|mode|code|count|'
    r'number|index|position|speed_ratio|efficiency')
REAL_CFG = REPO / '配置' / 'devices_real.yaml'
SIM_CFG = REPO / '配置' / 'devices_simulated.yaml'


# ============================================================
# 1. 环境变量展开
# ============================================================

class TestEnvVarExpansion:
    """三个设备加载器都必须展开 ``${VAR}``。"""

    LOADERS = [
        ('采集层/real_device_manager.py', 'RealDeviceManager'),
        ('采集层/simulated_device_manager.py', 'SimulatedDeviceManager'),
        ('采集层/device_manager.py', 'DeviceManager'),
    ]

    @pytest.mark.parametrize('rel_path,cls_name', LOADERS)
    def test_loader_uses_config_manager(self, rel_path, cls_name):
        """结构守卫：加载器必须走 `ConfigManager.load_yaml`，不许裸 `yaml.safe_load`。

        裸 `yaml.safe_load` 不会展开 `${VAR}`。这条断言防的是「以后又有人
        图省事直接读文件」—— 那会让环境变量注入静默失效。
        """
        src = (REPO / rel_path).read_text(encoding='utf-8')
        # 只看代码行：注释里提到这些名字是说明文字，不是调用
        code = '\n'.join(line.split('#', 1)[0] for line in src.splitlines())
        assert 'expand_env_vars' in code, (
            f'{rel_path} 没有调 expand_env_vars，${{VAR}} 不会展开')
        # 反过来：不许用 ConfigManager.load_yaml —— 它内部
        # `except Exception → return {}`，坏 YAML 会变成「0 台设备正常启动」。
        # 带括号才是**调用**；docstring 里提到方法名是说明文字
        assert 'ConfigManager.load_yaml(' not in code, (
            f'{rel_path} 用了 ConfigManager.load_yaml()：坏 YAML 会被吞成空配置，'
            f'进程以 0 台设备正常启动')

    def test_real_config_host_is_expanded(self, monkeypatch):
        """注入了环境变量，加载出来的 host 必须是真实值而不是占位符。"""
        from 采集层.real_device_manager import RealDeviceManager

        if not REAL_CFG.exists():
            pytest.skip('没有 devices_real.yaml')

        devices = yaml.safe_load(REAL_CFG.read_text(encoding='utf-8')).get('devices') or []
        modbus = next((d for d in devices if d.get('protocol') in ('modbus_tcp', 'modbus_rtu')), None)
        if modbus is None:
            pytest.skip('配置里没有 Modbus 设备')

        var_name = str(modbus['host']).removeprefix('${').removesuffix('}')
        if not var_name.startswith('DEVICE_'):
            pytest.skip('该设备 host 不是 ${DEVICE_*} 形式')

        monkeypatch.setenv(var_name, '10.9.8.7')
        mgr = RealDeviceManager(config_path=str(REAL_CFG))
        assert mgr.devices[modbus['id']]['host'] == '10.9.8.7', (
            '环境变量没有展开 —— 真实设备会拿占位符当主机名去连')

    def test_unset_var_keeps_placeholder_not_empty(self, monkeypatch):
        """未注入时**保留占位符**并告警，不能静默变成空串。

        空串会让 `host=''` 看起来"配了但连不上"；占位符至少能从日志里
        一眼看出是变量没注入。
        """
        from 采集层.real_device_manager import RealDeviceManager

        if not REAL_CFG.exists():
            pytest.skip('没有 devices_real.yaml')
        devices = yaml.safe_load(REAL_CFG.read_text(encoding='utf-8')).get('devices') or []
        target = next((d for d in devices if str(d.get('host', '')).startswith('${')), None)
        if target is None:
            pytest.skip('配置里没有 ${VAR} 形式的 host')

        var_name = str(target['host']).removeprefix('${').removesuffix('}')
        monkeypatch.delenv(var_name, raising=False)
        mgr = RealDeviceManager(config_path=str(REAL_CFG))
        assert mgr.devices[target['id']]['host'] == f'${{{var_name}}}', (
            '未注入的环境变量应当保留占位符（便于定位），不能变成空串')


# ============================================================
# 2. MQTT topics 两种写法
# ============================================================

class TestMqttTopicParsing:
    """`topics` 既支持 dict 也支持裸字符串。"""

    def test_string_topic_supported(self):
        """`- env/warehouse/temperature` 这种写法必须能用。

        原实现对每条都 `entry.get('topic')`，裸字符串会 AttributeError：
        在 `start()` 里被 except 吞掉 → 整个 MQTT 设备起不来；
        在 `_register_configured_topics()` 里只留一行 warning 就跳过 →
        「配了 2 个主题，一条数据也收不到」。
        """
        from 采集层.mqtt_client import parse_topic_entry

        topic, qos = parse_topic_entry('env/warehouse/temperature')
        assert topic == 'env/warehouse/temperature'
        assert qos in (0, 1, 2)

    def test_dict_topic_supported(self):
        from 采集层.mqtt_client import parse_topic_entry

        topic, qos = parse_topic_entry(
            {'name': 'vib_x', 'topic': 'factory/motor/x', 'json_path': 'value', 'qos': 1})
        assert topic == 'factory/motor/x'
        assert qos == 1

    def test_illegal_qos_falls_back(self):
        from 采集层.mqtt_client import parse_topic_entry

        topic, qos = parse_topic_entry({'topic': 'a/b', 'qos': 9})
        assert topic == 'a/b' and qos in (0, 1, 2), '非法 QoS 必须退回默认，不能原样传下去'

    def test_garbage_entry_does_not_raise(self):
        """非法条目（None / 数字）只跳过，不能抛异常打断整个订阅流程。"""
        from 采集层.mqtt_client import parse_topic_entry

        for bad in (None, 123, [], {'name': 'no_topic'}):
            topic, _ = parse_topic_entry(bad)
            assert topic == '', f'{bad!r} 应当返回空 topic 而不是抛异常'


# ============================================================
# 3. WebSocket 压缩默认关闭
# ============================================================

class TestWebSocketCompression:
    """服务端不得假设客户端能解压。"""

    def test_compression_is_opt_in(self):
        """结构守卫：两处 emit 都必须先判 `_all_clients_support_compression()`。

        原实现无条件 `compress_message(...)`，而前端没有解压实现 ——
        收到的 `{__compressed: true, data: "..."}` 里没有任何业务字段，
        界面静默不更新（不报错，最难发现）。
        """
        src = (REPO / '展示层' / 'websocket.py').read_text(encoding='utf-8')
        assert '_all_clients_support_compression' in src, (
            '缺少「客户端是否支持压缩」的判定，服务端会在无人声明时也发压缩包')

        lines = src.splitlines()
        # 只看**代码行**：注释里出现 compress_message() 是说明文字，不是调用
        for idx, line in enumerate(lines):
            code = line.split('#', 1)[0]
            if 'compress_message(' not in code or 'def ' in code:
                continue
            # 窗口要往后多取两行：三元表达式常常写成
            #   payload = (compress_message(x)
            #              if _all_clients_support_compression() else x)
            context = '\n'.join(l.split('#', 1)[0] for l in lines[max(0, idx - 5):idx + 3])
            assert '_all_clients_support_compression' in context, (
                f'第 {idx + 1} 行无条件压缩了消息，客户端解不开会静默丢数据')

    def test_no_client_means_no_compression(self):
        """无人连接时判定为 False（不发压缩包）。"""
        from 展示层 import websocket as ws

        with ws._clients_lock:
            saved_clients = set(ws._connected_clients)
            saved_gzip = set(ws._compression_clients)
            ws._connected_clients.clear()
            ws._compression_clients.clear()
        try:
            assert ws._all_clients_support_compression() is False
        finally:
            with ws._clients_lock:
                ws._connected_clients.update(saved_clients)
                ws._compression_clients.update(saved_gzip)

    def test_one_legacy_client_disables_compression(self):
        """只要有一个客户端没声明，整批发原文 —— 不能只顾支持的客户端。"""
        from 展示层 import websocket as ws

        with ws._clients_lock:
            saved_clients = set(ws._connected_clients)
            saved_gzip = set(ws._compression_clients)
            ws._connected_clients.clear()
            ws._compression_clients.clear()
            ws._connected_clients.update({'a', 'b'})
            ws._compression_clients.update({'a'})   # b 不支持
        try:
            assert ws._all_clients_support_compression() is False
            with ws._clients_lock:
                ws._compression_clients.add('b')
            assert ws._all_clients_support_compression() is True
        finally:
            with ws._clients_lock:
                ws._connected_clients.clear()
                ws._compression_clients.clear()
                ws._connected_clients.update(saved_clients)
                ws._compression_clients.update(saved_gzip)


# ============================================================
# 4. 回读必须新鲜
# ============================================================

class TestReadbackFreshness:
    """写后回读不能读缓存。"""

    def test_readback_checks_last_read_source(self):
        """结构守卫：回读分支必须判 `last_read_source`。"""
        src = (REPO / '智能层' / 'device_control.py').read_text(encoding='utf-8')
        assert "getattr(client, 'last_read_source', 'fresh')" in src, (
            '回读没有判新鲜度：写失败+缓存同值会判通过，写成功+回读超时会判失败')

    def test_stale_readback_yields_unverified(self):
        """回读走缓存时 `verified` 必须是 None（未验证），既不算通过也不算失败。"""
        from unittest.mock import MagicMock

        from 智能层.device_control import DeviceControlSafety

        dc = MagicMock(spec=DeviceControlSafety)
        dc._audit = MagicMock(return_value=True)
        # 直接调真实方法（绑定到 mock 实例）
        real = DeviceControlSafety.__dict__['write_with_verification']

        client = MagicMock()
        client.write_single_register.return_value = True
        client.read_holding_registers.return_value = [42]     # 缓存里恰好是 42
        client.last_read_source = 'cache'                     # 但它是陈旧的

        fake_self = MagicMock()
        fake_self.device_manager.get_client.return_value = client
        fake_self._audit = MagicMock(return_value=True)
        fake_self.validate_write = MagicMock(return_value={'allowed': True, 'reason': ''})
        fake_self._get_device_lock = MagicMock()
        fake_self._get_device_lock.return_value.__enter__ = MagicMock()
        fake_self._get_device_lock.return_value.__exit__ = MagicMock(return_value=False)
        fake_self._interlock_states = {}
        fake_self._estop_active = False

        result = real(fake_self, 'dev', 10, 42, 'op', kind='register')
        assert result.get('verified') is None, (
            f'陈旧缓存被当成验证结果: {result}')


# ============================================================
# 5. 控制路径必须走安全层
# ============================================================

class TestControlPathUnification:
    """`/start` `/stop` `/adjust` 不许绕过安全层。"""

    def test_device_endpoints_do_not_call_manager_directly(self):
        """结构守卫：api 层不许直接调 `device_manager.stop_device/start_device`。

        这两条路径绕过的是：急停检查、联锁检查、量程校验、审计、回读验证。
        而 `/api/control/*` 全都有 —— 同一件事两条路，闸门数量不同。
        """
        src = (REPO / '展示层' / 'api' / 'api_devices.py').read_text(encoding='utf-8')
        for forbidden in ('device_manager.stop_device(', 'device_manager.start_device('):
            assert forbidden not in src, (
                f'{forbidden} 绕过了安全层，应改走 device_control.batch_control')

    def test_adjust_does_not_call_manager_directly(self):
        """`/adjust` 同理 —— 而且 `adjust_device` 内部 `int()` 会静默截断 1.9→1。"""
        src = (REPO / '展示层' / 'api' / 'api_control.py').read_text(encoding='utf-8')
        assert 'device_manager.adjust_device(' not in src, (
            '/adjust 绕过了安全层（无急停/联锁/量程/审计/回读，且 int() 截断浮点）')

    def test_control_endpoints_503_when_no_safety_module(self):
        """安全模块缺失时必须 503，不能降级直写。"""
        src = (REPO / '展示层' / 'api' / 'api_devices.py').read_text(encoding='utf-8')
        assert '安全模块不可用' in src, (
            'device_control 不可用时应当明确拒绝，不能退回直接写设备')


# ============================================================
# 6. 启动鲁棒性
# ============================================================

class TestStartupRobustness:
    """一台设备配错不能让整个系统起不来。"""

    def test_illegal_byte_order_fails_fast_but_is_isolated(self):
        """非法 `byte_order` **构造时就抛**（fail-fast），但不会扩散到启动流程。

        两层各司其职：
        · 构造抛 ValueError —— 字序配错的后果是「所有 32 位值读成乱码」，
          而界面上的数字看起来完全正常（1.6e-38 也照样显示），
          静默退回 ABCD 等于把「配置错」变成「数据错」。
        · `RealDeviceManager.connect_all()` 逐台 try 隔离 ——
          一台配错只影响它自己，不会打断整个启动流程。
        """
        from 采集层.modbus_client import ModbusClient

        with pytest.raises(ValueError):
            ModbusClient({
                'id': 'bad', 'name': 'bad', 'protocol': 'modbus_tcp',
                'host': '127.0.0.1', 'port': 502, 'byte_order': 'XYZ',
            })

    def test_connect_all_isolates_exceptions(self):
        """`connect_all` 必须隔离单台异常。"""
        src = (REPO / '采集层' / 'real_device_manager.py').read_text(encoding='utf-8')
        assert '连接异常（已隔离' in src, (
            'connect_all 没有隔离单台设备异常，一台配错会打断整个启动流程')

    def test_rest_timeout_is_not_a_session_attribute(self):
        """REST 超时必须作为请求参数传，不能挂在 Session 上。

        `requests.Session` 没有 `timeout` 属性 —— 挂上去不生效，
        等于**所有 REST 请求无超时**，对端不响应时永久阻塞。
        """
        src = (REPO / '采集层' / 'rest_client.py').read_text(encoding='utf-8')
        code = '\n'.join(line.split('#', 1)[0] for line in src.splitlines())
        assert '_session.timeout' not in code, (
            'Session 上没有 timeout 属性，这行不生效；必须作为 timeout= 参数传给 request()')
        assert code.count('timeout=getattr(self') >= 3, (
            '所有 request 调用都必须带 timeout')

    def test_rest_timeout_resolution(self):
        """超时配置支持单值 / 二元组 / 字典，非法值退回默认。"""
        from 采集层.rest_client import RESTDeviceClient

        cases = [
            ({'timeout': 10}, (10.0, 10.0)),
            ({'timeout': [3, 10]}, (3.0, 10.0)),
            ({'timeout': {'connect': 2, 'read': 8}}, (2.0, 8.0)),
            ({'timeout': 'abc'}, (3.0, 5.0)),
            ({}, (3.0, 5.0)),
        ]
        for cfg, expect in cases:
            client = RESTDeviceClient.__new__(RESTDeviceClient)
            client.config = cfg
            client.device_name = 'probe'
            assert client._resolve_timeout() == expect, cfg


# ============================================================
# 7. 真实配置的完整性
# ============================================================

class TestRealConfigIntegrity:
    """`devices_real.yaml` 必须与模拟配置对齐，且不含明文凭据。"""

    @pytest.fixture(scope='class')
    def real_devices(self):
        if not REAL_CFG.exists():
            pytest.skip('没有 devices_real.yaml')
        return yaml.safe_load(REAL_CFG.read_text(encoding='utf-8')).get('devices') or []

    def test_ids_match_simulated(self, real_devices):
        """真实清单是模拟清单的**子集**：真机有的设备，模拟必须也有。

        对不上的后果：模拟验证过的告警规则/历史曲线/组态在真机上找不到设备，
        「模拟跑通了」这句话就不成立。

        round 351 之后**允许模拟多于真实**（129 vs 42）：扩出来的 89 台是
        `tools/gen_factory.py` 生成的演示产线，真机上并没有这些设备。
        但方向不能反 —— 真机清单里出现的每一个 id 都必须在模拟侧存在，
        否则那台设备的规则/曲线在模拟环境里无从验证。
        """
        if not SIM_CFG.exists() or not real_devices:
            pytest.skip('缺少配置')
        sim_ids = {d['id'] for d in yaml.safe_load(SIM_CFG.read_text(encoding='utf-8'))['devices']}
        real_ids = {d['id'] for d in real_devices}
        missing = sorted(real_ids - sim_ids)
        assert not missing, (
            f'真实设备在模拟清单里没有对应项（这些设备的规则无法在模拟环境验证）: {missing}')

    def test_no_plaintext_credentials(self, real_devices):
        """凭据只能是 `${VAR}`，不许明文。

        仓库会推送到远端；明文密码进 git 等于永久泄漏（历史里删不掉）。
        """
        import re
        suspicious = []
        for dev in real_devices:
            for key in ('password', 'auth_password', 'auth_token', 'api_key'):
                val = dev.get(key)
                if val and not re.fullmatch(r'\$\{[A-Z0-9_]+\}', str(val)):
                    suspicious.append(f'{dev["id"]}.{key}={val!r}')
        assert not suspicious, f'发现疑似明文凭据：{suspicious}'

    def test_modbus_devices_declare_byte_order(self, real_devices):
        """Modbus 设备必须显式声明 byte_order。

        默认 ABCD 只是"最常见"，不同厂家字序不同。不声明就会被默认值
        静默套用，现场表现为「温度读出来是 1.6e-38」。
        """
        missing = [d['id'] for d in real_devices
                   if d.get('protocol') in ('modbus_tcp', 'modbus_rtu')
                   and not d.get('byte_order')]
        assert not missing, f'这些 Modbus 设备没声明 byte_order: {missing}'

    def test_every_point_has_unit_and_name(self, real_devices):
        """每个点位必须有 name；工程单位点必须有 unit。

        没有 unit 的点位在界面上无法判断量纲，运维只能靠记忆。
        """
        bad = []
        for dev in real_devices:
            for section in ('registers', 'nodes', 'topics', 'endpoints'):
                for point in (dev.get(section) or []):
                    if not point.get('name'):
                        bad.append(f'{dev["id"]}.{section}: 缺 name')
                    elif section == 'registers' and point.get('data_type') in ('float32', 'float64') \
                            and not point.get('unit') \
                            and not _DIMENSIONLESS.search(str(point.get('name', ''))):
                        bad.append(f'{dev["id"]}.{point["name"]}: 浮点点位缺 unit')
        assert not bad, f'点位元数据不全（前 8 条）：{bad[:8]}'


class TestReloadConfigContract:
    """`reload_config` 必须是接口上的抽象方法，不能靠"记得补"。"""

    def test_interface_declares_reload_config_abstract(self):
        """漏实现要在**实例化时** TypeError，不是等到线上第一次热重载。

        原状：`reload_config` 只在 `DeviceManager` 上定义，而生产用的是
        `RealDeviceManager` / `SimulatedDeviceManager` ——
        `device_config_watch` 每 10 秒调一次，AttributeError 被记成
        「自动重载异常」，于是两种模式下热重载**从未生效**。
        """
        from 采集层.interfaces import IDeviceManager

        method = getattr(IDeviceManager, 'reload_config', None)
        assert method is not None, 'IDeviceManager 没有声明 reload_config'
        assert getattr(method, '__isabstractmethod__', False), (
            'reload_config 必须是 @abstractmethod，否则新增子类漏实现时不会报错')

    def test_both_managers_implement_it(self):
        """两个生产 manager 都必须实现（且返回契约结构）。"""
        from 采集层.real_device_manager import RealDeviceManager
        from 采集层.simulated_device_manager import SimulatedDeviceManager

        for cls in (RealDeviceManager, SimulatedDeviceManager):
            assert callable(getattr(cls, 'reload_config', None)), (
                f'{cls.__name__} 没有实现 reload_config')
            # 返回契约：必须有 rejected 键（None 表示成功）
            src = inspect.getsource(cls.reload_config)
            assert "'rejected'" in src, f'{cls.__name__}.reload_config 没返回 rejected 字段'

    def test_reload_rejects_empty_result(self):
        """重载成 0 台要回滚 —— 文件被清空不能让现场全停采集。"""
        from 采集层.simulated_device_manager import SimulatedDeviceManager

        mgr = SimulatedDeviceManager(config_path=str(SIM_CFG))
        before = len(mgr.devices)
        assert before > 0

        # 伪造 load_config 成功但清空设备表（模拟"文件被清空"）
        original = mgr.load_config
        def _wipe():
            mgr.devices = {}
            return True
        mgr.load_config = _wipe
        try:
            result = mgr.reload_config()
        finally:
            mgr.load_config = original

        assert result.get('rejected'), f'重载成 0 台应被拒绝，实际 {result}'
        assert len(mgr.devices) == before, '被拒绝时必须回滚到原配置'
