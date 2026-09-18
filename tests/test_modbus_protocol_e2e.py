"""
Modbus 协议真实 E2E 测试
=======================
用 pymodbus.client.ModbusTcpClient 连接本机起的标准 Modbus TCP 模拟器，
覆盖连接、动态数据、float32 解码、写后读回、越界异常、断连重连、slave 隔离。

测试自管理生命周期：subprocess 起 tools/modbus_simulator.py + TCP 就绪探测，
fixture 内 teardown（terminate + kill），不留僵尸进程/占用端口。
Windows (CI windows-latest) 兼容：用 sys.executable 起同解释器，非特权非常用端口。
"""

import sys
import os
import time
import socket
import subprocess
import yaml

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIMULATOR = os.path.join(REPO_ROOT, "tools", "modbus_simulator.py")
CONFIG_PATH = os.path.join(REPO_ROOT, "配置", "devices_modbus_sim.yaml")

# 复用模拟器的 float32 编解码，保证与编码字节序一致
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
try:
    import modbus_simulator as ms  # noqa: E402
except Exception:  # pragma: no cover
    ms = None

PORT = int(os.environ.get("MODBUS_SIM_PORT", "15020"))
HOST = "127.0.0.1"

try:
    from pymodbus.client import ModbusTcpClient
    HAVE_PYMODBUS = True
except Exception:  # pragma: no cover
    HAVE_PYMODBUS = False


def _load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    devices = [d for d in cfg.get("devices", []) if d.get("protocol") == "modbus_tcp"]
    byte_order = str(cfg.get("byte_order", "ABCD")).upper()
    return devices, byte_order


def _wait_ready(port, timeout=30.0):
    """TCP 就绪探测：直到端口可连或超时"""
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            with socket.create_connection((HOST, port), timeout=0.5):
                return True
        except OSError as e:
            last_err = e
            time.sleep(0.25)
    raise RuntimeError(f"模拟器未在 {timeout}s 内就绪: {last_err}")


