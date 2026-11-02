#!/usr/bin/env python
"""真实链路自测：用真 Modbus 从站验证「真实设备路径」能不能用。

## 为什么需要这个工具

项目有两套路径：模拟（`SimulatedModbusClient`）和真实（`ModbusClient`）。
模拟路径天天跑测试，真实路径**只有到现场才知道行不行** —— 而现场出问题
的代价最高（要出差、要停机窗口）。

本工具在本机起一个**真 Modbus TCP 从站**（pymodbus），让项目自己的
`ModbusClient` 连上去，逐项验证真实链路：

1. 连接 / 断开
2. 读 uint16
3. 读 float32 —— **四种字节序逐一验证**（现场最常见的错值来源）
4. 写单寄存器 + 回读
5. 写线圈 + 读线圈
6. **断线后的降级**：从站进程被杀 → 读应返回缓存值且
   `last_read_source == 'cache'`（不是静默返回旧值当新值）
7. 恢复：从站重启 → 重连 → 数据恢复新鲜

## 用法

    python tools/real_path_selftest.py            # 全部字节序
    python tools/real_path_selftest.py --order CDAB
    python tools/real_path_selftest.py --port 15021

退出码：0 全过 / 1 有失败。
"""

from __future__ import annotations

import argparse
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ORDERS = ('ABCD', 'BADC', 'CDAB', 'DCBA')


# ============================================================
# 从站侧（子进程）
# ============================================================

def _encode_float32(value: float, order: str) -> list[int]:
    """把 float32 编成两个寄存器（与 ModbusClient 的 byte_order 语义一致）。"""
    raw = struct.pack('>f', float(value))
    hi = struct.unpack('>H', raw[0:2])[0]
    lo = struct.unpack('>H', raw[2:4])[0]
    if order == 'CDAB':
        return [lo, hi]
    if order == 'BADC':
        return [((hi & 0xFF) << 8) | (hi >> 8), ((lo & 0xFF) << 8) | (lo >> 8)]
    if order == 'DCBA':
        b = list(raw)
        return [((b[3] << 8) | b[2]), ((b[1] << 8) | b[0])]
    return [hi, lo]


def run_slave(port: int, order: str) -> None:
    """子进程入口：起一个 Modbus TCP 从站，预置可校验的数据。"""
    from pymodbus.datastore import (
        ModbusSequentialDataBlock,
        ModbusServerContext,
        ModbusSlaveContext,
    )
    from pymodbus.server import StartTcpServer

    hr = [0] * 200
    co = [0] * 200

    # 地址 0：uint16 固定值
    hr[0] = 4321
    # 地址 2-3：float32 固定值（按指定字节序摆放）
    v = 1234.5678
    hr[2], hr[3] = _encode_float32(v, order)
    # 地址 4-5：另一个 float32，用于验证多值不串位
    v2 = -12.5
    hr[4], hr[5] = _encode_float32(v2, order)

    # ⚠️ pymodbus 3.9 的 ModbusSlaveContext.getValues() 里有硬编码的
    # `address += 1`（即协议地址 0 → 数据块地址 1）。为了让「协议地址 N」
    # 精确对应 hr[N]，在数据块前面垫一个占位元素。
    # 另外它的 __init__ 写成了 `co if di is not None else ...` —— 不传 di
    # 时 co 会被**静默丢弃**，所以 di 必须显式给（哪怕是空的）。
    store = ModbusSlaveContext(
        di=ModbusSequentialDataBlock(0, [0] * 201),
        hr=ModbusSequentialDataBlock(0, [0] + hr),
        co=ModbusSequentialDataBlock(0, [0] + co),
    )
    ctx = ModbusServerContext(slaves=store, single=True)
    print(f'[slave] listening 127.0.0.1:{port} order={order}', flush=True)
    StartTcpServer(context=ctx, address=('127.0.0.1', port))


# ============================================================
# 测试侧
# ============================================================

def _wait_port(port: int, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.15)
    return False


class Reporter:
    def __init__(self):
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = '') -> bool:
        self.rows.append((name, ok, detail))
        mark = 'PASS' if ok else 'FAIL'
        print(f'  [{mark}] {name}' + (f'  — {detail}' if detail else ''))
        return ok

    def summary(self) -> int:
        bad = [r for r in self.rows if not r[1]]
        print(f'\n合计 {len(self.rows)} 项，失败 {len(bad)} 项')
        if bad:
            print('失败项：')
            for name, _, detail in bad:
                print(f'  - {name}: {detail}')
            return 1
        return 0


