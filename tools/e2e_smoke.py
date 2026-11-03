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
import json
import os
import socket
import subprocess
import sys
import tempfile
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
"""


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


def run(port: int) -> int:
    from 采集层.real_device_manager import RealDeviceManager
    from 采集层.data_collector import DataCollector
    from 智能层.device_control import DeviceControlSafety

    rep = Reporter()
    slave = None
    tmpdir = Path(tempfile.mkdtemp(prefix='e2e_smoke_'))
    cfg_path = tmpdir / 'devices_real_smoke.yaml'
    cfg_path.write_text(DEVICE_YAML.format(port=port), encoding='utf-8')

    try:
        # ---------- 1. 起真从站 ----------
        print('\n=== 1. 启动真 Modbus 从站 ===')
        slave = _start_slave(port)
        rep.check('从站监听', _wait_port(port), f'127.0.0.1:{port}')

        # ---------- 2. 真实模式加载配置 ----------
        print('\n=== 2. 真实模式加载配置 ===')
        mgr = RealDeviceManager(config_path=str(cfg_path))
        rep.check('加载 1 台设备', len(mgr.devices) == 1, f'实际 {len(mgr.devices)}')
        cfg = mgr.devices.get('smoke_plc_01') or {}
        rep.check('点位表解析', len(cfg.get('registers') or []) == 3,
                  f"实际 {len(cfg.get('registers') or [])} 个点位")

        # ---------- 3. 连接 ----------
        print('\n=== 3. 连接真实设备 ===')
        results = mgr.connect_all(timeout=10)
        rep.check('connect_all 成功', results.get('smoke_plc_01') is True, str(results))
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

    rc = run(args.port)
    print('\n' + ('✅ 真实模式端到端冒烟通过' if rc == 0 else '❌ 有失败项'))
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
