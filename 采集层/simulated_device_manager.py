"""
模拟设备管理器
完全独立的模拟实现，不依赖真实设备
"""

import logging

from core.device_lifecycle import notify_device_removed
import threading
import yaml
from typing import Any
from pathlib import Path

import paths

from .interfaces import IDeviceManager, IDeviceClient
from .base_client import encode_float32_registers, encode_int32_registers
from core.safe_numeric import try_finite_float, try_int
from core.engineering_units import to_raw
from .simulated_client import is_device_stopped, _ESTOP_ACTIVE
from .simulated_client import set_estop_state, set_device_stopped
from .enhanced_simulated_client import (
    EnhancedSimulatedModbusClient,
    EnhancedSimulatedOPCUAClient,
    EnhancedSimulatedMQTTClient,
    EnhancedSimulatedRESTClient
)

logger = logging.getLogger(__name__)


class SimulatedDeviceManager(IDeviceManager):
    """
    模拟设备管理器
    
    特点：
    - 完全独立，不依赖任何真实硬件
    - 所有设备使用模拟客户端
    - 生成仿真数据用于演示和测试
    - 可以独立运行，无需切换模式
    """

    SUPPORTED_PROTOCOLS = ['modbus_tcp', 'modbus_rtu', 'opcua', 'mqtt', 'rest', 'mc', 'fins']

    def __init__(self, config_path: str | None = None,
                 live_protocol: bool = False):
        """
        初始化模拟设备管理器

        Args:
            config_path: 设备配置文件路径
            live_protocol: **真实协议线模式**。为 True 时不用进程内假客户端，
                而是用真实客户端（pymodbus / asyncua / paho / requests）连
                ``sim_endpoint``（`模拟服务端` 起的真服务端）。

                为什么要有这个开关：
                进程内假客户端**根本不经协议**，所以 float32 字序、寄存器拼接、
                JSON 路径这些最容易出错的地方从来没被验证过。开着它跑一遍，
                等于把「现场调试」提前到本机 —— 切现场只改 IP，代码一行不动。
        """
        self.config_path = config_path or '配置/devices.yaml'
        self.simulation_mode = True  # 模拟设备管理器始终为模拟模式
        #: True = 走真实协议线（连 模拟服务端），False = 进程内假客户端
        self.live_protocol = bool(live_protocol)
        self.devices: dict[str, dict[str, Any]] = {}
        #: 最近一次 load_config 失败的原因（None / missing / parse / structure）
        self._load_failed_reason: str | None = None
        self.clients: dict[str, IDeviceClient] = {}
        # 保护 devices / clients 两张表：API 线程增删设备，重连循环与
        # `_save_config()` 同时在读它们。不可重入锁，临界区只包字典操作。
        self._lock = threading.Lock()
        
        # 加载设备配置；失败拒绝启动（0 台设备不许静默跑）
        if not self.load_config():
            # 分级处理：不存在 → 软失败；存在但坏了 → 硬失败（见真实模式同名注释）
            if self._load_failed_reason == 'missing':
                logger.error("模拟设备配置不存在，以 0 台设备启动: %s",
                             self.config_path)
            else:
                raise RuntimeError(
                    f"模拟设备配置加载失败（{self._load_failed_reason}），"
                    f"拒绝以 0 台设备启动: {self.config_path}")

        logger.info("模拟设备管理器初始化完成")

    def load_config(self) -> bool:
        """加载设备配置文件（口径与 RealDeviceManager 一致）。

        Returns:
            bool: True = 成功；False = 失败且**设备表保持原样**。

        fail-closed 的三条理由见 `RealDeviceManager.load_config` 的 docstring：
        坏 YAML 不许变成空表、devices 必须是列表、全部成功才替换。
        模拟模式同样需要 —— 「模拟配置写错 → 0 台设备 → 界面空白」
        和真实模式的后果一样难排查。
        """
        self._load_failed_reason = None
        config_file = paths.resolve(self.config_path)
        if not config_file.exists():
            logger.error("配置文件不存在: %s", self.config_path)
            self._load_failed_reason = 'missing'
            return False

        try:
            from core.config_manager import expand_env_vars
            with open(config_file, 'r', encoding='utf-8') as f:
                raw = yaml.safe_load(f)
            # round 377：保留原始设备表，落盘时还原 ${VAR}（理由同 RealDeviceManager）
            raw_devices = ((raw or {}).get('devices')
                           if isinstance(raw, dict) else None)
            config = expand_env_vars(raw or {}, _source=str(config_file))
        except yaml.YAMLError as exc:
            logger.error("配置文件语法错误（保留原有设备表）: %s — %s",
                         self.config_path, exc)
            self._load_failed_reason = 'parse'
            return False
        except OSError as exc:
            logger.error("配置文件读取失败: %s — %s", self.config_path, exc)
            return False

        if not isinstance(config, dict):
            logger.error("配置顶层不是映射，拒绝使用: %s", self.config_path)
            self._load_failed_reason = 'structure'
            return False

        devices_config = config.get('devices', [])
        if not isinstance(devices_config, list):
            logger.error("配置的 devices 段必须是列表，实际 %s，拒绝使用: %s",
                         type(devices_config).__name__, self.config_path)
            self._load_failed_reason = 'structure'
            return False

        new_devices: dict[str, dict[str, Any]] = {}
        for device_config in devices_config:
            if not isinstance(device_config, dict):
                logger.warning("[模拟] 跳过非映射的设备条目: %r", device_config)
                continue
            device_id = device_config.get('id')
            if not device_id:
                logger.warning("[模拟] 跳过没有 id 的设备条目")
                continue
            new_devices[device_id] = device_config

        with self._lock:
            self.devices = new_devices
            self._raw_devices = [d for d in (raw_devices or [])
                                 if isinstance(d, dict)]

        proto_count: dict[str, int] = {}
        for d in new_devices.values():
            p = d.get('protocol', 'modbus_tcp')
            proto_count[p] = proto_count.get(p, 0) + 1
        summary = ', '.join(f"{k}:{v}" for k, v in proto_count.items())
        logger.info("[模拟] 共加载 %d 个设备 (%s)", len(new_devices), summary)
        return True


    def reload_config(self, allow_empty: bool = False) -> dict[str, Any]:
        """热重载设备配置（契约同 ``DeviceManager.reload_config``）。

        Returns:
            dict: ``{'added': [...], 'removed': [...], 'kept': [...],
                   'rejected': str | None}``
            ``rejected`` 非空表示本次重载被拒绝、配置表原样保留。

        ⚠️ round 341：这个方法原来是**缺的**。

        `core/device_config_watch.watch_once()` 每 10 秒调一次
        `device_manager.reload_config()`，但只有 `DeviceManager` 定义了它；
        生产用的 `RealDeviceManager` / `SimulatedDeviceManager` 都没有 ——
        `AttributeError` 被 `except` 记成「设备配置自动重载异常」，
        于是**两种模式下热重载都从未生效**，改 `devices_*.yaml` 必须重启，
        而维护任务每 10 秒刷一条 ERROR，把真正的问题埋掉。

        两条约束（与 DeviceManager 一致）：
        · 解析失败 → 保留原配置表，返回 `rejected`；
        · 重载成 0 台而原来有设备 → 回滚（文件被清空/编辑器先清后写的
          中间态，照单全收等于**现场全部设备停止采集**）。
        """
        old_ids = set(self.devices.keys())
        # 必须用 list(...) 快照：别的线程（API 增删设备）正在改这张表，
        # 直接迭代活视图会抛 RuntimeError: dictionary changed size during iteration。
        snapshot = {did: cfg for did, cfg in list(self.devices.items())}

        if not self.load_config():
            reason = (f'配置文件解析失败（{self.config_path}），'
                      f'已保留原配置（{len(old_ids)} 台设备）')
            logger.error("设备配置重载被拒绝: %s", reason)
            return {'added': [], 'removed': [], 'kept': sorted(old_ids),
                    'rejected': reason}

        new_ids = set(self.devices.keys())
        if not new_ids and old_ids and not allow_empty:
            with self._lock:
                self.devices = snapshot
            reason = ('重载后设备数为 0，已回滚 —— 防止文件被清空时'
                      '现场全部设备停止采集')
            logger.error("设备配置重载被拒绝: %s", reason)
            return {'added': [], 'removed': [], 'kept': sorted(old_ids),
                    'rejected': reason}

        added = sorted(new_ids - old_ids)
        removed = sorted(old_ids - new_ids)
        kept = sorted(new_ids & old_ids)
        if added or removed:
            logger.info("[重载] 设备变更: +%s -%s", added, removed)

        # round 369：**被删设备必须真正断开并通知全链路**（同 RealDeviceManager）。
        # 原来 removed 只用于打日志：模拟客户端实例留在 self.clients、
        # 停机标志留在 _DEVICE_STOPPED、下游 8 个模块保留幽灵状态。
        for did in removed:
            try:
                self.disconnect_device(did)
            except Exception as e:      # noqa: BLE001
                logger.error("[重载] 断开已删除设备 %s 失败: %s", did, e)
            with self._lock:
                self.clients.pop(did, None)
            try:
                # 顺手把「单台停止」标志也清掉：重加同名设备时不继承旧状态
                set_device_stopped(did, False)
            except Exception as e:      # noqa: BLE001
                logger.debug("[重载] 清理设备 %s 的停机标志失败: %s", did, e)
            try:
                notify_device_removed(did)
            except Exception as e:      # noqa: BLE001
                logger.error("[重载] 通知设备移除失败 %s: %s", did, e)
        if removed:
            logger.info("[重载] 已移除设备并清理资源: %s", removed)

        return {'added': added, 'removed': removed, 'kept': kept,
                'rejected': None}

    def _create_live_client(self, config: dict[str, Any]) -> IDeviceClient | None:
        """用**真实客户端**连模拟服务端（真实协议线模式）。

        配置里的 `host`/`port` 是**现场地址**，这里换成 `sim_endpoint`
        （127.0.0.1:<端口>）。这样同一份设备清单既能连模拟服务端、
        也能连现场设备，切换只改一个开关。

        没有 `sim_endpoint` 的设备**明确报错**而不是偷偷退回假客户端 ——
        悄悄退回会让「我在测真实协议」这句话变成假的。
        """
        from .real_device_manager import create_protocol_client

        endpoint = config.get('sim_endpoint') or {}
        host = endpoint.get('host')
        port = endpoint.get('port')
        if not host or not isinstance(port, int):
            logger.error(
                "设备 %s 没有 sim_endpoint，无法在真实协议线模式下连接。"
                "请用 tools/gen_factory.py 重新生成设备清单（会自动补端口）",
                config.get('id'))
            return None

        proto = str(config.get('protocol', '')).lower()
        live_cfg = dict(config)
        live_cfg['host'] = host
        live_cfg['port'] = port

        if proto == 'rest':
            # REST 客户端读的是 base_url，不是 host/port
            live_cfg['base_url'] = f'http://{host}:{port}'
        elif proto == 'opcua':
            # OPC UA 客户端读的是 **endpoint 字符串**（opc.tcp://host:port），
            # 不是 host/port 两个字段。只改 host/port 的话，客户端会去连
            # 配置里那个现场地址（192.168.1.50:4840）—— 本机当然连不上，
            # 而报错是「连接超时」，看不出是地址没换。
            live_cfg['endpoint'] = f'opc.tcp://{host}:{port}'
        elif proto == 'modbus_rtu':
            # 模拟服务端用 **TCP 从站**承载 RTU 设备（现场也常这样：
            # 串口服务器把 RTU 转成 Modbus TCP）。不转的话真实客户端会去
            # 开串口，报「Serial client requires pyserial」—— 一个和
            # 模拟服务端完全无关的错。
            live_cfg['protocol'] = 'modbus_tcp'

        client = create_protocol_client(live_cfg)
        if client is not None:
            logger.info("[模拟-真实协议] %s → %s:%s (%s)",
                        config.get('id'), host, port, config.get('protocol'))
        return client

    def _create_simulated_client(self, config: dict[str, Any]) -> IDeviceClient | None:
        """
        根据协议类型创建模拟客户端

        Args:
            config: 设备配置

        Returns:
            模拟客户端实例
        """
        protocol = config.get('protocol', 'modbus_tcp')

        if self.live_protocol:
            return self._create_live_client(config)

        if protocol in ('modbus_tcp', 'modbus_rtu'):
            return EnhancedSimulatedModbusClient(config)
        elif protocol == 'opcua':
            return EnhancedSimulatedOPCUAClient(config)
        elif protocol == 'mqtt':
            return EnhancedSimulatedMQTTClient(config)
        elif protocol == 'rest':
            return EnhancedSimulatedRESTClient(config)
        elif protocol == 'mc':
            # 三菱MC协议使用Modbus模拟客户端（寄存器结构兼容）
            return EnhancedSimulatedModbusClient(config)
        elif protocol == 'fins':
            # 欧姆龙FINS协议使用Modbus模拟客户端（寄存器结构兼容）
            return EnhancedSimulatedModbusClient(config)
        else:
            logger.error(f"不支持的协议类型: {protocol}")
            return None

    def get_client(self, device_id: str) -> IDeviceClient | None:
        """获取设备客户端（懒创建）。

        round 381：检查-创建必须原子。采集线程与 API 状态查询会并发进来，
        两个调用都能通过 `not in clients` → 各建一个客户端，后写覆盖前者，
        被覆盖的实例（live 模式下是真实连接）永远没人 disconnect。
        """
        client = self.clients.get(device_id)
        if client is not None:          # 无锁快路径
            return client

        with self._lock:
            client = self.clients.get(device_id)
            if client is not None:
                return client
            device_config = self.devices.get(device_id)
            if not device_config:
                logger.error(f"设备 {device_id} 配置不存在")
                return None

            client = self._create_simulated_client(device_config)
            if client is None:
                return None
            self.clients[device_id] = client
            return client

    def connect_device(self, device_id: str) -> bool:
        """连接设备"""
        client = self.get_client(device_id)
        if not client:
            return False
        return client.connect()

    def set_estop_override(self, active: bool) -> None:
        """设置紧急停机覆盖（模拟客户端停止机械类数据）"""
        set_estop_state(active)
        logger.info(f"[模拟] E-STOP override: {'激活' if active else '已解除'}")

    def stop_device(self, device_id: str, write: bool = True) -> bool:
        """停止指定设备（仅限机械类）。

        ``write`` 仅为与 ``RealDeviceManager`` 保持同一签名：模拟侧没有
        真实下发通道，状态标志就是唯一状态源，因此无论是否已由安全通道
        写过点位，都必须同步 ``set_device_stopped(True)``。
        """
        config = self.devices.get(device_id)
        if not config:
            return False
        category = IDeviceManager.get_device_category(config)
        if category != 'mechanical':
            logger.warning(f"设备 {device_id} 类型为 {category}，不可启停")
            return False
        set_device_stopped(device_id, True)
        logger.info(f"[模拟] 设备 {device_id} 已停止")
        return True

    def start_device(self, device_id: str, write: bool = True) -> bool:
        """启动指定设备（仅限机械类）。``write`` 语义同 ``stop_device``。"""
        config = self.devices.get(device_id)
        if not config:
            return False
        category = IDeviceManager.get_device_category(config)
        if category != 'mechanical':
            logger.warning(f"设备 {device_id} 类型为 {category}，不可启停")
            return False
        set_device_stopped(device_id, False)
        logger.info(f"[模拟] 设备 {device_id} 已启动")
        return True

    @staticmethod
    def _write_registers_compat(client: Any, address: int,
                                values: list[int]) -> bool:
        """连续写多个寄存器，兼容只有单寄存器能力的客户端（round 370）。"""
        fn = getattr(client, 'write_multiple_registers', None)
        if callable(fn):
            try:
                return bool(fn(address, [int(v) & 0xFFFF for v in values]))
            except NotImplementedError:
                pass
        ok = True
        for offset, word in enumerate(values):
            ok = bool(client.write_single_register(address + offset,
                                                   int(word) & 0xFFFF)) and ok
        return ok

    def adjust_device(self, device_id: str, register_name: str, value: float) -> dict:
        """调节设备参数（**真的写进模拟客户端**，而不是只打一行日志）。

        round 369：原实现校验完寄存器存在后只 `logger.info` 一句就返回
        ``{'success': True}`` —— 接口报成功、下一次采集读回来的数值纹丝不动，
        演练时"调好的参数"根本没生效（典型假成功）。
        """
        config = self.devices.get(device_id)
        if not config:
            return {'success': False, 'message': f'设备 {device_id} 不存在'}

        # 验证寄存器存在
        registers = config.get('registers', [])
        target = None
        for reg in registers:
            if reg.get('name') == register_name:
                target = reg
                break

        if not target:
            return {'success': False, 'message': f'寄存器 {register_name} 不存在'}
        if target.get('access') == 'ro':
            return {'success': False, 'message': f'寄存器 {register_name} 只读'}

        client = self.get_client(device_id)
        if not client:
            return {'success': False, 'message': f'设备 {device_id} 未连接'}

        # 模拟客户端按需懒创建，未必已 connect()；写接口在未连接时会直接
        # 返回 False（增强模拟客户端），这里先补一次连接再写。
        if not getattr(client, 'connected', False):
            try:
                client.connect()
            except Exception as e:      # noqa: BLE001
                logger.warning("[模拟] 调节前连接设备 %s 失败: %s", device_id, e)

        address = try_int(target.get('address', 0))
        if address is None:
            return {'success': False, 'message': f'寄存器 {register_name} 地址非法'}

        # 工程值 → 原始寄存器值
        scale = try_finite_float(target.get('scale', 1.0))
        if scale is None:
            scale = 1.0
        offset = try_finite_float(target.get('offset', 0.0))
        if offset is None:
            offset = 0.0
        # round 372：换算统一入口（scale/offset 的兜底保持本文件既有口径）
        raw_value = to_raw(value, scale, offset)
        data_type = str(target.get('data_type', 'uint16')).lower()

        try:
            byte_order = config.get('byte_order', 'ABCD')
            if data_type in ('float32', 'float', 'real'):
                regs = encode_float32_registers(float(raw_value), byte_order)
                # round 370（BUG-008）：**不能用 hasattr 判断**。
                # 基类 `ModbusClientInterface` 定义了 `write_multiple_registers`
                # 且直接 raise NotImplementedError，模拟客户端没重写它 ——
                # `hasattr` 恒为 True，一调即崩，float32 设定值 100% 失败。
                ok = self._write_registers_compat(client, address, regs)
            elif data_type in ('int32', 'uint32'):
                # round 370（BUG-010）：32 位整数必须写两个寄存器。
                # 原来落进 else 分支 `write_single_register(int(raw))`，
                # 高 16 位被丢掉，写 70000 会变成 4464。
                regs = encode_int32_registers(
                    int(round(raw_value)), byte_order,
                    signed=(data_type == 'int32'))
                ok = self._write_registers_compat(client, address, regs)
            elif data_type in ('bool', 'boolean', 'coil'):
                ok = client.write_single_coil(address, bool(value))
            else:
                ok = client.write_single_register(address, int(round(raw_value)))
        except Exception as e:          # noqa: BLE001
            logger.error("[模拟] 设备 %s 调节 %s 失败: %s", device_id, register_name, e)
            return {'success': False, 'message': str(e)}

        if not ok:
            return {'success': False,
                    'message': f'{register_name} 写入模拟设备失败'}

        # 同步设定点覆盖值，确保后续采集读回的就是这个值
        sim = getattr(client, 'behavior_simulator', None)
        if sim is not None and hasattr(sim, 'set_point_override'):
            try:
                sim.set_point_override(register_name, float(value))
            except Exception as e:      # noqa: BLE001
                logger.warning("[模拟] 设定点覆盖失败 %s/%s: %s",
                               device_id, register_name, e)

        logger.info(f"[模拟] 设备 {device_id} 调节 {register_name} = {value}"
                    f"（原始值 {raw_value}）")
        return {'success': True, 'message': f'{register_name} 设置为 {value}（模拟）'}

    def disconnect_device(self, device_id: str) -> None:
        """断开设备连接"""
        client = self.clients.get(device_id)
        if client:
            client.disconnect()

    def connect_all(self, **kwargs) -> dict[str, bool]:
        """连接所有设备"""
        results = {}
        for device_id in list(self.devices):     # 快照：连接期间可能有增删
            if self.devices.get(device_id, {}).get('enabled', True):
                results[device_id] = self.connect_device(device_id)
            else:
                results[device_id] = None  # 跳过禁用设备
        return results

    def disconnect_all(self) -> None:
        """断开所有设备连接"""
        for device_id in list(self.clients.keys()):
            self.disconnect_device(device_id)

    def start_reconnect_loop(self, interval: int = 30) -> None:
        """模拟模式无需断线重连（显式说明性 no-op）。

        模拟客户端在内存中维护连接，不会真正掉线，因此不启动任何后台重连线程。
        保留此方法仅为满足与真实管理器一致的 ``IDeviceManager`` 契约。
        """
        logger.info(
            "[模拟] 模拟模式无需断连重连：设备由内存中的模拟客户端维护，"
            "不会真正掉线，跳过重连循环（interval=%ss 被忽略）", interval)

    def stop_reconnect_loop(self) -> None:
        """模拟模式无重连循环，no-op（显式说明，保持契约对齐）。"""
        logger.debug("[模拟] stop_reconnect_loop 为 no-op（模拟模式本就未启动重连循环）")

    def get_device_status(self, device_id: str) -> dict[str, Any]:
        """获取设备状态"""
        client = self.clients.get(device_id)
        device_config = self.devices.get(device_id)

        if not device_config:
            return {'error': '设备配置不存在'}

        status = {
            'device_id': device_id,
            'name': device_config.get('name'),
            'description': device_config.get('description'),
            'protocol': device_config.get('protocol', 'modbus_tcp'),
            'host': device_config.get('host', device_config.get('endpoint', '')),
            'port': device_config.get('port'),
            'enabled': device_config.get('enabled', True),
            'connected': False,
            'stopped': is_device_stopped(device_id) or _ESTOP_ACTIVE,
            'device_category': IDeviceManager.get_device_category(device_config),
            # round 380：设备语义/铭牌/自主等级（界面编辑需要）
            'device_class': device_config.get('device_class', 'other'),
            'electrical': device_config.get('electrical') or {},
            'autonomy': device_config.get('autonomy', 'automatic'),
            'register_count': len(device_config.get('registers', [])) + len(device_config.get('nodes', [])) + len(device_config.get('topics', [])) + len(device_config.get('endpoints', [])),
            'registers': device_config.get('registers', []),
            'nodes': device_config.get('nodes', []),
            'topics': device_config.get('topics', []),
            'endpoints': device_config.get('endpoints', []),
            'stats': {},
            'mode': 'simulated'
        }

        if client:
            status['connected'] = getattr(client, 'connected', False)
            if hasattr(client, 'get_stats'):
                status['stats'] = client.get_stats()
            else:
                status['stats'] = getattr(client, 'stats', {})

        return status

    def get_all_devices(self) -> dict[str, dict[str, Any]]:
        """获取所有设备配置"""
        return self.devices.copy()

    def get_all_status(self, brief: bool = False) -> list[dict[str, Any]]:
        """获取所有设备状态

        Args:
            brief: True时返回精简字段，适用于100+设备的仪表盘轮询
        """
        if brief:
            return [self._get_brief_status(did) for did in list(self.devices)]
        return [self.get_device_status(did) for did in list(self.devices)]

    def _get_brief_status(self, device_id: str) -> dict[str, Any]:
        """获取设备精简状态（仅仪表盘所需字段）"""
        device_config = self.devices.get(device_id, {})
        client = self.clients.get(device_id)
        # 仪表盘需要寄存器名来显示数值，但不需要完整配置
        regs = device_config.get('registers', [])
        nodes = device_config.get('nodes', [])
        topics = device_config.get('topics', [])
        endpoints = device_config.get('endpoints', [])
        return {
            'device_id': device_id,
            'id': device_id,
            'name': device_config.get('name', device_id),
            'zone': device_config.get('zone', ''),
            'connected': getattr(client, 'connected', False) if client else False,
            'stopped': is_device_stopped(device_id) or _ESTOP_ACTIVE,
            'status': 'fault' if (is_device_stopped(device_id) or _ESTOP_ACTIVE) else ('online' if (client and getattr(client, 'connected', False)) else 'offline'),
            'protocol': device_config.get('protocol', 'modbus_tcp'),
            'host': device_config.get('host', device_config.get('endpoint', '')),
            'device_category': IDeviceManager.get_device_category(device_config),
            'registers': [{'name': r.get('name', ''), 'unit': r.get('unit', '')} for r in regs],
            'nodes': [{'name': n.get('name', ''), 'unit': n.get('unit', '')} for n in nodes],
            # round 370（BUG-021）：topics 允许裸字符串，直接 .get 会 AttributeError
            'topics': [{'name': (t.get('name', '') if isinstance(t, dict)
                                 else str(t).rsplit('/', 1)[-1]),
                        'unit': (t.get('unit', '') if isinstance(t, dict) else '')}
                       for t in topics],
            'endpoints': [{'name': e.get('name', ''), 'unit': e.get('unit', '')} for e in endpoints],
        }

    def add_device(self, device_config: dict[str, Any]) -> bool:
        """添加设备"""
        try:
            device_id = device_config.get('id')
            protocol = device_config.get('protocol', 'modbus_tcp')

            if not device_id:
                logger.error("设备配置缺少id字段")
                return False

            if protocol not in self.SUPPORTED_PROTOCOLS:
                logger.error(f"不支持的协议: {protocol}，支持: {self.SUPPORTED_PROTOCOLS}")
                return False

            if device_id in self.devices:
                logger.warning(f"设备 {device_id} 已存在，将被覆盖")
                self.disconnect_device(device_id)
                with self._lock:
                    self.clients.pop(device_id, None)

            with self._lock:
                self.devices[device_id] = device_config

            # round 308：**落盘失败必须回滚**。
            # `simulated_device_manager` 与 `device_manager` / `real_device_manager`
            # 是**平行实现**：round 302 修了后两者的 add_device/remove_device，
            # 漏了这一个 —— 因为三个类的同名方法各写了一份。
            # 后果相同：接口回「添加成功」、界面出现设备，重启后消失。
            if not self._save_config():
                with self._lock:
                    self.devices.pop(device_id, None)
                    self.clients.pop(device_id, None)
                logger.error("[模拟] 添加设备失败：配置落盘失败，已回滚 %s", device_id)
                return False

            logger.info(f"[模拟] 添加设备: {device_id} [{protocol}]")
            return True

        except Exception as e:
            logger.error(f"添加设备异常: {e}")
            return False

    def remove_device(self, device_id: str) -> bool:
        """移除设备"""
        try:
            self.disconnect_device(device_id)
            with self._lock:
                removed = self.devices.pop(device_id, None)
                self.clients.pop(device_id, None)

            # round 308：落盘失败要把设备放回去（与 add_device 同理）
            if not self._save_config():
                if removed is not None:
                    with self._lock:
                        self.devices[device_id] = removed
                logger.error("[模拟] 移除设备失败：配置落盘失败，已回滚 %s", device_id)
                return False

            logger.info(f"[模拟] 移除设备: {device_id}")
            # round 312：落盘成功之后才通知 —— 通知里各模块会清掉该设备的
            # 健康/失败计数/报表状态，回滚的设备不能清。
            notify_device_removed(device_id)
            return True
        except Exception as e:
            logger.error(f"移除设备异常: {e}")
            return False

    def get_protocol_summary(self) -> dict[str, int]:
        """获取各协议设备数量统计"""
        summary = {}
        for d in list(self.devices.values()):
            p = d.get('protocol', 'modbus_tcp')
            summary[p] = summary.get(p, 0) + 1
        return summary

    def switch_simulation_mode(self, new_mode: bool) -> dict:
        """
        切换模拟/真实模式（运行时热切换）

        模拟管理器始终为模拟模式，切换到真实模式需要重启。

        Args:
            new_mode: True=模拟模式, False=真实模式

        Returns:
            dict: {'success': bool, 'message': str}
        """
        if not new_mode:
            return {
                'success': False,
                'message': '当前为模拟设备管理器，无法切换到真实模式。请使用 --real 参数重启系统。'
            }
        return {'success': True, 'message': '当前已是模拟模式'}

    def _save_config(self) -> bool:
        """保存设备配置到文件；**返回是否真的写成功**。

        ⚠️ 必须返回 bool：`展示层/api/api_devices.py::update_device` 用
        `if not device_manager._save_config():` 判定落盘结果。
        这里返回 None 会让「保存成功」被判成失败 —— 纯模拟模式下
        每次改设备都回 500「未能写入配置文件」，而文件其实写好了。
        （反向的静默错误：假失败。与统一管理器返回 bool 的口径对齐。）
        """
        try:
            # 持锁取快照：API 线程正在增删时，裸 `list(self.devices.values())`
            # 会抛 RuntimeError，被下面的 except 吞掉 → 配置静默不写盘。
            # round 377：落盘前还原 ${VAR}（同 RealDeviceManager）
            from core.config_manager import restore_device_placeholders
            with self._lock:
                devices = list(self.devices.values())
                raw_devices = list(getattr(self, '_raw_devices', []))
            config = {'devices': restore_device_placeholders(devices, raw_devices)}
            config_file = paths.resolve(self.config_path)
            config_file.parent.mkdir(parents=True, exist_ok=True)
            # round 369：原子写（同 RealDeviceManager，理由见那边）
            from core.config_io import dump_yaml
            if not dump_yaml(config_file, config):
                logger.error("[模拟] 设备配置落盘失败（原文件未被破坏）: %s", config_file)
                return False
            logger.info("[模拟] 设备配置已保存")
            return True
        except Exception as e:
            logger.error(f"保存配置文件异常: {e}")
            return False
