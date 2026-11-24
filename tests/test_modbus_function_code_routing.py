"""round 391（Q9）：同一从站上输出线圈与输入点复用同一批地址。

现场缺陷
--------
``relay_output_01`` 的 6 路输出（relay_1..6）与 6 路输入（di_1..6）在
``配置/devices.yaml`` 里都写成地址 0~5。采集层过去只调
``read_holding_registers``（FC03）连续读，不区分功能码：

  把 relay_1 置 1 → 用同一块地址读 di_1 也得到 1
  → 系统把「输出已动作」当成「停机反馈已触发」（假反馈）
  → 联锁跟着误动作。

Modbus 协议里四类地址空间本来就是彼此独立的：FC01 线圈 / FC02 离散输入 /
FC03 保持寄存器 / FC04 输入寄存器。本文件钉死修复后的契约：

  1. 点位可配 ``function_code``（缺省 3，存量配置行为不变）；
  2. 采集层分组键 = ``(slave_id, function_code)``，分别用 01/02/03/04 读；
  3. 仿真客户端的线圈与离散输入是两套独立状态，写线圈不影响 di。

验收判据（题目原文）：向 relay_1 写 1 后读 di_1 必须仍是 0；
只有离散输入区地址 0 置 1 时 di_1 才读 1。
"""

import threading
from datetime import datetime
from pathlib import Path

import yaml

from 采集层.data_collector import DataCollector, _resolve_function_code
from 采集层.modbus_client import ModbusClient, normalize_function_code
from 采集层.simulated_client import SimulatedModbusClient

REPO_ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# 底层 pymodbus 替身：四块**独立**地址空间
# ============================================================

class _Ok:
    def isError(self):
        return False


class _Regs(_Ok):
    def __init__(self, registers):
        self.registers = list(registers)


class _Bits(_Ok):
    def __init__(self, bits):
        self.bits = [bool(b) for b in bits]


class _IndependentSlave:
    """一个从站，线圈 / 离散输入 / 保持 / 输入 四块空间互不相通。"""

    def __init__(self):
        self.coils: dict[int, bool] = {}
        self.discrete_inputs: dict[int, bool] = {}
        self.holding: dict[int, int] = {}
        self.input_regs: dict[int, int] = {}
        self.calls: list[tuple] = []

    def _bit_space(self, space, address, count):
        return _Bits([space.get(a, False) for a in range(address, address + count)])

    def read_coils(self, address, count, slave=None):
        self.calls.append(('fc01', address, count))
        return self._bit_space(self.coils, address, count)

    def read_discrete_inputs(self, address, count, slave=None):
        self.calls.append(('fc02', address, count))
        return self._bit_space(self.discrete_inputs, address, count)

    def read_holding_registers(self, address, count, slave=None):
        self.calls.append(('fc03', address, count))
        return _Regs([self.holding.get(a, 0)
                      for a in range(address, address + count)])

    def read_input_registers(self, address, count, slave=None):
        self.calls.append(('fc04', address, count))
        return _Regs([self.input_regs.get(a, 0)
                      for a in range(address, address + count)])

    def write_coil(self, address, value, slave=None):
        self.coils[address] = bool(value)
        return _Ok()

    def write_register(self, address, value, slave=None):
        self.holding[address] = value
        return _Ok()


def _make_client():
    client = ModbusClient({
        'id': 'relay_output_q9', 'name': 'Q9复用地址设备',
        'protocol': 'modbus_tcp', 'host': '127.0.0.1', 'port': 502,
        'slave_id': 1,
        # 写入安全档案从 registers 构建；relay 必须登记成 rw 才能通过写入校验
        # （di 与 relay 同地址，只登记 relay，否则只读档案会覆盖同地址的 rw 档案）
        'registers': [
            {'name': f'relay_{i}', 'address': i - 1, 'data_type': 'uint16',
             'access': 'rw', 'function_code': 1} for i in range(1, 7)
        ],
    })
    slave = _IndependentSlave()
    client.client = slave
    client.connected = True
    return client, slave


def _make_collector():
    """只装配 `_collect_modbus` 需要的属性（不跑 __init__，不连设备/建池）。"""
    collector = DataCollector.__new__(DataCollector)
    collector.stats = {}
    collector._stats_lock = threading.Lock()
    captured: list[dict] = []
    collector._enqueue_drop_oldest = lambda item: (captured.append(item), True)[1]
    return collector, captured


def _device_config(with_function_code: bool = True):
    registers = []
    for i in range(1, 7):
        reg = {'name': f'relay_{i}', 'address': i - 1,
               'data_type': 'uint16', 'access': 'rw'}
        if with_function_code:
            reg['function_code'] = 1
        registers.append(reg)
    for i in range(1, 7):
        reg = {'name': f'di_{i}', 'address': i - 1,
               'data_type': 'uint16', 'access': 'r'}
        if with_function_code:
            reg['function_code'] = 2
        registers.append(reg)
    return {'protocol': 'modbus_tcp', 'slave_id': 1, 'registers': registers}


# ============================================================
# 1. 客户端层：FC01 写不影响 FC02 读
# ============================================================

def test_write_relay_is_invisible_to_discrete_input():
    client, slave = _make_client()

    assert client.write_single_coil(0, True) is True
    assert client.read_by_function_code(0, 1, function_code=1) == [1]
    # 关键判据：同一从站、同一地址，输入区仍是 0
    assert client.read_by_function_code(0, 1, function_code=2) == [0]

    # 只有离散输入区本身动作，di_1 才读 1
    slave.discrete_inputs[0] = True
    assert client.read_by_function_code(0, 1, function_code=2) == [1]
    # 输出侧不受输入区影响
    assert client.read_by_function_code(0, 1, function_code=1) == [1]


