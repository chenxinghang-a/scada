#!/usr/bin/env python
"""真实模式端到端冒烟：从「起一个真 PLC」到「数据入库 / 指令下发 / 掉线告警」。

## 和 real_path_selftest.py 的区别

`real_path_selftest.py` 只测**客户端层**（`ModbusClient` 连得上、读得对、写得进）。
这个脚本测**整条链**：

    真 Modbus 从站  →  RealDeviceManager  →  DataCollector  →  数据队列
                                              ↓
                              DeviceControlSafety.write_with_verification
                                              ↓
                                        从站收到写入
                                              ↓
                            停从站 → 采集失败 → 设备健康降级

它回答的问题是「**真实模式整套跑起来，能不能采、能不能控、掉线知不知道**」——
这是「确保全真实设备可用可监控可操控」的最终验收。

## 用法

    python tools/e2e_smoke.py
    python tools/e2e_smoke.py --keep-log      # 保留服务端日志

退出码：0 全过 / 1 有失败。
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SLAVE_SCRIPT = ROOT / 'tools' / 'real_path_selftest.py'

#: 冒烟用的一台「现场 PLC」：地址布局与 配置/devices_real.yaml 的写法一致。
DEVICE_YAML = """\
devices:
  - id: smoke_plc_01
    name: 冒烟测试PLC
    protocol: modbus_tcp
    host: 127.0.0.1
    port: {port}
    slave_id: 1
    byte_order: ABCD
    collection_interval: 1
    enabled: true
    zone: 测试
    # ⚠️ write_limits 是**设备级**的，键是**点位名**，值是 [下限, 上限]。
    # 写成寄存器级的 min/max 两个键**不生效**（会被当成点位名叫 min、max 的
    # 量程），于是真实模式下 `validate_write` 一律拒绝写入。
    # （这段模板会被 .format() 处理，注释里不能出现裸花括号。）
    write_limits:
      speed_setpoint: [0, 600]
    registers:
      - address: 0
        name: run_status
        data_type: uint16
        description: 运行状态字
        unit: ''
        scale: 1
        access: r
      - address: 2
        name: line_speed
        data_type: float32
        description: 产线速度
        unit: m/min
        scale: 1
        access: r
      - address: 10
        name: speed_setpoint
        data_type: uint16
        description: 速度设定
        unit: m/min
        scale: 1
        access: rw
  # ---- 第二台：REST 网关（验证 HTTP 协议的端到端，不只是 Modbus）----
  - id: smoke_rest_01
    name: 冒烟测试网关
    protocol: rest
    base_url: http://127.0.0.1:{http_port}
    poll_interval: 1
    collection_interval: 1
    enabled: true
    zone: 测试
    endpoints:
      - name: line_status
        path: /api/status
        json_path: data.value
        method: GET
        description: 产线状态
        unit: ''
      - name: oven_temp
        path: /api/temp
        json_path: data.value
        method: GET
        description: 烘箱温度
        unit: °C
