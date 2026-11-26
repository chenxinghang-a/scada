"""
增强版模拟客户端
================
使用设备行为模拟器生成更真实的工业数据

特性：
1. 物理模型驱动 - 参数之间有关联性
2. 状态机模拟 - 真实的设备运行状态
3. 故障注入 - 支持模拟各种故障场景
4. 数据连续性 - 确保数据流连续
5. 工业4.0兼容 - 生成的数据可直接用于OEE、SPC、预测性维护等
"""

import math
import time
import struct
import random
import logging
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .base_client import (
    ModbusClientInterface,
    PushClientInterface,
    decode_float64_registers,
    encode_float32_registers,
    encode_float64_registers,
    encode_int32_registers,
    reorder_32_words,
)
from core.safe_numeric import try_finite_float
from core.engineering_units import to_raw
from .device_behavior_simulator import DeviceBehaviorSimulator, DeviceState, FaultType
from .simulated_client import is_device_stopped
from . import simulated_client as _simulated_client
from .modbus_client import ByteOrder

logger = logging.getLogger(__name__)


def _bad_quality_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    """断线时返回的快照：值保留做参考，但质量码一律降为 ``bad``。

    round 383：原来 `get_latest_data` 在 `connected=False` 时仍把上一次
    缓存的条目原样返回，`quality` 还是 `good` —— 上层看板与联锁把断线前的
    旧值当成新鲜数据使用（fail-open）。断线不得报 GOOD。
    """
    result: dict[str, Any] = {}
    for key, entry in list(data.items()):
        if isinstance(entry, dict):
            item = dict(entry)
            item['quality'] = 'bad'
            result[key] = item
        elif entry is not None:
            result[key] = {'value': entry, 'quality': 'bad'}
    return result


def _generate_enhanced_data(
    device_id: str,
    behavior_simulator,
    configs: list[dict],
    latest_data: dict,
    data_callbacks: list,
    stats: dict,
    extra_field_fn: Callable[[dict], dict] | None = None,
    stat_inc_keys: list[str] | None = None,
    callback_error_label: str = '回调',
    dt: float = 2.0,
) -> bool:
    """增强版通用数据生成逻辑（供OPC UA/MQTT/REST客户端复用）

    Args:
        device_id: 设备ID
        behavior_simulator: 行为模拟器实例
        configs: 节点/主题/端点配置列表
        latest_data: 存储最新数据的字典
        data_callbacks: 数据回调列表
        stats: 统计字典
        extra_field_fn: 接受config条目，返回额外字段的函数
        stat_inc_keys: 统计计数器的key列表（每个key都会+1）
        callback_error_label: 回调异常日志标签

    Returns:
        True if data was generated, False if stopped
    """
    # 停机设备不产生数据
    if is_device_stopped(device_id):
        return False

    # 更新行为模拟器
    data = behavior_simulator.update(dt)

    if data.get('_stopped'):
        return False

    for cfg in configs:
        name = cfg.get('name') or cfg.get('node_id') or cfg.get('topic') or 'unknown'
        unit = cfg.get('unit', '')

        # 50 + (30*t)%100 会随墙钟每秒跳 30，且可能到 150；兜底用常量。
        value = try_finite_float(data.get(name, 50.0))
        if value is None:
            value = 50.0

        entry = {
            'value': round(float(value), 2),
            'unit': unit,
            'timestamp': datetime.now().isoformat(),
            'quality': 'good',
            'device_id': device_id,
        }
        if extra_field_fn:
            entry.update(extra_field_fn(cfg))
        latest_data[name] = entry

        for key in (stat_inc_keys or []):
            stats[key] = stats.get(key, 0) + 1

        for callback in data_callbacks:
            try:
                callback(device_id, name, round(float(value), 2), unit)
            except Exception as e:
                logger.error(f"[增强模拟] {callback_error_label}回调异常: {e}")

    return True