def test_read_by_function_code_rejects_invalid_code():
    client, _ = _make_client()
    assert client.read_by_function_code(0, 1, function_code=5) is None
    assert client.read_by_function_code(0, 1, function_code='fc99') is None
    assert normalize_function_code(5) is None


def test_function_code_aliases_and_default():
    assert normalize_function_code(None) == 3
    assert normalize_function_code('') == 3
    assert normalize_function_code(1) == 1
    assert normalize_function_code('di') == 2
    assert normalize_function_code('discrete_input') == 2
    assert normalize_function_code('holding') == 3
    assert normalize_function_code('input_register') == 4
    assert _resolve_function_code(None) == 3
    assert _resolve_function_code('coil') == 1
    assert _resolve_function_code(9) is None


# ============================================================
# 2. 采集层：按 (slave, function_code) 分组读取
# ============================================================

def test_collector_routes_relay_and_di_to_different_function_codes():
    client, slave = _make_client()
    client.write_single_coil(0, True)   # relay_1 = 1
    collector, captured = _make_collector()

    ok = collector._collect_modbus(
        client, 'relay_output_q9', _device_config(), datetime.now())
    assert ok is True

    values = {item['register_name']: item['value'] for item in captured}
    assert values['relay_1'] == 1, '输出置位后本路应读到 1'
    assert values['di_1'] == 0, (
        'relay_1 写 1 后 di_1 必须仍是 0 —— 采集层又把输出状态当成输入反馈了')
    assert values['relay_2'] == 0 and values['di_2'] == 0

    methods = {call[0] for call in slave.calls}
    assert 'fc01' in methods, 'relay 组应走 FC01（read_coils）'
    assert 'fc02' in methods, 'di 组应走 FC02（read_discrete_inputs）'
    assert 'fc03' not in methods, '配置了功能码就不该再走默认 FC03'

    # 现场输入动作后，同一套配置读到的 di_1 变 1，输出不受影响
    slave.discrete_inputs[0] = True
    captured.clear()
    slave.calls.clear()
    assert collector._collect_modbus(
        client, 'relay_output_q9', _device_config(), datetime.now()) is True
    values = {item['register_name']: item['value'] for item in captured}
    assert values['di_1'] == 1
    assert values['relay_1'] == 1


def test_collector_defaults_to_holding_registers_without_function_code():
    """存量配置（无 function_code）必须仍走 FC03，行为不变。"""
    client, slave = _make_client()
    slave.holding = {0: 7, 1: 8}
    collector, captured = _make_collector()

    ok = collector._collect_modbus(
        client, 'legacy', _device_config(with_function_code=False), datetime.now())

    assert ok is True
    values = {item['register_name']: item['value'] for item in captured}
    assert values['relay_1'] == 7
    assert values['di_1'] == 7
    assert {call[0] for call in slave.calls} == {'fc03'}


def test_invalid_function_code_point_is_dropped_and_counted():
    client, _ = _make_client()
    collector, captured = _make_collector()
    cfg = {'protocol': 'modbus_tcp', 'registers': [
        {'name': 'bad', 'address': 0, 'data_type': 'uint16', 'function_code': 9}]}

    ok = collector._collect_modbus(client, 'bad_fc', cfg, datetime.now())

    assert ok is False, '功能码非法的点位不能"猜一个功能码"读'
    assert captured == []
    assert collector.stats['dropped_invalid_function_code'] == 1


# ============================================================
# 3. 配置：devices.yaml 里 di_1..di_6 必须是 FC02
# ============================================================

def test_devices_yaml_marks_di_as_discrete_inputs():
    with open(REPO_ROOT / '配置' / 'devices.yaml', encoding='utf-8') as f:
        cfg = yaml.safe_load(f)
    device = next(d for d in cfg['devices'] if d['id'] == 'relay_output_01')
    by_name = {r['name']: r for r in device['registers']}

    for i in range(1, 7):
        di = by_name[f'di_{i}']
        assert _resolve_function_code(
            di.get('function_code', di.get('register_type'))) == 2, (
            f'di_{i} 必须标成 function_code: 2（离散输入），否则会读回输出状态')
        relay = by_name[f'relay_{i}']
        assert _resolve_function_code(
            relay.get('function_code', relay.get('register_type'))) == 3


# ============================================================
# 4. 仿真客户端：线圈与离散输入是两套独立状态
# ============================================================

def _make_simulated():
    client = SimulatedModbusClient({
        'id': 'sim_q9', 'name': '仿真Q9', 'protocol': 'modbus_tcp',
        'host': '127.0.0.1', 'port': 502,
        'registers': [
            {'name': f'relay_{i}', 'address': i - 1, 'data_type': 'uint16',
             'function_code': 1} for i in range(1, 7)
        ] + [
            {'name': f'di_{i}', 'address': i - 1, 'data_type': 'uint16',
             'function_code': 2} for i in range(1, 7)
        ],
    })
    client.connect()
    return client


def test_simulated_client_keeps_coils_and_discrete_inputs_separate():
    client = _make_simulated()

    assert client.write_single_coil(0, True) is True
    assert client.read_coils(0, 1) == [True]
    assert client.read_discrete_inputs(0, 1) == [False], (
        '仿真层写线圈后同地址离散输入读到 1 —— 两块内存又共用了')

    client.set_discrete_input(0, True)
    assert client.read_discrete_inputs(0, 1) == [True]
    assert client.read_discrete_inputs(1, 1) == [False]
