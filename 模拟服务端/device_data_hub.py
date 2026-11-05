"""模拟服务端的数据中枢。

## 为什么需要这一层

模拟模式下，采集层拿到的是 ``EnhancedSimulatedModbusClient`` 直接返回的数值 ——
**没有一字节协议报文经过**。结果是：客户端的寄存器编解码、字节序、地址映射
全部没有被验证过，而这些恰好是现场调试时最容易出错、也最难远程定位的部分。

本模块把「造数」和「协议」拆开：

    DeviceBehaviorSimulator（已有，负责物理量耦合/工况/故障/写反馈）
                ↓ 每 tick 一次 update()
          DeviceDataHub（本模块，负责把工程值编码成 16 位寄存器）
                ↓ 被 Modbus/OPC UA/REST 从站读取
          真实协议服务端（modbus_slave_pool.py 等）

**造数逻辑一律复用 ``DeviceBehaviorSimulator``**，本模块不产生任何随机数，
也不自己编物理模型 —— 否则模拟模式会分裂出第二套「物理定律」，
现场看到的趋势和单元测试里的趋势对不上。

## 关键设计：工程值 vs 寄存器值

行为模拟器输出的是**工程值**（°C、MPa、m³/h）。Modbus 从站上跑的是
**原始 16 位寄存器**。两者之间差一个 ``scale`` 和一次按 ``byte_order``
的字节摆放。所有编码/解码都收口在 ``_encode_point`` / ``_decode_point``，
并且**刻意与 ``采集层/modbus_client.py`` 的 ``_combine_regs`` / ``decode_float32``
保持互逆** —— 两边一旦漂移，自测会"通过"而现场读出来是乱码。
"""

from __future__ import annotations

import logging
import math
import struct
import threading
import time
from typing import Any

from core.config_manager import ConfigManager
from core.safe_numeric import try_finite_float, try_int

from 采集层.device_behavior_simulator import DeviceBehaviorSimulator

logger = logging.getLogger(__name__)

#: data_type → 占用寄存器个数（Modbus 一个寄存器 16 位）。
#: 配置里写错的类型不能默默按 1 个寄存器处理 —— 那会让一个 float32 点位
#: 只占半个地址空间，相邻点位被覆盖，且没有任何报错。见 ``_build_register_index``。
_DATA_TYPE_WORDS: dict[str, int] = {
    'uint16': 1,
    'int16': 1,
    'uint32': 2,
    'int32': 2,
    'float32': 2,
    'float64': 4,
}

#: float32 能表示的最大有限值。超了 ``struct.pack('>f', ...)`` 抛 OverflowError，
#: 在从站线程里抛会打断整个请求处理；真实 PLC 的行为是饱和而不是崩。
_FLOAT32_MAX = 3.4028234663852886e38

_VALID_BYTE_ORDERS = ('ABCD', 'BADC', 'CDAB', 'DCBA')


def _split32(raw: int, order: str) -> list[int]:
    """把 32 位整数按 ``order`` 摆成两个 16 位寄存器。

    这是 ``ModbusClient._combine_regs`` 的逆运算。四种排列（32 位值 ABCD）：
    ABCD → ``[AB, CD]``；BADC → ``[BA, DC]``；CDAB → ``[CD, AB]``；DCBA → ``[DC, BA]``。
    """
    a = (raw >> 24) & 0xFF
    b = (raw >> 16) & 0xFF
    c = (raw >> 8) & 0xFF
    d = raw & 0xFF
    if order == 'BADC':
        return [(b << 8) | a, (d << 8) | c]
    if order == 'CDAB':
        return [(c << 8) | d, (a << 8) | b]
    if order == 'DCBA':
        return [(d << 8) | c, (b << 8) | a]
    return [(a << 8) | b, (c << 8) | d]


def _join32(words: list[int], order: str) -> int:
    """把两个 16 位寄存器按 ``order`` 拼回 32 位整数（与 ``_combine_regs`` 一致）。"""
    w1 = words[0] & 0xFFFF
    w2 = words[1] & 0xFFFF
    if order == 'BADC':
        w1 = ((w1 & 0xFF) << 8) | (w1 >> 8)
        w2 = ((w2 & 0xFF) << 8) | (w2 >> 8)
        return (w1 << 16) | w2
    if order == 'CDAB':
        return (w2 << 16) | w1
    if order == 'DCBA':
        b0 = (w1 >> 8) & 0xFF
        b1 = w1 & 0xFF
        b2 = (w2 >> 8) & 0xFF
        b3 = w2 & 0xFF
        return (b3 << 24) | (b2 << 16) | (b1 << 8) | b0
    return (w1 << 16) | w2


