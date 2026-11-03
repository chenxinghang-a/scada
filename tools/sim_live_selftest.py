#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""模拟设备「真实协议线」端到端自测。

## 它验证什么

    模拟服务端（真 Modbus 从站 / 真 OPC UA Server / 真 REST 网关 / 真 MQTT Broker）
        ↑ 真实协议报文
    采集层真实客户端（pymodbus / asyncua / requests / paho）
        ↓
    SimulatedDeviceManager(live_protocol=True)

和 `tools/e2e_smoke.py` 的区别：那个测的是**采集→写→健康**的业务链，
这个测的是**协议栈本身**——同一个点位的值，从服务端的数据块出发，
经过寄存器编码、字节序、报文、客户端解码，最后回到工程值，**必须一致**。

## 为什么值得单独一个脚本

原来的模拟模式是进程内假客户端，**根本不过协议**。所以 float32 字序、
32 位寄存器拼接、JSON 路径、MQTT topic 匹配这些最容易出错的地方，
在模拟环境里从来没被验证过 —— 现场换个字节序就全错，而模拟永远绿。

用法：
    .venv/Scripts/python.exe tools/sim_live_selftest.py
    .venv/Scripts/python.exe tools/sim_live_selftest.py --only modbus opcua
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# 数值一律走唯一真源：裸 float() 会把 nan 放进来（nan 的任何比较都是 False，
# 于是「量纲对不对」的校验对 NaN 直接失效），裸 int(float('inf')) 则抛
# OverflowError —— 项目有静态闸门锁这条，见 tests/test_non_finite_input_guard.py。
from core.safe_numeric import try_finite_float

logging.basicConfig(level=logging.WARNING,
                    format='%(levelname)-7s %(name)s | %(message)s')

CONFIG = REPO / '配置' / 'devices_simulated.yaml'


class Reporter:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = '') -> bool:
        self.rows.append((name, bool(ok), detail))
        print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ''))
        return bool(ok)

    def summary(self) -> int:
        total = len(self.rows)
        passed = sum(1 for _n, ok, _d in self.rows if ok)
        print(f"\n合计：{passed}/{total} 通过")
        for name, ok, detail in self.rows:
            if not ok:
                print(f"  ❌ {name} — {detail}")
        return 0 if passed == total else 1


def _pick(devices: dict, protocol: str, n: int = 3) -> list[dict]:
    """挑 n 台该协议的设备（优先 enabled 且有多个点位的）。"""
    out = []
    for cfg in devices.values():
        if not isinstance(cfg, dict):
            continue
        if str(cfg.get('protocol', '')).lower() != protocol:
            continue
        if cfg.get('enabled') is False:
            continue
        out.append(cfg)
    out.sort(key=lambda c: -sum(len(c.get(s) or []) for s in
                                ('registers', 'nodes', 'topics', 'endpoints')))
    return out[:n]


#: 比对容差：模拟值每个 tick 都在抖（噪声），服务端快照和客户端读到的是
#: **相邻两个 tick**，不可能完全相等。2% 相对容差足以覆盖噪声，
#: 又能抓住「量纲错 10 倍」「字节序错成 1.6e-38」这类真问题。
_TOL_REL = 0.02
_TOL_ABS = 1.0


def _close(got, want) -> bool:
    # 用 try_finite_float 而不是裸 float()：裸转换对 NaN 不抛，
    # `abs(nan - w) <= ...` 恒为 False 但**看不出是 NaN**；
    # 对 inf 则抛 OverflowError（不在 (TypeError, ValueError) 里，会漏出去）。
    # 项目有闸门专门扫这个，这里不是"测试代码可以随便写"的例外。
    from core.safe_numeric import try_finite_float

    g = try_finite_float(got)
    w = try_finite_float(want)
    if g is None or w is None:
        return False
    return abs(g - w) <= max(abs(w) * _TOL_REL, _TOL_ABS)


