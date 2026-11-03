#!/usr/bin/env python
"""模拟服务端三种协议的真实链路自测：OPC UA / REST / MQTT。

## 为什么要这个脚本

`模拟服务端/` 里的三个服务端如果只是「能 import、能 start」，说明不了任何事：
真正的风险在协议细节上 —— namespace index 对不对、`json_path` 反向构造的嵌套
能不能被客户端的 `_extract_by_path` 取到、MQTT 的 retained 到底会不会被新订阅者
收到。这些只有**用项目自己的采集客户端连上去**才能验证。

## 验证什么

1. OPC UA：`采集层.opcua_client.OPCUAClient` 连 `opc.tcp://127.0.0.1:15400/`，
   订阅某台设备的全部点位，3 秒内拿到值，且与 `hub.snapshot()` 同量级
2. REST：`采集层.rest_client.RESTDeviceClient` 连某台设备的模拟端口，
   `get_latest_data()` 拿到值且与 snapshot 同量级
3. MQTT：`采集层.mqtt_client.MQTTClient`（内部就是 paho）订阅设备 topic 收到值；
   再用裸 paho 验证 retained：先发 retained 再订阅，必须立刻收到且 `retain=True`

「同量级」的判定不是拍脑袋的百分比：脚本先采样 hub 三次拿到该点位的**抖动幅度**，
要求客户端读到的值落在 `[min - spread, max + spread]` 内。点位恒定 → 严格相等；
点位在动 → 允许一个采样周期的偏差。

## 用法

    .venv/Scripts/python.exe tools/sim_protocol_selftest.py
    .venv/Scripts/python.exe tools/sim_protocol_selftest.py --rest-port 16500

退出码：0 全过 / 1 有失败。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CONFIG_PATH = ROOT / '配置' / 'devices_simulated.yaml'


class Reporter:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = '') -> bool:
        self.rows.append((name, ok, detail))
        print(f'  [{"PASS" if ok else "FAIL"}] {name}' + (f'  — {detail}' if detail else ''))
        return ok

    def summary(self) -> int:
        bad = [r for r in self.rows if not r[1]]
        print(f'\n合计 {len(self.rows)} 项，失败 {len(bad)} 项')
        for name, _, detail in bad:
            print(f'  - {name}: {detail}')
        return 0 if not bad else 1


def _sample_snapshot(hub: Any, device_id: str, rounds: int = 3,
                     interval: float = 0.6) -> dict[str, list[float]]:
    """连续采样，返回 {point: [v1, v2, v3]}，用来算仿真值的抖动幅度。"""
    samples: dict[str, list[float]] = {}
    for i in range(rounds):
        snap = hub.snapshot(device_id) or {}
        for name, value in snap.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                samples.setdefault(name, []).append(float(value))
        if i < rounds - 1:
            time.sleep(interval)
    return samples


def _within_track(samples: list[float], value: Any) -> tuple[bool, str]:
    """判断客户端读到的值是否落在 hub 的抖动范围内。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False, f'值不是数值: {value!r}'
    if not samples:
        return True, f'{value}（hub 无采样，跳过比对）'
    lo, hi = min(samples), max(samples)
    spread = hi - lo
    tol = max(1e-6, spread)
    ok = (lo - tol) <= float(value) <= (hi + tol)
    return ok, f'client={value} hub=[{lo:.4f}, {hi:.4f}] tol={tol:.4f}'


def _wait_for(predicate, timeout: float, interval: float = 0.2) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _mark_writable_points(hub: Any) -> None:
    """在内存里把每台 OPC UA 设备的第一个点位标成 rw，覆盖「写入回流」路径。

    不改 `配置/`：现场配置里的 opcua 点位确实全是只读监视，但服务端必须支持写
    （设定值下发），否则这条链路永远没被验证过。改的是自测进程内的 dict，
    服务端建地址空间时读的就是它。
    """
    for cfg in (hub.devices or {}).values():
        if not isinstance(cfg, dict):
            continue
        if str(cfg.get('protocol', '')).lower() != 'opcua':
            continue
        nodes = cfg.get('nodes') or []
        if nodes and isinstance(nodes[0], dict):
            nodes[0]['access'] = 'rw'


