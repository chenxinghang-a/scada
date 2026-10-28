"""
设备管理器模块
管理所有设备的连接和配置
支持协议: Modbus TCP/RTU, OPC UA, MQTT, REST HTTP
面向现代黑灯工厂的多协议统一管理
"""

import logging
import yaml
import socket
import threading
import threading
from typing import Any
from pathlib import Path

from core.connection_pool import ConnectionPool
from core.device_lifecycle import notify_device_removed
from core.config_manager import load_connection_pool_settings
import paths

logger = logging.getLogger(__name__)


# ---- 协议工厂：根据protocol字段自动选择客户端 ----
def _create_client(config: dict[str, Any], simulation_mode: bool, use_enhanced: bool = True):
    """
    根据协议类型创建对应的客户端实例

    模拟模式下所有协议都使用模拟客户端，无需真实设备。
    真实模式下按协议创建对应的真实客户端。

    Args:
        config: 设备配置字典
        simulation_mode: 是否模拟模式
        use_enhanced: 是否使用增强版模拟客户端（默认True）

    Returns:
        协议客户端实例
    """
    protocol = config.get('protocol', 'modbus_tcp')

    if protocol in ('modbus_tcp', 'modbus_rtu'):
        if simulation_mode:
            if use_enhanced:
                from .enhanced_simulated_client import EnhancedSimulatedModbusClient
                return EnhancedSimulatedModbusClient(config)
            else:
                from .simulated_client import SimulatedModbusClient
                return SimulatedModbusClient(config)
        else:
            from .modbus_client import ModbusClient
            return ModbusClient(config)

    elif protocol == 'opcua':
        if simulation_mode:
            if use_enhanced:
                from .enhanced_simulated_client import EnhancedSimulatedOPCUAClient
                return EnhancedSimulatedOPCUAClient(config)
            else:
                from .simulated_client import SimulatedOPCUAClient
                return SimulatedOPCUAClient(config)
        else:
            from .opcua_client import OPCUAClient
            return OPCUAClient(config)

    elif protocol == 'mqtt':
        if simulation_mode:
            if use_enhanced:
                from .enhanced_simulated_client import EnhancedSimulatedMQTTClient
                return EnhancedSimulatedMQTTClient(config)
            else:
                from .simulated_client import SimulatedMQTTClient
                return SimulatedMQTTClient(config)
        else:
            from .mqtt_client import MQTTClient
            return MQTTClient(config)

    elif protocol == 'rest':
        if simulation_mode:
            if use_enhanced:
                from .enhanced_simulated_client import EnhancedSimulatedRESTClient
                return EnhancedSimulatedRESTClient(config)
            else:
                from .simulated_client import SimulatedRESTClient
                return SimulatedRESTClient(config)
        else:
            from .rest_client import RESTDeviceClient
            return RESTDeviceClient(config)

    else:
        logger.error(f"不支持的协议类型: {protocol}")
        return None


