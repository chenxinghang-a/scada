"""同族路径一致性守卫。

本轮缺陷复发的头号模式是「修了一份实现，漏了另一份」。这里用**同一份用例**
同时打两条路径，任一条退化都会红：

* modbus：`read_holding_registers` / `read_input_registers` 的短应答口径
* 模拟客户端：`simulated_client` / `enhanced_simulated_client` 的 64 位读长度
"""

from types import SimpleNamespace

import pytest

from 采集层.base_client import decode_float64_registers
from 采集层.enhanced_simulated_client import EnhancedSimulatedModbusClient
from 采集层.modbus_client import ModbusClient
from 采集层.simulated_client import SimulatedModbusClient


# ------------------------------------------------------------------ modbus
class _ShortResult:
    """模拟 pymodbus 的「成功但寄存器数不足」响应。"""

    def isError(self):
        return False

    def __init__(self, n):
        self.registers = [1] * n


def _bare_modbus(n_registers):
    client = object.__new__(ModbusClient)
    client.connected = True
    client.stats = {'total_reads': 0, 'failed_reads': 0, 'successful_reads': 0,
                    'last_error': None, 'last_read_time': None}
    client._consecutive_failures = 0
    client._stats_lock = __import__('threading').Lock()
    client.last_read_source = 'none'
    client.last_read_ok = False
    client._last_good_values = {}
    client._last_good_at = {}
    client._comm_log = []
    client._log_lock = __import__('threading').Lock()
    client.device_name = 'stub'
    client.device_id = 'stub'
    client.byte_order = 'ABCD'
    client._reconnect_lock = __import__('threading').Lock()
    client._resolve_slave = lambda slave_id: 1
    client.validate_address_range = lambda *a, **k: None
    client._inc_stat = lambda key, amount=1: client.stats.__setitem__(
        key, client.stats.get(key, 0) + amount)
    client._log_operation = lambda *a, **k: None
    client.client = SimpleNamespace(
        read_holding_registers=lambda **k: _ShortResult(n_registers),
        read_input_registers=lambda **k: _ShortResult(n_registers),
    )
    return client


@pytest.mark.parametrize('method', ['read_holding_registers', 'read_input_registers'])
def test_modbus_short_response_is_failure_on_both_paths(method):
    client = _bare_modbus(n_registers=1)   # 请求 4 个，只回 1 个
    result = getattr(client, method)(0, 4)

    assert result is None, f'{method} 把短应答当成功返回了: {result!r}'
    assert client.stats['failed_reads'] >= 1
    assert client.last_read_source in ('none', 'cache')
    assert client.stats['successful_reads'] == 0


# ------------------------------------------------------------ simulated 64bit
def _sim_config():
    return {
        'id': 'sim64', 'name': 'sim64', 'byte_order': 'ABCD',
        'registers': [{'address': 0, 'name': 't64', 'data_type': 'float64',
                       'scale': 1.0, 'offset': 0.0}],
    }


@pytest.mark.parametrize('factory', [SimulatedModbusClient, EnhancedSimulatedModbusClient])
def test_simulated_float64_read_is_full_width(factory):
    client = factory(_sim_config())
    client.connected = True          # 不启线程，只走读路径
    regs = client.read_holding_registers(0, 4)

    assert regs is not None
    assert len(regs) == 4, f'{factory.__name__} 的 float64 只回了 {len(regs)} 个字'
    value = decode_float64_registers(regs, 'ABCD')
    # 不再是「float32 + 补零」拼出来的假值：必须是有限且与模型量级一致
    assert value == value                     # 非 NaN
    assert abs(value) < 1e6
