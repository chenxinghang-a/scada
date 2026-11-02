#!/usr/bin/env python
"""模拟数据体检工具。

用途：模拟器输出「有值」不等于「像真的」。本工具把每台设备的关键点位
打出来，便于人工核对量纲（MPa 是不是 MPa、RPM 是不是 RPM），也便于给
新点位定标（gain 该取多少）。

用法：
    python tools/sim_probe.py                 # 全部设备，每个点位打一次
    python tools/sim_probe.py chiller_01      # 只看一台
    python tools/sim_probe.py --gaps          # 只列出「拿不到值」的点位
    python tools/sim_probe.py --base          # 打基变量实测值（定标用）
    python tools/sim_probe.py --rounds 5      # 多轮采样，输出 min/max

为什么需要它：
    改造前 `_generate_output_data` 是一张 90 项硬编码表，`配置/devices_simulated.yaml`
    里 229 个点位有 136 个（59%）取不到值 —— 但界面上看是「有数」的，
    因为客户端会兜底 25.0。**没有这个工具，这种缺失完全不可见。**
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from 采集层.device_behavior_simulator import (  # noqa: E402
    DeviceBehaviorSimulator,
    DeviceState,
)

POINT_SECTIONS = ('registers', 'nodes', 'topics', 'endpoints')


def _load_devices(config_path: Path):
    raw = yaml.safe_load(config_path.read_text(encoding='utf-8')) or {}
    return raw.get('devices') or []


def _point_names(dev) -> list[str]:
    names = []
    for section in POINT_SECTIONS:
        for point in (dev.get(section) or []):
            if isinstance(point, dict) and point.get('name'):
                names.append(point['name'])
    return names


def _point_units(dev) -> dict[str, str]:
    units = {}
    for section in POINT_SECTIONS:
        for point in (dev.get(section) or []):
            if isinstance(point, dict) and point.get('name'):
                units[point['name']] = point.get('unit', '')
    return units


def main() -> int:
    ap = argparse.ArgumentParser(description='模拟数据体检')
    ap.add_argument('device_ids', nargs='*', help='只检查这些设备（默认全部）')
    ap.add_argument('--config', default=str(ROOT / '配置' / 'devices_simulated.yaml'))
    ap.add_argument('--rounds', type=int, default=3, help='采样轮数（用于 min/max）')
    ap.add_argument('--gaps', action='store_true', help='只列出拿不到值的点位')
    ap.add_argument('--base', action='store_true', help='打印基变量实测值（定标用）')
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        pass

    devices = _load_devices(Path(args.config))
    if not devices:
        print(f'没有读到设备：{args.config}')
        return 1

    wanted = set(args.device_ids)
    if wanted:
        devices = [d for d in devices if d.get('id') in wanted]
        missing = wanted - {d.get('id') for d in devices}
        if missing:
            print(f'配置里没有这些设备：{", ".join(sorted(missing))}')

    total_points = total_gaps = 0

    for dev in devices:
        names = _point_names(dev)
        if not names:
            continue

        sim = DeviceBehaviorSimulator(dev['id'], dev)
        sim.force_state(DeviceState.RUNNING)

        samples: dict[str, list] = {n: [] for n in names}
        for _ in range(max(1, args.rounds)):
            out = sim.update(1.0)
            for name in names:
                if name in out:
                    samples[name].append(out[name])

        gaps = [n for n in names if not samples[n]]
        total_points += len(names)
        total_gaps += len(gaps)

        if args.gaps:
            if gaps:
                print(f'{dev["id"]:24} 缺 {len(gaps)}/{len(names)}: {", ".join(gaps[:8])}')
            continue

        units = _point_units(dev)
        print(f'\n=== {dev["id"]} | {dev.get("name", "")} | {dev.get("protocol", "")}')
        print(f'    点位 {len(names)}，缺值 {len(gaps)}')
        for name in names:
            values = samples[name]
            if not values:
                print(f'    {name:26} {"--":>12} {units.get(name, ""):8} **缺值**')
                continue
            numeric = [v for v in values if isinstance(v, (int, float))]
            if numeric:
                lo, hi = min(numeric), max(numeric)
                span = f'{lo:g} .. {hi:g}' if lo != hi else f'{lo:g}'
            else:
                span = repr(values[-1])
            print(f'    {name:26} {span:>22} {units.get(name, ""):8}')

        if args.base:
            # 基变量本身不参与过滤，单独算一次
            raw = sim.update(1.0)
            print('    --- 基变量（sim 段的 source 取的就是这些）---')
            print('    ' + '  '.join(
                f'{k}={raw.get(k)}' for k in
                ('temperature', 'pressure', 'flow', 'level', 'motor_speed',
                 'current_a', 'voltage_a', 'active_power', 'vibration')
                if k in raw))

    if not args.gaps:
        pct = (total_gaps * 100 // total_points) if total_points else 0
        print(f'\n合计：点位 {total_points}，缺值 {total_gaps}（{pct}%）')
        if total_gaps:
            print('缺值点位要么补 sim 段、要么补进 配置/simulation_profiles.yaml')
            return 2

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