def _split64(raw: bytes, order: str) -> list[int]:
    """把 8 字节按 ``order`` 摆成四个寄存器。

    ⚠️ 这里的排列**不是**教科书定义，而是跟着 ``ModbusClient.decode_float64``
    的实际行为走：它对 ``BADC`` 做的是**字交换**（``regs[1], regs[0]``）而不是
    字节交换。从站若按教科书摆，客户端解出来就是另一个数 —— 所以以客户端为准，
    并在两边都不改的前提下保持一致。
    """
    w = [
        (raw[0] << 8) | raw[1],
        (raw[2] << 8) | raw[3],
        (raw[4] << 8) | raw[5],
        (raw[6] << 8) | raw[7],
    ]

    def _swap16(value: int) -> int:
        return ((value & 0xFF) << 8) | ((value >> 8) & 0xFF)

    if order == 'BADC':
        return [w[1], w[0], w[3], w[2]]
    if order == 'CDAB':
        return [w[2], w[3], w[0], w[1]]
    if order == 'DCBA':
        return [_swap16(w[3]), _swap16(w[2]), _swap16(w[1]), _swap16(w[0])]
    return w


def _join64(words: list[int], order: str) -> bytes:
    """``_split64`` 的逆运算，产出大端 8 字节（交给 ``struct.unpack('>d')``）。"""

    def _swap16(value: int) -> int:
        return ((value & 0xFF) << 8) | ((value >> 8) & 0xFF)

    w = [v & 0xFFFF for v in words[:4]]
    if order == 'BADC':
        w = [w[1], w[0], w[3], w[2]]
    elif order == 'CDAB':
        w = [w[2], w[3], w[0], w[1]]
    elif order == 'DCBA':
        w = [_swap16(w[3]), _swap16(w[2]), _swap16(w[1]), _swap16(w[0])]
    return struct.pack('>HHHH', *w)


