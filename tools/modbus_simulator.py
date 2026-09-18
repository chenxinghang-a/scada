"""
独立 Modbus TCP 模拟器
======================
用 pymodbus 3.9 的 datastore API（ModbusServerContext + ModbusSlaveContext +
ModbusSequentialDataBlock）跑真实 Modbus TCP Slave。

为什么不用 pymodbus.simulator
---------------------------
pymodbus 3.9.2 的 `pymodbus.simulator` 模块 API 漂移严重：
  * `from pymodbus.simulator.simdata import DataType` 会 ImportError（3.9.2 里叫 SimDataType）
  * SimData 是不可变 dataclass，且参数名是 value（单数）
  * SimDevice 没有 simdata= 参数
  * 更关键：SimCore.build_config() 在 3.9.2 里没实现完（build_block 直接 return None），
    整条 simulator 路线不可用。

因此这里改用稳定的 datastore 路线：每台设备一个 SlaveContext（用 slave_id 区分），
后台线程直接改 hr.values（普通 list，可变）即可被客户端读到；客户端写入也落在同一个
list，写后读回天然成立。

地址映射（重要）
---------------
pymodbus 3.9.2 的 ModbusSlaveContext.getValues/setValues 内部硬编码 `address += 1`，
且 ModbusSlaveContext.__init__ 有个坑：co/ir/hr 的初始化被 `di is not None` 门控，
只传 hr 会被 create() 覆盖成全 0。所以必须四个块都传，且配置地址 X 要放进 block 索引 X+1，
客户端读地址 X 即得到配置地址 X 的寄存器（wire 上 1:1）。

用法:
    python tools/modbus_simulator.py                       # 默认 0.0.0.0:5020
    python tools/modbus_simulator.py --port 5020
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

# pymodbus 3.9 稳定 datastore API
from pymodbus.datastore import (
    ModbusServerContext,
    ModbusSlaveContext,
    ModbusSequentialDataBlock,
)
from pymodbus.server import StartTcpServer

# 项目路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paths  # noqa: E402
PROJECT_ROOT = paths.PROJECT_ROOT

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [ModbusSim] %(message)s',
)
logger = logging.getLogger(__name__)

# 越界/非法地址异常码（IllegalDataAddress）。pymodbus 服务端在
# async_getValues/async_setValues 返回 int 时回对应的 ExceptionResponse。
ILLEGAL_ADDRESS = 0x02

VALID_BYTE_ORDERS = ('ABCD', 'BADC', 'CDAB', 'DCBA')


# ================================================================
# float32 字节序打包 / 解包（与采集层解码需一致）
# ================================================================

def pack_float32(value: float, byte_order: str = 'ABCD') -> list[int]:
    """把 float32 打包成 2 个 16-bit 寄存器（按配置字节序）

    返回 [reg0, reg1]，需写入 block 索引 [addr+1, addr+2]。
    ABCD: 高字在前，字内大端；DCBA: 完全反转；BADC: 字内反转；CDAB: 字序交换。
    """
    b0, b1, b2, b3 = struct.pack('>f', float(value))
    bo = byte_order.upper()
    if bo == 'ABCD':
        return [(b0 << 8) | b1, (b2 << 8) | b3]
    if bo == 'BADC':
        return [(b1 << 8) | b0, (b3 << 8) | b2]
    if bo == 'CDAB':
        return [(b2 << 8) | b3, (b0 << 8) | b1]
    if bo == 'DCBA':
        return [(b3 << 8) | b2, (b1 << 8) | b0]
    raise ValueError(f"未知字节序: {byte_order}")


def unpack_float32(regs: list[int], byte_order: str = 'ABCD') -> float:
    """把 2 个 16-bit 寄存器按字节序解包成 float32（pack_float32 的逆操作）"""
    r0, r1 = regs[0], regs[1]
    bo = byte_order.upper()
    if bo == 'ABCD':
        b = bytes([(r0 >> 8) & 0xff, r0 & 0xff, (r1 >> 8) & 0xff, r1 & 0xff])
    elif bo == 'BADC':
        b = bytes([r0 & 0xff, (r0 >> 8) & 0xff, r1 & 0xff, (r1 >> 8) & 0xff])
    elif bo == 'CDAB':
        b = bytes([(r1 >> 8) & 0xff, r1 & 0xff, (r0 >> 8) & 0xff, r0 & 0xff])
    elif bo == 'DCBA':
        b = bytes([r1 & 0xff, (r1 >> 8) & 0xff, r0 & 0xff, (r0 >> 8) & 0xff])
    else:
        raise ValueError(f"未知字节序: {byte_order}")
    return struct.unpack('>f', b)[0]


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
# 自定义数据块
# ================================================================

class SimDataBlock(ModbusSequentialDataBlock):
    """带越界保护 + 写保持跟踪的顺序数据块。

    - getValues 越界时返回 int(ILLEGAL_ADDRESS)，让 pymodbus 回异常响应，
      而不是静默返回 0（满足"越界地址必须异常"）。
    - setValues 记录每个被写地址的过期时刻（写保持），模型线程据此跳过被写的寄存器。
    - 注意：模型线程直接改 .values[idx]，不经过 setValues，因此不会被记为"客户端写入"。
    """

    def __init__(self, address, values, written=None, hold_seconds=0):
        super().__init__(address, values)
        self._written = written if written is not None else {}
        self._hold = float(hold_seconds)

    def _in_bounds(self, start, count):
        return 0 <= start and start + count <= len(self.values)

    def getValues(self, address, count=1):
        start = address - self.address
        if not self._in_bounds(start, count):
            return ILLEGAL_ADDRESS
        return self.values[start:start + count]

    def setValues(self, address, values):
        if not isinstance(values, list):
            values = [values]
        start = address - self.address
        if not self._in_bounds(start, len(values)):
            return ILLEGAL_ADDRESS
        self.values[start:start + len(values)] = values
        now = time.monotonic()
        for i in range(len(values)):
            self._written[start + i] = now + self._hold
        return None


# ================================================================
# 寄存器构建
# ================================================================

def build_slave_block(registers: list[dict], byte_order: str,
                      writable_hold: float, read_only_hold: float):
    """从设备寄存器列表构建 (hr_block, di_block, co_block, ir_block, reg_specs, block_size)

    配置地址 X（length L）映射到 block 索引 [X+1, X+L]（pymodbus 内部 address+=1）。
    """
    max_block_idx = 0
    specs = []
    for reg in registers:
        addr = int(reg['address'])
        length = int(reg.get('length', 1))
        data_type = reg.get('data_type', 'uint16')
        name = reg.get('name', f'reg{addr}')
        writable = (reg.get('access', '').lower() == 'rw')
        # block 索引范围：addr+1 .. addr+length
        bi_start = addr + 1
        bi_end = addr + length
        max_block_idx = max(max_block_idx, bi_end)
        specs.append({
            'name': name,
            'bi_start': bi_start,
            'length': length,
            'data_type': data_type,
            'profile': get_profile(name),
            'writable': writable,
            'hold': writable_hold if writable else read_only_hold,
        })

    block_size = max_block_idx + 2  # +1 余量，block 索引 0 恒为空

    hr = SimDataBlock(0, [0] * block_size, written={}, hold_seconds=0)
    di = SimDataBlock(0, [1] * block_size, written={}, hold_seconds=0)  # 离散输入默认置 1
    co = SimDataBlock(0, [0] * block_size, written={}, hold_seconds=0)  # 线圈默认 0
    ir = SimDataBlock(0, [0] * block_size, written={}, hold_seconds=0)  # 输入寄存器默认 0
    return hr, di, co, ir, specs, block_size


def model_status_value(name: str, t: float) -> int:
    """状态类寄存器（锅炉状态/包装线状态等）的合模型：多数运行(1)，偶发停炉/故障"""
    if 'fault' in name.lower():
        return 2
    r = random.random()
    if 'status' in name.lower():
        return 1 if r < 0.95 else (0 if r < 0.99 else 2)
    return 1


def model_discrete_value(name: str, t: float) -> int:
    """继电器/指示灯/蜂鸣器类：多为开(1)"""
    if any(k in name.lower() for k in ('relay', 'light', 'buzzer')):
        return 1 if math.sin(2 * math.pi * t / 5.0) > -0.6 else 0
    return 0


def update_slave(hr: SimDataBlock, specs: list[dict], byte_order: str, start_time: float):
    """把单个 slave 的寄存器按物理模型写入 hr.values（跳过被客户端写保持的寄存器）"""
    now = time.monotonic()
    for spec in specs:
        bi = spec['bi_start']
        length = spec['length']
        # 若整段寄存器仍在写保持期内，跳过（rw 长期保持，只读短暂保持）
        if any(hr._written.get(i, 0.0) > now for i in range(bi, bi + length)):
            continue
        name = spec['name']
        data_type = spec['data_type']
        t = time.time() - start_time

        if data_type in ('float32', 'float', 'real', 'uint32'):
            val = calc_value(spec['profile'], t)
            if data_type == 'uint32':
                regs = list(struct.unpack('>HH', struct.pack('>I', int(max(0, val)))))
            else:
                regs = pack_float32(val, byte_order)
            hr.values[bi:bi + 2] = regs
        else:  # uint16 / int16
            if 'status' in name.lower():
                val = model_status_value(name, t)
            elif any(k in name.lower() for k in ('relay', 'light', 'buzzer')):
                val = model_discrete_value(name, t)
            else:
                val = int(max(0, min(65535, round(calc_value(spec['profile'], t)))))
            hr.values[bi] = val


def build_update_thread(slaves_specs: list[tuple[SimDataBlock, list[dict], str]], interval: float):
    """单后台线程，周期更新所有 slave 的 hr 块（dynamic data）"""
    start_time = time.time()

    def _loop():
        while True:
            for hr, specs, byte_order in slaves_specs:
                update_slave(hr, specs, byte_order, start_time)
            time.sleep(interval)

    th = threading.Thread(target=_loop, daemon=True, name='modbus-updater')
    th.start()
    return th


# ================================================================
# 主入口
# ================================================================

def load_devices(config_path: str, filter_ids: list[str] = None) -> list[dict]:
    with open(config_path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    devices = data.get('devices', [])
    if filter_ids:
        devices = [d for d in devices if any(fid in d['id'] for fid in filter_ids)]
    return devices, data


def main():
    parser = argparse.ArgumentParser(description='Modbus TCP 模拟器 (pymodbus datastore)')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=5020)
    parser.add_argument('--config', default=paths.get_config_path('devices_modbus_sim.yaml'))
    parser.add_argument('--devices', nargs='*', help='只模拟指定设备ID（模糊匹配）')
    parser.add_argument('--interval', type=float, default=1.0, help='更新间隔(秒)')
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Modbus TCP 模拟器启动 (pymodbus datastore 路线)")
    logger.info("=" * 60)

    devices, raw_cfg = load_devices(args.config, args.devices)
    if not devices:
        logger.error("没有找到匹配的设备")
        sys.exit(1)

    byte_order = str(raw_cfg.get('byte_order', 'ABCD')).upper()
    if byte_order not in VALID_BYTE_ORDERS:
        logger.warning(f"byte_order={byte_order} 非法，回退 ABCD")
        byte_order = 'ABCD'
    writable_hold = float(raw_cfg.get('writable_hold_seconds', 300))
    read_only_hold = float(raw_cfg.get('read_only_hold_seconds', 5))
    logger.info(f"全局配置: byte_order={byte_order}, "
                f"writable_hold={writable_hold}s, read_only_hold={read_only_hold}s")

    modbus_devices = [d for d in devices if d.get('protocol') == 'modbus_tcp']
    if not modbus_devices:
        logger.error("没有 modbus_tcp 协议的设备")
        sys.exit(1)

    logger.info(f"加载 {len(modbus_devices)} 台 modbus_tcp 设备")

    slaves = {}
    slaves_specs = []  # (hr, specs, byte_order) for updater
    for dev_cfg in modbus_devices:
        device_id = dev_cfg['id']
        slave_id = int(dev_cfg.get('slave_id', 1))
        registers = dev_cfg.get('registers', [])

        hr, di, co, ir, specs, block_size = build_slave_block(
            registers, byte_order, writable_hold, read_only_hold)

        # 关键：四个块都必须传，否则 ModbusSlaveContext 的 di-is-not-None 坑会把
        # co/ir/hr 覆盖成默认全 0 块
        slave_ctx = ModbusSlaveContext(di=di, co=co, hr=hr, ir=ir)
        slaves[slave_id] = slave_ctx
        slaves_specs.append((hr, specs, byte_order))

        n_float = sum(1 for s in specs if s['data_type'] in ('float32', 'float', 'real', 'uint32'))
        n_other = len(specs) - n_float
        logger.info(f"  slave_id={slave_id:>2} [{device_id}] {dev_cfg.get('name', device_id)}: "
                    f"block={block_size} 寄存器={len(specs)} (float={n_float}, 其他={n_other})")

    if len(slaves) == 1:
        # 单设备也走 slaves 字典，保持路由一致
        only_id = next(iter(slaves))
        context = ModbusServerContext(slaves=slaves, single=False)
        logger.info(f"单设备模式, slave_id={only_id}")
    else:
        context = ModbusServerContext(slaves=slaves, single=False)

    # 启动动态更新线程（在 StartTcpServer 阻塞前）
    build_update_thread(slaves_specs, args.interval)

    logger.info(f"Modbus TCP 监听: {args.host}:{args.port} (从配置真实地址, 1:1 映射)")
    logger.info("按 Ctrl+C 停止")

    def shutdown(signum, frame):
        logger.info("收到停止信号，退出")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # 内部 asyncio.run，会阻塞直到进程退出
    StartTcpServer(context=context, address=(args.host, args.port))


if __name__ == '__main__':
    main()