class EnhancedSimulatedModbusClient(ModbusClientInterface):
    """
    增强版模拟Modbus客户端
    
    使用设备行为模拟器生成真实的工业数据
    """
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.connected = False
        self.byte_order = ByteOrder(config.get('byte_order', 'ABCD').upper())

        # 创建设备行为模拟器
        self.behavior_simulator = DeviceBehaviorSimulator(
            device_id=config.get('id', 'unknown'),
            device_config=config
        )

        # 寄存器映射
        self._register_map = {}
        for reg in config.get('registers', []):
            self._register_map[reg['address']] = {
                'name': reg['name'],
                'data_type': reg.get('data_type', 'uint16'),
                'scale': reg.get('scale', 1.0),
                'offset': reg.get('offset', 0)
            }

        # 缓存最新数据
        self._latest_data: Dict[str, Any] = {}
        self._data_lock = threading.Lock()
        # 离散输入与输出线圈独立内存空间（与 SimulatedModbusClient 一致）
        self._discrete_input_values: Dict[int, bool] = {}

        # 每设备独立RNG（避免30台设备故障模式相关）
        self._rng = random.Random(hash(config.get('id', '')))

        # 通信故障模拟参数
        comm_cfg = config.get('communication', {})
        self._conn_fail_rate = comm_cfg.get('connect_fail_rate', 0.0)  # 默认不模拟连接失败
        self._latency_ms = comm_cfg.get('latency_ms', 0)  # 通信延迟
        self._packet_loss_rate = comm_cfg.get('packet_loss_rate', 0.0)  # 默认不模拟丢包
        self._random_disconnect_rate = comm_cfg.get('random_disconnect_rate', 0.0)  # 默认不模拟随机断线
        self._consecutive_failures = 0

        # 统计
        self.stats = {
            'total_reads': 0,
            'successful_reads': 0,
            'failed_reads': 0,
            'last_read_time': None,
            'last_error': None,
            'comm_failures': 0,
            'reconnects': 0
        }

        logger.info(f"[增强模拟] Modbus客户端初始化: {config.get('name', 'unknown')}")

    @staticmethod
    def _is_machinery(name: str) -> bool:
        """判断寄存器名是否属于机械类（停机时归零）"""
        import re
        _MACHINERY_KEYWORDS = ['motor_speed', 'conveyor_speed', 'pump_speed', 'fan_speed',
                               'spindle_speed', 'feed_rate', 'injection_speed', 'injection_force',
                               'clamping_force', 'shot_count', 'cycle_time', 'mixer']
        name_lower = name.lower()
        for kw in _MACHINERY_KEYWORDS:
            if re.search(r'(?<![a-z])' + re.escape(kw) + r'(?![a-z])', name_lower):
                return True
        return False

    @property
    def _stop_event(self) -> threading.Event:
        """可被 disconnect 立刻唤醒的等待事件。

        惰性创建：兼容 `__new__` 构造/反序列化的实例（这类实例没有走 __init__）。
        """
        ev = self.__dict__.get('_stop_event_obj')
        if ev is None:
            ev = threading.Event()
            self.__dict__['_stop_event_obj'] = ev
        return ev

    def connect(self) -> bool:
        """连接设备（支持连接失败模拟）"""
        # 模拟连接失败
        if self._rng.random() < self._conn_fail_rate:
            self.stats['comm_failures'] += 1
            logger.warning(f"[增强模拟] 设备 {self.device_name} 连接失败（模拟通信故障）")
            return False

        # 防止重复connect()产生多个更新线程
        if hasattr(self, '_update_thread') and self._update_thread and self._update_thread.is_alive():
            self._running = False
            self._update_thread.join(timeout=2)

        self.connected = True
        self._running = True
        self._stop_event.clear()
        self._consecutive_failures = 0
        self.behavior_simulator.start()

        # 启动数据更新线程
        # round 374：重连循环会反复调 connect()，已在跑就别再起一条 ——
        # 两条线程同时写 latest_data 会让上报值出现无规律跳变。
        if getattr(self, '_update_thread', None) is None \
                or not self._update_thread.is_alive():
            self._update_thread = threading.Thread(target=self._update_loop, daemon=True)
            self._update_thread.start()

        logger.info(f"[增强模拟] 设备 {self.device_name} 连接成功")
        return True
    
    def disconnect(self):
        """断开连接（安全停止更新线程）"""
        self._running = False
        self.connected = False
        self._stop_event.set()
        self.behavior_simulator.stop()
        if hasattr(self, '_update_thread') and self._update_thread and self._update_thread.is_alive():
            self._update_thread.join(timeout=2)
        logger.info(f"[增强模拟] 设备 {self.device_name} 已断开")

    def _update_loop(self):
        """数据更新循环"""
        last_update = time.time()
        while self._running and self.connected:
            try:
                now = time.time()
                dt = now - last_update
                last_update = now
                # 更新行为模拟器
                data = self.behavior_simulator.update(dt)

                # 更新缓存
                with self._data_lock:
                    self._latest_data = data

                # 必须可被 disconnect 立刻唤醒：固定 sleep(1) 会让
                # 每台设备的 join 白等最多 1 秒，30 台设备就是 30 秒（测试超时）
                self._stop_event.wait(1.0)
            except Exception as e:
                logger.error(f"[增强模拟] 更新异常: {e}")
                self._stop_event.wait(1)
    
    def read_holding_registers(self, address: int, count: int,
                               slave_id: Optional[int] = None) -> Optional[List[int]]:
        """读取保持寄存器 — 支持写入值回读和通信故障模拟"""
        if not self.connected:
            return None

        # 停机/E-STOP：机械归零，传感器保持环境值
        device_stopped = is_device_stopped(self.device_id)
        # 读模块属性而不是导入时冻结的值；set_estop_state 是重新绑定全局。
        estop_active = _simulated_client._ESTOP_ACTIVE
        if device_stopped or estop_active:
            # round 369：**按点位宽度步进**。
            # 原来 `for i in range(count)` 遇到 float32 会追加 2 个字，
            # 下一轮又访问 address+1（浮点的第二个字地址），多寄存器批量读取
            # 的长度与地址映射彻底错位（读 2 个点位返回 3 个值，且第 2 个值
            # 实际是第 1 个点位的高/低字）。
            results: List[int] = []
            i = 0
            while i < count:
                reg_addr = address + i
                reg_cfg = self._register_map.get(reg_addr, {})
                reg_name = reg_cfg.get('name', '')
                is_mach = self._is_machinery(reg_name)
                data_type = reg_cfg.get('data_type', 'uint16')
                # 64 位类型占 4 个寄存器；漏掉会让批量读长度与地址映射整体错位
                width = (4 if data_type in ('float64', 'int64', 'uint64')
                         else 2 if data_type in ('float32', 'int32', 'uint32') else 1)
                if device_stopped or (estop_active and is_mach):
                    results.extend([0] * width)
                else:
                    # 传感器：从缓存读取（行为模拟器仍在更新环境值）
                    # round 370（BUG-012）：① 必须在 `_data_lock` 内取快照，
                    # 更新线程每轮都会整体替换 `_latest_data`；
                    # ② 反向换算必须减 offset，与正常分支 `(value-offset)/scale`
                    # 同口径，否则带 offset 的点位在停机期间读数跳变。
                    with self._data_lock:
                        val = (self._latest_data or {}).get(reg_name)
                    if val is None:
                        val = 25.0
                    scale = try_finite_float(reg_cfg.get('scale', 1.0))
                    if scale is None or scale == 0:
                        scale = 1.0
                    offset = try_finite_float(reg_cfg.get('offset', 0.0)) or 0.0
                    raw = to_raw(val, scale, offset)
                    if data_type == 'float32':
                        results.extend(encode_float32_registers(
                            float(raw), self.byte_order))
                    elif data_type in ('int32', 'uint32'):
                        results.extend(encode_int32_registers(
                            int(round(raw)), self.byte_order,
                            signed=data_type == 'int32'))
                    else:
                        # 按 width 补齐（float64 占 4 字，只补 1 字会让 results
                        # 短于 count，后续点位整体错位）
                        word = int(round(raw)) & 0xFFFF
                        results.extend([word] + [0] * (width - 1))
                i += width
            return results[:count]

        # 模拟通信延迟
        if self._latency_ms > 0:
            time.sleep(self._latency_ms / 1000.0)

        # 模拟丢包
        if self._rng.random() < self._packet_loss_rate:
            self.stats['total_reads'] += 1
            self.stats['failed_reads'] += 1
            self.stats['comm_failures'] += 1
            self._consecutive_failures += 1
            return None

        # 模拟随机断线（连续失败时概率上升）
        disconnect_prob = self._random_disconnect_rate * (1 + self._consecutive_failures)
        if self._rng.random() < disconnect_prob:
            self.connected = False
            self.stats['comm_failures'] += 1
            logger.warning(f"[增强模拟] 设备 {self.device_name} 随机断线")
            return None

        self.stats['total_reads'] += 1
        self.stats['successful_reads'] += 1
        self.stats['last_read_time'] = time.time()
        self._consecutive_failures = 0
        
        # 检查是否有写入的值（用于回读验证）
        written_value = self.behavior_simulator.get_written_register_value(address)
        if written_value is not None and count == 1:
            if not isinstance(written_value, bool):
                num_written = try_finite_float(written_value)
                if num_written is not None:
                    return [int(round(num_written)) & 0xFFFF]
        
        # round 383：**逐点按宽度步进**，与上面的停机分支同口径。
        # 原来这里只编码 `address` 一个点位，其余 count-1 个字一律补 0 ——
        # 批量读 10 个点位，后 9 个全是 0，看板上除首点外全部"归零"。
        results = []
        i = 0
        while i < count:
            reg_addr = address + i
            reg_config = self._register_map.get(reg_addr)
            if not reg_config:
                # 未知寄存器：单位宽占位值（步进 1，不猜测宽度）
                results.append(100)
                i += 1
                continue

            data_type = str(reg_config.get('data_type', 'uint16')).lower()
            width = (4 if data_type in ('float64', 'int64', 'uint64')
                     else 2 if data_type in ('float32', 'int32', 'uint32') else 1)

            # 从缓存获取数据（None保护：行为模拟器边界条件下可能返回None）
            with self._data_lock:
                value = (self._latest_data or {}).get(reg_config.get('name'))

            if value is None:
                # 缓存还没生成这一轮数据：按安全侧补 0，绝不用随机数冒充真实值
                results.extend([0] * width)
                i += width
                continue

            # 应用缩放和偏移
            scale = try_finite_float(reg_config.get('scale', 1.0))
            offset = try_finite_float(reg_config.get('offset', 0)) or 0.0
            raw_value = to_raw(value, scale if scale else 1.0, offset)

            # round 369：**编解码必须同源**。原来这里硬编码 `>f` / `>i` / `>I`
            # （固定 ABCD），而 decode_float32 是按 byte_order 解的 ——
            # 配成 CDAB（施耐德 M340）、BADC、DCBA 的设备，写进去的浮点数
            # 读回来就是乱码或 NaN。
            if data_type == 'float32':
                results.extend(encode_float32_registers(
                    float(raw_value), self.byte_order))
            elif data_type in ('int32', 'uint32'):
                results.extend(encode_int32_registers(
                    int(round(raw_value)), self.byte_order,
                    signed=data_type == 'int32'))
            elif data_type in ('float64', 'int64', 'uint64'):
                # 真编码器（decode_float64_registers 的逆），不再用 float32+补零占位
                results.extend(encode_float64_registers(
                    float(raw_value), self.byte_order))
            else:
                # uint16/int16 等单字类型
                results.append(int(round(raw_value)) & 0xFFFF)
            i += width

        # 编码器返回的字数应与请求长度一致；少补多截，避免调用方拿到
        # 比 count 短的列表导致后续点位整体错位。
        if len(results) < count:
            results.extend([0] * (count - len(results)))
        return results[:count]
    
    def read_input_registers(self, address: int, count: int,
                             slave_id: Optional[int] = None) -> Optional[List[int]]:
        """读取输入寄存器（与保持寄存器逻辑相同）"""
        return self.read_holding_registers(address, count, slave_id)
    
    def read_coils(self, address: int, count: int,
                   slave_id: Optional[int] = None) -> Optional[List[bool]]:
        """读取线圈"""
        if not self.connected:
            return None
        
        self.stats['total_reads'] += 1
        self.stats['successful_reads'] += 1
        
        # round 369：**优先返回被写入的线圈值**。
        # 原来恒返回「设备是否 RUNNING」：非 RUNNING 时调用
        # `write_single_coil(..., verify=True)` 回读必然不一致 → 判定写入失败，
        # 而线圈其实已经写进去了。回读校验的前提是「读得到刚写的东西」。
        state = self.behavior_simulator.state
        default_on = state == DeviceState.RUNNING
        getter = getattr(self.behavior_simulator, 'get_written_coil_value', None)
        is_mechanical = bool(getattr(self.behavior_simulator, 'is_mechanical_device', True))
        estop_blocking = bool(_simulated_client._ESTOP_ACTIVE) and is_mechanical
        result: List[bool] = []
        for offset in range(count):
            if estop_blocking:
                result.append(False)
                continue
            written = None
            if getter is not None:
                try:
                    written = getter(address + offset)
                except Exception:       # noqa: BLE001
                    written = None
            result.append(default_on if written is None else bool(written))
        return result

    def set_discrete_input(self, address: int, value: bool) -> None:
        """注入离散输入状态（与输出线圈独立）。"""
        self._discrete_input_values[int(address)] = bool(value)

    def set_coil(self, address: int, value: bool) -> None:
        """直接设置线圈状态。"""
        self.behavior_simulator._written_coils[int(address)] = bool(value)

    def read_discrete_inputs(self, address: int, count: int,
                             slave_id: Optional[int] = None) -> Optional[List[bool]]:
        """读取离散输入（只读 `_discrete_input_values`，不看输出线圈）。"""
        if not self.connected:
            return None
        self.stats['total_reads'] += 1
        self.stats['successful_reads'] += 1
        is_mechanical = bool(getattr(self.behavior_simulator, 'is_mechanical_device', True))
        estop_blocking = bool(_simulated_client._ESTOP_ACTIVE) and is_mechanical
        result: List[bool] = []
        for offset in range(count):
            if estop_blocking:
                result.append(False)
            else:
                result.append(bool(self._discrete_input_values.get(address + offset, False)))
        return result
    
    def write_single_register(self, address: int, value: int,
                              slave_id: Optional[int] = None) -> bool:
        """写入单个寄存器 — 转发到行为模拟器影响模拟状态"""
        if not self.connected:
            return False
        if isinstance(value, bool) or (isinstance(value, float) and not math.isfinite(value)):
            return False
        int_val = int(value)
        logger.info(f"[增强模拟] 设备 {self.device_name} 写入寄存器: address={address}, value={int_val}")
        # 转发到行为模拟器，影响设备状态和物理参数
        self.behavior_simulator.handle_write_register(address, int_val)
        return True
    
    def write_single_coil(self, address: int, value: bool,
                          slave_id: Optional[int] = None) -> bool:
        """写入单个线圈 — 转发到行为模拟器影响模拟状态"""
        if not self.connected:
            return False
        
        logger.info(f"[增强模拟] 设备 {self.device_name} 写入线圈: address={address}, value={value}")
        # 转发到行为模拟器，影响设备状态
        self.behavior_simulator.handle_write_coil(address, bool(value))
        return True
    
    def decode_float32(self, registers: List[int]) -> float:
        """解码float32，支持4种字节序"""
        if len(registers) < 2:
            raise ValueError("需要至少2个寄存器")

        w1, w2 = registers[0], registers[1]

        if self.byte_order == ByteOrder.ABCD:
            raw = (w1 << 16) | w2
        elif self.byte_order == ByteOrder.BADC:
            # Byte-swap within each word, then combine
            w1_swapped = ((w1 & 0xFF) << 8) | ((w1 >> 8) & 0xFF)
            w2_swapped = ((w2 & 0xFF) << 8) | ((w2 >> 8) & 0xFF)
            raw = (w1_swapped << 16) | w2_swapped
        elif self.byte_order == ByteOrder.CDAB:
            # Word swap only
            raw = (w2 << 16) | w1
        elif self.byte_order == ByteOrder.DCBA:
            b0 = (w1 >> 8) & 0xFF
            b1 = w1 & 0xFF
            b2 = (w2 >> 8) & 0xFF
            b3 = w2 & 0xFF
            raw = (b3 << 24) | (b2 << 16) | (b1 << 8) | b0
        else:
            raw = (w1 << 16) | w2

        return struct.unpack('!f', struct.pack('!I', raw))[0]
    
    def decode_float64(self, registers: List[int]) -> float:
        """解码 float64，支持 4 种字节序（round 370：与 base_client 同源）。"""
        if len(registers) < 4:
            raise ValueError("需要至少4个寄存器")
        return decode_float64_registers(registers, self.byte_order)
    
    def decode_uint16(self, register: int) -> int:
        """解码uint16"""
        return register & 0xFFFF
    
    def decode_int16(self, register: int) -> int:
        """解码int16"""
        if register & 0x8000:
            return register - 0x10000
        return register
    
    def decode_int32(self, registers: List[int]) -> int:
        """解码 int32（round 370：按配置字节序，原先硬编码 ABCD）"""
        raw = reorder_32_words(registers[0], registers[1], self.byte_order)
        if raw & 0x80000000:
            raw -= 0x100000000
        return raw

    def decode_uint32(self, registers: List[int]) -> int:
        """解码 uint32（round 370：按配置字节序，原先硬编码 ABCD）"""
        return reorder_32_words(registers[0], registers[1], self.byte_order) & 0xFFFFFFFF
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            'device_id': self.device_id,
            'device_name': self.device_name,
            'connected': self.connected,
            'state': self.behavior_simulator.state.name,
            'health_score': self.behavior_simulator.health.overall_score,
            **self.stats
        }
    
    def get_latest_data(self) -> Dict[str, Any]:
        """获取最新数据"""
        with self._data_lock:
            # 停机设备返回空数据
            if is_device_stopped(self.device_id):
                return {}
            # round 383：断线不得把断线前的缓存当新鲜数据交出去
            if not self.connected:
                return {}
            return dict(self._latest_data)
    
    def inject_fault(self, fault_type: FaultType, severity: float = 0.5):
        """注入故障"""
        self.behavior_simulator.inject_fault(fault_type, severity)
    
    def force_state(self, state: DeviceState):
        """强制设置状态"""
        self.behavior_simulator.force_state(state)
    
    def inject_simulation_params(self, sim_params: dict):
        """注入模拟参数（由SimulationInitializer调用）"""
        self.behavior_simulator.inject_simulation_params(sim_params)


