#!/usr/bin/env python
"""OPC UA 真实链路冒烟：起一个真 OPC UA 服务器，让项目客户端连上去读。

## 为什么单独一个脚本

`tools/e2e_smoke.py` 用的是同步的 Modbus / HTTP 栈；OPC UA 是**异步**的
（`asyncua`），server 要跑在自己的事件循环里，混进去会把主冒烟脚本搞得很乱。

## 验证什么

1. 真 OPC UA server（`asyncua`）起在 127.0.0.1，带一个可写变量
2. `OPCUAClient`（项目自己的实现，独立线程跑 asyncio loop）连上并订阅
3. server 改值 → 客户端收到 `datachange_notification` → `latest_data` 更新
4. `DataCollector._collect_device_data(..., 'opcua')` 能采到数据
5. **server 停掉后**采集必须计失败（不能假成功）

## 用法

    python tools/opcua_e2e_smoke.py
    python tools/opcua_e2e_smoke.py --port 48401

退出码：0 全过 / 1 有失败。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))



class OpcuaStub:
    """一个最小的 OPC UA 服务器（独立线程 + 独立事件循环）。"""

    def __init__(self, port: int):
        self.port = port
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._vars: dict[str, object] = {}
        #: 真实的 node_id 字符串（namespace index 由 server 分配，**不能硬编码 ns=2**）
        self.node_ids: dict[str, str] = {}
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self, timeout: float = 15.0) -> bool:
        self._thread.start()
        return self._ready.wait(timeout)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:      # noqa: BLE001
            print(f'  [stub] server 异常: {type(e).__name__}: {e}')
        finally:
            self._loop.close()

    async def _serve(self):
        from asyncua import Server

        server = Server()
        await server.init()
        server.set_endpoint(f'opc.tcp://127.0.0.1:{self.port}/')
        server.set_server_name('SCADA Smoke OPC UA')

        idx = await server.register_namespace('http://scada.smoke')
        device = await server.nodes.objects.add_object(idx, 'SmokeDevice')
        temp = await device.add_variable(idx, 'Temperature', 25.0)
        press = await device.add_variable(idx, 'Pressure', 0.101)
        await temp.set_writable()
        await press.set_writable()
        self._vars = {'Temperature': temp, 'Pressure': press}
        self.node_ids = {
            'temperature': temp.nodeid.to_string(),
            'pressure': press.nodeid.to_string(),
        }
        print(f'  [stub] node_id: {self.node_ids}')

        async with server:
            self._ready.set()
            # 一直挂着，直到进程退出
            while True:
                await asyncio.sleep(0.5)

    def set_value(self, name: str, value: float) -> bool:
        """从别的线程改 server 上的变量值（触发客户端的 datachange 通知）。"""
        node = self._vars.get(name)
        if node is None or self._loop is None:
            return False
        fut = asyncio.run_coroutine_threadsafe(node.write_value(float(value)), self._loop)
        try:
            fut.result(timeout=5)
            return True
        except Exception as e:      # noqa: BLE001
            print(f'  [stub] 写值失败: {type(e).__name__}: {e}')
            return False


class Reporter:
    def __init__(self):
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


def run(port: int) -> int:
    from 采集层.opcua_client import OPCUAClient
    from 采集层.data_collector import DataCollector

    rep = Reporter()
    stub = OpcuaStub(port)

    print('\n=== 1. 启动真 OPC UA 服务器 ===')
    ok_stub = rep.check('服务器就绪', stub.start(), f'opc.tcp://127.0.0.1:{port}/')
    if not ok_stub:
        return rep.summary()

    cfg = {
        'id': 'smoke_opcua_01',
        'name': '冒烟OPC UA设备',
        'protocol': 'opcua',
        'endpoint': f'opc.tcp://127.0.0.1:{port}/',
        'nodes': [
            {'node_id': stub.node_ids['temperature'], 'name': 'temperature', 'unit': '°C'},
            {'node_id': stub.node_ids['pressure'], 'name': 'pressure', 'unit': 'MPa'},
        ],
    }

    print('\n=== 2. 项目客户端连接 + 订阅 ===')
    client = OPCUAClient(cfg)
    connected = client.connect()
    rep.check('连接成功', connected, f'connected={connected}')

    if connected:
        print('\n=== 3. 服务器改值 → 客户端收到通知 ===')
        ok_write = stub.set_value('Temperature', 88.5)
        rep.check('服务器写值成功', ok_write)

        # 等订阅通知送达（数据变化通知是异步的）
        deadline = time.time() + 8
        got = {}
        while time.time() < deadline:
            got = client.get_latest_data() or {}
            if got.get('temperature', {}).get('value') == 88.5:
                break
            time.sleep(0.2)

        rep.check('客户端收到 temperature=88.5',
                  got.get('temperature', {}).get('value') == 88.5,
                  f"latest={ {k: v.get('value') for k, v in got.items()} }")
        rep.check('客户端收到 pressure=0.101',
                  got.get('pressure', {}).get('value') == 0.101,
                  f"pressure={got.get('pressure', {}).get('value')}")

        print('\n=== 4. 采集器能从缓存取到 OPC UA 数据 ===')
        db = MagicMock()
        mgr = MagicMock()
        mgr.get_client.return_value = client
        collector = DataCollector(mgr, db, MagicMock())

        ok_collect = collector._collect_device_data('smoke_opcua_01', cfg, 'opcua')
        rep.check('采集返回成功', ok_collect is True, f'got={ok_collect}')

        items = []
        while not collector.data_queue.empty():
            try:
                it = collector.data_queue.get_nowait()
            except Exception:      # noqa: BLE001
                break
            if it.get('device_id') == 'smoke_opcua_01':
                items.append(it)
        vals = {it['register_name']: it['value'] for it in items}
        # 用**点位集合**判断而不是条数：`data_queue` 是 DiskBackedQueue（落盘），
        # 里面可能残留上次运行的同名设备数据。
        rep.check('采到 2 个点位', set(vals) == {'temperature', 'pressure'},
                  f'实际 {sorted(vals)}')
        rep.check('温度值正确', vals.get('temperature') == 88.5, str(vals))

        print('\n=== 5. 服务器停掉 → 采集必须计失败 ===')
        # 客户端断开连接（模拟服务器不可达）
        client.disconnect()
        time.sleep(0.3)
        ok_after = collector._collect_device_data('smoke_opcua_01', cfg, 'opcua')
        rep.check('断开后采集计失败（不假成功）', ok_after is False, f'got={ok_after}')

    print('\n' + ('✅ OPC UA 真实链路冒烟通过' if not [r for r in rep.rows if not r[1]]
                  else '❌ 有失败项'))
    return rep.summary()


def main() -> int:
    ap = argparse.ArgumentParser(description='OPC UA 真实链路冒烟')
    ap.add_argument('--port', type=int, default=48400)
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        pass

    return run(args.port)


if __name__ == '__main__':
    raise SystemExit(main())