# --------------------------------------------------------------------------- OPC UA

def test_opcua(hub: Any, server: Any, rep: Reporter) -> None:
    from 采集层.opcua_client import OPCUAClient

    print('\n=== 1. OPC UA 真实链路 ===')
    node_ids = server.node_ids()
    if not rep.check('地址空间已建立', bool(node_ids),
                     f'{len(node_ids)} 台设备: {sorted(node_ids)}'):
        return

    device_id = sorted(node_ids)[0]
    points = node_ids[device_id]
    print(f'  设备 {device_id}，{len(points)} 个点位，node_id 示例: '
          f'{next(iter(points.items()))}')

    cfg = {
        'id': device_id,
        'name': f'{device_id} 模拟',
        'protocol': 'opcua',
        'endpoint': server.endpoint,
        'security_mode': None,
        'nodes': [{'node_id': nid, 'name': name, 'unit': ''}
                  for name, nid in points.items()],
    }
    client = OPCUAClient(cfg)
    try:
        connected = client.connect()
        if not rep.check('客户端连接成功', connected, server.endpoint):
            return

        ok = _wait_for(lambda: len(client.get_latest_data()) >= len(points), timeout=10.0)
        latest = client.get_latest_data()
        rep.check('订阅收到全部点位', ok,
                  f'{len(latest)}/{len(points)}: {sorted(latest)}')

        samples = _sample_snapshot(hub, device_id)
        bad = []
        for name in points:
            value = (latest.get(name) or {}).get('value')
            good, detail = _within_track(samples.get(name, []), value)
            if not good:
                bad.append(f'{name}: {detail}')
        rep.check('读到的值与 hub.snapshot() 一致', not bad,
                  '; '.join(bad) if bad else f'{len(points)} 个点位全部吻合')

        # 可写点位：客户端写入 → 服务端 value_setter → hub.write_point（若实现）
        writable = [n for n in points if _is_writable_node(hub, device_id, n)]
        if writable:
            point = writable[0]
            target = 999.0
            wrote = _opcua_write(client, points[point], target)
            rep.check('可写点位接受客户端写入', wrote, f'{point} <- {target}')
            if wrote:
                if callable(getattr(hub, 'write_point', None)):
                    back = _wait_for(
                        lambda: abs(float(hub.snapshot(device_id).get(point, -1e9)) - target) < 1e-6,
                        timeout=3.0)
                    rep.check('写入回流到 hub.write_point()', back,
                              f'hub.snapshot({point})={hub.snapshot(device_id).get(point)}')
                    if back:
                        # 跨一拍仍然保持：hub 每 tick 都会用行为模拟器重算 snapshot，
                        # 写入若没进 override 层，1 秒后就被冲掉 —— 现场表现是
                        # 「下发成功、下一秒又变回去」，比直接失败更难查。
                        time.sleep(1.5)
                        held = abs(float(hub.snapshot(device_id).get(point, -1e9)) - target) < 1e-6
                        rep.check('写入跨 tick 保持（不被快照覆盖）', held,
                                  f'1.5s 后 hub.snapshot({point})={hub.snapshot(device_id).get(point)}')
                else:
                    print('  [SKIP] hub 未实现 write_point()，无法验证写入回流')
        else:
            print('  [SKIP] 该设备没有 rw 点位，跳过写测试')
    finally:
        client.disconnect()


def _is_writable_node(hub: Any, device_id: str, point: str) -> bool:
    cfg = (hub.devices.get(device_id) or {})
    for node in cfg.get('nodes') or []:
        if node.get('name') == point:
            return str(node.get('access', '')).lower() in ('rw', 'r/w') or bool(node.get('writable'))
    return False