def run_tests(port: int, order: str) -> int:
    from 采集层.modbus_client import ModbusClient

    rep = Reporter()
    # ⚠️ registers 必须声明 —— 写入安全校验会拒绝「不在设备配置里的地址」
    # （`WriteSafetyValidator`：地址没登记就无法做量程/联锁校验，fail-closed）。
    # 这不是测试的绕行，而是现场必须遵守的规则：真实设备清单里每个可写点位
    # 都要登记，否则写入一律被拒。
    cfg = {
        'id': 'selftest_plc', 'name': '自测PLC', 'protocol': 'modbus_tcp',
        'host': '127.0.0.1', 'port': port, 'slave_id': 1, 'byte_order': order,
        'registers': [
            {'address': 0, 'name': 'r0', 'data_type': 'uint16', 'access': 'r'},
            {'address': 2, 'name': 'f2', 'data_type': 'float32', 'access': 'r'},
            {'address': 4, 'name': 'f4', 'data_type': 'float32', 'access': 'r'},
            {'address': 10, 'name': 'rw10', 'data_type': 'uint16', 'access': 'rw',
             'write_limits': {'min': 0, 'max': 65535}},
        ],
    }
    client = ModbusClient(cfg)

    # --- 1. 连接 ---
    ok = client.connect()
    rep.check('连接建立', ok)
    if not ok:
        return rep.summary()

    # --- 2. 读 uint16 ---
    raw = client.read_holding_registers(0, 1)
    rep.check('读 uint16 @0', raw == [4321], f'got={raw} expect=[4321]')

    # --- 3. 读 float32（两种值，验证字节序）---
    for addr, expect in ((2, 1234.5678), (4, -12.5)):
        raw = client.read_holding_registers(addr, 2)
        got = client.decode_float32(raw) if raw else None
        ok = got is not None and abs(got - expect) < 1e-3
        rep.check(f'读 float32 @{addr} (order={order})', ok,
                  f'got={got!r} expect={expect!r} raw={raw}')

    # --- 4. 写寄存器 + 回读 ---
    wr = client.write_single_register(10, 42)
    back = client.read_holding_registers(10, 1)
    rep.check('写寄存器 @10=42 并回读', bool(wr) and back == [42],
              f'write={wr} readback={back}')

    # --- 5. 写线圈 + 读回 ---
    wc = client.write_single_coil(0, True)
    coils = client.read_coils(0, 1)
    rep.check('写线圈 @0=True 并读回', bool(wc) and coils == [True],
              f'write={wc} readback={coils}')

    # --- 6. 断线降级 ---
    print('\n  -- 杀掉从站进程，验证断线降级 --')
    global _SLAVE_PROC
    if _SLAVE_PROC is not None:
        _SLAVE_PROC.kill()
        _SLAVE_PROC.wait(timeout=5)
        _SLAVE_PROC = None
    time.sleep(0.4)

    raw = client.read_holding_registers(0, 1)
    src = getattr(client, 'last_read_source', '<无此属性>')
    rep.check('断线后不返回 None（缓存兜底）', raw is not None, f'raw={raw}')
    rep.check("断线后 last_read_source == 'cache'", src == 'cache',
              f'got={src!r} —— 调用方必须能区分新值与旧值')
    age_getter = getattr(client, 'get_cache_age', None)
    age = age_getter(0, 1) if age_getter else None
    rep.check('能查到缓存年龄（多旧）', age is not None, f'age={age}')

    # --- 7. 恢复 ---
    print('\n  -- 重启从站，验证重连 --')
    _SLAVE_PROC = _start_slave(port, order)
    up = _wait_port(port, timeout=10)
    rep.check('从站重新监听', up)
    if up:
        client.disconnect()
        reconnected = client.connect()
        rep.check('重连成功', reconnected)
        if reconnected:
            raw = client.read_holding_registers(0, 1)
            src = getattr(client, 'last_read_source', '<无此属性>')
            rep.check('重连后数据恢复新鲜', raw == [4321] and src == 'fresh',
                      f'raw={raw} source={src!r}')

    client.disconnect()
    return rep.summary()


_SLAVE_PROC: subprocess.Popen | None = None


def _start_slave(port: int, order: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), '--slave',
         '--port', str(port), '--order', order],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description='真实链路自测（真 Modbus 从站）')
    ap.add_argument('--port', type=int, default=15020)
    ap.add_argument('--order', choices=ORDERS, help='只测某一种字节序（默认全测）')
    ap.add_argument('--slave', action='store_true', help=argparse.SUPPRESS)
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        pass

    if args.slave:
        run_slave(args.port, args.order or 'ABCD')
        return 0

    global _SLAVE_PROC
    orders = (args.order,) if args.order else ORDERS
    rc = 0

    for idx, order in enumerate(orders):
        port = args.port + idx
        print(f'\n=== 字节序 {order}（端口 {port}）===')
        _SLAVE_PROC = _start_slave(port, order)
        if not _wait_port(port):
            print(f'  [FAIL] 从站启动超时（端口 {port}）')
            rc = 1
            continue
        try:
            rc |= run_tests(port, order)
        finally:
            if _SLAVE_PROC is not None:
                _SLAVE_PROC.kill()
                try:
                    _SLAVE_PROC.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                _SLAVE_PROC = None

    print('\n' + ('✅ 真实链路自测通过' if rc == 0 else '❌ 真实链路自测有失败项'))
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
