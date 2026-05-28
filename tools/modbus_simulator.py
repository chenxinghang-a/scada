"""
独立 Modbus TCP 模拟器
======================
用 pymodbus 3.x 的 SimDevice API 跑真实的 Modbus TCP Slave。
采集层连这个模拟器走的是真实 Modbus 协议，不是内部 mock。

用法:
    python tools/modbus_simulator.py                    # 默认 0.0.0.0:5020
    python tools/modbus_simulator.py --port 502
    python tools/modbus_simulator.py --devices siemens
"""

import sys
import math
import time
import struct
import random
import signal
import logging
import argparse
import threading
from pathlib import Path
from datetime import datetime

import yaml

# pymodbus 3.x
from pymodbus.server import StartTcpServer
from pymodbus.simulator import SimDevice, SimData
from pymodbus.simulator.simdata import DataType

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [ModbusSim] %(message)s',
)
logger = logging.getLogger(__name__)


# ================================================================
# 物理模型
# ================================================================

PHYSICS_PROFILES = {
    'boiler_temperature': {'base': 120.0, 'amp': 20.0, 'period': 180, 'noise': 3.0, 'drift': 0.05},
    'heat_exchanger_temperature': {'base': 75.0, 'amp': 10.0, 'period': 150, 'noise': 1.5, 'drift': 0.08},
    'flue_gas_temperature': {'base': 180.0, 'amp': 30.0, 'period': 200, 'noise': 5.0, 'drift': 0.03},
    'temperature': {'base': 85.0, 'amp': 15.0, 'period': 120, 'noise': 2.0, 'drift': 0.1},
    'pressure': {'base': 0.5, 'amp': 0.15, 'period': 90, 'noise': 0.02, 'drift': 0.001},
    'boiler_pressure': {'base': 1.2, 'amp': 0.3, 'period': 120, 'noise': 0.05, 'drift': 0.002},
    'flow': {'base': 50.0, 'amp': 15.0, 'period': 60, 'noise': 3.0, 'drift': 0.05},
    'steam_flow': {'base': 10.0, 'amp': 3.0, 'period': 90, 'noise': 0.5, 'drift': 0.01},
    'level': {'base': 500.0, 'amp': 50.0, 'period': 300, 'noise': 10.0, 'drift': 0.5},
    'feed_water_level': {'base': 350.0, 'amp': 40.0, 'period': 200, 'noise': 8.0, 'drift': 0.3},
    'oxygen_content': {'base': 4.5, 'amp': 1.0, 'period': 180, 'noise': 0.3, 'drift': 0.01},
    'voltage': {'base': 380.0, 'amp': 10.0, 'period': 60, 'noise': 2.0, 'drift': 0.0},
    'current': {'base': 25.0, 'amp': 8.0, 'period': 30, 'noise': 1.5, 'drift': 0.02},
    'power': {'base': 500.0, 'amp': 100.0, 'period': 60, 'noise': 20.0, 'drift': 0.5},
    'vibration': {'base': 2.5, 'amp': 1.0, 'period': 10, 'noise': 0.3, 'drift': 0.005},
    'ph': {'base': 7.0, 'amp': 0.5, 'period': 300, 'noise': 0.1, 'drift': 0.001},
    'default': {'base': 50.0, 'amp': 10.0, 'period': 120, 'noise': 2.0, 'drift': 0.05},
}


def get_profile(reg_name: str) -> dict:
    name_lower = reg_name.lower()
    for key, profile in PHYSICS_PROFILES.items():
        if key in name_lower:
            return profile
    return PHYSICS_PROFILES['default']


def calc_value(profile: dict, t: float) -> float:
    v = profile['base']
    v += profile['amp'] * math.sin(2 * math.pi * t / profile['period'])
    v += (profile['amp'] * 0.3) * math.sin(2 * math.pi * t / (profile['period'] * 3.7))
    v += profile['drift'] * t
    v += random.gauss(0, profile['noise'])
    return v


# ================================================================
# 寄存器构建
# ================================================================