class EnhancedSimulatedOPCUAClient(PushClientInterface):
    """
    增强版模拟OPC UA客户端
    
    使用设备行为模拟器生成真实的工业数据
    """
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.connected = False
        
        # 创建设备行为模拟器
        self.behavior_simulator = DeviceBehaviorSimulator(
            device_id=config.get('id', 'unknown'),
            device_config=config
        )
        
        # 节点配置
        self.node_configs = config.get('nodes', [])
        
        # 缓存最新数据
        self.latest_data: Dict[str, Dict[str, Any]] = {}
        self._data_callbacks: List[Callable] = []
        self._push_thread: Optional[threading.Thread] = None
        self._running = False
        self._data_lock = threading.RLock()

        # 统计
        self.stats = {
            'connected_since': None,
            'nodes_subscribed': 0,
            'data_updates': 0,
            'errors': 0,
            'last_error': None
        }

        logger.info(f"[增强模拟] OPC UA客户端初始化: {config.get('name', 'unknown')}")
    
    def add_data_callback(self, callback: Callable):
        """添加数据回调"""
        self._data_callbacks.append(callback)
    
    def connect(self) -> bool:
        """连接"""
        self.connected = True
        self._running = True
        self.stats['connected_since'] = datetime.now().isoformat()
        self.stats['nodes_subscribed'] = len(self.node_configs)
        
        # 启动行为模拟器
        self.behavior_simulator.start()
        
        # 启动数据推送线程（round 374：同 Modbus 分支，不重复起线程）
        if getattr(self, '_push_thread', None) is None \
                or not self._push_thread.is_alive():
            self._push_thread = threading.Thread(target=self._push_loop, daemon=True)
            self._push_thread.start()

        logger.info(f"[增强模拟] OPC UA设备 {self.device_name} 连接成功")
        return True
    
    def disconnect(self):
        """断开连接"""
        self._running = False
        self.connected = False
        self.behavior_simulator.stop()
        logger.info(f"[增强模拟] OPC UA设备 {self.device_name} 已断开")
    
    def _push_loop(self):
        """数据推送循环"""
        while self._running and self.connected:
            try:
                self._generate_data()
                time.sleep(2)
            except Exception as e:
                logger.error(f"[增强模拟] OPC UA推送异常: {e}")
                time.sleep(1)
    
    def _generate_data(self):
        """生成数据"""
        with self._data_lock:
            _generate_enhanced_data(
                device_id=self.device_id,
                behavior_simulator=self.behavior_simulator,
                configs=self.node_configs,
                latest_data=self.latest_data,
                data_callbacks=self._data_callbacks,
                stats=self.stats,
                extra_field_fn=lambda cfg: {'node_id': cfg.get('node_id', '')},
                stat_inc_keys=['data_updates'],
                callback_error_label='OPC UA',
            )

    def get_latest_data(self) -> Dict[str, Dict[str, Any]]:
        """获取最新数据"""
        with self._data_lock:
            if self.connected:
                self._generate_data()
            # 停机设备返回空数据
            if self.behavior_simulator.state == DeviceState.STOPPED and not self.behavior_simulator.is_monitoring_device:
                return {}
            snapshot = {k: dict(v) for k, v in self.latest_data.copy().items()}
            # round 383：断线时旧值只能以 bad 质量交付
            if not self.connected:
                return _bad_quality_snapshot(snapshot)
            return snapshot
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            'device_id': self.device_id,
            'device_name': self.device_name,
            'connected': self.connected,
            'state': self.behavior_simulator.state.name,
            'health_score': self.behavior_simulator.health.overall_score,
            **self.stats
        }
    
    def datachange_notification(self, node, val, data):
        pass
    
    def event_notification(self, event):
        pass
    
    def status_change_notification(self, status):
        pass


