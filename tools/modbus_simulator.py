"""
独立 Modbus TCP 模拟器
======================
用 pymodbus 跑一个真实的 Modbus TCP Slave 进程。
采集层连这个模拟器走的是真实 Modbus 协议，不是内部 mock。

用法:
    python tools/modbus_simulator.py                    # 默认 0.0.0.0:5020
    python tools/modbus_simulator.py --port 502         # 自定义端口
    python tools/modbus_simulator.py --devices siemens  # 只模拟西门子PLC

原理:
    1. 从 devices_simulated.yaml 读取设备寄存器定义
    2. 在 Modbus 数据区填充物理模型驱动的仿真值
    3. 采集层的 ModbusClient 连过来就能读到实时变化的数据
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
from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusSlaveContext,
    ModbusServerContext,
)

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [ModbusSim] %(message)s',
)
logger = logging.getLogger(__name__)


# ================================================================
# 物理模型：每个变量有基础值 + 噪声 + 漂移
# ================================================================

PHYSICS_PROFILES = {
    # 温度类：缓慢漂移 + 小噪声
    'temperature': {'base': 85.0, 'amplitude': 15.0, 'period': 120, 'noise': 2.0, 'drift_rate': 0.1},
    'boiler_temperature': {'base': 120.0, 'amplitude': 20.0, 'period': 180, 'noise': 3.0, 'drift_rate': 0.05},
    'heat_exchanger_temperature': {'base': 75.0, 'amplitude': 10.0, 'period': 150, 'noise': 1.5, 'drift_rate': 0.08},
    'flue_gas_temperature': {'base': 180.0, 'amplitude': 30.0, 'period': 200, 'noise': 5.0, 'drift_rate': 0.03},
    'extrusion_temperature': {'base': 200.0, 'amplitude': 15.0, 'period': 90, 'noise': 3.0, 'drift_rate': 0.05},
    'mold_temperature': {'base': 60.0, 'amplitude': 8.0, 'period': 60, 'noise': 1.0, 'drift_rate': 0.1},
    # 压力类：中等波动
    'pressure': {'base': 0.5, 'amplitude': 0.15, 'period': 90, 'noise': 0.02, 'drift_rate': 0.001},
    'boiler_pressure': {'base': 1.2, 'amplitude': 0.3, 'period': 120, 'noise': 0.05, 'drift_rate': 0.002},
    'injection_pressure': {'base': 80.0, 'amplitude': 20.0, 'period': 30, 'noise': 5.0, 'drift_rate': 0.1},
    # 流量类
    'flow': {'base': 50.0, 'amplitude': 15.0, 'period': 60, 'noise': 3.0, 'drift_rate': 0.05},
    'steam_flow': {'base': 10.0, 'amplitude': 3.0, 'period': 90, 'noise': 0.5, 'drift_rate': 0.01},
    'cooling_water_flow': {'base': 25.0, 'amplitude': 8.0, 'period': 120, 'noise': 1.0, 'drift_rate': 0.02},
    # 液位类
    'level': {'base': 500.0, 'amplitude': 50.0, 'period': 300, 'noise': 10.0, 'drift_rate': 0.5},
    'feed_water_level': {'base': 350.0, 'amplitude': 40.0, 'period': 200, 'noise': 8.0, 'drift_rate': 0.3},
    'hopper_level': {'base': 60.0, 'amplitude': 20.0, 'period': 600, 'noise': 5.0, 'drift_rate': 0.2},
    # 气体成分
    'oxygen_content': {'base': 4.5, 'amplitude': 1.0, 'period': 180, 'noise': 0.3, 'drift_rate': 0.01},
    # 电气类
    'voltage': {'base': 380.0, 'amplitude': 10.0, 'period': 60, 'noise': 2.0, 'drift_rate': 0.0},
    'current': {'base': 25.0, 'amplitude': 8.0, 'period': 30, 'noise': 1.5, 'drift_rate': 0.02},
    'power': {'base': 500.0, 'amplitude': 100.0, 'period': 60, 'noise': 20.0, 'drift_rate': 0.5},
    # 振动
    'vibration': {'base': 2.5, 'amplitude': 1.0, 'period': 10, 'noise': 0.3, 'drift_rate': 0.005},
    # pH
    'ph': {'base': 7.0, 'amplitude': 0.5, 'period': 300, 'noise': 0.1, 'drift_rate': 0.001},
    # 通用 fallback
    'default': {'base': 50.0, 'amplitude': 10.0, 'period': 120, 'noise': 2.0, 'drift_rate': 0.05},
}

# 状态寄存器值
STATUS_RUNNING = 1
STATUS_IDLE = 0
STATUS_FAULT = 2


def get_profile(reg_name: str) -> dict:
    """根据寄存器名匹配物理模型"""
    name_lower = reg_name.lower()
    for key, profile in PHYSICS_PROFILES.items():
        if key in name_lower:
            return profile
    return PHYSICS_PROFILES['default']


def float_to_regs(value: float) -> list[int]:
    """float32 -> 2 个 Modbus 寄存器 (Big Endian)"""
    raw = struct.pack('>f', value)
    return [struct.unpack('>H', raw[0:2])[0], struct.unpack('>H', raw[2:4])[0]]


# ================================================================
# 模拟器核心
# ================================================================

class DeviceSimulator:
    """单台设备的物理模拟"""

    def __init__(self, device_config: dict):
        self.device_id = device_config['id']
        self.device_name = device_config.get('name', self.device_id)
        self.slave_id = device_config.get('slave_id', 1)
        self.registers = device_config.get('registers', [])
        self.start_time = time.time()
        self._profiles = {}
        self._fault_injected = False

        # 为每个寄存器初始化物理模型
        for reg in self.registers:
            name = reg.get('name', '')
            self._profiles[name] = get_profile(name)

        logger.info(f"  [{self.device_id}] {self.device_name}: {len(self.registers)} 个寄存器 (slave_id={self.slave_id})")

    def tick(self) -> dict[int, int]:
        """
        产生一个时间步的寄存器值。
        返回 {address: value} 映射，value 是 uint16。
        """
        t = time.time() - self.start_time
        result = {}

        for reg in self.registers:
            addr = reg['address']
            name = reg.get('name', '')
            data_type = reg.get('data_type', 'uint16')
            profile = self._profiles.get(name, PHYSICS_PROFILES['default'])

            if data_type in ('float32', 'float', 'real'):
                val = self._calc_value(profile, t)
                # 故障注入：温度飙升
                if self._fault_injected and 'temperature' in name.lower():
                    val += 50.0
                regs = float_to_regs(val)
                result[addr] = regs[0]
                result[addr + 1] = regs[1]
            elif data_type in ('uint16', 'int16', 'uint32'):
                if 'status' in name.lower():
                    # 状态寄存器：95%运行，4%空闲，1%故障
                    r = random.random()
                    if self._fault_injected:
                        result[addr] = STATUS_FAULT
                    elif r < 0.95:
                        result[addr] = STATUS_RUNNING
                    elif r < 0.99:
                        result[addr] = STATUS_IDLE
                    else:
                        result[addr] = STATUS_FAULT
                else:
                    val = self._calc_value(profile, t)
                    result[addr] = max(0, min(65535, int(val)))
            else:
                val = self._calc_value(profile, t)
                result[addr] = max(0, min(65535, int(val)))

        return result

    def _calc_value(self, profile: dict, t: float) -> float:
        """物理模型计算：base + sin漂移 + 噪声"""
        base = profile['base']
        amp = profile['amplitude']
        period = profile['period']
        noise = profile['noise']
        drift = profile['drift_rate']

        value = base
        value += amp * math.sin(2 * math.pi * t / period)
        value += (amp * 0.3) * math.sin(2 * math.pi * t / (period * 3.7))
        value += drift * t
        value += random.gauss(0, noise)
        return value

    def inject_fault(self):
        self._fault_injected = True
        logger.warning(f"  [{self.device_id}] 故障注入已激活")

    def clear_fault(self):
        self._fault_injected = False
        logger.info(f"  [{self.device_id}] 故障已清除")


# ================================================================
# Modbus Server 数据区更新线程
# ================================================================

class RegisterUpdater(threading.Thread):
    """后台线程：周期更新 Modbus 数据区"""

    def __init__(self, context: ModbusServerContext, simulators: list[DeviceSimulator], interval: float = 1.0):
        super().__init__(daemon=True)
        self.context = context
        self.simulators = simulators
        self.interval = interval
        self.running = True

    def run(self):
        logger.info(f"寄存器更新线程启动 (间隔={self.interval}s)")
        while self.running:
            for sim in self.simulators:
                values = sim.tick()
                slave = self.context[sim.slave_id]
                if slave is None:
                    continue
                store = slave.store['h']  # Holding Registers
                for addr, val in values.items():
                    try:
                        store.setValues(addr, [val])
                    except Exception:
                        pass
            time.sleep(self.interval)

    def stop(self):
        self.running = False


# ================================================================
# 主入口
# ================================================================

def load_devices(config_path: str, filter_ids: list[str] = None) -> list[dict]:
    """加载设备配置"""
    with open(config_path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)

    devices = data.get('devices', [])
    if filter_ids:
        devices = [d for d in devices if any(fid in d['id'] for fid in filter_ids)]
    return devices


def main():
    parser = argparse.ArgumentParser(description='Modbus TCP 模拟器')
    parser.add_argument('--host', default='0.0.0.0', help='监听地址')
    parser.add_argument('--port', type=int, default=5020, help='监听端口')
    parser.add_argument('--config', default=str(PROJECT_ROOT / '配置' / 'devices_simulated.yaml'))
    parser.add_argument('--devices', nargs='*', help='只模拟指定设备ID（模糊匹配）')
    parser.add_argument('--interval', type=float, default=1.0, help='寄存器更新间隔(秒)')
    parser.add_argument('--fault', type=str, help='对指定设备注入故障')
    args = parser.parse_args()

    logger.info("=" * 50)
    logger.info("Modbus TCP 模拟器启动")
    logger.info("=" * 50)

    # 加载设备
    devices = load_devices(args.config, args.devices)
    if not devices:
        logger.error("没有找到匹配的设备")
        sys.exit(1)

    logger.info(f"加载 {len(devices)} 台设备:")
    simulators = [DeviceSimulator(d) for d in devices]

    # 故障注入
    if args.fault:
        for sim in simulators:
            if args.fault in sim.device_id:
                sim.inject_fault()

    # 构建 Modbus 数据区
    # 每个 slave_id 独立的数据区，65536 个 Holding Registers
    slaves = {}
    for sim in simulators:
        if sim.slave_id not in slaves:
            slaves[sim.slave_id] = ModbusSequentialDataBlock(0, [0] * 65536)
            logger.info(f"  创建 slave_id={sim.slave_id} 数据区")

    slave_context = ModbusSlaveContext(
        di=ModbusSequentialDataBlock(0, [0] * 65536),  # Discrete Inputs
        co=ModbusSequentialDataBlock(0, [0] * 65536),  # Coils
        hr=ModbusSequentialDataBlock(0, [0] * 65536),  # Holding Registers
        ir=ModbusSequentialDataBlock(0, [0] * 65536),  # Input Registers
        zero_mode=True,
    )

    # 多 slave 支持：用 slave_id 映射
    context = ModbusServerContext(slaves={0: slave_context}, single=False)

    # 启动寄存器更新线程
    updater = RegisterUpdater(context, simulators, interval=args.interval)
    updater.start()

    logger.info(f"Modbus TCP 监听: {args.host}:{args.port}")
    logger.info("按 Ctrl+C 停止")

    # 优雅退出
    def shutdown(signum, frame):
        logger.info("收到停止信号，正在关闭...")
        updater.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # 启动 Modbus TCP 服务器（阻塞）
    StartTcpServer(context=context, address=(args.host, args.port))


if __name__ == '__main__':
    main()