"""



class _FakeGateway(http.server.BaseHTTPRequestHandler):
    """一个极简的「现场网关」：返回固定 JSON，用来验证 REST 真实链路。"""

    #: 由测试改写的返回值（模拟现场数值变化）
    values = {'status': 1, 'temp': 88.5}
    #: 让网关返回 500，验证「HTTP 错误不能当成功」
    fail_mode = False

    def do_GET(self):      # noqa: N802 —— BaseHTTPRequestHandler 规定的名字
        if self.fail_mode:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error": "internal"}')
            return
        payload = {
            '/api/status': {'data': {'value': self.values['status']}},
            '/api/temp': {'data': {'value': self.values['temp']}},
        }.get(self.path)
        if payload is None:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(payload).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):     # 静音，否则每个请求刷一行
        pass


def _start_fake_http(port: int) -> http.server.HTTPServer:
    srv = http.server.HTTPServer(('127.0.0.1', port), _FakeGateway)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


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


def _wait_port(port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.4):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def _start_slave(port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(SLAVE_SCRIPT), '--slave', '--port', str(port), '--order', 'ABCD'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def run(port: int, http_port: int) -> int:
    from 采集层.real_device_manager import RealDeviceManager
    from 采集层.data_collector import DataCollector
    from 智能层.device_control import DeviceControlSafety

    rep = Reporter()
    slave = None
    gateway = None
    tmpdir = Path(tempfile.mkdtemp(prefix='e2e_smoke_'))
    cfg_path = tmpdir / 'devices_real_smoke.yaml'
    cfg_path.write_text(DEVICE_YAML.format(port=port, http_port=http_port),
                        encoding='utf-8')

    try:
        # ---------- 1. 起真设备 ----------
        print('\n=== 1. 启动真 Modbus 从站 + 假 HTTP 网关 ===')
        slave = _start_slave(port)
        rep.check('从站监听', _wait_port(port), f'127.0.0.1:{port}')
        gateway = _start_fake_http(http_port)
        rep.check('HTTP 网关监听', _wait_port(http_port), f'127.0.0.1:{http_port}')

        # ---------- 2. 真实模式加载配置 ----------
        print('\n=== 2. 真实模式加载配置 ===')
        mgr = RealDeviceManager(config_path=str(cfg_path))
        rep.check('加载 2 台设备（Modbus + REST）', len(mgr.devices) == 2,
                  f'实际 {len(mgr.devices)}: {sorted(mgr.devices)}')
        cfg = mgr.devices.get('smoke_plc_01') or {}
        rep.check('点位表解析', len(cfg.get('registers') or []) == 3,
                  f"实际 {len(cfg.get('registers') or [])} 个点位")

        # ---------- 3. 连接 ----------
        print('\n=== 3. 连接真实设备 ===')
        results = mgr.connect_all(timeout=10)
        rep.check('connect_all 成功', results.get('smoke_plc_01') is True, str(results))
        rep.check('REST 网关连接成功', results.get('smoke_rest_01') is True, str(results))
        client = mgr.get_client('smoke_plc_01')
        rep.check('客户端已连接', bool(client and client.connected))

        # ---------- 4. 采集 ----------
        print('\n=== 4. 采集一轮（DataCollector） ===')
        db = MagicMock()
        alarm_mgr = MagicMock()
        control = DeviceControlSafety(db, mgr, alarm_mgr)
        collector = DataCollector(mgr, db, alarm_mgr, device_control=control)

        ok = collector._collect_device_data('smoke_plc_01', cfg, 'modbus_tcp')
        rep.check('采集返回成功', ok is True)

        # ⚠️ `data_queue` 是 DiskBackedQueue（落盘），里面可能有**历史运行**
        # 留下的数据。只取本次设备的条目，否则会把上次跑的东西当成结果。
        items = []
        while not collector.data_queue.empty():
            try:
                it = collector.data_queue.get_nowait()
            except Exception:      # noqa: BLE001 —— 取空/IO 问题都不该中断冒烟
                break
            if it.get('device_id') == 'smoke_plc_01':
                items.append(it)
        names = {it['register_name'] for it in items}
        rep.check('采到 3 个点位', names == {'run_status', 'line_speed', 'speed_setpoint'},
                  f'实际 {sorted(names)}')
        # 从站预置：地址 0 = 4321（uint16）、地址 2 = 1234.5678（float32）
        vals = {it['register_name']: it['value'] for it in items}
        rep.check('uint16 值正确', vals.get('run_status') == 4321, f"got={vals.get('run_status')}")
        speed = vals.get('line_speed')
        rep.check('float32 值正确', speed is not None and abs(speed - 1234.5678) < 1e-3,
                  f'got={speed}')

        # ---------- 4b. REST 采集 ----------
        print('\n=== 4b. 采集 REST 网关（真 HTTP 请求） ===')
        # REST 客户端是**轮询型**：`connect()` 起线程按 poll_interval 拉数据，
        # 采集器再从它的 `latest_data` 缓存读。所以要先等它拉一轮。
        rest_cfg = mgr.devices.get('smoke_rest_01') or {}
        deadline = time.time() + 6
        while time.time() < deadline:
            rest_client = mgr.get_client('smoke_rest_01')
            if rest_client and rest_client.get_latest_data():
                break
            time.sleep(0.3)

        rest_client = mgr.get_client('smoke_rest_01')
        latest = rest_client.get_latest_data() if rest_client else {}
        rep.check('REST 已拉到数据', bool(latest), f'点位 {sorted(latest)}')

        ok_rest = collector._collect_device_data('smoke_rest_01', rest_cfg, 'rest')
        rep.check('REST 采集返回成功', ok_rest is True, f'got={ok_rest}')

        rest_items = []
        while not collector.data_queue.empty():
            try:
                it = collector.data_queue.get_nowait()
            except Exception:      # noqa: BLE001
                break
            if it.get('device_id') == 'smoke_rest_01':
                rest_items.append(it)
        rest_vals = {it['register_name']: it['value'] for it in rest_items}
        rep.check('REST 采到 2 个点位', len(rest_items) == 2, f'实际 {sorted(rest_vals)}')
        rep.check('REST 值正确（status=1, temp=88.5）',
                  rest_vals.get('line_status') == 1 and rest_vals.get('oven_temp') == 88.5,
                  str(rest_vals))

        # ---------- 4c. HTTP 500 不能当成功 ----------
        print('\n=== 4c. 网关返回 500 时不许当成功 ===')
        _FakeGateway.fail_mode = True
        time.sleep(2.2)          # 等轮询线程拉到一次 500
        ok_bad = collector._collect_device_data('smoke_rest_01', rest_cfg, 'rest')
        rep.check('网关 500 → 采集计失败', ok_bad is False, f'got={ok_bad}')
        _FakeGateway.fail_mode = False

        # ---------- 5. 健康上报 ----------
        print('\n=== 5. 设备健康（采集后应上报） ===')
        health = (control.get_device_health_summary().get('devices') or {}).get('smoke_plc_01') or {}
        rep.check('健康状态为 connected', health.get('status') == 'connected', str(health))

        # ---------- 6. 写指令 ----------
        print('\n=== 6. 写指令（走安全层 + 回读验证） ===')
        result = control.write_with_verification(
            'smoke_plc_01', 10, 300, operator='e2e_smoke', kind='register')
        rep.check('写入成功', bool(result.get('success')), str(result.get('message')))
        rep.check('回读一致（verified=True）', result.get('verified') is True,
                  f"verified={result.get('verified')}")

        back = client.read_holding_registers(10, 1)
        rep.check('从站真的收到了', back == [300], f'从站读回 {back}')

        # ---------- 7. 掉线检测 ----------
        print('\n=== 7. 停掉从站，验证掉线检测 ===')
        slave.kill()
        slave.wait(timeout=5)
        slave = None
        time.sleep(0.5)

        ok2 = collector._collect_device_data('smoke_plc_01', cfg, 'modbus_tcp')
        rep.check('采集返回失败（不再假成功）', ok2 is False, f'got={ok2}')

        # 缓存兜底 + 新鲜度标记
        src = getattr(client, 'last_read_source', '<无>')
        rep.check("客户端标记为 cache", src == 'cache', f'last_read_source={src!r}')

        health2 = (control.get_device_health_summary().get('devices') or {}).get('smoke_plc_01') or {}
        rep.check('健康状态降级（不再是 connected）',
                  health2.get('status') != 'connected', str(health2))

        # ---------- 8. 恢复 ----------
        print('\n=== 8. 重启从站，验证恢复 ===')
        slave = _start_slave(port)
        up = _wait_port(port)
        rep.check('从站重新监听', up)
        if up:
            client.disconnect()
            rep.check('重连成功', client.connect())
            ok3 = collector._collect_device_data('smoke_plc_01', cfg, 'modbus_tcp')
            rep.check('采集恢复成功', ok3 is True)
            health3 = (control.get_device_health_summary().get('devices') or {}).get('smoke_plc_01') or {}
            rep.check('健康状态恢复 connected', health3.get('status') == 'connected', str(health3))

    finally:
        if gateway is not None:
            try:
                gateway.shutdown()
            except Exception as e:      # noqa: BLE001
                # 收尾阶段的异常不该让冒烟「看起来失败」，但也不能吞干净 ——
                # 端口没释放会导致下一次跑不起来，必须留下痕迹。
                print(f'  [warn] 关闭 HTTP 网关异常: {type(e).__name__}: {e}')
        if slave is not None:
            slave.kill()
            try:
                slave.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        try:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        except OSError:
            pass

    return rep.summary()


def main() -> int:
    ap = argparse.ArgumentParser(description='真实模式端到端冒烟')
    ap.add_argument('--port', type=int, default=15030)
    ap.add_argument('--http-port', type=int, default=15080)
    ap.add_argument('--slave', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--order', default='ABCD', help=argparse.SUPPRESS)
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        pass

    if args.slave:
        # 复用 real_path_selftest 的从站实现，避免两份预置数据不一致
        from tools.real_path_selftest import run_slave
        run_slave(args.port, args.order)
        return 0

    rc = run(args.port, args.http_port)
    print('\n' + ('✅ 真实模式端到端冒烟通过' if rc == 0 else '❌ 有失败项'))
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