class EnhancedSimulatedMQTTClient(PushClientInterface):
    """
    增强版模拟MQTT客户端
    
    使用设备行为模拟器生成真实的工业数据
    """
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.connected = False
        
        # 创建设备行为模拟器
        self.behavior_simulator = DeviceBehaviorSimulator(
            device_id=config.get('id', 'unknown'),
            device_config=config
        )
        
        # 主题配置
        self.topics_config = self.config.get('topics', [])
        
        # 缓存最新数据
        self.latest_data: Dict[str, Dict[str, Any]] = {}
        self._data_callbacks: List[Callable] = []
        self._subscriptions: Dict[str, int] = {}
        self._push_thread: Optional[threading.Thread] = None
        self._running = False
        
        # 统计
        self.stats = {
            'messages_received': 0,
            'messages_parsed': 0,
            'parse_errors': 0,
            'connected_since': None,
            'last_message_time': None
        }
        
        logger.info(f"[增强模拟] MQTT客户端初始化: {config.get('name', 'unknown')}")
    
    def add_data_callback(self, callback: Callable):
        """添加数据回调"""
        self._data_callbacks.append(callback)
    
    def connect(self) -> bool:
        """连接"""
        self.connected = True
        self._running = True
        self.stats['connected_since'] = datetime.now().isoformat()
        
        # 启动行为模拟器
        self.behavior_simulator.start()
        
        # 订阅主题
        for topic_cfg in self.topics_config:
            topic = topic_cfg.get('topic', '')
            if topic:
                self._subscriptions[topic] = topic_cfg.get('qos', 1)
        
        # 启动数据推送线程（round 374：同 OPC UA 分支，不重复起线程）
        if getattr(self, '_push_thread', None) is None \
                or not self._push_thread.is_alive():
            self._push_thread = threading.Thread(target=self._push_loop, daemon=True)
            self._push_thread.start()

        logger.info(f"[增强模拟] MQTT设备 {self.device_name} 连接成功")
        return True
    
    def disconnect(self):
        """断开连接"""
        self._running = False
        self.connected = False
        self.behavior_simulator.stop()
        logger.info(f"[增强模拟] MQTT设备 {self.device_name} 已断开")
    
    def subscribe(self, topic: str, qos: int = 1) -> bool:
        """订阅主题"""
        self._subscriptions[topic] = qos
        return True
    
    def unsubscribe(self, topic: str):
        """取消订阅"""
        self._subscriptions.pop(topic, None)
    
    def _push_loop(self):
        """数据推送循环"""
        while self._running and self.connected:
            try:
                self._generate_data()
                time.sleep(3)
            except Exception as e:
                logger.error(f"[增强模拟] MQTT推送异常: {e}")
                time.sleep(1)
    
    def _generate_data(self):
        """生成数据"""
        now = datetime.now().isoformat()
        _generate_enhanced_data(
            device_id=self.device_id,
            behavior_simulator=self.behavior_simulator,
            configs=self.topics_config,
            latest_data=self.latest_data,
            data_callbacks=self._data_callbacks,
            stats=self.stats,
            stat_inc_keys=['messages_received', 'messages_parsed'],
            callback_error_label='MQTT',
            dt=3.0,          # 与 _push_loop 的 3s 周期一致
        )
        self.stats['last_message_time'] = now
    
    def get_latest_data(self) -> Dict[str, Dict[str, Any]]:
        """获取最新数据"""
        # 停机设备返回空数据
        if self.behavior_simulator.state == DeviceState.STOPPED and not self.behavior_simulator.is_monitoring_device:
            return {}
        snapshot = dict(self.latest_data)
        # round 383：断线时旧值只能以 bad 质量交付
        if not self.connected:
            return _bad_quality_snapshot(snapshot)
        return snapshot
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            'device_id': self.device_id,
            'device_name': self.device_name,
            'connected': self.connected,
            'state': self.behavior_simulator.state.name,
            'health_score': self.behavior_simulator.health.overall_score,
            **self.stats
        }
    
    def get_status(self) -> Dict[str, Any]:
        """获取状态"""
        return {
            'connected': self.connected,
            'broker': f"{self.config.get('host', 'localhost')}:{self.config.get('port', 1883)}",
            'subscriptions': list(self._subscriptions.keys()),
            'stats': self.stats.copy()
        }