def _magnitude_ok(got, want, ratio: float = 0.5, abs_tol: float = 2.0) -> bool:
    """量纲比对：只查「没差一个数量级」，不查精确值。

    推送型协议（OPC UA / REST / MQTT）的客户端缓存是**上一次推送/轮询**的值，
    服务端快照是**当前 tick** 的值 —— 慢变量（电池 SOC、空闲台数）天然差一截，
    精确比对等于在比「谁手快」。所以这里只抓量纲错误。
    """
    from core.safe_numeric import try_finite_float

    g = try_finite_float(got)
    w = try_finite_float(want)
    if g is None or w is None:
        return False
    return abs(g - w) <= max(abs(w) * ratio, abs_tol)


def _decode_register(client, reg: dict, raw: list[int]):
    """把原始寄存器按点位声明的类型解码成工程值。

    走客户端自己的 decode_* 方法 —— 那正是现场用的解码路径，
    包括字节序处理。自己另写一份解码就等于没测到客户端的实现。
    """
    if not raw:
        return None
    dt = str(reg.get('data_type', 'uint16')).lower()
    try:
        if dt == 'float32':
            v = client.decode_float32(raw)
        elif dt == 'float64':
            v = client.decode_float64(raw)
        elif dt == 'uint32':
            v = client.decode_uint32(raw)
        elif dt == 'int32':
            v = client.decode_int32(raw)
        elif dt == 'int16':
            v = client.decode_int16(raw[0])
        else:
            v = client.decode_uint16(raw[0])
    except Exception:      # noqa: BLE001 —— 解码失败就是要报的问题
        return None
    from core.safe_numeric import try_finite_float

    scale = try_finite_float(reg.get('scale')) or 1.0
    v_num = try_finite_float(v)
    return None if v_num is None else v_num * scale


def _latest_of(client) -> dict:
    try:
        return client.get_latest_data() or {}
    except Exception:      # noqa: BLE001 —— 自测脚本，取不到就当空
        return {}


def _val(latest: dict, name: str):
    raw = latest.get(name)
    return raw.get('value') if isinstance(raw, dict) else raw


