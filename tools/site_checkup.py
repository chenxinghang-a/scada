#!/usr/bin/env python
"""现场上线体检：一条命令回答「这套部署现在能不能上现场」。

## 为什么需要

前面几十轮修的缺陷，绝大多数现场工程师**没法自己发现**：

* `host: ${PLC_HOST_01}` 没注入 → 42 台一台都连不上，日志只说「连接失败」
* Modbus 设备没写 `byte_order` → 32 位值读成 1.6e-38，界面上数字看着正常
* `access: rw` 但没配 `write_limits` → 所有写入被拒，提示只说「未配置量程」
* 审计目录不可写 → 操作照常执行但**一条记录都没有**
* OPC UA `node_id` 抄错 → 设备显示「已连接」，一个点位都读不到

这些的共同点：**不报错、看起来正常、只有现场数据是错的**。

这个脚本把它们变成可执行的检查 —— 只读，不改任何东西。

## 用法

    python tools/site_checkup.py                      # 体检真实清单
    python tools/site_checkup.py --config 配置/devices_simulated.yaml
    python tools/site_checkup.py --probe              # 顺带 TCP 探测各设备
    python tools/site_checkup.py --json               # 机器可读（CI 用）

退出码：0 = 可上线；1 = 有阻断项必须先处理。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

VAR_PATTERN = re.compile(r'\$\{([A-Z0-9_]+)(?::-([^}]*))?\}')

#: 本来就无量纲的物理量（功率因数、pH、比率、状态码）
DIMENSIONLESS = re.compile(
    r'power_factor|factor|ratio|ph\b|ph_value|status|state|mode|code|count|'
    r'number|index|position|efficiency')

BLOCK, WARN, OK = 'BLOCK', 'WARN', 'OK'


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str, str]] = []   # (组, 级别, 项, 说明)

    def add(self, group: str, level: str, item: str, detail: str = ''):
        self.rows.append((group, level, item, detail))

    def blocked(self) -> list:
        return [r for r in self.rows if r[1] == BLOCK]

    def warned(self) -> list:
        return [r for r in self.rows if r[1] == WARN]

    def render(self):
        groups: dict[str, list] = {}
        for row in self.rows:
            groups.setdefault(row[0], []).append(row)

        mark = {BLOCK: '❌', WARN: '⚠️ ', OK: '✅'}
        for group, rows in groups.items():
            print(f'\n=== {group} ===')
            for _, level, item, detail in rows:
                line = f'  {mark[level]} {item}'
                if detail:
                    line += f'  — {detail}'
                print(line)

        print('\n' + '=' * 60)
        b, w = len(self.blocked()), len(self.warned())
        if b:
            print(f'❌ 有 {b} 项**阻断**必须先处理，{w} 项警告')
            for _, _, item, detail in self.blocked():
                print(f'   - {item}: {detail}')
        elif w:
            print(f'⚠️  没有阻断项，但有 {w} 项警告（不挡上线，建议现场确认）')
        else:
            print('✅ 全部通过，可以上现场')
        print('=' * 60)

    def to_json(self) -> str:
        return json.dumps({
            'blocked': len(self.blocked()),
            'warned': len(self.warned()),
            'rows': [{'group': g, 'level': l, 'item': i, 'detail': d}
                     for g, l, i, d in self.rows],
        }, ensure_ascii=False, indent=2)


def check_config(cfg_path: Path, rep: Report) -> dict | None:
    """配置完整性（这是最能提前发现现场问题的一组）。"""
    import yaml

    group = '一、配置'
    if not cfg_path.exists():
        rep.add(group, BLOCK, '配置文件存在', f'找不到 {cfg_path}')
        return None

    try:
        raw_text = cfg_path.read_text(encoding='utf-8')
        cfg = yaml.safe_load(raw_text) or {}
    except Exception as e:      # noqa: BLE001
        rep.add(group, BLOCK, '配置文件可解析', f'{type(e).__name__}: {e}')
        return None

    devices = cfg.get('devices')
    if not isinstance(devices, list):
        rep.add(group, BLOCK, 'devices 是列表', f'实际是 {type(devices).__name__}')
        return None

    rep.add(group, OK, '设备数量', f'{len(devices)} 台')
    if not devices:
        rep.add(group, BLOCK, '设备清单非空', '0 台设备 —— 进程会以空表启动')
        return cfg

    # ---- 环境变量注入 ----
    used = {m.group(1) for m in VAR_PATTERN.finditer(raw_text)}
    missing = sorted(v for v in used if not os.environ.get(v))
    if missing:
        rep.add(group, BLOCK, '环境变量已注入',
                f'{len(missing)} 个未设置，设备将拿占位符当主机名去连: {missing[:5]}'
                f'{" ..." if len(missing) > 5 else ""}')
    else:
        rep.add(group, OK, '环境变量已注入', f'{len(used)} 个')

    # ---- 协议分布 / 点位 ----
    proto_count: dict[str, int] = {}
    total_points = 0
    enabled = 0
    for d in devices:
        if not isinstance(d, dict):
            rep.add(group, BLOCK, '设备条目是映射', f'发现 {type(d).__name__}')
            continue
        p = d.get('protocol', 'modbus_tcp')
        proto_count[p] = proto_count.get(p, 0) + 1
        total_points += sum(len(d.get(s) or []) for s in ('registers', 'nodes', 'topics', 'endpoints'))
        if d.get('enabled', True):
            enabled += 1
    rep.add(group, OK, '协议分布', ', '.join(f'{k}:{v}' for k, v in sorted(proto_count.items())))
    rep.add(group, OK, '点位总数', str(total_points))
    if enabled == 0:
        rep.add(group, BLOCK, '至少一台设备启用', '全部 enabled: false —— 起来也不会采')
    else:
        rep.add(group, OK, '启用设备', f'{enabled}/{len(devices)}')

    # ---- 重复 id ----
    ids = [d.get('id') for d in devices if isinstance(d, dict)]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        rep.add(group, BLOCK, '设备 id 唯一', f'重复: {sorted(dup)}')
    else:
        rep.add(group, OK, '设备 id 唯一')

    # ---- Modbus 字序 ----
    no_order = [d['id'] for d in devices
                if isinstance(d, dict) and d.get('protocol') in ('modbus_tcp', 'modbus_rtu')
                and not d.get('byte_order')]
    if no_order:
        rep.add(group, WARN, 'Modbus 声明 byte_order',
                f'{len(no_order)} 台未声明，按 ABCD 处理 —— 32 位值可能读错: {no_order[:4]}')
    else:
        rep.add(group, OK, 'Modbus 声明 byte_order')

    # ---- 可写点位缺 write_limits（会**直接导致写不进去**）----
    rw_without_limits = []
    for d in devices:
        if not isinstance(d, dict):
            continue
        rw = [r.get('name') for r in (d.get('registers') or [])
              if isinstance(r, dict) and r.get('access') == 'rw']
        if not rw:
            continue
        limits = d.get('write_limits')
        if not isinstance(limits, dict) or not limits:
            rw_without_limits.append(f"{d['id']}({len(rw)} 个)")
        else:
            uncovered = [n for n in rw if n not in limits]
            if uncovered:
                rw_without_limits.append(f"{d['id']}(缺 {uncovered[:3]})")
    if rw_without_limits:
        rep.add(group, BLOCK, '可写点位都有 write_limits',
                f'{len(rw_without_limits)} 台设备的 rw 点位没量程 → 写入会被拒绝: '
                f'{rw_without_limits[:3]}')
    else:
        rep.add(group, OK, '可写点位都有 write_limits')

    # ---- 浮点点位缺 unit ----
    no_unit = []
    for d in devices:
        if not isinstance(d, dict):
            continue
        for r in (d.get('registers') or []):
            if not isinstance(r, dict):
                continue
            if r.get('data_type') in ('float32', 'float64') and not r.get('unit') \
                    and not DIMENSIONLESS.search(str(r.get('name', ''))):
                no_unit.append(f"{d['id']}.{r.get('name')}")
    if no_unit:
        rep.add(group, WARN, '浮点点位有单位',
                f'{len(no_unit)} 个缺 unit（界面无法判断量纲）: {no_unit[:4]}')
    else:
        rep.add(group, OK, '浮点点位有单位')

    # ---- OPC UA node_id 占位 ----
    opcua_placeholder = [d['id'] for d in devices
                         if isinstance(d, dict) and d.get('protocol') == 'opcua'
                         and any('s=' in str(n.get('node_id', '')) and
                                 str(n.get('name', '')) in str(n.get('node_id', ''))
                                 for n in (d.get('nodes') or []))]
    if opcua_placeholder:
        rep.add(group, WARN, 'OPC UA node_id 已按实测填写',
                f'{len(opcua_placeholder)} 台可能仍是占位值（ns=2;s=<点位名>），'
                f'照抄会 BadNodeIdUnknown: {opcua_placeholder[:3]}')

    return cfg


def check_deps(rep: Report):
    group = '二、依赖'
    for mod, label, required in (
        ('pymodbus', 'pymodbus（Modbus）', True),
        ('asyncua', 'asyncua（OPC UA）', False),
        ('paho.mqtt.client', 'paho-mqtt（MQTT）', False),
        ('requests', 'requests（REST）', False),
    ):
        try:
            m = __import__(mod, fromlist=['__name__'])
            ver = getattr(m, '__version__', '?')
            rep.add(group, OK, label, str(ver))
        except Exception as e:      # noqa: BLE001
            rep.add(group, BLOCK if required else WARN, label,
                    f'不可用（{type(e).__name__}）—— 该协议的设备无法采集')


def check_storage(rep: Report):
    group = '三、存储与审计'
    import paths

    data_dir = Path(paths.resolve('data')) if hasattr(paths, 'resolve') else ROOT / 'data'
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / '.checkup_probe'
        probe.write_text('x', encoding='utf-8')
        probe.unlink()
        rep.add(group, OK, 'data 目录可写', str(data_dir))
    except Exception as e:      # noqa: BLE001
        rep.add(group, BLOCK, 'data 目录可写', f'{type(e).__name__}: {e} —— 采集数据无法入库')

    # 审计文件所在目录（`_audit` 失败 = 自证缺口）
    try:
        from 智能层.device_control import DeviceControlSafety
        dc = DeviceControlSafety.__new__(DeviceControlSafety)
        audit_file = Path(getattr(dc, '_audit_file', ROOT / 'logs' / 'ops_audit.jsonl'))
    except Exception:      # noqa: BLE001
        audit_file = ROOT / 'logs' / 'ops_audit.jsonl'
    parent = audit_file.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
        probe = parent / '.checkup_probe'
        probe.write_text('x', encoding='utf-8')
        probe.unlink()
        rep.add(group, OK, '审计目录可写', str(parent))
    except Exception as e:      # noqa: BLE001
        rep.add(group, BLOCK, '审计目录可写',
                f'{type(e).__name__}: {e} —— 写操作会被拒绝（无法自证）')

    # 数据库
    try:
        db_path = Path(paths.get_db_path('real'))
        db_path.parent.mkdir(parents=True, exist_ok=True)
        import sqlite3
        conn = sqlite3.connect(str(db_path), timeout=3)
        conn.execute('PRAGMA user_version')
        conn.close()
        rep.add(group, OK, '数据库可打开', str(db_path))
    except Exception as e:      # noqa: BLE001
        rep.add(group, BLOCK, '数据库可打开', f'{type(e).__name__}: {e}')


def check_network(cfg: dict | None, rep: Report, timeout: float = 1.0):
    group = '四、网络可达（--probe）'
    if not cfg:
        rep.add(group, WARN, '跳过', '没有可用配置')
        return
    devices = [d for d in (cfg.get('devices') or []) if isinstance(d, dict) and d.get('enabled', True)]
    if not devices:
        rep.add(group, WARN, '跳过', '没有启用的设备')
        return

    unreachable = []
    skipped = 0
    for d in devices:
        host = str(d.get('host') or '')
        port = d.get('port')
        if not host or VAR_PATTERN.search(host) or not port:
            skipped += 1
            continue
        try:
            with socket.create_connection((host, int(port)), timeout=timeout):
                pass
        except OSError as e:
            unreachable.append(f"{d.get('id')}({host}:{port}) {type(e).__name__}")

    total = len(devices) - skipped
    if total == 0:
        rep.add(group, WARN, '跳过', f'{skipped} 台 host 仍是占位符（先注入环境变量）')
        return
    if unreachable:
        rep.add(group, WARN, '设备 TCP 可达',
                f'{len(unreachable)}/{total} 台连不上（现场未接线时可忽略）: {unreachable[:4]}')
    else:
        rep.add(group, OK, '设备 TCP 可达', f'{total}/{total}')


def main() -> int:
    ap = argparse.ArgumentParser(description='现场上线体检（只读）')
    ap.add_argument('--config', default=None, help='设备清单路径（默认 配置/devices_real.yaml）')
    ap.add_argument('--probe', action='store_true', help='额外做 TCP 可达探测')
    ap.add_argument('--json', action='store_true', help='输出 JSON（CI 用）')
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        pass

    cfg_path = Path(args.config) if args.config else ROOT / '配置' / 'devices_real.yaml'
    if not cfg_path.is_absolute():
        cfg_path = ROOT / cfg_path

    rep = Report()
    cfg = check_config(cfg_path, rep)
    check_deps(rep)
    check_storage(rep)
    if args.probe:
        check_network(cfg, rep)

    if args.json:
        print(rep.to_json())
    else:
        print(f'体检目标: {cfg_path}')
        rep.render()

    return 1 if rep.blocked() else 0


if __name__ == '__main__':
    raise SystemExit(main())