class DeviceDataHub:
    """模拟服务端的数据中枢：一个后台线程按周期推进所有设备的行为模拟。

    线程模型：
      * 一个 daemon 线程按 ``tick`` 秒推进所有 ``DeviceBehaviorSimulator``；
      * 从站（Modbus/OPC UA/REST）在自己的线程里读 ``snapshot()`` / 调
        ``read_registers()``；
      * 写操作会立刻转交给行为模拟器（``handle_write_register`` /
        ``handle_write_coil``），所以「下发设定值 → 过程量跟着动」的闭环成立。
    """

    def __init__(self, config_path: str, tick: float = 1.0):
        """加载设备清单并构造行为模拟器。

        Args:
            config_path: 设备清单 YAML（``配置/devices_simulated.yaml``）。
                走 ``ConfigManager.load_yaml``，因此 ``${VAR}`` 会被展开。
            tick: 推进周期（秒）。非有限值/非正数回退到 1.0 —— 一个
                ``tick=0`` 会变成忙等线程，比配错更糟。
        """
        self._config_path = str(config_path)
        tick_value = try_finite_float(tick)
        self._tick = tick_value if tick_value and tick_value > 0 else 1.0

        config = ConfigManager.load_yaml(self._config_path) or {}
        raw_devices = config.get('devices') or []
        if not isinstance(raw_devices, list):
            raise ValueError(f'设备清单格式不对（devices 应为列表）: {self._config_path}')

        self._devices: dict[str, dict] = {}
        for entry in raw_devices:
            if not isinstance(entry, dict):
                continue
            device_id = str(entry.get('id') or '').strip()
            if not device_id:
                logger.warning('[数据中枢] 设备清单里有条目缺 id，已跳过: %r', entry.get('name'))
                continue
            if entry.get('enabled') is False:
                # 显式关闭的设备不该占端口、也不该产生数据
                continue
            if device_id in self._devices:
                logger.warning('[数据中枢] 设备 id 重复，后者覆盖前者: %s', device_id)
            self._devices[device_id] = entry

        if not self._devices:
            # 空清单必须炸 —— 否则从站池"启动成功但一台都没起"，现场排查时
            # 会以为协议不通，实际是配置路径写错了。
            raise ValueError(f'设备清单里没有可用设备: {self._config_path}')

        self._sims: dict[str, DeviceBehaviorSimulator] = {}
        self._snapshot: dict[str, dict[str, float]] = {}
        self._reg_maps: dict[str, dict[int, dict]] = {}
        self._addr_index: dict[str, dict[int, tuple[int, int, dict]]] = {}
        self._coil_maps: dict[str, dict[int, str]] = {}
        self._byte_orders: dict[str, str] = {}
        #: 写入覆盖层：{device_id: {起始地址: [原始寄存器...]}}。
        #: 从站语义里保持寄存器"记住最后一次写入值"，所以读回时优先返回它；
        #: 模拟器的下一拍输出不会覆盖掉刚写进去的值（否则写-读回验证永远失败）。
        self._overlay: dict[str, dict[int, list[int]]] = {}
        self._coil_overlay: dict[str, dict[int, bool]] = {}

        for device_id, device in self._devices.items():
            try:
                sim = DeviceBehaviorSimulator(device_id, device)
            except Exception:
                # 一台设备的模拟器构造失败不该让整个中枢起不来（42 台里坏一台，
                # 其余 41 台仍然可以给现场用），但必须留 ERROR 痕迹。
                logger.exception('[数据中枢] 设备 %s 的行为模拟器构造失败，该设备不提供服务',
                                 device_id)
                continue
            self._sims[device_id] = sim
            self._snapshot[device_id] = {}
            self._overlay[device_id] = {}
            self._coil_overlay[device_id] = {}
            reg_map, addr_index, coil_map, order = self._build_index(device_id, device)
            self._reg_maps[device_id] = reg_map
            self._addr_index[device_id] = addr_index
            self._coil_maps[device_id] = coil_map
            self._byte_orders[device_id] = order

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._ticks = 0
        self._last_tick_ms = 0.0

        logger.info('[数据中枢] 加载 %d 台设备（其中 %d 台有行为模拟器），tick=%.2fs',
                    len(self._devices), len(self._sims), self._tick)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> None:
        """起后台推进线程（幂等）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        for sim in self._sims.values():
            # 行为模拟器在 _running=False 时 update() 直接返回 {}，必须先 start()
            sim.start()
        # daemon：即使调用方忘了 stop()，进程也能退出（atexit 里 stop 仍会被调到）
        self._thread = threading.Thread(target=self._run, name='sim-data-hub', daemon=True)
        self._thread.start()
        logger.info('[数据中枢] 已启动，%d 台设备开始推进', len(self._sims))

    def stop(self) -> None:
        """停线程（幂等，可在 atexit 里安全调用）。"""
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=max(2.0, self._tick * 2))
            if thread.is_alive():
                logger.warning('[数据中枢] 后台线程 %.1fs 内未退出，可能有设备 update() 卡住',
                               max(2.0, self._tick * 2))
        for sim in self._sims.values():
            try:
                sim.stop()
            except Exception:
                logger.exception('[数据中枢] 停止设备 %s 的行为模拟器失败', sim.device_id)

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    @property
    def devices(self) -> dict[str, dict]:
        """device_id → 设备配置（原始 YAML 条目，只读）。"""
        return self._devices

    def snapshot(self, device_id: str) -> dict[str, float]:
        """device_id → {点位名: 当前工程值}，只含能取到有限数值的点位。"""
        # dict 整体替换是原子的，读侧无需加锁即可拿到一致的一份快照
        return self._snapshot.get(device_id) or {}

    def register_map(self, device_id: str) -> dict[int, dict]:
        """device_id → {起始地址: {'name','data_type','scale','count'}}。"""
        reg_map = self._reg_maps.get(device_id) or {}
        return {
            address: {
                'name': info['name'],
                'data_type': info['data_type'],
                'scale': info['scale'],
                'count': info['count'],
            }
            for address, info in reg_map.items()
        }

    def coil_map(self, device_id: str) -> dict[int, str]:
        """device_id → {线圈地址: 点位名}。"""
        return dict(self._coil_maps.get(device_id) or {})

    def stats(self) -> dict:
        """排查用计数：设备数 / 已推进拍数 / 上一拍耗时。"""
        return {
            'devices': len(self._sims),
            'ticks': self._ticks,
            'last_tick_ms': round(self._last_tick_ms, 2),
        }

    # ------------------------------------------------------------------
    # 协议侧读写（寄存器 / 线圈）
    # ------------------------------------------------------------------

    def read_registers(self, device_id: str, address: int, count: int) -> list[int]:
        """按协议地址读保持寄存器，返回**原始 16 位整数**列表。

        未映射的地址返回 0（现场从站常见行为：数据块是一整片内存，读未用区域
        得到 0，而不是异常）。这样采集层读超范围时能看到"有值但为 0"，
        而不是"通信异常"，便于区分配置错误和链路故障。
        """
        count_i = try_int(count)
        addr_i = try_int(address)
        if count_i is None or count_i <= 0:
            return []
        if addr_i is None or addr_i < 0:
            return [0] * count_i

        index = self._addr_index.get(device_id)
        if not index:
            return [0] * count_i

        overlay = self._overlay.get(device_id) or {}
        snap = self._snapshot.get(device_id) or {}
        order = self._byte_orders.get(device_id, 'ABCD')

        out: list[int] = []
        for protocol_addr in range(addr_i, addr_i + count_i):
            hit = index.get(protocol_addr)
            if hit is None:
                out.append(0)
                continue
            start, word_index, info = hit
            words = overlay.get(start)
            if words is None:
                words = self._encode_point(snap.get(info['name']), info, order)
            out.append(words[word_index] if word_index < len(words) else 0)
        return out

    def write_registers(self, device_id: str, address: int, values: list[int]) -> bool:
        """写寄存器：把原始寄存器值转回工程值交给行为模拟器。

        Returns:
            False 表示地址不在设备配置里（Modbus 从站应回异常码 02）。
        """
        index = self._addr_index.get(device_id)
        if not index:
            return False
        addr_i = try_int(address)
        if addr_i is None:
            return False
        hit = index.get(addr_i)
        if hit is None:
            logger.debug('[数据中枢] %s 写入未登记地址 %s，返回异常码 02', device_id, address)
            return False
        if not isinstance(values, (list, tuple)) or not values:
            return False

        start, word_index, info = hit
        order = self._byte_orders.get(device_id, 'ABCD')
        # 先取当前完整点位（含覆盖层），再把这几个字盖上去 —— 这样
        # "只写 float32 的高字"这种现场很常见的操作也能正确解码。
        words = self.read_registers(device_id, start, info['count'])
        for offset, raw in enumerate(values):
            position = word_index + offset
            if position >= info['count']:
                break
            raw_int = try_int(raw)
            if raw_int is None:
                logger.warning('[数据中枢] %s 地址 %s 的写入值 %r 不是整数，已拒绝',
                               device_id, start, raw)
                return False
            words[position] = raw_int & 0xFFFF

        engineering = self._decode_point(info, words, order)
        if engineering is None:
            # NaN/Inf 会污染整个行为模拟器（下游所有比较对 NaN 都是 False），
            # 宁可回异常码也不让它进来。
            logger.warning('[数据中枢] %s 地址 %s 的写入解码出非有限值，已拒绝',
                           device_id, start)
            return False

        with self._lock:
            self._overlay[device_id][start] = list(words)
            sim = self._sims.get(device_id)
            if sim is None:
                logger.warning('[数据中枢] %s 没有行为模拟器，写入只落在覆盖层', device_id)
                return True
            try:
                # round 370（BUG-088）：这里已经是 `_decode_point()` 解码后的
                # **工程值**，必须显式告诉模拟器"不要再乘一次 scale" ——
                # 原来按原始值处理，带 scale=0.1 的设定写 50 会被算成 500。
                sim.handle_write_register(start, engineering, engineering=True)
            except Exception:
                logger.exception('[数据中枢] %s 写寄存器 %s 时行为模拟器抛异常',
                                 device_id, start)
                return False
        logger.debug('[数据中枢] %s 写地址 %s = %r（工程值 %r）',
                     device_id, start, values, engineering)
        return True

    def write_coil(self, device_id: str, address: int, value: bool) -> bool:
        """写线圈，交给行为模拟器（``_written_coils`` 会反映到继电器/信号灯输出）。"""
        if device_id not in self._sims:
            return False
        addr_i = try_int(address)
        if addr_i is None or addr_i < 0 or addr_i > 0xFFFF:
            return False
        flag = bool(value)
        with self._lock:
            sim = self._sims[device_id]
            try:
                sim.handle_write_coil(addr_i, flag)
            except Exception:
                logger.exception('[数据中枢] %s 写线圈 %s 时行为模拟器抛异常', device_id, addr_i)
                return False
            self._coil_overlay[device_id][addr_i] = flag
        return True

    def read_coils(self, device_id: str, address: int, count: int) -> list[bool]:
        """读线圈状态（先看写入覆盖层，再看模拟器输出的同名点位）。"""
        count_i = try_int(count)
        addr_i = try_int(address)
        if count_i is None or count_i <= 0:
            return []
        if addr_i is None or addr_i < 0:
            return [False] * count_i

        coil_map = self._coil_maps.get(device_id) or {}
        overlay = self._coil_overlay.get(device_id) or {}
        snap = self._snapshot.get(device_id) or {}

        out: list[bool] = []
        for protocol_addr in range(addr_i, addr_i + count_i):
            if protocol_addr in overlay:
                out.append(overlay[protocol_addr])
                continue
            name = coil_map.get(protocol_addr)
            if name is None:
                out.append(False)
                continue
            value = try_finite_float(snap.get(name))
            out.append(bool(value) and value != 0.0)
        return out

    def last_written_value(self, device_id: str, address: int) -> float | None:
        """行为模拟器最近一次从该地址收到的**工程值**（排查"写到底进没进模拟器"）。

        读寄存器只能证明"从站收到了"，证明不了"模拟器收到了" —— 两者之间还有
        一次 scale/字节序转换，那正是最容易出错的地方，所以单独给一个查询口。
        """
        sim = self._sims.get(device_id)
        addr_i = try_int(address)
        if sim is None or addr_i is None:
            return None
        return sim.get_written_register_value(addr_i)

    def last_written_coil(self, device_id: str, address: int) -> bool | None:
        """行为模拟器最近一次从该线圈地址收到的值。"""
        sim = self._sims.get(device_id)
        addr_i = try_int(address)
        if sim is None or addr_i is None:
            return None
        return sim.get_written_coil_value(addr_i)

    def write_point(self, device_id: str, point_name: str, value: Any) -> bool:
        """按**点位名**写值（OPC UA / REST 这类没有寄存器地址的协议用）。

        内部仍然转成寄存器语义，保证「不管从哪个协议写进来，行为模拟器收到的
        都是同一个工程值」。
        """
        name = str(point_name or '')
        if not name:
            return False
        for address, info in (self._reg_maps.get(device_id) or {}).items():
            if info['name'] == name:
                eng = try_finite_float(value)
                if eng is None:
                    logger.warning('[数据中枢] %s 点位 %s 的写入值 %r 不是有限数值',
                                   device_id, name, value)
                    return False
                words = self._encode_point(eng, info, self._byte_orders.get(device_id, 'ABCD'))
                return self.write_registers(device_id, address, words)
        for address, coil_name in (self._coil_maps.get(device_id) or {}).items():
            if coil_name == name:
                return self.write_coil(device_id, address, bool(value))

        # nodes / topics / endpoints 段的点位没有寄存器地址 —— 交给行为模拟器的
        # 点位写入接口（``set_point_override``）。
        #
        # round 351 之前这里直接 return False，于是 OPC UA / REST / MQTT 设备
        # **只能读不能控**：界面上改设定值，服务端返回成功，模拟器完全没收到，
        # 下一拍 snapshot() 推送又把它冲回模拟值。
        # 覆盖逻辑收口在模拟器里（``_generate_output_data`` 之后叠加），
        # 本模块只负责转交，不另建一套"点位覆盖层"——两套语义迟早会打架。
        sim = self._sims.get(device_id)
        if sim is None:
            logger.warning('[数据中枢] %s 没有行为模拟器，点位 %s 无法写入', device_id, name)
            return False
        setter = getattr(sim, 'set_point_override', None)
        if setter is None:
            logger.error('[数据中枢] %s 的行为模拟器不支持按点位写入（set_point_override 缺失），'
                         '点位 %s 写入被拒绝', device_id, name)
            return False
        try:
            # round 373：**写覆盖层必须与 tick 同一把锁**。
            # `_tick_once()` 持有 `self._lock` 调 `sim.update()`，而 update 会
            # 迭代 `_point_overrides`；这里原来在锁外改同一个字典 ——
            # OPC UA/REST 写入撞上采样拍就会 RuntimeError（写成功但服务端报错）。
            with self._lock:
                accepted = bool(setter(name, value))
        except Exception:
            logger.exception('[数据中枢] %s 点位 %s 交给 set_point_override 失败',
                             device_id, name)
            return False
        if not accepted:
            return False

        # 模拟器的覆盖要等下一拍 update() 才反映到快照（≤1s）。REST 是"写完立刻读"，
        # 这 1 秒里读到的还是旧值，会被误判成写入失败 —— 所以这里同步补一次快照。
        numeric = try_finite_float(value)
        if numeric is not None:
            with self._lock:
                snapshot = dict(self._snapshot.get(device_id) or {})
                snapshot[name] = numeric
                self._snapshot[device_id] = snapshot
        logger.debug('[数据中枢] %s 点位 %s = %r（交给行为模拟器）', device_id, name, value)
        return True

    # ------------------------------------------------------------------
    # 内部：索引构建
    # ------------------------------------------------------------------

    def _build_index(
        self, device_id: str, device: dict
    ) -> tuple[dict[int, dict], dict[int, tuple[int, int, dict]], dict[int, str], str]:
        """从设备配置构建寄存器表、地址索引、线圈表、字节序。"""
        order = str(device.get('byte_order') or 'ABCD').strip().upper()
        if order not in _VALID_BYTE_ORDERS:
            logger.warning('[数据中枢] %s 的 byte_order=%r 非法，按 ABCD 处理',
                           device_id, device.get('byte_order'))
            order = 'ABCD'

        reg_map: dict[int, dict] = {}
        addr_index: dict[int, tuple[int, int, dict]] = {}
        coil_map: dict[int, str] = {}

        for reg in device.get('registers') or []:
            if not isinstance(reg, dict):
                continue
            name = str(reg.get('name') or '').strip()
            if str(reg.get('kind') or '').strip().lower() == 'coil':
                # 线圈点位和保持寄存器共用 registers 段（现场清单常见写法），
                # 靠 kind 区分，否则会给线圈点位分配寄存器地址。
                address = try_int(reg.get('address'))
                if address is not None and name:
                    coil_map[address] = name
                continue
            address = try_int(reg.get('address'))
            if address is None or address < 0 or address > 0xFFFF:
                logger.warning('[数据中枢] %s 寄存器 %r 的 address 非法，已跳过',
                               device_id, name or reg)
                continue
            if not name:
                logger.warning('[数据中枢] %s 地址 %s 缺 name，已跳过', device_id, address)
                continue

            data_type = str(reg.get('data_type') or 'uint16').strip().lower()
            count = _DATA_TYPE_WORDS.get(data_type)
            if count is None:
                logger.warning('[数据中枢] %s 点位 %s 的 data_type=%r 不支持，'
                               '按 1 个寄存器 uint16 处理（相邻点位可能被覆盖）',
                               device_id, name, data_type)
                data_type, count = 'uint16', 1

            scale = try_finite_float(reg.get('scale', 1.0))
            if scale is None or scale == 0:
                # scale=0 会让 raw=eng/scale 直接除零；配置写错不该让从站崩
                logger.warning('[数据中枢] %s 点位 %s 的 scale=%r 非法，按 1.0 处理',
                               device_id, name, reg.get('scale'))
                scale = 1.0
            offset = try_finite_float(reg.get('offset', 0.0)) or 0.0

            if address in reg_map:
                logger.warning('[数据中枢] %s 地址 %s 重复登记（%s 覆盖 %s）',
                               device_id, address, name, reg_map[address]['name'])

            info = {
                'name': name,
                'data_type': data_type,
                'scale': scale,
                'offset': offset,
                'count': count,
            }
            reg_map[address] = info
            for word_offset in range(count):
                addr_index[address + word_offset] = (address, word_offset, info)

        for coil in device.get('coils') or []:
            if not isinstance(coil, dict):
                continue
            address = try_int(coil.get('address'))
            name = str(coil.get('name') or '').strip()
            if address is None or not name:
                logger.warning('[数据中枢] %s 线圈条目非法，已跳过: %r', device_id, coil)
                continue
            coil_map[address] = name

        return reg_map, addr_index, coil_map, order

    # ------------------------------------------------------------------
    # 内部：编解码
    # ------------------------------------------------------------------

    def _encode_point(self, value: Any, info: dict, order: str) -> list[int]:
        """工程值 → 原始寄存器（先减 offset、再除 scale，最后按类型摆放）。"""
        count = info['count']
        engineering = try_finite_float(value)
        if engineering is None:
            # 模拟器这一拍没给出该点位（或给了 NaN）→ 从站读到 0，
            # 与"设备没上电"的表现一致，而不是抛异常打断整个读请求。
            return [0] * count

        scaled = (engineering - info['offset']) / info['scale']
        data_type = info['data_type']

        if data_type == 'float32':
            if not math.isfinite(scaled):
                return [0] * count
            clamped = max(-_FLOAT32_MAX, min(_FLOAT32_MAX, scaled))
            raw = struct.unpack('>I', struct.pack('>f', clamped))[0]
            return _split32(raw, order)

        if data_type == 'float64':
            if not math.isfinite(scaled):
                return [0] * count
            return _split64(struct.pack('>d', scaled), order)

        if data_type in ('uint32', 'int32'):
            raw = int(round(scaled)) & 0xFFFFFFFF
            return _split32(raw, order)

        # uint16 / int16：负数按补码落位（现场 PLC 就是这么存的）
        return [int(round(scaled)) & 0xFFFF]

    def _decode_point(self, info: dict, words: list[int], order: str) -> float | None:
        """原始寄存器 → 工程值（``_encode_point`` 的逆运算）。

        Returns:
            工程值；非有限值返回 None（由调用方拒绝写入）。
        """
        data_type = info['data_type']
        if data_type in ('float32', 'uint32', 'int32'):
            if len(words) < 2:
                return None
            raw = _join32(words, order)
            if data_type == 'float32':
                value = struct.unpack('>f', struct.pack('>I', raw))[0]
            elif data_type == 'int32':
                value = raw - 0x100000000 if raw & 0x80000000 else raw
            else:
                value = raw
        elif data_type == 'float64':
            if len(words) < 4:
                return None
            value = struct.unpack('>d', _join64(words, order))[0]
        else:
            if not words:
                return None
            raw = words[0] & 0xFFFF
            if data_type == 'int16' and raw & 0x8000:
                value = raw - 0x10000
            else:
                value = raw

        engineering = value * info['scale'] + info['offset']
        return engineering if math.isfinite(engineering) else None

    # ------------------------------------------------------------------
    # 内部：推进线程
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """后台推进循环。

        用 ``time.monotonic()`` 而不是 ``time.time()``：墙上时钟会被 NTP
        校正，一次 -5s 的跳变会让 ``wait()`` 立刻返回、连着补几十拍，
        模拟出来的趋势出现陡坡，排查时会被误判成设备异常。
        """
        next_at = time.monotonic()
        while not self._stop_event.is_set():
            began = time.perf_counter()
            self._tick_once()
            self._last_tick_ms = (time.perf_counter() - began) * 1000.0

            next_at += self._tick
            now = time.monotonic()
            if next_at < now:
                # 这一拍已经超时（机器忙 / 设备多）→ 对齐到当前时刻，
                # 不补拍：补拍会让 dt 失真，物理模型算出来的趋势是假的。
                next_at = now
            self._stop_event.wait(next_at - now)

    def _tick_once(self) -> None:
        """推进所有设备一次。单台设备异常只影响它自己。"""
        self._ticks += 1
        with self._lock:
            for device_id, sim in self._sims.items():
                try:
                    data = sim.update(self._tick)
                except Exception:
                    # 隔离：一台设备的模拟崩了，其余设备必须继续出数；
                    # 但不能静默 —— 现场看到某台设备数据"冻住"时，
                    # 日志里要有对应的 ERROR 可查。
                    logger.exception('[数据中枢] 设备 %s 的行为模拟 update() 失败，'
                                     '该设备本拍保持上一拍的值', device_id)
                    continue
                if not isinstance(data, dict):
                    logger.error('[数据中枢] 设备 %s 的 update() 返回 %s，期望 dict',
                                 device_id, type(data).__name__)
                    continue

                clean: dict[str, object] = {}
                for key, raw_value in data.items():
                    if not isinstance(key, str) or key.startswith('_'):
                        # `_device_state` / `_active_faults` 这类是元数据，不是点位
                        continue
                    numeric = try_finite_float(raw_value)
                    if numeric is None:
                        # BUG-089：**字符串/布尔状态点位不能丢**。
                        # `agv_system_status`、`machine_mode` 这类本来就不是数值，
                        # 原来在这里被整条过滤 → REST 仿真网关永远读不到它们，
                        # OPC UA 侧也只剩 float，Boolean 节点被建成 Double。
                        if isinstance(raw_value, (str, bool)):
                            clean[key] = raw_value
                        continue
                    clean[key] = numeric
                self._snapshot[device_id] = clean