class EnhancedSimulatedRESTClient(PushClientInterface):
    """
    增强版模拟REST客户端
    
    使用设备行为模拟器生成真实的工业数据
    """
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.connected = False
        
        # 创建设备行为模拟器
        self.behavior_simulator = DeviceBehaviorSimulator(
            device_id=config.get('id', 'unknown'),
            device_config=config
        )
        
        # 端点配置
        self.endpoints = config.get('endpoints', [])
        
        # 缓存最新数据
        self.latest_data: Dict[str, Dict[str, Any]] = {}
        self._data_callbacks: List[Callable] = []
        
        # 统计
        self.stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'connected_since': None,
            'last_request_time': None,
            'last_error': None
        }
        
        logger.info(f"[增强模拟] REST客户端初始化: {config.get('name', 'unknown')}")

    def _resolve_endpoint_address(self, name: str) -> int | None:
        """从端点名称反查寄存器地址（用于写操作反馈到行为模拟器）"""
        if not hasattr(self, 'behavior_simulator') or not self.behavior_simulator:
            return None
        for addr, reg_info in self.behavior_simulator._register_address_map.items():
            if reg_info.get('name') == name:
                return addr
        return None
    
    def add_data_callback(self, callback: Callable):
        """添加数据回调"""
        self._data_callbacks.append(callback)
    
    def connect(self) -> bool:
        """连接"""
        self.connected = True
        self.stats['connected_since'] = datetime.now().isoformat()
        
        # 启动行为模拟器
        self.behavior_simulator.start()
        
        # 生成初始数据
        self._generate_data()
        
        logger.info(f"[增强模拟] REST设备 {self.device_name} 连接成功")
        return True
    
    def disconnect(self):
        """断开连接"""
        self.connected = False
        self.behavior_simulator.stop()
        logger.info(f"[增强模拟] REST设备 {self.device_name} 已断开")
    
    def get_latest_data(self) -> Dict[str, Dict[str, Any]]:
        """获取最新数据"""
        if self.connected:
            self._generate_data()
        # 停机设备返回空数据
        if self.behavior_simulator.state == DeviceState.STOPPED and not self.behavior_simulator.is_monitoring_device:
            return {}
        snapshot = dict(self.latest_data)
        # round 383：断线时旧值只能以 bad 质量交付
        if not self.connected:
            return _bad_quality_snapshot(snapshot)
        return snapshot
    
    def read_endpoint(self, endpoint_config: Dict[str, Any]) -> Any:
        """读取端点"""
        self.stats['total_requests'] += 1
        self.stats['last_request_time'] = datetime.now().isoformat()
        
        name = endpoint_config.get('name', 'unknown')
        
        # 更新行为模拟器
        data = self.behavior_simulator.update(1.0)
        
        # 从行为模拟器获取数据
        # 只有真正取到读数才算成功：原实现先计成功，后续异常时计数虚高
        self.stats['successful_requests'] += 1
        value = data.get(name, 50.0)

        return round(float(value), 2)
    
    def write_endpoint(self, endpoint_config: Dict[str, Any], value: Any, method: str = 'PUT') -> bool:
        """
        写入端点（支持POST/PUT方法）

        Args:
            endpoint_config: 端点配置
            value: 写入值
            method: HTTP方法 (POST/PUT)

        Returns:
            是否写入成功
        """
        name = endpoint_config.get('name', 'unknown')
        path = endpoint_config.get('path', '/')

        # 模拟写入延迟
        time.sleep(0.05)

        # 模拟写入成功/失败（95%成功率）
        success = random.random() < 0.95

        if success:
            # 更新本地缓存
            self.latest_data[name] = {
                'value': value,
                'unit': endpoint_config.get('unit', ''),
                'timestamp': datetime.now().isoformat(),
                'quality': 'good',
                'endpoint': path,
                'device_id': self.device_id,
                'method': method
            }

            # 更新行为模拟器的内部状态（通过名称反查寄存器地址）
            if hasattr(self, 'behavior_simulator') and self.behavior_simulator:
                addr = self._resolve_endpoint_address(name)
                if addr is not None:
                    # REST/OPC UA 点位写进来的是工程值
                    finite_val = None if isinstance(value, bool) else try_finite_float(value)
                    if finite_val is None or not isinstance(value, (int, float)):
                        # 非数值不能静默按 0 写进寄存器，否则测点被错误置零
                        logger.warning("[增强模拟] REST %s 写入非有限数值 %r，已拒绝写寄存器 %s",
                                       method, value, name)
                    else:
                        self.behavior_simulator.handle_write_register(
                            addr,
                            finite_val,
                            engineering=True)

            logger.info(f"[增强模拟] REST {method}写入成功: {path} -> {name} = {value}")
        else:
            logger.warning(f"[增强模拟] REST {method}写入失败: {path} -> {name} = {value}")

        self.stats['total_requests'] += 1
        if success:
            self.stats['successful_requests'] += 1
        else:
            self.stats['failed_requests'] += 1

        return success

    def post_endpoint(self, endpoint_config: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        """
        POST请求到端点（创建/提交操作）

        Args:
            endpoint_config: 端点配置
            data: POST数据

        Returns:
            响应数据
        """
        name = endpoint_config.get('name', 'unknown')
        path = endpoint_config.get('path', '/')

        # 模拟POST处理延迟
        time.sleep(0.1)

        # 模拟POST成功/失败（90%成功率）
        success = random.random() < 0.90

        if success:
            # 生成响应数据
            response = {
                'success': True,
                'message': f'{name} 操作成功',
                'data': data,
                'timestamp': datetime.now().isoformat(),
                'request_id': f'req_{int(time.time() * 1000)}'
            }

            logger.info(f"[增强模拟] REST POST成功: {path} -> {name}")
        else:
            response = {
                'success': False,
                'message': f'{name} 操作失败',
                'error': 'SERVICE_UNAVAILABLE',
                'timestamp': datetime.now().isoformat()
            }

            logger.warning(f"[增强模拟] REST POST失败: {path} -> {name}")

        self.stats['total_requests'] += 1
        if success:
            self.stats['successful_requests'] += 1
        else:
            self.stats['failed_requests'] += 1

        return response

    def put_endpoint(self, endpoint_config: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        """
        PUT请求到端点（更新操作）

        Args:
            endpoint_config: 端点配置
            data: PUT数据

        Returns:
            响应数据
        """
        name = endpoint_config.get('name', 'unknown')
        path = endpoint_config.get('path', '/')

        # 模拟PUT处理延迟
        time.sleep(0.08)

        # 模拟PUT成功/失败（95%成功率）
        success = random.random() < 0.95

        if success:
            # 更新本地缓存
            if 'value' in data:
                self.latest_data[name] = {
                    'value': data['value'],
                    'unit': endpoint_config.get('unit', ''),
                    'timestamp': datetime.now().isoformat(),
                    'quality': 'good',
                    'endpoint': path,
                    'device_id': self.device_id,
                    'method': 'PUT'
                }

            # 与 write_endpoint 对齐：回写行为模拟器，否则下次数据生成会覆盖写入值
            raw_put_val = data.get('value')
            finite_put_val = (
                None if isinstance(raw_put_val, bool)
                else try_finite_float(raw_put_val)
            )
            if isinstance(raw_put_val, (int, float)) and finite_put_val is not None:
                addr = self._resolve_endpoint_address(name)
                if addr is not None and getattr(self, 'behavior_simulator', None):
                    self.behavior_simulator.handle_write_register(
                        addr, finite_put_val, engineering=True)

            response = {
                'success': True,
                'message': f'{name} 更新成功',
                'data': data,
                'timestamp': datetime.now().isoformat()
            }

            logger.info(f"[增强模拟] REST PUT成功: {path} -> {name}")
        else:
            response = {
                'success': False,
                'message': f'{name} 更新失败',
                'error': 'CONFLICT',
                'timestamp': datetime.now().isoformat()
            }

            logger.warning(f"[增强模拟] REST PUT失败: {path} -> {name}")

        self.stats['total_requests'] += 1
        if success:
            self.stats['successful_requests'] += 1
        else:
            self.stats['failed_requests'] += 1

        return response

    def _generate_data(self):
        """生成数据"""
        _generate_enhanced_data(
            device_id=self.device_id,
            behavior_simulator=self.behavior_simulator,
            configs=self.endpoints,
            latest_data=self.latest_data,
            data_callbacks=self._data_callbacks,
            stats=self.stats,
            extra_field_fn=lambda cfg: {'endpoint': cfg.get('path', '')},
            callback_error_label='REST',
            dt=1.0,          # 与 REST 轮询周期一致
        )
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            'device_id': self.device_id,
            'device_name': self.device_name,
            'connected': self.connected,
            'state': self.behavior_simulator.state.name,
            'health_score': self.behavior_simulator.health.overall_score,
            **self.stats
        }
