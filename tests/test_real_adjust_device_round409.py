"""真实设备管理器 `adjust_device` 的全分支验证。

背景：该模块行覆盖率只有 49%，`adjust_device`（现场最常被调用的写路径）
几乎没被执行过。这里用一个假客户端把**每种 data_type / 每种失败原因**都打一遍，
并断言真正下发的寄存器字（而不只是"返回 success=True"）。
"""

import yaml
import pytest

from 采集层.base_client import encode_float32_registers, encode_int32_registers
from 采集层.real_device_manager import RealDeviceManager


class FakeClient:
    def __init__(self, *, multi=True, fail=False, raise_on_multi=False):
        self.multi = multi
        self.fail = fail
        self.raise_on_multi = raise_on_multi
        self.calls = []          # (方法名, 地址, 值)

    def write_multiple_registers(self, address, values):
        self.calls.append(('multi', address, list(values)))
        if self.raise_on_multi:
            raise NotImplementedError
        return not self.fail

    def write_single_register(self, address, value):
        self.calls.append(('single', address, value))
        return not self.fail


def _dev(**kw):
    regs = [
        {'name': 'speed', 'address': 10, 'data_type': 'uint16'},
        {'name': 'temp_sp', 'address': 20, 'data_type': 'float32'},
        {'name': 'count_sp', 'address': 30, 'data_type': 'int32'},
        {'name': 'readonly', 'address': 40, 'data_type': 'uint16', 'access': 'ro'},
        {'name': 'bad_type', 'address': 50, 'data_type': 'float128'},
        {'name': 'no_addr', 'address': None, 'data_type': 'uint16'},
    ]
    dev = {'id': 'dev1', 'name': 'dev1', 'protocol': 'modbus_tcp',
           'host': '127.0.0.1', 'port': 502, 'byte_order': kwargs_order(kw),
           'registers': regs}
    dev.update({k: v for k, v in kw.items() if k != 'byte_order'})
    return dev


def kwargs_order(kw):
    return kw.get('byte_order', 'ABCD')


@pytest.fixture()
def manager(tmp_path):
    cfg = tmp_path / 'devices.yaml'
    cfg.write_text(yaml.safe_dump({'devices': [_dev(byte_order='CDAB')]},
                                  allow_unicode=True), encoding='utf-8')
    mgr = RealDeviceManager(config_path=str(cfg))
    return mgr


def _attach(manager, client):
    manager.clients['dev1'] = client
    return client


def test_unknown_device(manager):
    r = manager.adjust_device('nope', 'speed', 1)
    assert r['success'] is False and '不存在' in r['message']


def test_unknown_register(manager):
    _attach(manager, FakeClient())
    r = manager.adjust_device('dev1', 'nope', 1)
    assert r['success'] is False and '寄存器' in r['message']


def test_readonly_register_rejected(manager):
    _attach(manager, FakeClient())
    r = manager.adjust_device('dev1', 'readonly', 1)
    assert r['success'] is False and '只读' in r['message']


def test_no_client_available(manager, monkeypatch):
    """拿不到客户端对象 → 明确报"未连接"，不能假装写入成功。"""
    monkeypatch.setattr(manager, 'get_client', lambda _device_id: None)
    r = manager.adjust_device('dev1', 'speed', 1)
    assert r['success'] is False and '未连接' in r['message']


def test_client_exists_but_write_fails(manager):
    """客户端对象存在但底层未连接/写失败 → 报"写入失败"（真实行为）。"""
    _attach(manager, FakeClient(fail=True))
    r = manager.adjust_device('dev1', 'speed', 1)
    assert r['success'] is False and '写入失败' in r['message']


def test_invalid_address(manager):
    _attach(manager, FakeClient())
    r = manager.adjust_device('dev1', 'no_addr', 1)
    assert r['success'] is False and '地址非法' in r['message']


def test_float32_uses_byte_order(manager):
    client = _attach(manager, FakeClient())
    r = manager.adjust_device('dev1', 'temp_sp', 50.0)

    assert r['success'] is True
    method, address, values = client.calls[-1]
    assert (method, address) == ('multi', 20)
    assert values == encode_float32_registers(50.0, 'CDAB'), '浮点写入没按字节序编码'


def test_int32_writes_two_registers(manager):
    client = _attach(manager, FakeClient())
    r = manager.adjust_device('dev1', 'count_sp', 70000)

    assert r['success'] is True
    method, address, values = client.calls[-1]
    assert address == 30 and len(values) == 2
    assert values == encode_int32_registers(70000, 'CDAB', signed=True)


def test_uint16_masks_to_16_bits(manager):
    client = _attach(manager, FakeClient())
    r = manager.adjust_device('dev1', 'speed', -1)

    assert r['success'] is True
    assert client.calls[-1] == ('single', 10, 65535)


def test_unsupported_data_type(manager):
    client = _attach(manager, FakeClient())
    r = manager.adjust_device('dev1', 'bad_type', 1)

    assert r['success'] is False and '不支持' in r['message']
    assert client.calls == [], '不支持的 data_type 不能真的写下去'


def test_write_failure_reported(manager):
    _attach(manager, FakeClient(fail=True))
    r = manager.adjust_device('dev1', 'speed', 1)
    assert r['success'] is False and '写入失败' in r['message']


def test_fallback_when_multiple_registers_unsupported(manager):
    client = _attach(manager, FakeClient(raise_on_multi=True))
    r = manager.adjust_device('dev1', 'temp_sp', 1.5)

    assert r['success'] is True
    singles = [c for c in client.calls if c[0] == 'single']
    assert len(singles) == 2, '多寄存器不支持时应逐字写'
    assert [c[1] for c in singles] == [20, 21]


def test_client_exception_is_reported(manager):
    class Boom(FakeClient):
        def write_single_register(self, address, value):
            raise RuntimeError('总线错误')

    _attach(manager, Boom())
    r = manager.adjust_device('dev1', 'speed', 1)
    assert r['success'] is False and '总线错误' in r['message']
