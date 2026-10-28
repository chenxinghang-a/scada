"""
真实设备管理器
完全独立的真实设备实现，连接实际硬件
"""

import logging
import threading
import yaml
import time
from typing import Any
from pathlib import Path

from .interfaces import IDeviceManager, IDeviceClient
from .modbus_client import ModbusClient
from .opcua_client import OPCUAClient
from .mqtt_client import MQTTClient
from .rest_client import RESTDeviceClient
import paths
from core.safe_numeric import try_int

logger = logging.getLogger(__name__)


class RealDeviceManager(IDeviceManager):
    """
    真实设备管理器
    
    特点：
    - 完全独立，连接真实工业设备
    - 使用真实的协议客户端（Modbus/OPC UA/MQTT/REST）
    - 需要实际的硬件设备才能运行
    - 适用于生产环境
    """

    SUPPORTED_PROTOCOLS = ['modbus_tcp', 'modbus_rtu', 'opcua', 'mqtt', 'rest', 'mc', 'fins']

    # 连接失败重试策略
    MAX_RETRY_COUNT = 3       # 最大重试次数后进入退避
    RETRY_BACKOFF_BASE = 30   # 退避基数（秒）
    MAX_RETRY_INTERVAL = 300  # 最大退避间隔（5分钟）

    #: 启停动作 → 历史扁平键名（写法 B：`control.stop_coil` / `control.start_register`）。
    #: 保留是为了兼容既有配置；**新配置建议用写法 A**（与 `智能层/device_control.py`
    #: 的 `control` 段同形，避免「同一个概念两种字段名」）。
    _LEGACY_ACTION_KEYS = {
        'start': ('start_coil', 'start_register'),
        'stop': ('stop_coil', 'stop_register'),
    }
    #: 未显式给出 value 时各动作的缺省写入值（与 device_control 保持一致）
    _DEFAULT_ACTION_VALUES = {'start': True, 'stop': False}

    def __init__(self, config_path: str | None = None):
        """
        初始化真实设备管理器
        
        Args:
            config_path: 设备配置文件路径
        """
        self.config_path = config_path or '配置/devices.yaml'
        self.simulation_mode = False  # 真实设备管理器始终为真实模式
        self.devices: dict[str, dict[str, Any]] = {}
        self.clients: dict[str, IDeviceClient] = {}
        # 保护 devices / clients 两张表：API 线程增删设备，重连循环与
        # `_save_config()` 同时在读它们。不可重入锁，临界区只包字典操作
        # （绝不包 connect/disconnect —— 那些会阻塞在网络上）。
        self._lock = threading.Lock()
        self._stopped_devices: set[str] = set()  # 单独停止的设备
        self._estop_active = False

        # 连接失败跟踪：device_id -> {'retry_count': int, 'last_retry': timestamp}
        self._connection_failures: dict[str, dict[str, Any]] = {}

        # 断线自动重连循环状态（run.py 后台线程调用 start_reconnect_loop）
        self._reconnect_running = False
        self._reconnect_interval = 30
        self._reconnect_stop_event = threading.Event()
        
        # 加载设备配置
        self.load_config()
        
        logger.info("真实设备管理器初始化完成")

    def load_config(self):
        """加载设备配置文件"""
        try:
            config_file = paths.resolve(self.config_path)
            if not config_file.exists():
                logger.error(f"配置文件不存在: {self.config_path}")
                return

            with open(config_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)

            # 解析设备配置
            devices_config = config.get('devices', [])
            for device_config in devices_config:
                device_id = device_config.get('id')
                if device_id:
                    self.devices[device_id] = device_config
                    protocol = device_config.get('protocol', 'modbus_tcp')
                    logger.info(f"[真实] 加载设备配置: {device_id} [{protocol}] - {device_config.get('name')}")

            # 按协议统计
            proto_count = {}
            for d in list(self.devices.values()):   # 快照：热重载时可能并发增删
                p = d.get('protocol', 'modbus_tcp')
                proto_count[p] = proto_count.get(p, 0) + 1
            summary = ', '.join(f"{k}:{v}" for k, v in proto_count.items())
            logger.info(f"[真实] 共加载 {len(self.devices)} 个设备 ({summary})")

        except Exception as e:
            logger.error(f"加载配置文件异常: {e}")

    def _create_real_client(self, config: dict[str, Any]) -> IDeviceClient | None:
        """
        根据协议类型创建真实客户端

        Args:
            config: 设备配置

        Returns:
            真实客户端实例
        """
        protocol = config.get('protocol', 'modbus_tcp')

        if protocol in ('modbus_tcp', 'modbus_rtu'):
            return ModbusClient(config)
        elif protocol == 'opcua':
            return OPCUAClient(config)
        elif protocol == 'mqtt':
            return MQTTClient(config)
        elif protocol == 'rest':
            return RESTDeviceClient(config)
        elif protocol == 'mc':
            from .mc_client import MCClient
            return MCClient(config)
        elif protocol == 'fins':
            from .fins_client import FINSClient
            return FINSClient(config)
        else:
            logger.error(f"不支持的协议类型: {protocol}")
            return None

    def get_client(self, device_id: str) -> IDeviceClient | None:
        """获取设备客户端（懒创建）"""
        if device_id not in self.clients:
            device_config = self.devices.get(device_id)
            if not device_config:
                logger.error(f"设备 {device_id} 配置不存在")
                return None

            client = self._create_real_client(device_config)
            if client is None:
                return None

            self.clients[device_id] = client

        return self.clients[device_id]

    def _should_skip_connect(self, device_id: str) -> bool:
        """检查设备是否在退避期内，应该跳过重试"""
        failure = self._connection_failures.get(device_id)
        if not failure:
            return False
        retry_count = failure.get('retry_count', 0)
        if retry_count < self.MAX_RETRY_COUNT:
            return False
        # 指数退避计算
        wait = min(
            self.RETRY_BACKOFF_BASE * (2 ** (retry_count - self.MAX_RETRY_COUNT)),
            self.MAX_RETRY_INTERVAL
        )
        elapsed = time.time() - failure.get('last_retry', 0)
        return elapsed < wait

    def _record_connection_failure(self, device_id: str):
        """记录连接失败（计数+1）"""
        failure = self._connection_failures.setdefault(device_id, {'retry_count': 0, 'last_retry': 0})
        failure['retry_count'] += 1
        failure['last_retry'] = time.time()
        cnt = failure['retry_count']
        if cnt >= self.MAX_RETRY_COUNT:
            wait = min(self.RETRY_BACKOFF_BASE * (2 ** (cnt - self.MAX_RETRY_COUNT)), self.MAX_RETRY_INTERVAL)
            logger.warning(f"[真实] 设备 {device_id} 已连续 {cnt} 次连接失败，暂停重试 {wait:.0f} 秒")
        else:
            logger.warning(f"[真实] 设备 {device_id} 第 {cnt} 次连接失败")

    def _record_connection_success(self, device_id: str):
        """连接成功，重置失败计数"""
        self._connection_failures.pop(device_id, None)

    def set_estop_override(self, active: bool):
        """设置紧急停机覆盖（真实设备通过写操作控制，此处仅记录）"""
        self._estop_active = active
        if active:
            logger.info("[真实] 紧急停机已激活")

    def _resolve_control_action(self, config: dict[str, Any], action: str):
        """解析启停点位，返回 ``(kind, address, value)``；解析不出返回 ``None``。

        ⚠️ **真实模式绝不猜点位**（2026-10-13 修）。历史实现在找不到
        ``control.stop_coil`` / ``control.start_coil`` 时会**回退写 coil 0**：
        - 本仓自己的注释已指出「地址 0 是 relay_1（输出线圈）与 di_1 共用的地址」
          （见 `智能层/device_control.py` 的联锁说明）—— 盲写就是**误动作**；
        - 而且 `device_control.batch_control` 会**先按配置点位写一次**，再调用本类
          的 `start_device()`/`stop_device()`，于是每次启停都会**额外多写一次 coil 0**。

        支持的写法（A/C 与 `智能层/device_control.py` 的 `control` 段同形）：

            control:                                   # A：按动作声明（推荐）
              start: {type: coil, address: 7, value: true}
              stop:  {type: coil, address: 9, value: false}

            control: {type: coil, address: 3,          # C：扁平共用点位
                      start_value: true, stop_value: false}

            control: {start_coil: 7, stop_coil: 9}     # B：本模块历史写法（兼容）
            control: {start_register: 321, stop_register: 322}
        """
        ctrl = config.get('control') or {}
        if not isinstance(ctrl, dict):
            return None

        default_value = self._DEFAULT_ACTION_VALUES.get(action)
        spec = None

        candidate = ctrl.get(action)
        if isinstance(candidate, dict):
            spec = dict(candidate)                                   # 写法 A
        elif ctrl.get('type') is not None and ctrl.get('address') is not None:
            spec = {                                                 # 写法 C
                'type': ctrl.get('type'),
                'address': ctrl.get('address'),
                'value': ctrl.get(f'{action}_value', default_value),
            }
        else:                                                        # 写法 B
            coil_key, reg_key = self._LEGACY_ACTION_KEYS[action]
            if ctrl.get(coil_key) is not None:
                spec = {'type': 'coil', 'address': ctrl[coil_key], 'value': default_value}
            elif ctrl.get(reg_key) is not None:
                spec = {'type': 'register', 'address': ctrl[reg_key],
                        'value': 1 if action == 'start' else 0}

        if spec is None:
            logger.error(
                "[真实] 设备 %s 未配置启停点位 control.%s —— 拒绝执行"
                "（真实模式不猜点位、不回退 coil0）。请在设备配置里声明 control 段，"
                "例如 control.%s: {type: coil, address: <现场地址>, value: %s}",
                config.get('id'), action, action, default_value,
            )
            return None

        kind = str(spec.get('type', 'coil')).lower()
        if kind not in ('coil', 'register'):
            logger.error("[真实] 设备 %s 启停点位类型非法: %r",
                         config.get('id'), spec.get('type'))
            return None
        address = try_int(spec.get('address'))
        if address is None:
            # YAML 的 `.inf` / `.nan` 是合法字面量 → `int(inf)` 抛 OverflowError
            # （不在 TypeError/ValueError 里），原本会让整台设备初始化崩掉。
            logger.error("[真实] 设备 %s 启停点位地址非法: %r",
                         config.get('id'), spec.get('address'))
            return None

        value = spec.get('value', default_value)
        return kind, address, value

    @staticmethod
    def _write_control(client, kind: str, address: int, value) -> bool:
        """按点位类型写入；客户端不支持**该类型**时返回 False（不静默当成功）。

        ⚠️ 不允许「线圈写不了就退回写寄存器」这种跨类型兜底。
        寄存器 0 和线圈 0 在现场是两个不同对象（数据字 vs 开关位），
        退回写等于**写错了地方**，而且调用方只会看到「成功」。
        客户端不支持就明确失败，由上层报给操作员。
        （实测：只有 Modbus / 模拟客户端实现了这两个方法，且两者**都**实现；
        跨类型兜底在现有实现里打不到，只会成为将来的坑。）
        """
        if kind == 'coil':
            if not hasattr(client, 'write_single_coil'):
                logger.error("[真实] 客户端不支持线圈写入（点位类型 coil）—— 拒绝跨类型退回写寄存器")
                return False
            return bool(client.write_single_coil(address, bool(value)))

        if not hasattr(client, 'write_single_register'):
            logger.error("[真实] 客户端不支持寄存器写入（点位类型 register）—— 拒绝跨类型退回写线圈")
            return False
        return bool(client.write_single_register(address, int(value)))

    def stop_device(self, device_id: str) -> bool:
        """停止指定设备（仅限机械类设备）。

        ⚠️ 返回 False 的**每一种**情形都必须明确（历史实现有两处会静默返回 True，
        调用方（`/api/devices/{id}/stop`）会把"什么都没做"报成"停止成功"）：
        ① 设备不存在；② 非机械类设备；③ 未配置启停点位（真实模式不猜点位）；
        ④ 客户端不存在；⑤ 写入失败。
        """
        config = self.devices.get(device_id)
        if not config:
            logger.error("[真实] 停止失败：设备 %s 不存在", device_id)
            return False

        category = IDeviceManager.get_device_category(config)
        if category != 'mechanical':
            logger.warning(f"设备 {device_id} 类型为 {category}，不可启停")
            return False

        resolved = self._resolve_control_action(config, 'stop')
        if resolved is None:
            return False

        client = self.get_client(device_id)
        if not client:
            logger.error("[真实] 停止失败：设备 %s 客户端不存在（配置或协议不支持）", device_id)
            return False

        if not self._write_control(client, *resolved):
            logger.error(f"[真实] 设备 {device_id} 停止命令发送失败")
            return False

        # 只有**写成功**才标记为已停止（原来在写之前就标记，失败也会显示"已停止"）
        self._stopped_devices.add(device_id)
        logger.info(f"[真实] 设备 {device_id} 已停止")
        return True

    def start_device(self, device_id: str) -> bool:
        """启动指定设备（仅限机械类设备）。失败情形同 `stop_device`。"""
        config = self.devices.get(device_id)
        if not config:
            logger.error("[真实] 启动失败：设备 %s 不存在", device_id)
            return False

        category = IDeviceManager.get_device_category(config)
        if category != 'mechanical':
            logger.warning(f"设备 {device_id} 类型为 {category}，不可启停")
            return False

        resolved = self._resolve_control_action(config, 'start')
        if resolved is None:
            return False

        client = self.get_client(device_id)
        if not client:
            logger.error("[真实] 启动失败：设备 %s 客户端不存在（配置或协议不支持）", device_id)
            return False

        if not self._write_control(client, *resolved):
            logger.error(f"[真实] 设备 {device_id} 启动命令发送失败")
            return False

        self._stopped_devices.discard(device_id)
        logger.info(f"[真实] 设备 {device_id} 已启动")
        return True

    def adjust_device(self, device_id: str, register_name: str, value: float) -> dict:
        """
        调节设备参数（写入指定寄存器值）

        Args:
            device_id: 设备ID
            register_name: 寄存器名（如 'speed', 'temperature_setpoint'）
            value: 目标值

        Returns:
            {'success': bool, 'message': str}
        """
        config = self.devices.get(device_id)
        if not config:
            return {'success': False, 'message': f'设备 {device_id} 不存在'}

        # 找到对应的寄存器配置
        registers = config.get('registers', [])
        target_reg = None
        for reg in registers:
            if reg.get('name') == register_name:
                target_reg = reg
                break

        if not target_reg:
            return {'success': False, 'message': f'寄存器 {register_name} 不存在'}

        # 检查是否可写
        if target_reg.get('access') == 'ro':
            return {'success': False, 'message': f'寄存器 {register_name} 只读'}

        client = self.get_client(device_id)
        if not client:
            return {'success': False, 'message': f'设备 {device_id} 未连接'}

        address = target_reg['address']
        data_type = target_reg.get('data_type', 'uint16')
        scale = target_reg.get('scale', 1.0)
        offset = target_reg.get('offset', 0)

        # 反向换算：工程值 → 原始值
        raw_value = (value - offset) / scale if scale else value

        try:
            if data_type in ('float32', 'float', 'real'):
                # float32 写入需要编码为两个寄存器
                import struct
                raw_bytes = struct.pack('>f', float(raw_value))
                reg0 = struct.unpack('>H', raw_bytes[0:2])[0]
                reg1 = struct.unpack('>H', raw_bytes[2:4])[0]
                success = client.write_multiple_registers(address, [reg0, reg1])
            elif data_type in ('uint16', 'int16'):
                success = client.write_single_register(address, int(raw_value))
            else:
                success = client.write_single_register(address, int(raw_value))

            if success:
                logger.info(f"[真实] 设备 {device_id} 调节 {register_name} = {value}")
                return {'success': True, 'message': f'{register_name} 设置为 {value}'}
            else:
                return {'success': False, 'message': '写入失败'}

        except Exception as e:
            logger.error(f"调节失败 {device_id}/{register_name}: {e}")
            return {'success': False, 'message': str(e)}

    def connect_device(self, device_id: str) -> bool:
        """连接设备（含退避逻辑）"""
        if self._should_skip_connect(device_id):
            logger.debug(f"[真实] 跳过设备 {device_id} 连接（退避中）")
            return False

        client = self.get_client(device_id)
        if not client:
            return False

        success = client.connect()
        if success:
            self._record_connection_success(device_id)
        else:
            self._record_connection_failure(device_id)
        return success

    def disconnect_device(self, device_id: str):
        """断开设备连接（重置失败计数）"""
        client = self.clients.get(device_id)
        if client:
            client.disconnect()
        self._connection_failures.pop(device_id, None)

    def connect_all(self, timeout: float = 20.0) -> dict[str, bool]:
        """
        连接所有设备
        
        Args:
            timeout: 总超时（秒），避免阻塞启动太久，默认20秒
        """
        results = {}
        start = time.time()
        for device_id in list(self.devices):     # 快照：连接期间可能有增删
            if not self.devices.get(device_id, {}).get('enabled', True):
                results[device_id] = None
                continue

            if time.time() - start > timeout:
                logger.warning(f"[真实] connect_all 已达超时 {timeout}s，剩余 {len(self.devices) - len(results)} 个设备跳过")
                for rid in list(self.devices.keys())[len(results):]:
                    if self.devices[rid].get('enabled', True):
                        results[rid] = False
                    else:
                        results[rid] = None
                break

            results[device_id] = self.connect_device(device_id)

        return results

    def disconnect_all(self):
        """断开所有设备连接，并停止重连循环（关停语义）"""
        self._reconnect_running = False
        self._reconnect_stop_event.set()
        for device_id in list(self.clients.keys()):
            self.disconnect_device(device_id)

    def start_reconnect_loop(self, interval: int = 30) -> None:
        """启动断线自动重连后台循环。

        周期扫描所有已加载设备，对「配置启用、客户端存在或已创建、但当前未连接」
        的设备尝试重连（复用 ``connect_device`` 的指数退避逻辑）。成功记 INFO，
        失败记 WARNING，异常被捕获后继续下一轮，绝不静默退出线程。

        Args:
            interval: 扫描间隔（秒），默认 30。
        """
        if self._reconnect_running:
            logger.debug("[真实] 重连循环已在运行，跳过重复启动")
            return

        self._reconnect_interval = interval
        self._reconnect_stop_event.clear()
        self._reconnect_running = True

        def _loop() -> None:
            while self._reconnect_running and not self._reconnect_stop_event.is_set():
                try:
                    # 快照迭代：`add_device` / `remove_device` 会在 API 线程里
                    # 增删这张表；直接迭代活视图会抛
                    # RuntimeError（dictionary changed size during iteration），
                    # 被下面 except 吞掉 → 这一轮重连整批跳过。
                    # （写入侧已持 `self._lock`，但这里读的是网络重连循环，
                    #  不能为了读一次表去抢锁 —— 快照更便宜也更安全。）
                    for device_id, cfg in list(self.devices.items()):
                        if not self._reconnect_running or self._reconnect_stop_event.is_set():
                            break
                        if not cfg.get('enabled', True):
                            continue
                        client = self.clients.get(device_id)
                        # 尚未创建客户端，或客户端已掉线 → 尝试重连
                        if client is None or not getattr(client, 'connected', False):
                            if self._should_skip_connect(device_id):
                                logger.debug(f"[真实] 设备 {device_id} 处于退避期，跳过本次重连")
                                continue
                            try:
                                logger.info(f"[真实] 尝试重连设备: {device_id}")
                                ok = self.connect_device(device_id)
                                if ok:
                                    logger.info(f"[真实] 设备 {device_id} 重连成功")
                                else:
                                    logger.warning(
                                        f"[真实] 设备 {device_id} 重连失败（将在 ~{interval}s 后重试）")
                            except Exception as _e:
                                logger.warning(f"[真实] 设备 {device_id} 重连异常: {_e}")
                except Exception as _e:
                    logger.error(f"[真实] 重连循环内部异常: {_e}", exc_info=True)
                # 等待间隔；若期间被 stop 则提前唤醒退出
                self._reconnect_stop_event.wait(self._reconnect_interval)

        t = threading.Thread(target=_loop, daemon=True, name="reconnect-loop")
        t.start()
        logger.info(f"[真实] 断线自动重连循环已启动（间隔 {interval}s）")

    def stop_reconnect_loop(self) -> None:
        """停止断线自动重连循环（关停路径调用）。

        与 ``disconnect_all()`` 等价地清除运行状态并通过 Event 唤醒等待中的线程。
        """
        self._reconnect_running = False
        self._reconnect_stop_event.set()
        logger.info("[真实] 断线自动重连循环已停止")

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
            'stopped': device_id in self._stopped_devices or self._estop_active,
            'device_category': IDeviceManager.get_device_category(device_config),
            'registers': device_config.get('registers', []),
            'nodes': device_config.get('nodes', []),
            'topics': device_config.get('topics', []),
            'endpoints': device_config.get('endpoints', []),
            'stats': {},
            'mode': 'real'  # 标记为真实模式
        }

        if client:
            status['connected'] = getattr(client, 'connected', False)
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
        connected = getattr(client, 'connected', False) if client else False
        stopped = device_id in self._stopped_devices or self._estop_active
        regs = device_config.get('registers', [])
        nodes = device_config.get('nodes', [])
        topics = device_config.get('topics', [])
        endpoints = device_config.get('endpoints', [])
        return {
            'device_id': device_id,
            'id': device_id,
            'name': device_config.get('name', device_id),
            'zone': device_config.get('zone', ''),
            'connected': connected,
            'stopped': stopped,
            'status': 'fault' if stopped else ('online' if connected else 'offline'),
            'protocol': device_config.get('protocol', 'modbus_tcp'),
            'host': device_config.get('host', device_config.get('endpoint', '')),
            'device_category': IDeviceManager.get_device_category(device_config),
            'registers': [{'name': r.get('name', ''), 'unit': r.get('unit', '')} for r in regs],
            'nodes': [{'name': n.get('name', ''), 'unit': n.get('unit', '')} for n in nodes],
            'topics': [{'name': t.get('name', ''), 'unit': t.get('unit', '')} for t in topics],
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

            # round 302：落盘失败必须回滚并如实返回（同 device_manager.add_device）
            if not self._save_config():
                with self._lock:
                    self.devices.pop(device_id, None)
                    self.clients.pop(device_id, None)
                logger.error("[真实] 添加设备失败：配置落盘失败，已回滚 %s", device_id)
                return False

            logger.info(f"[真实] 添加设备: {device_id} [{protocol}]")
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

            # round 302：落盘失败要把设备放回去（同 device_manager.remove_device）
            if not self._save_config():
                if removed is not None:
                    with self._lock:
                        self.devices[device_id] = removed
                logger.error("[真实] 移除设备失败：配置落盘失败，已回滚 %s", device_id)
                return False

            logger.info(f"[真实] 移除设备: {device_id}")
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

        真实管理器始终为真实模式，切换到模拟模式需要重启。

        Args:
            new_mode: True=模拟模式, False=真实模式

        Returns:
            dict: {'success': bool, 'message': str}
        """
        if new_mode:
            return {
                'success': False,
                'message': '当前为真实设备管理器，无法切换到模拟模式。请使用 --simulator 参数重启系统。'
            }
        return {'success': True, 'message': '当前已是真实模式'}

    def _save_config(self) -> bool:
        """保存设备配置到文件；**返回是否真的写成功**。

        ⚠️ 必须返回 bool：`展示层/api/api_devices.py::update_device` 用
        `if not device_manager._save_config():` 判定落盘结果。
        这里返回 None 会让「保存成功」被判成失败 —— 真实设备模式下
        每次改设备都回 500「未能写入配置文件，重启后会丢失」，而文件其实写好了。
        （反向的静默错误：假失败。与统一管理器返回 bool 的口径对齐。）
        """
        try:
            # 持锁取快照：API 线程正在增删时，裸 `list(self.devices.values())`
            # 会抛 RuntimeError，被下面的 except 吞掉 → 配置静默不写盘。
            with self._lock:
                config = {'devices': list(self.devices.values())}
            config_file = paths.resolve(self.config_path)
            config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(config_file, 'w', encoding='utf-8') as f:
                yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
            logger.info("[真实] 设备配置已保存")
            return True
        except Exception as e:
            logger.error(f"保存配置文件异常: {e}")
            return False
