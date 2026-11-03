"""OPC UA 模拟服务端：用 asyncua 起一台**真**服务器，让采集层走真协议线。

## 为什么需要它

原来的「模拟模式」是进程内假客户端（`采集层/simulated_client.py` 直接返回
随机数），完全没有协议报文。于是现场调试时这几件事一件都验证不了：

* 客户端能不能连上（endpoint / 安全策略 / 端口）
* 节点地址空间对不对（`node_id` 写错时客户端只会静默收不到数据）
* 订阅能不能收到 datachange（采集层靠订阅，不靠轮询读）

本模块把 `DeviceDataHub` 的仿真值暴露成真正的 OPC UA 地址空间，
`采集层.opcua_client.OPCUAClient` 可以像连真 PLC 一样连上来。

## 地址空间

    Objects/SCADA/<device_id>/<point_name>

namespace index 由 `register_namespace()` 分配，**绝不硬编码** —— asyncua 的
索引取决于服务器已注册的命名空间数量，硬编码 `ns=2` 在换版本/换初始化顺序后
会静默失效（客户端表现为「连上了但一个点位都订不上」）。需要 node_id 的调用方
一律用 `node_ids()` 拿真值。

## 线程模型

asyncua 是 asyncio 的，而 SCADA 主程序是同步的，所以服务器跑在自己的线程 +
自己的事件循环里；`start()` 用 `threading.Event` 阻塞到地址空间建完、端口真正
在 listen 之后才返回，避免调用方「启动成功但立刻连接被拒」。
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import threading
from typing import Any

from core.safe_numeric import try_finite_float, try_int

logger = logging.getLogger(__name__)

try:
    from asyncua import Server, ua

    OPCUA_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖缺失时由 start() 明确报错
    OPCUA_AVAILABLE = False
    Server = None  # type: ignore[assignment]
    ua = None  # type: ignore[assignment]
    logger.warning("asyncua 未安装，OPC UA 模拟服务端不可用。请运行: pip install opcua-asyncio")

#: 标记「这次写值来自本模块的周期推送」，用来把客户端写入和内部推送区分开。
#:
#: 用 ContextVar 而不是布尔字段：`write_attribute` 是 await 的，布尔字段在
#: await 期间可能被其它任务看到，导致「客户端写入被误判成内部推送」→ 写入丢失。
#: ContextVar 是 per-task 的，天然不受并发交错影响。
_INTERNAL_WRITE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    'opcua_sim_internal_write', default=False
)


class OpcuaSimServer:
    """一台服务器承载所有 `protocol: opcua` 设备（用 folder 区分设备）。"""

    def __init__(self, hub: Any, port: int = 15400, namespace: str = 'urn:scada:sim'):
        self.hub = hub
        self.port = port
        self.namespace = namespace

        #: {device_id: {point_name: node_id 字符串}}
        self._node_ids: dict[str, dict[str, str]] = {}
        #: {device_id: {point_name: (Node, ua.VariantType)}}
        self._nodes: dict[str, dict[str, tuple[Any, Any]]] = {}

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: Any = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._start_error: str | None = None

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> bool:
        """起服务并**阻塞到就绪**。

        Returns:
            bool: 端口已在 listen、地址空间已建好时为 True。
        """
        if not OPCUA_AVAILABLE:
            logger.error("asyncua 未安装，无法启动 OPC UA 模拟服务端")
            return False
        if self._thread and self._thread.is_alive():
            logger.warning("OPC UA 模拟服务端已在运行: %s", self.endpoint)
            return True

        self._ready.clear()
        self._stop.clear()
        self._start_error = None
        self._thread = threading.Thread(
            target=self._run_loop, name='opcua-sim-server', daemon=True)
        self._thread.start()

        # 15 秒：地址空间里设备/点位多时（几十台 × 十几个点）建节点本身要几秒
        if not self._ready.wait(timeout=15.0):
            logger.error("OPC UA 模拟服务端启动超时（15s）")
            return False
        if self._start_error:
            logger.error("OPC UA 模拟服务端启动失败: %s", self._start_error)
            return False
        return True

    def stop(self) -> None:
        """停服务（幂等）。"""
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=10.0)
            if thread.is_alive():
                # 不静默：线程卡住说明服务器没正常关闭，端口可能仍被占用
                logger.warning("OPC UA 模拟服务端线程 10s 内未退出")
        self._thread = None
        self._server = None

    @property
    def endpoint(self) -> str:
        return f'opc.tcp://127.0.0.1:{self.port}/'

    def node_ids(self) -> dict[str, dict[str, str]]:
        """{device_id: {点位名: node_id}}，给配置生成用（真实可用的字符串）。"""
        return {dev: dict(points) for dev, points in self._node_ids.items()}

    # ------------------------------------------------------------------ 线程内实现

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as exc:  # noqa: BLE001 - 线程顶层必须兜住，否则 start() 永远等不到就绪
            self._start_error = f'{type(exc).__name__}: {exc}'
            logger.exception("OPC UA 模拟服务端异常退出")
        finally:
            self._ready.set()
            self._loop.close()

    async def _serve(self) -> None:
        # 同一个实例 stop() 后再 start() 时，旧节点表必须清空：
        # 否则会用相同 NodeId 再 add_variable 一次，asyncua 抛
        # BadNodeIdExists，而报错信息完全看不出是「重启没清状态」。
        self._nodes.clear()
        self._node_ids.clear()

        server = Server()
        await server.init()
        server.set_endpoint(self.endpoint)
        server.set_server_name('SCADA 模拟 OPC UA 服务器')
        idx = await self._align_namespaces(server)

        root = await server.nodes.objects.add_folder(idx, 'SCADA')
        await self._build_address_space(server, root, idx)

        self._server = server
        push_task = asyncio.create_task(self._push_loop())
        async with server:
            # 走到这里端口已经在 listen（async with 内部 start 完成）
            self._ready.set()
            while not self._stop.is_set():
                await asyncio.sleep(0.1)

        push_task.cancel()
        try:
            await push_task
        except asyncio.CancelledError:
            # cancel() 之后的正常收尾，不是故障
            pass
        logger.info("OPC UA 模拟服务端已停止")

    async def _align_namespaces(self, server: Any) -> int:
        """让服务端的 namespace index 与**配置里写的 node_id** 对齐。

        ## 为什么不能让配置去适配服务端

        真实设备的 NodeId 是厂家地址空间定死的（`ns=3;s=Temperature`）。
        模拟服务端如果自己分配一个 ns（比如 ns=2）并生成
        `ns=2;s=opcua_plc_01.Temperature`，那配置里的 node_id 就永远是错的 ——
        现场抄来的地址在模拟环境里连不上，而模拟环境里跑通的地址到现场又是错的。
        两边对不上，「模拟验证」这件事就失去了意义。

        所以服务端必须**服从配置**：配置写 ns=3，服务端就把 index 3 准备好。

        asyncua 的 `register_namespace()` 从 1 开始递增分配，所以注册 N 次
        就能拿到 1..N。如果某个 index 已被占用（asyncua 内部注册了别的 uri），
        就退回到「用服务端分配的 index」，并在日志里**明确说出退化**
        —— 不能让运维以为 node_id 还是配置里那个。
        """
        # 只收集**配置里真正用到**的 ns index。不能从 1 开始一路对齐：
        # 配置只用 ns=3 时，去对齐 ns=1 会因为 asyncua 已占 index 1 而报一条
        # 无意义的 ERROR，把真正的问题淹掉。
        wanted: set[int] = set()
        for cfg in (getattr(self.hub, 'devices', {}) or {}).values():
            if not isinstance(cfg, dict):
                continue
            if str(cfg.get('protocol', '')).lower() != 'opcua':
                continue
            for point in (cfg.get('nodes') or []):
                nid = str(point.get('node_id') or '')
                m = re.match(r'ns=(\d+);', nid)
                if m:
                    wanted.add(int(m.group(1)))
        if not wanted:
            wanted = {1}
        max_ns = max(wanted)

        # 关键：asyncua 的 `Server()` 自己会先注册一个 namespace（index 1），
        # 所以第一个自定义 namespace 拿到的是 **2**，不是 1。
        #
        # 直接按 1..N 顺序注册会整体偏移一位（配置要 ns=3、实际建成 ns=4），
        # 客户端订阅照样 BadNodeIdUnknown，而日志里只看到「映射 {1:2,2:3,3:4}」
        # —— 一眼看不出偏了。
        #
        # 所以要先读当前 namespace 表，算出**下一个可用 index**，
        # 只在 `next == want` 时才注册；`next < want` 就补几个占位把空档填掉。
        ns_array = await server.get_namespace_array()
        next_idx = len(ns_array)

        self._ns_map: dict[int, int] = {}
        for want in sorted(wanted):
            while next_idx < want:
                # 空档：注册一个占位 uri 把 index 推进到 want
                await server.register_namespace(f'{self.namespace}:pad{next_idx}')
                next_idx += 1

            if next_idx != want:
                # next_idx > want：这个 index 已经被别人（asyncua 自己）占了，
                # 无法对齐。记下来让 `_resolve_node_id` 走退化路径，
                # 并且**明确告诉运维要改配置**，不能让他以为 node_id 还是配置里那个。
                self._ns_map[want] = want
                logger.error(
                    "OPC UA namespace 无法对齐：配置要求 ns=%d，但该 index 已被占用"
                    "（下一个可用是 ns=%d）。点位会建在 ns=%d 下，"
                    "**采集端必须把 node_id 改成 ns=%d**，否则订阅会 BadNodeIdUnknown",
                    want, next_idx, next_idx, next_idx)
                continue

            got = await server.register_namespace(f'{self.namespace}:{want}')
            next_idx = got + 1
            self._ns_map[want] = got
            if got != want:
                logger.error(
                    "OPC UA namespace 对不齐：配置要求 ns=%d，服务端给的是 ns=%d。"
                    "**配置里的 node_id 需要改成 ns=%d**",
                    want, got, got)

        primary = self._ns_map.get(max_ns, 1)
        logger.info("OPC UA 模拟服务端命名空间 %s（配置最大 ns=%d，映射 %s）",
                    self.namespace, max_ns, self._ns_map)
        return primary

    async def _build_address_space(self, server: Any, root: Any, idx: int) -> None:
        """按设备配置建 folder + 变量节点。"""
        devices = getattr(self.hub, 'devices', {}) or {}
        for device_id, cfg in devices.items():
            if not isinstance(cfg, dict):
                continue
            if str(cfg.get('protocol', '')).lower() != 'opcua':
                continue
            if cfg.get('enabled') is False:
                continue

            points = cfg.get('nodes') or []
            if not points:
                logger.warning("OPC UA 设备 %s 没有配置 nodes，跳过", device_id)
                continue

            device_obj = await root.add_object(idx, device_id)
            self._nodes[device_id] = {}
            self._node_ids[device_id] = {}

            for point_cfg in points:
                name = str(point_cfg.get('name') or '').strip()
                if not name:
                    logger.warning("OPC UA 设备 %s 有点位缺 name，跳过: %r", device_id, point_cfg)
                    continue

                value, vtype = self._initial_value(device_id, name)
                node_id = self._resolve_node_id(device_id, name, point_cfg, idx)
                try:
                    node = await device_obj.add_variable(
                        node_id, name, value, varianttype=vtype)
                except Exception as exc:      # noqa: BLE001
                    # 同一台设备里两个点位配了相同的 node_id（抄错了），
                    # 或者 index 对不齐 —— 报清楚是哪个点位，不要笼统失败
                    logger.error(
                        "OPC UA 建节点失败 %s/%s (node_id=%s): %s",
                        device_id, name, node_id.to_string(), exc)
                    self.stats['errors'] = self.stats.get('errors', 0) + 1
                    continue

                if self._is_writable(point_cfg):
                    await node.set_writable()
                    # 客户端写入要能回流到 hub，否则「下发设定值」在模拟环境里永远是假的
                    server.set_attribute_value_setter(
                        node_id, self._make_setter(device_id, name))

                self._nodes[device_id][name] = (node, vtype)
                self._node_ids[device_id][name] = node.nodeid.to_string()

        logger.info(
            "OPC UA 模拟地址空间: %d 台设备 / %d 个点位",
            len(self._node_ids), sum(len(p) for p in self._node_ids.values()))

    def _resolve_node_id(self, device_id: str, name: str,
                         point_cfg: dict[str, Any], default_idx: int) -> Any:
        """优先用配置里写的 node_id，对不齐时才退回生成值。

        配置示例：`node_id: ns=3;s=Temperature`
        数字标识符：`node_id: ns=3;i=12`
        """
        raw = str(point_cfg.get('node_id') or '').strip()
        if raw:
            m = re.match(r'ns=(\d+);([si]=)(.+)', raw)
            if m:
                want_ns = int(m.group(1))
                mapped = getattr(self, '_ns_map', {}).get(want_ns)
                if mapped is not None:
                    return ua.NodeId(m.group(3), mapped,
                                     ua.NodeIdType.String if m.group(2) == 's='
                                     else ua.NodeIdType.Numeric)
            else:
                logger.warning(
                    "OPC UA 设备 %s 点位 %s 的 node_id=%r 格式不认识"
                    "（应为 ns=<数字>;s=<字符串> 或 ns=<数字>;i=<数字>），已改为生成值",
                    device_id, name, raw)

        # 退化路径：生成 `ns=<服务端分配的>;s=<设备>.<点位>`。
        # 前缀带 device_id 是因为不同设备常有点位重名（temperature/pressure），
        # 裸点位名在同一 namespace 下会撞 NodeId。
        return ua.NodeId(f'{device_id}.{name}', default_idx, ua.NodeIdType.String)

    def _initial_value(self, device_id: str, point: str) -> tuple[Any, Any]:
        """从 hub 的当前快照推断点位类型与初值。"""
        raw = self._snapshot_point(device_id, point)
        if isinstance(raw, bool):
            return bool(raw), ua.VariantType.Boolean
        if isinstance(raw, int):
            return raw, ua.VariantType.Int32
        fv = try_finite_float(raw)
        return (fv if fv is not None else 0.0), ua.VariantType.Double

    @staticmethod
    def _is_writable(point_cfg: dict[str, Any]) -> bool:
        access = str(point_cfg.get('access', '')).lower()
        return access in ('rw', 'r/w', 'readwrite') or bool(point_cfg.get('writable'))

    def _snapshot_point(self, device_id: str, point: str) -> Any:
        try:
            snap = self.hub.snapshot(device_id) or {}
        except (KeyError, ValueError, RuntimeError) as exc:
            logger.warning("读取 %s 快照失败: %s", device_id, exc)
            return None
        return snap.get(point)

    def _make_setter(self, device_id: str, point: str):
        """构造 asyncua 的 value_setter：客户端写入时先落值，再回流 hub。"""

        def setter(node_data: Any, attr: Any, data_value: Any) -> None:
            # 必须先自己落值：设了 value_setter 之后 asyncua 不再自动写入属性
            node_data.attributes[attr].value = data_value
            if _INTERNAL_WRITE.get():
                return

            raw = data_value.Value.Value if data_value.Value is not None else None
            self._write_back(device_id, point, raw)

        return setter

    def _write_back(self, device_id: str, point: str, raw: Any) -> None:
        """把客户端写入回传给 hub。

        `DeviceDataHub` 契约里只有 `write_registers()`（寄存器语义），OPC UA 是
        点位语义，所以优先用可选的 `write_point()`。两者都没有时只能放弃 ——
        但要**明确记一条日志**，否则「下发了但没生效」在现场无法定位。
        """
        writer = getattr(self.hub, 'write_point', None)
        if callable(writer):
            try:
                ok = bool(writer(device_id, point, raw))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("hub.write_point(%s, %s) 失败: %s", device_id, point, exc)
                return
            if not ok:
                logger.warning("hub.write_point(%s, %s, %r) 返回 False", device_id, point, raw)
            return

        logger.warning(
            "hub 未实现 write_point()，OPC UA 写 %s/%s=%r 只改了节点值，"
            "下一次推送（≤1s）会被 hub 快照覆盖", device_id, point, raw)

    async def _push_loop(self) -> None:
        """每 1 秒把 hub 快照写进节点 —— 采集层靠订阅收数据，必须持续产生 datachange。"""
        while True:
            try:
                await self._push_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单轮失败不能拖死整个推送循环
                logger.warning("OPC UA 推送轮次异常: %s: %s", type(exc).__name__, exc)
            await asyncio.sleep(1.0)

    async def _push_once(self) -> None:
        token = _INTERNAL_WRITE.set(True)
        try:
            for device_id, points in self._nodes.items():
                try:
                    snap = self.hub.snapshot(device_id) or {}
                except (KeyError, ValueError, RuntimeError) as exc:
                    logger.warning("推送 %s 快照失败: %s", device_id, exc)
                    continue
                for point, (node, vtype) in points.items():
                    value = self._coerce(snap.get(point), vtype)
                    if value is None:
                        continue
                    await node.write_value(ua.Variant(value, vtype))
        finally:
            _INTERNAL_WRITE.reset(token)

    @staticmethod
    def _coerce(raw: Any, vtype: Any) -> Any:
        """按节点声明类型转换；非法值返回 None（跳过这一轮，不写坏值）。"""
        if raw is None:
            return None
        if vtype == ua.VariantType.Boolean:
            # 先按数值解释：字符串 '0' 直接 bool() 会得到 True，把「停」写成「开」
            fv = try_finite_float(raw)
            return bool(fv) if fv is not None else bool(raw)
        if vtype == ua.VariantType.Int32:
            return try_int(raw)
        return try_finite_float(raw)