def main() -> int:
    ap = argparse.ArgumentParser(description='模拟设备真实协议线自测')
    ap.add_argument('--only', nargs='*', default=None,
                    help='只测这些协议（modbus/opcua/rest/mqtt）')
    ap.add_argument('--wait', type=float, default=4.0,
                    help='推送型协议（OPC UA/MQTT）等数据的秒数')
    args = ap.parse_args()

    from 模拟服务端.server_main import start_all, stop_all
    from 采集层.simulated_device_manager import SimulatedDeviceManager

    rep = Reporter()
    protocols = args.only or ['modbus', 'opcua', 'rest', 'mqtt']

    print('=== 1. 启动模拟服务端 ===')
    summary = start_all(str(CONFIG))
    hub = summary.get('hub')
    if hub is None:
        rep.check('数据中枢启动', False, 'start_all 没返回 hub')
        return rep.summary()
    rep.check('数据中枢启动', True, f"{summary.get('devices')} 台设备")
    if summary.get('errors'):
        print(f"  ⚠️  部分协议未启动: {'; '.join(summary['errors'])}")

    try:
        print('\n=== 2. 采集层用真实客户端连接 ===')
        mgr = SimulatedDeviceManager(str(CONFIG), live_protocol=True)
        rep.check('live_protocol 已生效', mgr.live_protocol is True)
        rep.check('加载全部设备', len(mgr.devices) > 100, f'{len(mgr.devices)} 台')

        t0 = time.time()
        results = mgr.connect_all(timeout=15)
        elapsed = time.time() - t0
        ok_n = sum(1 for v in results.values() if v)
        # 不是每台都能连上（有些端口可能被占），但绝大多数必须成功。
        # 门槛定 90%：低于这个数说明协议栈有系统性问题，不是个别设备。
        rep.check('连接成功率 ≥ 90%', ok_n >= len(results) * 0.9,
                  f'{ok_n}/{len(results)} 成功，耗时 {elapsed:.1f}s')

        # ---- Modbus：值一致性 ----
        if 'modbus' in protocols:
            print('\n=== 3. Modbus：寄存器编码 / 字节序 / 解码 ===')
            for cfg in _pick(mgr.devices, 'modbus_tcp', 3):
                dev_id = cfg['id']
                client = mgr.get_client(dev_id)
                if client is None:
                    rep.check(f'{dev_id} 客户端存在', False, 'get_client 返回 None')
                    continue
                snap = hub.snapshot(dev_id) or {}
                regs = [r for r in (cfg.get('registers') or [])
                        if r.get('kind') != 'coil' and r.get('name') in snap]
                if not regs:
                    rep.check(f'{dev_id} 有可比对的点位', False, '配置里没有可读寄存器')
                    continue

                bad = []
                for r in regs[:6]:
                    name = r['name']
                    count = int(r.get('length') or 1)
                    raw = client.read_holding_registers(int(r['address']), count)
                    got = _decode_register(client, r, raw or [])
                    want = snap.get(name)
                    if got is None or want is None or not _close(got, want):
                        bad.append(f'{name}: 客户端={got} 服务端={want} '
                                   f'(src={getattr(client, "last_read_source", "?")})')
                rep.check(f'{dev_id} 点位值与服务端一致（{min(len(regs), 6)} 个）',
                          not bad, '; '.join(bad[:2]))

            # 写一个可写点位，验证能回流到行为模拟器
            print('\n=== 3b. Modbus：写入能否回流到数据中枢 ===')
            wrote = False
            # 遍历**全部** Modbus 设备找 rw 点位：只挑点位数最多的前几台
            # 很可能一台都没有可写点位（模板默认只读），会误报「找不到」。
            for dev_id, cfg in list(mgr.devices.items()):
                if str(cfg.get('protocol', '')).lower() not in ('modbus_tcp', 'modbus_rtu'):
                    continue
                client = mgr.get_client(dev_id)
                limits = cfg.get('write_limits') or {}
                # 只挑**配了量程**的可写点位：没配的话 `WriteSafetyValidator`
                # 会 fail-closed 拒绝（这是对的），但那是安全策略的测试，
                # 不是协议链路的测试 —— 混在一起会让人以为是协议问题。
                rw = [r for r in (cfg.get('registers') or [])
                      if r.get('access') == 'rw' and r.get('kind') != 'coil'
                      and isinstance(limits.get(r.get('name')), (list, tuple))]
                if client is None or not rw:
                    continue
                reg = rw[0]
                from core.safe_numeric import try_finite_float, try_int

                lo_v, hi_v = limits[reg['name']][:2]
                lo_i = try_int(lo_v)
                hi_i = try_int(hi_v)
                # 写一个**量程内**的值。硬写 42 对继电器（0..1）会被
                # `WriteSafetyValidator` 拒绝 —— 那是在测安全策略，
                # 不是测协议链路，会让人误判成「写不进去」。
                if lo_i is None or hi_i is None or hi_i < lo_i:
                    target = 1
                else:
                    mid = try_int((try_finite_float(lo_v) + try_finite_float(hi_v)) / 2)
                    target = max(lo_i, min(hi_i, mid if mid else max(lo_i, 1)))
                ok = client.write_single_register(int(reg['address']), target)
                time.sleep(0.4)
                snap = hub.snapshot(dev_id) or {}
                got = snap.get(reg['name'])
                rep.check(f'{dev_id} 写入 {reg["name"]}={target} 后被服务端采纳',
                          bool(ok) and got is not None
                          and abs(float(got) - target) <= max(1.0, abs(target) * 0.01),
                          f'write={ok} 服务端读到={got} 量程={limits[reg["name"]]}')
                wrote = True
                break
            if not wrote:
                rep.check('找到可写点位', False, '所有 Modbus 设备都没有 rw 寄存器')

        # 冻结数据中枢再比对推送型协议。
        #
        # OPC UA / REST / MQTT 的客户端缓存里是「上一次推送/轮询」的值，
        # 而服务端快照是「当前 tick」的值 —— 两者天然差一个周期。
        # 不冻结的话，慢变量（AGV 空闲台数、电池 SOC）会差出 10% 以上，
        # 测试变成「比谁手快」，而不是「比协议链路对不对」。
        hub.stop()
        time.sleep(0.5)

        # ---- OPC UA：订阅 ----
        if 'opcua' in protocols:
            print('\n=== 4. OPC UA：node_id / 订阅 / 值一致 ===')
            for cfg in _pick(mgr.devices, 'opcua', 2):
                dev_id = cfg['id']
                client = mgr.get_client(dev_id)
                if client is None:
                    rep.check(f'{dev_id} 客户端存在', False)
                    continue
                deadline = time.time() + args.wait
                latest = {}
                while time.time() < deadline:
                    latest = _latest_of(client)
                    if latest:
                        break
                    time.sleep(0.3)
                names = [n['name'] for n in (cfg.get('nodes') or [])]
                got_names = set(latest)
                rep.check(f'{dev_id} 收到订阅数据（{len(names)} 个点位配置）',
                          bool(latest), f'收到 {sorted(got_names)[:4]}')
                # 订阅全失败时客户端会置这个标志
                stats = getattr(client, 'stats', {}) or {}
                rep.check(f'{dev_id} 订阅没有全失败',
                          not stats.get('subscription_failed'),
                          f"ok={stats.get('nodes_subscribed_ok')} "
                          f"failed={stats.get('subscription_failed')}")

                snap = hub.snapshot(dev_id) or {}
                bad = []
                for name in names:
                    got = _val(latest, name)
                    want = snap.get(name)
                    if got is None:
                        bad.append(f'{name}: 客户端没有值')
                        continue
                    if want is None:
                        continue
                    # 推送型协议**不做精确比对**：客户端缓存是上一次推送，
                    # 服务端快照是当前 tick，慢变量天然差一截。
                    # 这里只查「量纲没跑偏」—— 差一个数量级以上一定是解码错。
                    if not _magnitude_ok(got, want):
                        bad.append(f'{name}: 客户端={got} 服务端={want}')
                rep.check(f'{dev_id} 订阅值量纲正确', not bad, '; '.join(bad[:2]))

        # ---- REST ----
        if 'rest' in protocols:
            print('\n=== 5. REST：JSON 路径 / 值与服务端一致 ===')
            for cfg in _pick(mgr.devices, 'rest', 2):
                dev_id = cfg['id']
                client = mgr.get_client(dev_id)
                if client is None:
                    rep.check(f'{dev_id} 客户端存在', False)
                    continue
                deadline = time.time() + args.wait
                latest = {}
                while time.time() < deadline:
                    latest = _latest_of(client)
                    if latest:
                        break
                    time.sleep(0.3)
                snap = hub.snapshot(dev_id) or {}
                names = [e['name'] for e in (cfg.get('endpoints') or [])]
                ok_vals, bad = [], []
                for name in names:
                    got = _val(latest, name)
                    want = snap.get(name)
                    if got is None or want is None:
                        continue
                    # 同 OPC UA：REST 是轮询缓存，只查量纲不查精确值。
                    # 精确值一致性由 Modbus 段（主动读）覆盖。
                    if _magnitude_ok(got, want):
                        ok_vals.append(name)
                    else:
                        bad.append(f'{name}: 客户端={got} 服务端={want}')
                rep.check(f'{dev_id} 拉到 {len(latest)}/{len(names)} 个端点',
                          len(latest) >= max(1, len(names) // 2), f'{sorted(latest)[:4]}')
                rep.check(f'{dev_id} 端点值量纲正确（{len(ok_vals)} 个比对）',
                          not bad, '; '.join(bad[:2]))

        # ---- MQTT ----
        if 'mqtt' in protocols:
            print('\n=== 6. MQTT：topic 匹配 / payload 解析 ===')
            for cfg in _pick(mgr.devices, 'mqtt', 2):
                dev_id = cfg['id']
                client = mgr.get_client(dev_id)
                if client is None:
                    rep.check(f'{dev_id} 客户端存在', False)
                    continue
                deadline = time.time() + args.wait
                latest = {}
                while time.time() < deadline:
                    latest = _latest_of(client)
                    if latest:
                        break
                    time.sleep(0.3)
                topics = [t['topic'] for t in (cfg.get('topics') or [])]
                rep.check(f'{dev_id} 收到 MQTT 消息（{len(topics)} 个 topic）',
                          bool(latest), f'收到 {sorted(latest)[:4]}')

        # 解冻（后面还要正常断开，服务端要能继续供数）
        hub.start()

        print('\n=== 7. 断开 ===')
        mgr.disconnect_all()
        rep.check('disconnect_all 无异常', True)
    finally:
        stop_all()

    return rep.summary()


if __name__ == '__main__':
    sys.exit(main())
