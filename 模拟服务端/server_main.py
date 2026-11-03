#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""模拟协议服务端统一入口 —— 让「模拟模式」跑在真实协议线上。

## 为什么需要它

原来的模拟模式是**进程内假客户端**（`EnhancedSimulatedModbusClient` 等直接
返回数值），没有任何协议报文经过。后果：

* 客户端的编码/解码（float32 字序、寄存器拼接、JSON 路径）从未被验证过 ——
  现场换个字节序就全错，而模拟环境永远绿；
* 改一个点位地址，模拟照常出数（因为它根本不看地址），现场却读不到；
* 「能读不能写」这类问题只能到现场才发现。

现在：起一组**真服务端**（Modbus 从站 / OPC UA Server / REST 网关 /
MQTT Broker），采集层用**真客户端**连 `127.0.0.1:<sim_endpoint 端口>`。
模拟与现场走**同一条代码路径**，切换只是改 IP。

## 用法

    # 独立跑（前台，Ctrl-C 停）
    .venv/Scripts/python.exe -m 模拟服务端.server_main

    # 应用内嵌跑（run.py --sim-live 会调 start_all）
    from 模拟服务端.server_main import start_all, stop_all
    endpoints = start_all('配置/devices_simulated.yaml')

## 端口约定

端口不在这里算 —— 每台设备的 `sim_endpoint.port` 由 `tools/gen_factory.py`
生成时写进配置。这样服务端和采集层读**同一份配置**，不会各自算一套。
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from 模拟服务端 import (          # noqa: E402
    DeviceDataHub,
    ModbusSlavePool,
    MqttSimBroker,
    OpcuaSimServer,
    RestSimGateway,
)

logger = logging.getLogger(__name__)

#: 已启动的服务端（进程内单例）。重复 start 会复用，避免端口占用。
_STARTED: dict[str, Any] = {}
_LOCK = threading.Lock()


def _endpoint_port(hub: DeviceDataHub, protocol: str) -> int:
    """从设备配置里取该协议的监听端口。

    取**第一台**该协议设备的 `sim_endpoint.port` —— 同一协议的设备要么各自
    一个端口（Modbus/REST，服务端按每台设备的端口起），要么共用一个
    （OPC UA/MQTT，配置里所有设备的端口相同）。
    """
    for dev in (hub.devices or {}).values():
        if not isinstance(dev, dict):
            continue
        if str(dev.get('protocol', '')).lower() != protocol:
            continue
        port = (dev.get('sim_endpoint') or {}).get('port')
        if isinstance(port, int) and port > 0:
            return port
    return 0