@pytest.fixture(scope="module")
def simulator():
    """起模拟器子进程，返回监听端口；测试结束可靠 teardown"""
    if not HAVE_PYMODBUS:
        pytest.skip("pymodbus 不可用")
    if not os.path.exists(SIMULATOR):
        pytest.skip(f"找不到模拟器: {SIMULATOR}")

    proc = subprocess.Popen(
        [sys.executable, SIMULATOR, "--host", HOST, "--port", str(PORT), "--interval", "1.0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_ready(PORT, timeout=30.0)
        yield PORT
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
        # 端口释放兜底
        time.sleep(0.3)


def _client(port=PORT):
    c = ModbusTcpClient(HOST, port=port)
    assert c.connect(), "无法连接模拟器"
    return c


# ----------------------------------------------------------------
# 1. 连接成功（抽样 3 台不同 slave_id）
# ----------------------------------------------------------------

def test_connect_sample_slaves(simulator):
    devices, _ = _load_config()
    slave_ids = [int(d["slave_id"]) for d in devices]
    sample = [slave_ids[0], slave_ids[len(slave_ids) // 2], slave_ids[-1]]
    assert len(set(sample)) >= 3, "设备不足 3 台，无法抽样"
    for sid in sample:
        c = _client(simulator)
        try:
            assert c.is_socket_open(), f"slave {sid} 未连接"
        finally:
            c.close()


# ----------------------------------------------------------------
# 2. 保持寄存器动态变化（连续两次读取值不同，反驳"能启动但数据不变"）
# ----------------------------------------------------------------

def test_holding_register_dynamic(simulator):
    # 选一台纯 float 设备（schneider_m340_01, slave_id=5），且不被其它用例写入
    devs, bo = _load_config()
    target = next(d for d in devs if d["id"] == "schneider_m340_01")
    sid = int(target["slave_id"])
    c = _client(simulator)
    try:
        r1 = c.read_holding_registers(address=0, count=2, slave=sid)
        assert not r1.isError(), r1
        v1 = ms.unpack_float32(r1.registers, bo)
        time.sleep(1.5)  # 至少跨一个模型更新周期
        r2 = c.read_holding_registers(address=0, count=2, slave=sid)
        assert not r2.isError(), r2
        v2 = ms.unpack_float32(r2.registers, bo)
        assert v1 != v2, f"两次读数相同({v1})，数据未动态变化"
    finally:
        c.close()


# ----------------------------------------------------------------
# 3. float32 解码正确（按配置字节序，落在物理模型合理区间）
# ----------------------------------------------------------------

def test_float32_decode(simulator):
    # 把解码逻辑复用模拟器模块，保证与编码字节序一致
    sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
    import modbus_simulator as ms  # noqa: E402
    devs, bo = _load_config()
    target = next(d for d in devs if d["id"] == "schneider_m340_01")
    sid = int(target["slave_id"])
    c = _client(simulator)
    try:
        rr = c.read_holding_registers(address=0, count=2, slave=sid)
        assert not rr.isError(), rr
        decoded = ms.unpack_float32(rr.registers, bo)
        # inlet_flow: base=50, amp=15 → 合理区间 [10, 100]
        assert 10.0 <= decoded <= 100.0, f"解码值越界: {decoded}"
    finally:
        c.close()


# ----------------------------------------------------------------
# 4. 写单个保持寄存器 → 立即读回一致
# ----------------------------------------------------------------

def test_write_single_readback(simulator):
    # relay_output_01 全部 rw (slave_id=9)
    devs, _ = _load_config()
    target = next(d for d in devs if d["id"] == "relay_output_01")
    sid = int(target["slave_id"])
    c = _client(simulator)
    try:
        wv = 12345
        wr = c.write_register(address=0, value=wv, slave=sid)
        assert not wr.isError(), wr
        rr = c.read_holding_registers(address=0, count=1, slave=sid)
        assert not rr.isError(), rr
        assert rr.registers[0] == wv, f"写回不一致: {rr.registers[0]} != {wv}"
    finally:
        c.close()


# ----------------------------------------------------------------
# 5. 写多个寄存器 → 读回一致
# ----------------------------------------------------------------

def test_write_multiple_readback(simulator):
    devs, _ = _load_config()
    target = next(d for d in devs if d["id"] == "relay_output_01")
    sid = int(target["slave_id"])
    c = _client(simulator)
    try:
        vals = [111, 222, 333, 444]
        wr = c.write_registers(address=0, values=vals, slave=sid)
        assert not wr.isError(), wr
        rr = c.read_holding_registers(address=0, count=len(vals), slave=sid)
        assert not rr.isError(), rr
        assert rr.registers == vals, f"批量写回不一致: {rr.registers} != {vals}"
    finally:
        c.close()


# ----------------------------------------------------------------
# 6. 越界/未定义地址 → 异常响应（不得静默返回 0）
# ----------------------------------------------------------------

def test_out_of_range_error(simulator):
    devs, _ = _load_config()
    sid = int(devs[0]["slave_id"])
    c = _client(simulator)
    try:
        rr = c.read_holding_registers(address=10000, count=1, slave=sid)
        assert rr.isError(), f"越界读应返回异常, 实际: {getattr(rr, 'registers', None)}"
        wr = c.write_register(address=10000, value=1, slave=sid)
        assert wr.isError(), "越界写应返回异常"
    finally:
        c.close()


# ----------------------------------------------------------------
# 7. 断连后重连成功并继续读到数据
# ----------------------------------------------------------------

def test_disconnect_reconnect(simulator):
    devs, _ = _load_config()
    sid = int(devs[0]["slave_id"])
    c1 = _client(simulator)
    try:
        r1 = c1.read_holding_registers(address=0, count=2, slave=sid)
        assert not r1.isError(), r1
    finally:
        c1.close()

    c2 = _client(simulator)  # 全新连接
    try:
        r2 = c2.read_holding_registers(address=0, count=2, slave=sid)
        assert not r2.isError(), r2
        assert len(r2.registers) == 2
    finally:
        c2.close()


# ----------------------------------------------------------------
# 8. slave_id 隔离：向某 slave 写入不影响另一 slave 读取
# ----------------------------------------------------------------

def test_slave_isolation(simulator):
    devs, _ = _load_config()
    sids = [int(d["slave_id"]) for d in devs]
    writer = next(d for d in devs if d["id"] == "relay_output_01")  # rw, slave 9
    other = devs[0]  # slave 1
    w_sid = int(writer["slave_id"])
    o_sid = int(other["slave_id"])
    assert w_sid != o_sid
    c = _client(simulator)
    try:
        # 向 writer(slave9) 写入特征值，读 other(slave1) 的同一地址，应不相等
        sentinel = 9999
        wr = c.write_register(address=0, value=sentinel, slave=w_sid)
        assert not wr.isError(), wr
        ro = c.read_holding_registers(address=0, count=1, slave=o_sid)
        assert not ro.isError(), ro
        assert ro.registers[0] != sentinel, (
            f"slave 隔离失败: slave{o_sid} 读到了写入 slave{w_sid} 的值 {sentinel}")

        # 反向：向 other(slave1) 写特征值，读另一台(slave2) 同一地址，应不相等
        s2 = sids[1]
        if s2 != o_sid:
            sentinel2 = 54321
            wr2 = c.write_register(address=0, value=sentinel2, slave=o_sid)
            assert not wr2.isError(), wr2
            r2 = c.read_holding_registers(address=0, count=1, slave=s2)
            assert not r2.isError(), r2
            assert r2.registers[0] != sentinel2, "slave 隔离失败(反向)"
    finally:
        c.close()


# ----------------------------------------------------------------
# 9. 线圈/离散输入块已初始化（只读请求有响应而非异常，验证 A.6）
# ----------------------------------------------------------------

def test_coil_and_discrete_respond(simulator):
    devs, _ = _load_config()
    sid = int(devs[0]["slave_id"])
    c = _client(simulator)
    try:
        rc = c.read_coils(address=0, count=1, slave=sid)
        assert not rc.isError(), f"读线圈应成功: {rc}"
        rd = c.read_discrete_inputs(address=0, count=1, slave=sid)
        assert not rd.isError(), f"读离散输入应成功: {rd}"
    finally:
        c.close()


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