def build_simdata_list(registers: list[dict]) -> tuple[list[SimData], dict[int, int]]:
    """从设备配置的寄存器列表构建 SimData 列表

    pymodbus 3.x 的 SimData 要求地址严格递增且不重叠。
    原始 Modbus 地址是紧凑布局（float32@0 占 0-1, float32@2 占 2-3），
    但 SimData 的 overlap 检查要求 next_addr > prev_addr + occupied_regs。
    所以需要重映射地址，返回 (simdata_list, addr_map)。
    addr_map[原始地址] = 模拟器地址，用于更新线程。

    count 含义：值的个数，不是寄存器数。
    """
    simdata_list = []
    addr_map = {}
    next_addr = 0

    for reg in registers:
        orig_addr = reg['address']
        data_type = reg.get('data_type', 'uint16')

        if data_type in ('float32', 'float', 'real'):
            sd = SimData(address=next_addr, count=1, values=0.0, datatype=DataType.FLOAT32)
            occupied = 2
        elif data_type in ('int16',):
            sd = SimData(address=next_addr, count=1, values=0, datatype=DataType.INT16)
            occupied = 1
        elif data_type in ('uint32',):
            sd = SimData(address=next_addr, count=1, values=0, datatype=DataType.UINT32)
            occupied = 2
        else:
            sd = SimData(address=next_addr, count=1, values=0, datatype=DataType.UINT16)
            occupied = 1

        addr_map[orig_addr] = next_addr
        simdata_list.append(sd)
        next_addr += occupied + 1  # +1 避免 overlap 检查失败

    return simdata_list, addr_map


def build_update_thread(simdata_map: dict[int, SimData], registers: list[dict], interval: float):
    """创建后台线程，周期更新 SimData 对象的 values（pymodbus 3.x 直接修改属性）"""
    start_time = time.time()

    def _update_loop():
        while True:
            t = time.time() - start_time
            for reg in registers:
                addr = reg['address']
                name = reg.get('name', '')
                data_type = reg.get('data_type', 'uint16')

                if addr not in simdata_map:
                    continue

                sd = simdata_map[addr]
                profile = get_profile(name)
                val = calc_value(profile, t)

                if data_type in ('float32', 'float', 'real'):
                    sd.values = round(val, 2)
                elif 'status' in name.lower():
                    r = random.random()
                    sd.values = 1 if r < 0.95 else (0 if r < 0.99 else 2)
                else:
                    sd.values = max(0, min(65535, int(val)))

            time.sleep(interval)

    thread = threading.Thread(target=_update_loop, daemon=True, name="updater")
    thread.start()
    return thread


# ================================================================
# 主入口
# ================================================================

def load_devices(config_path: str, filter_ids: list[str] = None) -> list[dict]:
    with open(config_path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    devices = data.get('devices', [])
    if filter_ids:
        devices = [d for d in devices if any(fid in d['id'] for fid in filter_ids)]
    return devices


def main():
    parser = argparse.ArgumentParser(description='Modbus TCP 模拟器')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=5020)
    parser.add_argument('--config', default=str(PROJECT_ROOT / '配置' / 'devices_simulated.yaml'))
    parser.add_argument('--devices', nargs='*', help='只模拟指定设备ID（模糊匹配）')
    parser.add_argument('--interval', type=float, default=1.0, help='更新间隔(秒)')
    args = parser.parse_args()

    logger.info("=" * 50)
    logger.info("Modbus TCP 模拟器启动")
    logger.info("=" * 50)

    devices = load_devices(args.config, args.devices)
    if not devices:
        logger.error("没有找到匹配的设备")
        sys.exit(1)

    logger.info(f"加载 {len(devices)} 台设备:")

    # 只处理 modbus_tcp 协议的设备
    modbus_devices = [d for d in devices if d.get('protocol') == 'modbus_tcp']
    if not modbus_devices:
        logger.error("没有 modbus_tcp 协议的设备")
        sys.exit(1)

    sim_devices = []
    for idx, dev_cfg in enumerate(modbus_devices):
        device_id = dev_cfg['id']
        # 模拟器中每个设备用不同 slave_id（真实环境各设备有独立 IP）
        slave_id = dev_cfg.get('slave_id', 1)
        if len(modbus_devices) > 1:
            slave_id = idx + 1
        registers = dev_cfg.get('registers', [])

        logger.info(f"  [{device_id}] {dev_cfg.get('name', device_id)}: "
                     f"{len(registers)} 个寄存器, slave_id={slave_id}")

        # 构建 SimData 列表 + 地址映射
        simdata_list, addr_map = build_simdata_list(registers)

        # 构建 simdata_map: 原始地址 → SimData 对象
        simdata_map = {}
        for reg in registers:
            orig_addr = reg['address']
            mapped_addr = addr_map[orig_addr]
            # 找到对应的 SimData
            for sd in simdata_list:
                if sd.address == mapped_addr:
                    simdata_map[orig_addr] = sd
                    break

        # 创建 SimDevice
        sim_dev = SimDevice(id=slave_id, simdata=simdata_list)
        sim_devices.append(sim_dev)

        # 启动更新线程
        build_update_thread(simdata_map, registers, args.interval)

    if len(sim_devices) == 1:
        sim_devices = sim_devices[0]

    logger.info(f"Modbus TCP 监听: {args.host}:{args.port}")
    logger.info("按 Ctrl+C 停止")

    def shutdown(signum, frame):
        logger.info("停止")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    StartTcpServer(context=sim_devices, address=(args.host, args.port))


if __name__ == '__main__':
    main()