def start_all(config_path: str | None = None,
              tick: float = 1.0) -> dict[str, Any]:
    """起全部模拟服务端，返回启动摘要。

    Returns:
        dict: ``{'hub': ..., 'modbus': {dev: port}, 'opcua': bool,
                'rest': {dev: port}, 'mqtt': bool, 'devices': n}``

    任何**单个**协议起不来都不该拖垮其它协议 —— 现场也一样：
    OPC UA 端口被占，Modbus 设备照样要能采。所以逐个 try，失败的记在
    `errors` 里由调用方决定是否阻断启动。
    """
    path = config_path or str(REPO / '配置' / 'devices_simulated.yaml')

    with _LOCK:
        if _STARTED:
            logger.info('模拟服务端已在运行，复用现有实例')
            return _STARTED.get('summary') or {}

        summary: dict[str, Any] = {'errors': [], '_objs': {}}

        hub = DeviceDataHub(path, tick=tick)
        hub.start()
        summary['hub'] = hub
        summary['devices'] = len(hub.devices)
        logger.info('数据中枢已启动：%d 台设备', len(hub.devices))

        # ---- Modbus：每台设备一个真从站 ----
        try:
            pool = ModbusSlavePool(hub)
            ports = pool.start()
            summary['modbus'] = ports
            summary['_objs']['modbus'] = pool
            logger.info('Modbus 从站池：%d 个从站，端口 %s..%s',
                        len(ports), min(ports.values()) if ports else '-',
                        max(ports.values()) if ports else '-')
        except Exception as exc:      # noqa: BLE001
            summary['errors'].append(f'Modbus: {exc}')
            logger.error('Modbus 从站池启动失败（其它协议继续）: %s', exc)

        # ---- OPC UA：一个 Server 承载所有 OPC UA 设备 ----
        opcua_port = _endpoint_port(hub, 'opcua')
        if opcua_port:
            try:
                opcua = OpcuaSimServer(hub, port=opcua_port)
                summary['opcua'] = opcua.start()
                summary['_objs']['opcua'] = opcua
                if not summary['opcua']:
                    summary['errors'].append('OPC UA: start() 返回 False')
            except Exception as exc:      # noqa: BLE001
                summary['opcua'] = False
                summary['errors'].append(f'OPC UA: {exc}')
                logger.error('OPC UA 服务端启动失败（其它协议继续）: %s', exc)

        # ---- REST：每台设备一个 HTTP 网关 ----
        rest_port = _endpoint_port(hub, 'rest')
        if rest_port:
            try:
                gateway = RestSimGateway(hub, base_port=rest_port)
                summary['rest'] = gateway.start()
                summary['_objs']['rest'] = gateway
                logger.info('REST 网关：%d 个', len(summary['rest']))
            except Exception as exc:      # noqa: BLE001
                summary['rest'] = {}
                summary['errors'].append(f'REST: {exc}')
                logger.error('REST 网关启动失败（其它协议继续）: %s', exc)

        # ---- MQTT：一个 Broker 承载所有 MQTT 设备 ----
        mqtt_port = _endpoint_port(hub, 'mqtt')
        if mqtt_port:
            try:
                broker = MqttSimBroker(hub, port=mqtt_port)
                summary['mqtt'] = broker.start()
                summary['_objs']['mqtt'] = broker
                if not summary['mqtt']:
                    summary['errors'].append('MQTT: start() 返回 False')
            except Exception as exc:      # noqa: BLE001
                summary['mqtt'] = False
                summary['errors'].append(f'MQTT: {exc}')
                logger.error('MQTT Broker 启动失败（其它协议继续）: %s', exc)

        _STARTED['summary'] = summary
        return summary


def stop_all() -> None:
    """关闭全部模拟服务端。必须能重复调用（atexit + 信号处理都会调）。"""
    with _LOCK:
        summary = _STARTED.get('summary') or {}
        # 顺序有意为之：先停采集侧（协议服务端），最后停数据中枢 ——
        # 反过来的话，服务端还会往已经停掉的中枢里取数，刷一片告警。
        for key in ('modbus', 'opcua', 'rest', 'mqtt'):
            obj = (summary.get('_objs') or {}).get(key)
            if obj is None:
                continue
            try:
                obj.stop()
            except Exception as exc:      # noqa: BLE001
                logger.warning('关闭 %s 失败: %s', key, exc)
        hub = summary.get('hub')
        if hub is not None:
            try:
                hub.stop()
            except Exception as exc:      # noqa: BLE001
                logger.warning('关闭数据中枢失败: %s', exc)
        _STARTED.clear()
    logger.info('模拟服务端已全部停止')


def main() -> int:
    ap = argparse.ArgumentParser(description='模拟协议服务端（Modbus/OPC UA/REST/MQTT）')
    ap.add_argument('--config', default=None, help='设备清单（默认 配置/devices_simulated.yaml）')
    ap.add_argument('--tick', type=float, default=1.0, help='行为模拟推进周期（秒）')
    ap.add_argument('--duration', type=float, default=0, help='跑 N 秒后自动退出（0=常驻）')
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)-7s %(name)s | %(message)s',
        datefmt='%H:%M:%S')

    summary = start_all(args.config, tick=args.tick)
    if summary.get('errors'):
        logger.warning('部分协议未启动: %s', '; '.join(summary['errors']))

    stop_flag = threading.Event()

    def _on_signal(signum, _frame):
        logger.info('收到信号 %s，准备退出', signum)
        stop_flag.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            # 非主线程 / Windows 某些环境下装不了，退化为靠 duration 退出
            pass

    if args.duration:
        stop_flag.wait(args.duration)
    else:
        logger.info('模拟服务端已就绪，Ctrl-C 退出')
        stop_flag.wait()

    stop_all()
    return 0


if __name__ == '__main__':
    sys.exit(main())
