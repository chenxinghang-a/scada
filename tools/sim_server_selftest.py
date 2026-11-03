#!/usr/bin/env python
"""模拟服务端协议自测：真 Modbus TCP 从站 ↔ 项目自己的 ModbusClient。

## 这个脚本证明什么

模拟模式最大的问题不是"数据不好看"，而是**没有任何协议报文经过** ——
客户端怎么编码地址、怎么摆字节序、写进去的值有没有真的到达行为模拟器，
全都只有到现场才知道。本脚本在本机起一套真的从站池（``模拟服务端``），
然后用**采集层生产代码里的** ``ModbusClient`` 连上去，逐项核对：

1. 从站数量 / 端口分配与设备清单一致
2. uint16 点位：读回的原始寄存器 × scale == hub.snapshot() 的工程值
3. **float32 点位：解码后与 snapshot 一致** —— 这是字节序正确性的证明
4. uint32 点位：同上（32 位整数的寄存器组合顺序）
5. 未登记地址：读返回 0（现场从站行为），写返回异常码 0x02
6. 写寄存器：主站写入 → 行为模拟器收到（``last_written_value``）
7. 写线圈：主站写入 → 线圈读回 + snapshot 里的继电器输出跟着变

## 用法

    .venv/Scripts/python.exe tools/sim_server_selftest.py
    .venv/Scripts/python.exe tools/sim_server_selftest.py --base-port 16020

退出码：0 全过 / 1 有失败 / 2 环境准备失败。
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.safe_numeric import try_finite_float  # noqa: E402 - 必须先插入 ROOT


class Reporter:
    """逐项记录 PASS/FAIL，最后统一汇总（失败项要能一眼看到）。"""

    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = '') -> bool:
        self.rows.append((name, ok, detail))
        print(f'  [{"PASS" if ok else "FAIL"}] {name}' + (f'  — {detail}' if detail else ''))
        return ok

    def summary(self) -> int:
        bad = [row for row in self.rows if not row[1]]
        print(f'\n合计 {len(self.rows)} 项，失败 {len(bad)} 项')
        for name, _, detail in bad:
            print(f'  - {name}: {detail}')
        return 0 if not bad else 1


def _matches(got: float | None, before: float | None, after: float | None,
             tol: float) -> bool:
    """读到的值必须落在「读之前」和「读之后」两份快照之间（含量化误差）。

    为什么不能只跟读之前那一份比：hub 每秒推进一拍，一次 Modbus 往返
    跨过拍边界时过程量已经变了 —— 直接比对会把"正常的物理变化"报成
    "编码错误"。用前后两份夹住，既容忍了一拍的正常漂移，又能抓住真正的
    字节序/地址错误（那类错值会偏出好几个量级）。
    """
    if got is None or before is None or after is None:
        return False
    low = min(before, after) - tol
    high = max(before, after) + tol
    return math.isfinite(got) and low <= got <= high


def _client_config(device: dict, port: int, extra_registers: list[dict] | None = None) -> dict:
    """按从站那台设备的配置构造客户端配置（除了 host/port 换成 127.0.0.1）。

    刻意复用同一份 ``registers``：这样"从站按配置提供服务、客户端按同一份
    配置解析"这条链路才和现场一致。地址映射写错会立刻在这里暴露。
    """
    registers = list(device.get('registers') or [])
    if extra_registers:
        registers = registers + list(extra_registers)
    return {
        'id': device.get('id'),
        'name': device.get('name') or device.get('id'),
        'protocol': 'modbus_tcp',
        'host': '127.0.0.1',
        'port': port,
        'slave_id': device.get('slave_id', 1),
        'byte_order': device.get('byte_order') or 'ABCD',
        'registers': registers,
    }


def _pick_device(hub, predicate) -> str | None:
    """在从站池覆盖的设备里找第一台满足条件的（配置会变，不能写死 id）。"""
    for device_id in hub.devices:
        device = hub.devices[device_id]
        if str(device.get('protocol') or '').lower() != 'modbus_tcp':
            continue
        if not hub.register_map(device_id) and not hub.coil_map(device_id):
            continue
        if predicate(device):
            return device_id
    return None


def _scale_of(reg: dict) -> float:
    """取点位 scale；缺失/非法/0 一律按 1.0（与 hub 的处理口径一致）。"""
    scale = try_finite_float(reg.get('scale', 1.0))
    return scale if scale else 1.0


def _find_register(device: dict, *, data_type: str, writable: bool = False) -> dict | None:
    for reg in device.get('registers') or []:
        if str(reg.get('kind') or '').lower() == 'coil':
            continue
        if str(reg.get('data_type') or '') != data_type:
            continue
        if writable and str(reg.get('access') or '') not in ('rw', 'write'):
            continue
        return reg
    return None


def _best_point(hub, ports: dict[str, int], data_type: str,
                min_abs: float = 0.0, reg_filter=None) -> tuple[str, dict, float] | None:
    """挑一个「当前值绝对值最大」的点位。

    为什么不取第一台设备：像 ``conveyor_count`` 这种计数器开局就是 0，
    拿 0 去比对 0 是**空验证** —— 字节序错了也照样 0。必须挑非零值。
    """
    best: tuple[str, dict, float] | None = None
    for device_id in ports:
        device = hub.devices[device_id]
        snapshot = hub.snapshot(device_id)
        for reg in device.get('registers') or []:
            if str(reg.get('kind') or '').lower() == 'coil':
                continue
            if str(reg.get('data_type') or '') != data_type:
                continue
            if reg_filter is not None and not reg_filter(reg):
                continue
            value = snapshot.get(reg.get('name'))
            if value is None or abs(value) <= min_abs:
                continue
            if best is None or abs(value) > abs(best[2]):
                best = (device_id, reg, value)
    return best


def main() -> int:
    parser = argparse.ArgumentParser(description='模拟服务端协议自测（真 Modbus TCP 从站）')
    parser.add_argument('--config', default=str(ROOT / '配置' / 'devices_simulated.yaml'))
    parser.add_argument('--base-port', type=int, default=15020)
    parser.add_argument('--tick', type=float, default=1.0)
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        pass

    from 模拟服务端 import DeviceDataHub, ModbusSlavePool
    from 采集层.modbus_client import ModbusClient

    rep = Reporter()

    print(f'== 加载设备清单 {args.config}')
    try:
        hub = DeviceDataHub(args.config, tick=args.tick)
    except (OSError, ValueError) as exc:
        print(f'  [FAIL] 数据中枢初始化失败: {exc}')
        return 2

    expected_slaves = sum(
        1 for device in hub.devices.values()
        if str(device.get('protocol') or '').lower() == 'modbus_tcp'
        and (hub.register_map(str(device.get('id'))) or hub.coil_map(str(device.get('id'))))
    )
    total_points = sum(len(hub.register_map(device_id)) + len(hub.coil_map(device_id))
                       for device_id in hub.devices)
    print(f'  设备 {len(hub.devices)} 台，其中 modbus_tcp 应起 {expected_slaves} 台从站，'
          f'点位合计 {total_points} 个')

    pool = ModbusSlavePool(hub, base_port=args.base_port)
    ports: dict[str, int] = {}
    hub.start()
    # 等两拍，让 snapshot 里有点位值（第一拍之前是空的）
    time.sleep(max(1.2, args.tick * 1.5))

    try:
        print(f'\n== 起从站池（base_port={args.base_port}）')
        ports = pool.start()
        rep.check('从站全部监听', len(ports) == expected_slaves,
                  f'起 {len(ports)} 台 / 预期 {expected_slaves} 台')
        rep.check('端口连续递增', sorted(ports.values()) == list(
            range(args.base_port, args.base_port + len(ports))),
            f'{min(ports.values()) if ports else "-"}..{max(ports.values()) if ports else "-"}')

        # --------------------------------------------------------------
        # 1. uint16（优先选带 scale 的点位：scale 错一位读出来就明显不对）
        # --------------------------------------------------------------
        print('\n== uint16 点位（含 scale 换算）')
        picked_point = _best_point(
            hub, ports, 'uint16', min_abs=0.5,
            reg_filter=lambda reg: _scale_of(reg) != 1.0,
        )
        if picked_point is None:
            picked_point = _best_point(hub, ports, 'uint16')
        if picked_point is None:
            rep.check('找到 uint16 点位', False, '配置里没有 uint16 寄存器')
        else:
            device_id, reg, _ = picked_point
            port = ports[device_id]
            device = hub.devices[device_id]
            scale = _scale_of(reg)
            offset = try_finite_float(reg.get('offset', 0.0)) or 0.0
            client = ModbusClient(_client_config(device, port))
            try:
                connected = client.connect()
                rep.check(f'{device_id} 连接 127.0.0.1:{port}', connected)
                if connected:
                    address = int(reg['address'])
                    name = reg['name']
                    before = hub.snapshot(device_id).get(name)
                    raw = client.read_holding_registers(address, 1)
                    after = hub.snapshot(device_id).get(name)
                    got = (raw[0] * scale + offset) if raw else None
                    # uint16 的量化误差就是半个 scale，不能按浮点精度比
                    ok = _matches(got, before, after, abs(scale) / 2 + 1e-9)
                    rep.check(f'{name} @{address} (uint16, scale={scale})', ok,
                              f'读={got!r} 快照={before!r}→{after!r} raw={raw}')
            finally:
                client.disconnect()

        # --------------------------------------------------------------
        # 2. float32 —— 字节序正确性的关键证据
        # --------------------------------------------------------------
        print('\n== float32 点位（字节序正确性）')
        picked_point = _best_point(hub, ports, 'float32', min_abs=1.0) \
            or _best_point(hub, ports, 'float32')
        if picked_point is None:
            rep.check('找到 float32 点位', False, '配置里没有 float32 寄存器')
        else:
            device_id, reg, _ = picked_point
            port = ports[device_id]
            device = hub.devices[device_id]
            order = device.get('byte_order') or 'ABCD'
            client = ModbusClient(_client_config(device, port))
            try:
                connected = client.connect()
                rep.check(f'{device_id} 连接 127.0.0.1:{port}', connected)
                if connected:
                    address = int(reg['address'])
                    name = reg['name']
                    before = hub.snapshot(device_id).get(name)
                    raw = client.read_holding_registers(address, 2)
                    after = hub.snapshot(device_id).get(name)
                    got = client.decode_float32(raw) if raw else None
                    tol = max(abs(before or 0.0), abs(after or 0.0), 1.0) * 1e-5
                    rep.check(f'{name} @{address} (float32, order={order})',
                              _matches(got, before, after, tol),
                              f'读={got!r} 快照={before!r}→{after!r} raw={raw}')
            finally:
                client.disconnect()

        # --------------------------------------------------------------
        # 3. uint32
        # --------------------------------------------------------------
        print('\n== uint32 点位')
        picked_point = _best_point(hub, ports, 'uint32', min_abs=0.0)
        if picked_point is None or abs(picked_point[2]) < 1:
            print('  [SKIP] 当前没有非零的 uint32 点位（0 vs 0 比对不算验证）')
        else:
            device_id, reg, _ = picked_point
            port = ports[device_id]
            device = hub.devices[device_id]
            client = ModbusClient(_client_config(device, port))
            try:
                connected = client.connect()
                rep.check(f'{device_id} 连接 127.0.0.1:{port}', connected)
                if connected:
                    address = int(reg['address'])
                    name = reg['name']
                    before = hub.snapshot(device_id).get(name)
                    raw = client.read_holding_registers(address, 2)
                    after = hub.snapshot(device_id).get(name)
                    got = client.decode_uint32(raw) if raw else None
                    rep.check(f'{name} @{address} (uint32)',
                              _matches(got, before, after, 1.0),
                              f'读={got!r} 快照={before!r}→{after!r} raw={raw}')
            finally:
                client.disconnect()

        # --------------------------------------------------------------
        # 4. 未登记地址：读 0 / 写异常码 02
        # --------------------------------------------------------------
        print('\n== 未登记地址（读 0 / 写 0x02）')
        device_id = next(iter(ports))
        device = hub.devices[device_id]
        unused = 60000
        # 客户端配置里补登一个从站没有的地址：否则写入安全层会在本地就拦下
        # （fail-closed），测不到从站的异常码。
        extra = [{'address': unused, 'name': 'selftest_unused', 'data_type': 'uint16',
                  'access': 'rw', 'scale': 1}]
        client = ModbusClient(_client_config(device, ports[device_id], extra))
        try:
            if client.connect():
                raw = client.read_holding_registers(unused, 2)
                rep.check('读未登记地址返回 0', raw == [0, 0], f'raw={raw}')
                wrote = client.write_single_register(unused, 1)
                rep.check('写未登记地址被拒（异常码 0x02）', wrote is False,
                          f'write_single_register 返回 {wrote!r}')
            else:
                rep.check(f'{device_id} 连接', False, '无法连接')
        finally:
            client.disconnect()

        # --------------------------------------------------------------
        # 5. 写寄存器 → 行为模拟器收到
        # --------------------------------------------------------------
        print('\n== 写寄存器（主站 → 行为模拟器）')
        picked = _pick_device(hub, lambda d: _find_register(d, data_type='uint16', writable=True))
        if picked is None:
            print('  [SKIP] 配置里没有 access: rw 的 uint16 寄存器')
        else:
            device_id, port = picked, ports[picked]
            device = hub.devices[device_id]
            reg = _find_register(device, data_type='uint16', writable=True)
            address = int(reg['address'])
            client = ModbusClient(_client_config(device, port))
            try:
                if client.connect():
                    wrote = client.write_single_register(address, 1)
                    rep.check(f'{device_id} 写 {reg["name"]} @{address} = 1', bool(wrote))
                    # 从站侧读回：覆盖层保证"写进去什么就读回什么"
                    back = hub.read_registers(device_id, address, 1)
                    rep.check('从站读回写入值', back == [1], f'raw={back}')
                    # 关键：行为模拟器收到了（这才是"闭环"成立的前提）
                    seen = hub.last_written_value(device_id, address)
                    rep.check('行为模拟器收到写入', seen is not None and abs(seen - 1.0) < 1e-9,
                              f'handle_write_register 收到 {seen!r}')
                else:
                    rep.check(f'{device_id} 连接', False, '无法连接')
            finally:
                client.disconnect()

        # --------------------------------------------------------------
        # 6. 写线圈 → 继电器输出跟着变
        # --------------------------------------------------------------
        print('\n== 写线圈（主站 → 行为模拟器 → snapshot）')
        picked = _pick_device(hub, lambda d: bool(hub.coil_map(str(d.get('id')))))
        if picked is None:
            print('  [SKIP] 配置里没有 kind: coil 点位')
        else:
            device_id, port = picked, ports[picked]
            device = hub.devices[device_id]
            coil_address, coil_name = next(iter(hub.coil_map(device_id).items()))
            client = ModbusClient(_client_config(device, port))
            try:
                if client.connect():
                    wrote = client.write_single_coil(coil_address, True)
                    rep.check(f'{device_id} 写线圈 {coil_name} @{coil_address} = True',
                              bool(wrote))
                    coils = client.read_coils(coil_address, 1)
                    rep.check('主站读回线圈', coils == [True], f'coils={coils}')
                    seen = hub.last_written_coil(device_id, coil_address)
                    rep.check('行为模拟器收到线圈写入', seen is True, f'收到 {seen!r}')
                else:
                    rep.check(f'{device_id} 连接', False, '无法连接')
            finally:
                client.disconnect()

        # --------------------------------------------------------------
        # 7. 非寄存器点位（OPC UA nodes / REST endpoints）写入
        #
        # 这类设备没有 registers，写进去的值不经过任何寄存器编码，
        # 只靠点位名交给行为模拟器 —— 断掉的话现场表现是
        # 「下发设定值 1 秒后自己变回模拟值」。
        # --------------------------------------------------------------
        print('\n== 非寄存器点位写入（nodes）')
        picked_point = None
        for device_id, device in hub.devices.items():
            if str(device.get('protocol') or '').lower() != 'opcua':
                continue
            for node in device.get('nodes') or []:
                if isinstance(node, dict) and node.get('name'):
                    picked_point = (device_id, str(node['name']))
                    break
            if picked_point:
                break
        if picked_point is None:
            print('  [SKIP] 配置里没有 opcua nodes 点位')
        else:
            device_id, point_name = picked_point
            target = 999.0
            wrote = hub.write_point(device_id, point_name, target)
            rep.check(f'{device_id}/{point_name} 按点位名写入', wrote)
            now = hub.snapshot(device_id).get(point_name)
            rep.check('写入立刻反映到快照', _matches(now, target, target, 1e-9), f'读到 {now!r}')
            time.sleep(max(1.1, args.tick * 1.5))
            after = hub.snapshot(device_id).get(point_name)
            rep.check('跨拍不被模拟值冲掉', _matches(after, target, target, 1e-9),
                      f'一拍后读到 {after!r}')
            rep.check('不存在的点位被拒', hub.write_point(device_id, 'no_such_point', 1.0) is False)

        stats = hub.stats()
        print(f'\n== 中枢统计 {stats}')
        rep.check('后台推进线程在跑', stats['ticks'] > 0, f'ticks={stats["ticks"]}')
    finally:
        pool.stop()
        hub.stop()

    # 端口必须释放干净，否则下一次启动会"端口被占用"
    print('\n== 关闭后端口释放')
    import socket

    def _still_listening() -> list[int]:
        open_ports = []
        for port in ports.values():
            try:
                # 必须用 create_connection：`connect_ex` 在带 timeout 的 socket 上
                # 返回的是 WSAEWOULDBLOCK(10035)（非阻塞 connect 进行中），
                # 不是"连上了"。只有真的握手成功才算还在监听。
                with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                    open_ports.append(port)
            except OSError:
                continue
        return open_ports

    # 关闭回调是异步的，给 2s 的收敛窗口（超过就说明真的没释放）
    deadline = time.time() + 2.0
    still_open = _still_listening()
    while still_open and time.time() < deadline:
        time.sleep(0.1)
        still_open = _still_listening()
    rep.check('全部端口已释放', not still_open, f'仍在监听: {still_open[:5]}')

    return rep.summary()


if __name__ == '__main__':
    raise SystemExit(main())