def _opcua_write(client: Any, node_id: str, value: Any) -> bool:
    """在客户端自己的事件循环里写一次值。"""
    import asyncio

    loop = getattr(client, '_loop', None)
    if loop is None or not loop.is_running():
        return False
    new_value = float(value)

    async def _do() -> None:
        node = client.client.get_node(node_id)
        await node.write_value(new_value)

    try:
        fut = asyncio.run_coroutine_threadsafe(_do(), loop)
        fut.result(timeout=5)
        return True
    except (RuntimeError, TimeoutError, OSError) as exc:
        print(f'  [warn] OPC UA 写入失败: {type(exc).__name__}: {exc}')
        return False


# --------------------------------------------------------------------------- REST

def test_rest(hub: Any, ports: dict[str, int], rep: Reporter) -> None:
    import requests

    from 采集层.rest_client import RESTDeviceClient

    print('\n=== 2. REST 真实链路 ===')
    if not rep.check('网关已启动', bool(ports), f'{len(ports)} 台: {ports}'):
        return

    # 优先选 sim_endpoint 明确指定的那台（配置里 base_url 已经指向模拟端口）
    device_id = 'mes_api_02' if 'mes_api_02' in ports else sorted(ports)[0]
    cfg = dict(hub.devices[device_id])
    cfg['base_url'] = f'http://127.0.0.1:{ports[device_id]}'
    cfg['poll_interval'] = 1
    cfg['timeout'] = 3
    endpoints = cfg.get('endpoints') or []
    print(f'  设备 {device_id} @ {cfg["base_url"]}，{len(endpoints)} 个端点')

    client = RESTDeviceClient(cfg)
    try:
        if not rep.check('客户端连接成功', client.connect(), cfg['base_url']):
            return

        names = [e.get('name') for e in endpoints]
        ok = _wait_for(
            lambda: all(client.get_latest_data().get(n, {}).get('value') is not None
                        for n in names), timeout=10.0)
        latest = client.get_latest_data()
        got = {n: latest.get(n, {}).get('value') for n in names}
        rep.check('轮询拿到全部端点', ok, f'{got}')

        samples = _sample_snapshot(hub, device_id)
        bad = []
        for name in names:
            good, detail = _within_track(samples.get(name, []), got.get(name))
            if not good:
                bad.append(f'{name}: {detail}')
        rep.check('读到的值与 hub.snapshot() 一致', not bad,
                  '; '.join(bad) if bad else f'{len(names)} 个端点全部吻合')

        # 未知路径必须 404，否则「配错 path」在现场会表现为拿到别的点位
        unknown = requests.get(cfg['base_url'] + '/api/__no_such_path__', timeout=3)
        rep.check('未知路径返回 404', unknown.status_code == 404,
                  f'status={unknown.status_code}')

        health = requests.get(cfg['base_url'] + '/health', timeout=3)
        body = health.json() if health.status_code == 200 else {}
        rep.check('/health 探活正常', body.get('ok') is True,
                  f'status={health.status_code} body={body}')

        _check_multi_endpoint_same_path(hub, ports, rep)
    finally:
        client.disconnect()


def _check_multi_endpoint_same_path(hub: Any, ports: dict[str, int],
                                    rep: Reporter) -> None:
    """同一 path 挂多个端点（不同 json_path）时必须合并成一个 JSON。

    `agv_system_01` 的 `/api/agv/fleet` 一次返回 total/active/idle/charging/fault/
    avg_battery —— 按 path 去重成「最后一个」的实现只会剩一个字段，
    采集侧其余 5 个点位永远是 None，且没有任何报错。
    """
    import requests

    for device_id, port in ports.items():
        cfg = hub.devices.get(device_id) or {}
        by_path: dict[str, list[dict[str, Any]]] = {}
        for endpoint in cfg.get('endpoints') or []:
            if isinstance(endpoint, dict) and endpoint.get('path'):
                by_path.setdefault(endpoint['path'], []).append(endpoint)
        multi = {p: eps for p, eps in by_path.items() if len(eps) > 1}
        if not multi:
            continue

        path, endpoints = next(iter(multi.items()))
        resp = requests.get(f'http://127.0.0.1:{port}{path}', timeout=3)
        body = resp.json() if resp.status_code == 200 else {}
        want = {str(e.get('json_path', '')).split('.')[0] for e in endpoints}
        missing = sorted(want - set(body))
        rep.check('同 path 多端点合并成一个 JSON', not missing,
                  f'{device_id}{path} 需要 {sorted(want)}，缺 {missing}，实际 {sorted(body)}')
        return
    print('  [SKIP] 没有「同 path 多端点」的设备配置')