class DeviceManager:
    """
    设备管理器
    统一管理 Modbus / OPC UA / MQTT / REST 四种协议设备

    黑灯工厂核心能力：
    1. 多协议统一接入 - 工厂内不同品牌/型号设备一网打尽
    2. 配置热加载 - 运行时新增/删除设备无需重启
    3. 模拟模式 - 无真实设备时自动切换模拟数据
    4. 自动重连 - 设备断开后自动恢复连接
    """

    # 支持的协议列表
    SUPPORTED_PROTOCOLS = ['modbus_tcp', 'modbus_rtu', 'opcua', 'mqtt', 'rest']

    def __init__(self, config_path: str | None = None, simulation_mode: bool = True, use_enhanced_simulation: bool = True):
        """
        初始化设备管理器

        Args:
            config_path: 设备配置文件路径
            simulation_mode: 是否启用模拟模式（无真实设备时使用模拟数据）
            use_enhanced_simulation: 是否使用增强版模拟（物理模型驱动）
        """
        self.config_path = config_path or '配置/devices.yaml'
        self.simulation_mode = simulation_mode
        self.use_enhanced_simulation = use_enhanced_simulation
        self.devices = {}  # device_id -> device_config
        self.clients = {}  # device_id -> protocol client (legacy, kept for backward compat)
        self._lock = threading.Lock()

        # 设备状态缓存（TTL 5秒，避免频繁查询）
        self._status_cache: dict[str, dict] = {}
        self._status_cache_time: dict[str, float] = {}
        self._status_cache_ttl = 5.0

        # 断线自动重连循环的状态。
        # 此前这两个属性**从未在 __init__ 里初始化**，只靠 disconnect_all() /
        # start_reconnect_loop() 赋值 —— 任何在两者之前读取它的路径都会
        # AttributeError。这里显式声明，顺带把间隔记下来，
        # 供 switch_simulation_mode() 原样恢复（见该方法内注释）。
        self._reconnect_running = False
        self._reconnect_interval = 30

        # 连接池 - 复用连接，避免频繁创建/销毁
        #
        # round 257：这里原来是 `max_size=50, max_idle_time=300,
        # max_lifetime=3600` 的**硬编码**，而 `配置/system.yaml` 的
        # `performance.connection_pool` 写的是 `size: 10 / timeout: 30` ——
        # 两边都对不上，运维按配置调小连接数**没有任何效果**。
        # 现在真正读配置（读不到才回退 `CONNECTION_POOL_DEFAULTS`）。
        pool_cfg = load_connection_pool_settings()
        self._connection_pool = ConnectionPool(
            factory=self._create_client_for_pool,
            max_size=pool_cfg['size'],
            max_idle_time=pool_cfg['idle_timeout_seconds'],
            max_lifetime=pool_cfg['max_lifetime_seconds'],
            name="modbus",
        )
        logger.info("连接池已按配置初始化: size=%d idle=%ds lifetime=%ds",
                    pool_cfg['size'], pool_cfg['idle_timeout_seconds'],
                    pool_cfg['max_lifetime_seconds'])

        # 加载设备配置
        self.load_config()

    def _load_config_into(self, target: dict) -> bool:
        """把配置文件解析进 `target`（**不触碰 `self.devices`**）。

        之所以拆出来：热重载必须「先解析完、再原子替换」，
        不能直接往正在被别的线程迭代的 `self.devices` 里写。

        Returns:
            bool: **是否解析成功**。

        round 284：原实现 `except Exception: logger.error(...)` 把失败**吞掉**，
        调用方完全不知道 `target` 是空的。于是热重载遇到写坏的 yaml / 文件被清空 /
        文件不存在时，会拿一张**空表**去替换 `self.devices` ——
        **全部设备从配置表里消失**，紧接着采集任务被全部停掉，
        而接口回的是 `success: true`「移除 N 台设备」。

        一次保存失误 = 现场全部设备停止采集，且没有任何错误提示。
        失败必须能被调用方看见（fail-closed）。
        """
        try:
            config_file = paths.resolve(self.config_path)
            if not config_file.exists():
                logger.error(f"配置文件不存在: {self.config_path}")
                return False

            with open(config_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}

            if not isinstance(config, dict):
                logger.error(
                    "设备配置文件顶层不是映射（期望 `devices:` 列表），拒绝使用: %s",
                    type(config).__name__)
                return False

            # 解析设备配置
            devices_config = config.get('devices', [])
            if devices_config is None:
                devices_config = []
            if not isinstance(devices_config, list):
                logger.error(
                    "设备配置文件的 `devices` 不是列表（实际是 %s），拒绝使用",
                    type(devices_config).__name__)
                return False

            for device_config in devices_config:
                if not isinstance(device_config, dict):
                    logger.error("设备配置条目不是映射，跳过: %r", device_config)
                    continue
                device_id = device_config.get('id')
                if device_id:
                    target[device_id] = device_config
                    protocol = device_config.get('protocol', 'modbus_tcp')
                    logger.info(f"加载设备配置: {device_id} [{protocol}] - {device_config.get('name')}")

            # 按协议统计
            proto_count = {}
            for d in target.values():
                p = d.get('protocol', 'modbus_tcp')
                proto_count[p] = proto_count.get(p, 0) + 1
            summary = ', '.join(f"{k}:{v}" for k, v in proto_count.items())
            logger.info(f"共加载 {len(target)} 个设备 ({summary})")
            return True

        except Exception as e:
            logger.error(f"加载配置文件异常: {e}")
            return False

    def load_config(self) -> bool:
        """加载设备配置文件（整体替换，保证读取方永远看到完整的一张表）

        Returns:
            bool: 是否加载成功。**失败时保留原设备表**（round 284）——
            启动阶段解析失败还能靠"表是空的"看出来，运行期热重载失败时
            把表清空就等于把所有设备删了。
        """
        new_devices: dict[str, Any] = {}
        if not self._load_config_into(new_devices):
            logger.error(
                "设备配置加载失败，保留原有设备表（%d 台）: %s",
                len(self.devices), self.config_path)
            return False
        with self._lock:
            self.devices = new_devices
        return True

    def reload_config(self, allow_empty: bool = False) -> dict[str, Any]:
        """热重载设备配置（运行时调用，不会中断已有连接）

        实现要点：**先在临时字典里解析完，再一次性原子替换** `self.devices`。

        不能写成 `self.devices.clear()` 然后逐条填 —— 重连循环正在
        `for device_id, config in self.devices.items()`（见 start_reconnect_loop），
        中间态（空表或半张表）会让它抛
        `RuntimeError: dictionary changed size during iteration`，
        或者静默漏掉一整批设备（表现为「重载后一段时间内某些设备不采集」）。

        Args:
            allow_empty: 允许把设备表重载成**空表**。默认 False ——
                见下面的 `rejected` 说明。

        Returns:
            dict: `{'added': [...], 'removed': [...], 'kept': [...],
                   'rejected': str | None}`
            —— round 283：返回值是给**采集器**用的。重载只改配置表，
            采集器的定时器不会自己跟着变（删掉的设备会一直重试采集、
            新增的设备永远不采），调用方必须拿这个差集去
            `DataCollector.sync_device_tasks()`。

            `rejected` 非空表示**本次重载被拒绝、配置表原样保留**
            （round 284）。两种拒绝情形：
            · 配置文件解析失败（写坏的 yaml / 顶层不是映射 / devices 不是列表 /
              文件不存在）；
            · 解析结果是 0 台设备，而原配置有设备 —— 典型是文件被清空、
              或编辑器"先清空再写入"的中间态。此时若照旧替换，采集任务会被
              全部停掉，**现场所有设备停止采集**，而接口还会报成功。
        """
        old_ids = set(self.devices.keys())

        new_devices: dict[str, Any] = {}
        if not self._load_config_into(new_devices):
            reason = (f'配置文件解析失败（{self.config_path}），已保留原配置'
                      f'（{len(old_ids)} 台设备）')
            logger.error("设备配置重载被拒绝: %s", reason)
            return {'added': [], 'removed': [], 'kept': sorted(old_ids),
                    'rejected': reason}

        if not new_devices and old_ids and not allow_empty:
            reason = (f'解析结果为 0 台设备，而原配置有 {len(old_ids)} 台 —— '
                      f'疑似文件被清空或写坏，已保留原配置')
            logger.error("设备配置重载被拒绝: %s", reason)
            return {'added': [], 'removed': [], 'kept': sorted(old_ids),
                    'rejected': reason}

        with self._lock:
            self.devices = new_devices

        new_ids = set(self.devices.keys())

        added = sorted(new_ids - old_ids)
        removed = sorted(old_ids - new_ids)
        if added:
            logger.info(f"新增设备: {added}")
        if removed:
            for did in removed:
                self.disconnect_device(did)
                # round 312：**热重载删设备不走 `remove_device()`** ——
                # 它只替换配置表，所以这里必须逐个通知，
                # 否则「yaml 里删掉一台设备」之后 OEE/能耗报表里它还在，
                # 重新加回同名设备时还会继承旧的连续失败计数。
                notify_device_removed(did)
            logger.info(f"移除设备: {removed}")

        return {
            'added': added,
            'removed': removed,
            'kept': sorted(new_ids & old_ids),
            'rejected': None,
        }

    def get_device_config(self, device_id: str) -> dict[str, Any] | None:
        """获取设备配置"""
        return self.devices.get(device_id)

    def get_all_devices(self) -> dict[str, dict[str, Any]]:
        """获取所有设备配置"""
        return self.devices.copy()

    def _create_client_for_pool(self, device_id: str):
        """连接池工厂方法 - 根据 device_id 创建对应的协议客户端"""
        device_config = self.devices.get(device_id)
        if not device_config:
            logger.error(f"设备 {device_id} 配置不存在")
            return None
        return _create_client(device_config, self.simulation_mode, self.use_enhanced_simulation)

    def get_client(self, device_id: str):
        """
        获取设备客户端（懒创建，通过连接池复用）

        Returns:
            协议客户端实例（ModbusClient / OPCUAClient / MQTTClient / RESTDeviceClient）
        """
        # 无锁快速路径 - 先查 legacy 缓存
        client = self.clients.get(device_id)
        if client is not None:
            return client

        with self._lock:
            # 双重检查：另一个线程可能已经创建了客户端
            client = self.clients.get(device_id)
            if client is not None:
                return client

            # 从连接池获取（池会自动创建或复用）
            device_config = self.devices.get(device_id)
            if not device_config:
                logger.error(f"设备 {device_id} 配置不存在")
                return None

            client = self._connection_pool.acquire(device_id)
            if client is None:
                return None

            # 同步到 legacy 缓存，保持向后兼容
            self.clients[device_id] = client

        return client

    def connect_device(self, device_id: str, timeout: int = 20) -> bool:
        """连接设备

        Args:
            timeout: 连接超时秒数（默认20s）
        """
        client = self.get_client(device_id)
        if not client:
            return False
        import threading
        result = [False]
        def _do_connect():
            try:
                result[0] = client.connect()
            except Exception:
                result[0] = False
        t = threading.Thread(target=_do_connect, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            logger.warning(f"设备 {device_id} 连接超时 ({timeout}s)")
            return False
        return result[0]

    def disconnect_device(self, device_id: str):
        """断开设备连接（同时从连接池中移除）"""
        with self._lock:
            client = self.clients.pop(device_id, None)
        if client:
            client.disconnect()
        # 从连接池中移除
        self._connection_pool.remove(device_id)

    def connect_all(self, timeout: int = 20) -> dict[str, bool]:
        """连接所有设备

        Args:
            timeout: 单个设备连接超时秒数（默认20s）
        """
        results = {}
        for device_id in list(self.devices):     # 快照：connect 期间可能有增删
            if self.devices.get(device_id, {}).get('enabled', True):
                results[device_id] = self.connect_device(device_id, timeout=timeout)
            else:
                results[device_id] = None
        return results

    def disconnect_all(self):
        """断开所有设备连接"""
        self._reconnect_running = False
        for device_id in list(self.clients.keys()):
            self.disconnect_device(device_id)
        self._connection_pool.shutdown()
        self.clients.clear()

    def start_reconnect_loop(self, interval: int = 30):
        """启动断线自动重连后台线程

        Args:
            interval: 检查间隔秒数（默认30s）
        """
        self._reconnect_interval = interval
        self._reconnect_running = True
        def _reconnect_loop():
            while self._reconnect_running:
                try:
                    # 快照迭代：`add_device` / `remove_device` 会在别的线程里
                    # 增删这张表，直接 `self.devices.items()` 会抛
                    # RuntimeError（dictionary changed size during iteration）。
                    for device_id, config in list(self.devices.items()):
                        if not self._reconnect_running:
                            break
                        if not config.get('enabled', True):
                            continue
                        client = self.clients.get(device_id)
                        if client and not getattr(client, 'connected', False):
                            try:
                                logger.info(f"自动重连设备: {device_id}")
                                client.connect()
                            except Exception as e:
                                logger.debug(f"自动重连失败 {device_id}: {e}")
                except Exception as e:
                    logger.error(f"重连循环异常: {e}")
                import time as _time
                _time.sleep(interval)
        t = threading.Thread(target=_reconnect_loop, daemon=True)
        t.start()
        logger.info(f"设备自动重连线程已启动（间隔{interval}秒）")

    def switch_simulation_mode(self, new_mode: bool) -> dict[str, Any]:
        """
        运行时切换模拟/真实模式（热切换，无需重启）

        Args:
            new_mode: True=模拟模式, False=真实模式

        Returns:
            切换结果字典
        """
        old_mode = self.simulation_mode
        if old_mode == new_mode:
            return {
                'success': True,
                'message': f'已经是{"模拟" if new_mode else "真实"}模式，无需切换',
                'simulation_mode': new_mode,
                'reconnected': 0
            }

        logger.info(f"切换模式: {'模拟' if old_mode else '真实'} → {'模拟' if new_mode else '真实'}")

        # 0. 记住重连循环此前是否在跑。
        # `disconnect_all()` 会把 `_reconnect_running` 置 False —— 这在一处是正确的
        # （它同时服务于 run.py 的关停路径），但**模式切换不是关停**。
        # 原先切换后从不重启重连循环，后果是**自动重连永久静默失效**：
        # 切一次模式，之后任何设备掉线都不会再重连，也没有任何报错，
        # 运维只看到设备一直离线。
        reconnect_was_running = self._reconnect_running

        # 1. 断开所有现有连接并关闭连接池
        self.disconnect_all()

        # 2. 清除客户端缓存（下次get_client时会用新模式创建）
        self.clients.clear()

        # 3. 重建连接池（使用新模式的工厂方法）
        #    round 257：这里原来又是一份 `max_size=50 / 300 / 3600` 的硬编码副本 ——
        #    切换模拟/真实模式会把 __init__ 里按配置建好的池**换回硬编码值**，
        #    配置只在启动时生效一次、切一次模式就失效。改成同一个来源。
        pool_cfg = load_connection_pool_settings()
        self._connection_pool = ConnectionPool(
            factory=self._create_client_for_pool,
            max_size=pool_cfg['size'],
            max_idle_time=pool_cfg['idle_timeout_seconds'],
            max_lifetime=pool_cfg['max_lifetime_seconds'],
            name="modbus",
        )

        # 4. 更新模式标志
        self.simulation_mode = new_mode

        # 5. 重新连接所有设备
        results = self.connect_all()
        connected = sum(1 for v in results.values() if v is True)
        failed = sum(1 for v in results.values() if v is False)

        logger.info(f"模式切换完成: {connected}个设备连接成功, {failed}个失败")

        # 6. 恢复第 0 步记下的重连循环（模式切换不该把自动重连关掉）
        if reconnect_was_running:
            self.start_reconnect_loop(interval=self._reconnect_interval)
            logger.info("模式切换后已恢复断线自动重连循环")

        return {
            'success': True,
            'message': f'已切换到{"模拟" if new_mode else "真实"}模式',
            'simulation_mode': new_mode,
            'reconnected': connected,
            'failed': failed,
            'reconnect_loop_restored': reconnect_was_running,
        }

    def get_device_status(self, device_id: str) -> dict[str, Any]:
        """获取设备状态（带5秒缓存）"""
        import time
        now = time.time()

        # 检查缓存
        if device_id in self._status_cache:
            cache_age = now - self._status_cache_time.get(device_id, 0)
            if cache_age < self._status_cache_ttl:
                return self._status_cache[device_id]

        device_config = self.devices.get(device_id)

        if not device_config:
            return {'error': '设备配置不存在'}

        # 局部导入：`采集层.interfaces` 与本模块存在循环引用风险，
        # 本文件既有代码（`_get_brief_status`）也是这么做的，保持一致。
        from 采集层.interfaces import IDeviceManager

        status = {
            'device_id': device_id,
            'name': device_config.get('name'),
            'description': device_config.get('description'),
            'protocol': device_config.get('protocol', 'modbus_tcp'),
            'host': device_config.get('host', device_config.get('endpoint', '')),
            'port': device_config.get('port'),
            'enabled': device_config.get('enabled', True),
            'connected': False,
            # ⚠️ 这两个字段必须在这里就有 —— 前端读它们（见
            # `tests/test_device_status_shape_contract.py` 的 FRONTEND_REQUIRED）：
            #   * `device_category` 缺 → 「机械类」筛选/计数恒为 0，
            #     且 Dashboard 的启停按钮（条件含 `device_category === 'mechanical'`）**永不显示**
            #   * `stopped` 缺 → "已停止"状态永不出现、按钮文案永远错
            # 本类的 `_get_brief_status` 早就给了它们，只有 `get_device_status` 漏了 ——
            # 同一条接口两种形状，正是本仓库反复踩的坑。
            'stopped': False,
            'device_category': IDeviceManager.get_device_category(device_config),
            'registers': device_config.get('registers', []),
            'nodes': device_config.get('nodes', []),
            'topics': device_config.get('topics', []),
            'endpoints': device_config.get('endpoints', []),
            'stats': {}
        }

        # 使用 get_client() 实现懒创建，确保客户端存在
        client = self.get_client(device_id)
        if client:
            # 模拟模式下，尝试连接但不强制标记为已连接
            if self.simulation_mode:
                if not getattr(client, 'connected', False):
                    if device_config.get('enabled', True):
                        try:
                            client.connect()
                        except Exception as e:
                            logger.debug(f"模拟设备 {device_id} 连接失败: {e}")
            else:
                # 真实模式下，尝试自动重连
                if not getattr(client, 'connected', False):
                    if device_config.get('enabled', True):
                        try:
                            client.connect()
                        except Exception as e:
                            # 连接失败不影响返回状态（connected 字段仍为 False），
                            # 但状态查询是高频轮询路径，故用 debug 避免刷屏
                            logger.debug(f"设备 {device_id} 自动重连失败: {e}")

            status['connected'] = getattr(client, 'connected', False)
            status['stopped'] = getattr(client, 'stopped', False)
            status['stats'] = getattr(client, 'stats', {})

        # 更新缓存
        self._status_cache[device_id] = status
        self._status_cache_time[device_id] = now
        return status

    def get_all_status(self, brief: bool = False) -> list[dict[str, Any]]:
        """获取所有设备状态

        Args:
            brief: True时返回精简字段，适用于100+设备的仪表盘轮询
        """
        if brief:
            # 快照：推导式同样是活视图迭代，增删设备并发时抛 RuntimeError
            return [self._get_brief_status(did) for did in list(self.devices)]
        return [self.get_device_status(did) for did in list(self.devices)]

    def _get_brief_status(self, device_id: str) -> dict[str, Any]:
        """获取设备精简状态（仅仪表盘所需字段）"""
        from 采集层.interfaces import IDeviceManager
        device_config = self.devices.get(device_id, {})
        client = self.clients.get(device_id)
        connected = getattr(client, 'connected', False) if client else False
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
            'stopped': getattr(client, 'stopped', False) if client else False,
            'status': 'online' if connected else 'offline',
            'protocol': device_config.get('protocol', 'modbus_tcp'),
            'host': device_config.get('host', device_config.get('endpoint', '')),
            'device_category': IDeviceManager.get_device_category(device_config),
            'registers': [{'name': r.get('name', ''), 'unit': r.get('unit', '')} for r in regs],
            'nodes': [{'name': n.get('name', ''), 'unit': n.get('unit', '')} for n in nodes],
            'topics': [{'name': t.get('name', ''), 'unit': t.get('unit', '')} for t in topics],
            'endpoints': [{'name': e.get('name', ''), 'unit': e.get('unit', '')} for e in endpoints],
        }

    def add_device(self, device_config: dict[str, Any]) -> bool:
        """
        添加设备（运行时热添加）

        Args:
            device_config: 设备配置，必须包含 id 和 protocol 字段
        """
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
                self._connection_pool.remove(device_id)

            # ⚠️ 必须持锁写入：`_save_config()` 在锁内做
            # `list(self.devices.values())`，而重连循环在读 `self.devices.items()`。
            # 不加锁就会出现「迭代中被插入/删除」→
            # `RuntimeError: dictionary changed size during iteration`，
            # 被两处的 `except Exception` 各自吞掉，表现为
            # 「配置没存下来」+「这一轮重连整批跳过」，日志里只有一行异常。
            with self._lock:
                self.devices[device_id] = device_config

            # round 302：**落盘失败必须回滚并如实返回**。
            # 原来 `self._save_config()` 的返回值直接丢弃、紧接着 `return True`：
            # 磁盘满 / 只读目录 / 原子写失败时接口回「添加成功」、界面出现设备、
            # 采集也起来了，**重启后设备消失**（devices.yaml 从来没写进去），
            # 运维只会认为"系统丢配置"。同文件的 `update_device` 早就是正确写法，
            # 这一处漏了。
            if not self._save_config():
                with self._lock:
                    self.devices.pop(device_id, None)
                    self.clients.pop(device_id, None)
                try:
                    self._connection_pool.remove(device_id)
                except Exception as e:      # noqa: BLE001 —— 池里本来就没有时不该二次失败
                    logger.debug("回滚连接池条目失败: %s", e, exc_info=True)
                logger.error("添加设备失败：配置落盘失败，已回滚 %s", device_id)
                return False

            # 热添加后自动连接设备
            if device_config.get('enabled', True):
                try:
                    connected = self.connect_device(device_id)
                    if connected:
                        logger.info(f"添加设备并连接成功: {device_id} [{protocol}]")
                    else:
                        logger.warning(f"添加设备成功但连接失败: {device_id} [{protocol}]")
                except Exception as conn_e:
                    logger.warning(f"添加设备成功但连接异常: {device_id} [{conn_e}]")
            else:
                logger.info(f"添加设备（已禁用）: {device_id} [{protocol}]")

            return True

        except Exception as e:
            logger.error(f"添加设备异常: {e}")
            return False

    def remove_device(self, device_id: str) -> bool:
        """移除设备"""
        try:
            self.disconnect_device(device_id)
            # 持锁删除：与 add_device 同理，防「迭代中被删除」
            with self._lock:
                removed = self.devices.pop(device_id, None)
                self.clients.pop(device_id, None)
            self._connection_pool.remove(device_id)

            # round 302：落盘失败要把设备**放回去**，并如实返回 False。
            # 否则「删掉了但文件里还在」—— 重启后设备回来，运维以为见鬼；
            # 而接口当时回的是「已删除」。
            if not self._save_config():
                if removed is not None:
                    with self._lock:
                        self.devices[device_id] = removed
                logger.error("移除设备失败：配置落盘失败，已回滚 %s", device_id)
                return False

            # round 310：**状态缓存必须一起清**。`_status_cache` / `_status_cache_time`
            # 以 device_id 为 key、TTL 5 秒，但删除设备时从不清 ——
            # 后果不是"多占几十字节"，而是**同名设备重新添加后 5 秒内返回旧状态**：
            # 旧设备删之前是 connected=True，新设备其实还没连上，
            # `get_device_status()` 却回"已连接"，界面/写操作前置检查全部被误导。
            # 放在落盘成功之后：落盘失败会回滚设备，那时缓存仍然有效。
            self._status_cache.pop(device_id, None)
            self._status_cache_time.pop(device_id, None)
            logger.info(f"移除设备: {device_id}")
            # round 312：通知各模块清掉按设备索引的运行时状态。
            # 这条路径是「界面上删设备」，但**热重载不走它** ——
            # 所以 `reload_config()` 里的差集也要单独通知（见下）。
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

    def get_pool_stats(self) -> dict:
        """获取连接池统计信息"""
        return self._connection_pool.get_stats()

    def _save_config(self) -> bool:
        """保存设备配置到文件（线程安全 + 原子写入）。

        Returns:
            bool: 是否真的落盘成功。

        ⚠️ 返回值不是装饰品。此前这个方法吞掉异常并返回 None，而
        `展示层/api/api_devices.py::update_device` 丢掉返回值、无条件回
        「设备 X 更新成功」—— 于是**磁盘写不进去时，用户看到成功、重启后改动消失**。
        这正是本项目反复修的「静默假成功」。同仓的
        `报警层/alarm_manager._save_config` 与 `智能层/energy_manager._save_config`
        早已改成返回 bool，这里补齐口径。
        """
        try:
            with self._lock:
                config = {'devices': list(self.devices.values())}
            config_file = paths.resolve(self.config_path)
            config_file.parent.mkdir(parents=True, exist_ok=True)
            # 原子写入：先写临时文件再 rename，防止崩溃时配置损坏
            tmp_file = config_file.with_suffix('.tmp')
            with open(tmp_file, 'w', encoding='utf-8') as f:
                yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
            tmp_file.replace(config_file)
            logger.info("设备配置已保存")
            return True
        except Exception as e:
            logger.error(f"保存配置文件异常: {e}")
            return False
