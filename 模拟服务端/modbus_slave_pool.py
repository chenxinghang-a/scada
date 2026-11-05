"""Modbus TCP 从站池：每台模拟设备一个真实的 TCP 从站。

## 为什么要「一台设备一个端口」

现场是「一台设备一个 IP:502」。如果所有模拟设备共用一个从站，采集层里
「按设备配置的 host:port 建连」这条路径就从来没被真正走过 —— 端口写错、
设备串号、从站地址配错都不会暴露。所以这里按设备起独立监听，端口从
``base_port`` 递增分配（默认 15020、15021……），模拟的就是那张拓扑表。

## pymodbus 3.9.2 的两个坑（都踩过）

1. ``ModbusSlaveContext.getValues()`` / ``setValues()`` 里硬编码了
   ``address += 1``（协议地址 0 → 数据块下标 1）。本模块的数据块在
   ``getValues`` / ``setValues`` 里把这个偏移减回去，并在构造时垫一个占位
   元素 —— 少任何一半，读出来的值都会整体错位一个寄存器。
2. ``ModbusSlaveContext.__init__`` 写的是
   ``self.store["c"] = co if di is not None else ...`` —— **不传 ``di`` 时
   ``co`` 会被静默丢弃**，线圈写进去直接消失、读回永远是 0，且没有任何报错。
   所以四个块必须全部显式传入。

## 数据块不做缓存

块里的 ``getValues`` 直接问 ``DeviceDataHub``，``setValues`` 直接写回
``DeviceDataHub``（→ 行为模拟器）。**不维护寄存器副本**：一旦有副本，
「主站写设定值 → 过程量变化」的闭环就会断在副本上，而且副本与 hub 的
一致性没有任何东西保证。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import ModbusTcpServer

from core.safe_numeric import try_int

from .device_data_hub import DeviceDataHub

logger = logging.getLogger(__name__)

#: 从站异常码：非法数据地址（GB/T 19582 / Modbus 规范 0x02）。
ILLEGAL_ADDRESS = 0x02


class _HubRegisterBlock(ModbusSequentialDataBlock):
    """保持寄存器 / 输入寄存器块：读写全部委托给 hub。"""

    def __init__(self, hub: DeviceDataHub, device_id: str):
        # 占位元素：见模块 docstring 的坑 1。这里不保存任何真实数据，
        # 只为让父类的构造与 reset() 有个合法的 values 列表。
        super().__init__(0, [0])
        self._hub = hub
        self._device_id = device_id

    def getValues(self, address: int, count: int = 1) -> list[int]:
        # pymodbus 已经做过 +1，这里减回去换成协议地址
        return self._hub.read_registers(self._device_id, address - 1, count)

    def setValues(self, address: int, values: list[int]) -> int | None:
        if not self._hub.write_registers(self._device_id, address - 1, list(values)):
            # 地址没登记 → 必须让主站收到 0x02。回一个"成功"的回显会让
            # 上位机以为真的写进去了（现场最常见的假成功）。
            return ILLEGAL_ADDRESS
        return None


class _HubCoilBlock(ModbusSequentialDataBlock):
    """线圈 / 离散输入块：委托给 hub 的线圈接口。"""

    def __init__(self, hub: DeviceDataHub, device_id: str):
        super().__init__(0, [False])
        self._hub = hub
        self._device_id = device_id

    def getValues(self, address: int, count: int = 1) -> list[bool]:
        return self._hub.read_coils(self._device_id, address - 1, count)

    def setValues(self, address: int, values: list[bool]) -> int | None:
        ok = True
        for offset, value in enumerate(values):
            if not self._hub.write_coil(self._device_id, address - 1 + offset, bool(value)):
                ok = False
        return None if ok else ILLEGAL_ADDRESS


class _HubSlaveContext(ModbusSlaveContext):
    """修正父类把写入返回码丢掉的问题（见模块 docstring）。"""

    def setValues(self, fc_as_hex: int, address: int, values: Any) -> int | None:
        # 父类只调用 datablock.setValues 而不返回它的结果，于是 0x02 永远
        # 到不了主站。这里照抄父类逻辑，唯一区别是把返回值透传出去。
        address += 1  # 与父类的硬编码偏移保持一致
        return self.store[self.decode(fc_as_hex)].setValues(address, values)


class ModbusSlavePool:
    """按设备配置动态构建一组 Modbus TCP 从站。

    一个事件循环线程承载全部从站：从站数量在几十台量级，每台一个线程
    会白占几十个线程栈，而且启停顺序更难保证。
    """

    def __init__(self, hub: DeviceDataHub, base_port: int = 15020):
        self._hub = hub
        base = try_int(base_port)
        if base is None or not 1 <= base <= 65535:
            logger.warning('[从站池] base_port=%r 非法，回退到 15020', base_port)
            base = 15020
        self._base_port = base
        self._ports: dict[str, int] = {}
        self._servers: dict[str, ModbusTcpServer] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        # RLock 而不是 Lock：start() 在起从站失败时会回调 stop() 做清理，
        # 普通 Lock 在同一线程里二次获取会**永久死锁** —— 表现为"启动失败后
        # 进程既不退出也不报错"，端口一直占着（实测踩到过）。
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------

    def start(self) -> dict[str, int]:
        """起所有从站，返回 {device_id: port}。端口冲突会抛 OSError。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return dict(self._ports)

            targets = self._targets()
            if not targets:
                logger.error('[从站池] 没有可服务的 modbus_tcp 设备，未启动任何从站')

            loop = asyncio.new_event_loop()
            self._loop = loop
            self._thread = threading.Thread(
                target=self._run_loop, args=(loop,), name='modbus-slave-pool', daemon=True
            )
            self._thread.start()

            try:
                for device_id, device, port in targets:
                    future = asyncio.run_coroutine_threadsafe(
                        self._start_one(device_id, device, port), loop
                    )
                    future.result(timeout=15)
                    self._ports[device_id] = port
            except Exception:
                # 起了一半就失败：必须把已经起来的收掉，否则端口泄漏，
                # 下一次启动全部失败且原因指向"端口被占用"。
                logger.exception('[从站池] 启动过程中失败，回收已启动的从站')
                self.stop()
                raise

            logger.info('[从站池] 已启动 %d 台从站（端口 %d-%d）',
                        len(self._ports), self._base_port,
                        self._base_port + max(len(self._ports) - 1, 0))
            return dict(self._ports)

    def stop(self) -> None:
        """关闭全部从站并释放端口（幂等，可在 atexit 里安全调用）。"""
        with self._lock:
            loop = self._loop
            servers = list(self._servers.values())
            self._servers.clear()
            self._ports.clear()
            thread = self._thread
            self._thread = None
            self._loop = None

            if loop is None:
                return
            try:
                # 统一在事件循环里关闭并**让出几轮**，见 _shutdown_servers 的说明
                asyncio.run_coroutine_threadsafe(
                    self._shutdown_servers(servers), loop
                ).result(timeout=10)
            except Exception:
                logger.exception('[从站池] 关闭从站失败，端口可能未释放')
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                logger.warning('[从站池] 事件循环已停止，跳过 stop 调用')

        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
            if thread.is_alive():
                logger.warning('[从站池] 事件循环线程 5s 内未退出')

    def ports(self) -> dict[str, int]:
        """{device_id: port}（未启动时为空）。"""
        return dict(self._ports)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _targets(self) -> list[tuple[str, dict, int]]:
        """挑出需要起 TCP 从站的设备，并按顺序分配端口。

        只处理 ``modbus_tcp``：``modbus_rtu`` 走串口，起不了 TCP 从站
        （现场 RTU 需要真实串口或串口转 TCP 网关，不是本工具的职责）。
        """
        targets: list[tuple[str, dict, int]] = []
        port = self._base_port
        skipped_rtu: list[str] = []
        for device_id, device in self._hub.devices.items():
            protocol = str(device.get('protocol') or '').strip().lower()
            if protocol == 'modbus_rtu':
                skipped_rtu.append(device_id)
                continue
            if protocol != 'modbus_tcp':
                continue
            if not self._hub.register_map(device_id) and not self._hub.coil_map(device_id):
                # 有设备声明了 modbus_tcp 却一个点位都没登记：起个空从站
                # 只会让采集层"连得上但全是 0"，不如明确报出来。
                logger.warning('[从站池] 设备 %s 是 modbus_tcp 但没登记任何寄存器/线圈，跳过',
                               device_id)
                continue
            # BUG-090：设备配置里写死的 `sim_endpoint.port` 必须优先。
            # 原来一律从 base_port 按枚举顺序递增 —— 设备顺序一调整（或中间
            # 禁用一台），后面所有从站端口整体错位，采集端连到别人的从站上，
            # 数据全串台。
            sim = device.get('sim_endpoint')
            fixed_port = sim.get('port') if isinstance(sim, dict) else None
            if isinstance(fixed_port, int) and 0 < fixed_port < 65536:
                targets.append((device_id, device, fixed_port))
                port = max(port, fixed_port + 1)
                continue
            if port > 65535:
                logger.error('[从站池] 端口耗尽，设备 %s 及之后的设备不会启动', device_id)
                break
            targets.append((device_id, device, port))
            port += 1
        if skipped_rtu:
            logger.info('[从站池] 跳过 %d 台 modbus_rtu 设备（串口不在本工具范围）: %s',
                        len(skipped_rtu), ', '.join(skipped_rtu))
        return targets

    async def _shutdown_servers(self, servers: list[ModbusTcpServer]) -> None:
        """逐个关闭从站，并让事件循环把关闭回调跑完。

        ``server.shutdown()`` 内部只是 ``transport.close()`` —— 真正的
        ``connection_lost``（关闭监听 socket 的地方）是**排到下一轮事件循环**的。
        如果紧接着 ``loop.stop()``，这些回调永远不执行，端口会一直处于
        LISTEN 状态：同一个进程内重启会报"地址已被占用"，而进程退出后才恢复。
        所以关完必须 ``await`` 几轮，把回调放出去。
        """
        for server in servers:
            try:
                await server.shutdown()
            except Exception:
                # 一台关不掉不能阻断其余从站关闭（否则端口泄漏得更多）
                logger.exception('[从站池] 关闭单个从站失败，该端口可能未释放')
        for _ in range(3):
            await asyncio.sleep(0)

    async def _start_one(self, device_id: str, device: dict, port: int) -> None:
        """在事件循环里起一台从站，监听成功后才返回。"""
        slave_id = try_int(device.get('slave_id', 1))
        if slave_id is None or not 0 <= slave_id <= 247:
            logger.warning('[从站池] 设备 %s 的 slave_id=%r 非法，按 1 处理',
                           device_id, device.get('slave_id'))
            slave_id = 1

        # 四个块全部显式给：不传 di 会让 co 被静默丢弃（见模块 docstring 坑 2）
        context = _HubSlaveContext(
            di=_HubCoilBlock(self._hub, device_id),
            co=_HubCoilBlock(self._hub, device_id),
            ir=_HubRegisterBlock(self._hub, device_id),
            hr=_HubRegisterBlock(self._hub, device_id),
        )
        server_context = ModbusServerContext(slaves={slave_id: context}, single=False)
        server = ModbusTcpServer(server_context, address=('127.0.0.1', port))

        listening = await server.listen()
        if not listening:
            # listen() 内部吞掉 OSError 只返回 False，不自己抛 —— 不在这里
            # 转成异常的话，start() 会返回一个"有端口但没监听"的假结果。
            raise OSError(f'从站 {device_id} 无法监听 127.0.0.1:{port}（端口被占用？）')

        self._servers[device_id] = server
        logger.info('[从站池] %s 监听 127.0.0.1:%d (slave_id=%d)', device_id, port, slave_id)

    def _run_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """事件循环线程入口。"""
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            # run_forever 返回后还有未完成的任务（连接处理协程），不取消
            # 会让 loop.close() 打一堆 "Task was destroyed but it is pending"
            try:
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    # 加超时：pymodbus 的连接处理协程有可能吞掉 CancelledError，
                    # 那样 gather 会永远不返回，线程虽然 daemon 但会一直占着
                    # socket 到进程结束 —— 表现为"关不掉、端口不释放"。
                    loop.run_until_complete(asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True), timeout=3.0
                    ))
            except (asyncio.TimeoutError, asyncio.CancelledError):
                logger.warning('[从站池] 事件循环收尾超时，强制关闭（个别连接协程未响应取消）')
            except Exception:
                logger.exception('[从站池] 事件循环收尾时异常')
            finally:
                loop.close()