# --------------------------------------------------------------------------- MQTT
def test_mqtt(hub: Any, broker: Any, rep: Reporter) -> None:
    print('\n=== 3. MQTT 真实链路 ===')
    mqtt_devices = [d for d, c in (hub.devices or {}).items()
                    if isinstance(c, dict) and str(c.get('protocol', '')).lower() == 'mqtt']
    if not rep.check('找到 mqtt 设备', bool(mqtt_devices), str(mqtt_devices)):
        return

    device_id = mqtt_devices[0]
    cfg = dict(hub.devices[device_id])
    topics = [t for t in (cfg.get('topics') or []) if isinstance(t, dict) and t.get('topic')]
    print(f'  设备 {device_id} @ 127.0.0.1:{broker.port}，{len(topics)} 个 topic')

    _test_mqtt_topics(hub, broker, device_id, topics, rep)
    _test_project_mqtt_client(cfg, broker, rep)
    _test_retained(broker, rep)


def _test_mqtt_topics(hub: Any, broker: Any, device_id: str,
                      topics: list[dict[str, Any]], rep: Reporter) -> None:
    """裸 paho 订阅 `factory/#`，按 topic 校验 payload 与 snapshot 一致。

    这里**不用**项目的 `MQTTClient` 做数值校验：它把 payload 的 key 当寄存器名
    （`{"value": 12.3}` -> 寄存器 `value`），完全忽略配置里的 `topics[].name`，
    四个 topic 会塌缩成同一个 `value`。那是采集层的既有问题，不该由模拟服务端
    自测来背 —— 协议线本身用 paho 直接验。
    """
    import paho.mqtt.client as mqtt

    received: dict[str, bytes] = {}
    done = threading.Event()
    wanted = {t['topic'] for t in topics}

    def on_message(_c: Any, _u: Any, msg: Any) -> None:
        received[msg.topic] = msg.payload
        if wanted.issubset(received):
            done.set()

    client = mqtt.Client(client_id='selftest-sub', protocol=mqtt.MQTTv311)
    client.on_message = on_message
    client.connect('127.0.0.1', broker.port, keepalive=30)
    client.loop_start()
    try:
        client.subscribe('factory/#', 1)
        ok = done.wait(timeout=10.0)
        rep.check('通配符订阅收到全部 topic', ok,
                  f'{len(received)}/{len(wanted)}: {sorted(received)}')

        samples = _sample_snapshot(hub, device_id)
        bad = []
        for entry in topics:
            topic = entry['topic']
            raw = received.get(topic)
            if raw is None:
                bad.append(f'{topic}: 未收到')
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                bad.append(f'{topic}: 非 JSON {raw!r}')
                continue
            value = _extract(payload, entry.get('json_path', 'value'))
            good, detail = _within_track(samples.get(entry.get('name'), []), value)
            if not good:
                bad.append(f'{topic}: {detail}')
        rep.check('payload 按 json_path 可解析且与 snapshot 一致', not bad,
                  '; '.join(bad) if bad else f'{len(topics)} 个 topic 全部吻合')
    finally:
        client.loop_stop()
        client.disconnect()


def _extract(data: Any, path: str) -> Any:
    cur = data
    for part in [p for p in str(path or '').split('.') if p]:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _test_project_mqtt_client(cfg: dict[str, Any], broker: Any, rep: Reporter) -> None:
    """项目自己的 MQTTClient 也要能收到消息（验证 QoS1 订阅/发布与 LWT 报文）。"""
    from 采集层.mqtt_client import MQTTClient

    client_cfg = dict(cfg)
    client_cfg['host'] = '127.0.0.1'
    client_cfg['port'] = broker.port
    client = MQTTClient(client_cfg)
    try:
        if not rep.check('项目 MQTTClient 连接成功', client.connect(),
                         'mqtt://127.0.0.1:%d' % broker.port):
            return
        ok = _wait_for(lambda: client.stats.get('messages_received', 0) > 0, timeout=10.0)
        rep.check('项目 MQTTClient 收到消息', ok, f"stats={client.get_stats()}")
    finally:
        client.disconnect()


def _test_retained(broker: Any, rep: Reporter) -> None:
    """retained 语义：先发布、后订阅，新订阅者必须立刻收到且 retain=True。"""
    import paho.mqtt.client as mqtt

    topic = 'factory/selftest/retained'
    payload = json.dumps({'value': 42.0}).encode('utf-8')
    broker.publish_retained(topic, payload)
    time.sleep(0.3)

    received: list[tuple[bytes, bool]] = []
    done = threading.Event()

    def on_message(_c: Any, _u: Any, msg: Any) -> None:
        if msg.topic == topic:
            received.append((msg.payload, bool(msg.retain)))
            done.set()

    client = mqtt.Client(client_id='selftest-retained', protocol=mqtt.MQTTv311)
    client.on_message = on_message
    client.connect('127.0.0.1', broker.port, keepalive=30)
    client.loop_start()
    try:
        client.subscribe(topic, 1)
        ok = done.wait(timeout=5.0)
        rep.check('retained 消息在新订阅时立即收到', ok, f'payload={received[:1]}')
        if ok:
            got_payload, got_retain = received[0]
            rep.check('retained 标志被正确置位', got_retain is True,
                      f'retain={got_retain}')
            rep.check('retained payload 一致', got_payload == payload,
                      f'{got_payload!r}')
    finally:
        client.loop_stop()
        client.disconnect()


# --------------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description='模拟服务端三协议自测')
    parser.add_argument('--opcua-port', type=int, default=15400)
    parser.add_argument('--rest-port', type=int, default=15500)
    parser.add_argument('--mqtt-port', type=int, default=18830)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format='%(levelname)s %(name)s: %(message)s')

    from 模拟服务端.device_data_hub import DeviceDataHub
    from 模拟服务端.mqtt_broker import MqttSimBroker
    from 模拟服务端.opcua_server import OpcuaSimServer
    from 模拟服务端.rest_gateway import RestSimGateway

    rep = Reporter()
    print('=== 0. 启动 DeviceDataHub ===')
    hub = DeviceDataHub(str(CONFIG_PATH))
    hub.start()
    time.sleep(0.5)
    rep.check('hub 已启动', bool(hub.devices), f'{len(hub.devices)} 台设备')
    _mark_writable_points(hub)

    opcua = OpcuaSimServer(hub, port=args.opcua_port)
    rest = RestSimGateway(hub, base_port=args.rest_port)
    broker = MqttSimBroker(hub, port=args.mqtt_port)

    try:
        rep.check('OPC UA 服务端就绪', opcua.start(), opcua.endpoint)
        ports = rest.start()
        rep.check('REST 网关就绪', bool(ports), str(ports))
        rep.check('MQTT broker 就绪', broker.start(), f'127.0.0.1:{args.mqtt_port}')

        test_opcua(hub, opcua, rep)
        test_rest(hub, ports, rep)
        test_mqtt(hub, broker, rep)
    finally:
        print('\n=== 清理 ===')
        opcua.stop()
        rest.stop()
        broker.stop()
        hub.stop()

    return rep.summary()


if __name__ == '__main__':
    raise SystemExit(main())
