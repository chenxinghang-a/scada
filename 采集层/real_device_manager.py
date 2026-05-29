"""
真实设备管理器
完全独立的真实设备实现，连接实际硬件
"""

import logging
import yaml
import time
from typing import Any
from pathlib import Path

from .interfaces import IDeviceManager, IDeviceClient
from .modbus_client import ModbusClient
from .opcua_client import OPCUAClient
from .mqtt_client import MQTTClient
from .rest_client import RESTDeviceClient

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

    SUPPORTED_PROTOCOLS = ['modbus_tcp', 'modbus_rtu', 'opcua', 'mqtt', 'rest']

    # 连接失败重试策略
    MAX_RETRY_COUNT = 3       # 最大重试次数后进入退避
    RETRY_BACKOFF_BASE = 30   # 退避基数（秒）
    MAX_RETRY_INTERVAL = 300  # 最大退避间隔（5分钟）

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
        self._stopped_devices: set[str] = set()  # 单独停止的设备
        
        # 连接失败跟踪：device_id -> {'retry_count': int, 'last_retry': timestamp}
        self._connection_failures: dict[str, dict[str, Any]] = {}
        
        # 加载设备配置
        self.load_config()
        
        logger.info("真实设备管理器初始化完成")

    def load_config(self):
        """加载设备配置文件"""
        try:
            config_file = Path(self.config_path)
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
            for d in self.devices.values():
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

    def stop_device(self, device_id: str) -> bool:
        """停止指定设备（仅限机械类设备）"""
        config = self.devices.get(device_id)
        if not config:
            return False

        category = IDeviceManager.get_device_category(config)
        if category != 'mechanical':
            logger.warning(f"设备 {device_id} 类型为 {category}，不可启停")
            return False

        self._stopped_devices.add(device_id)
        client = self.get_client(device_id)
        if client:
            # 从配置读取控制地址，而非硬编码
            ctrl = config.get('control', {})
            stop_coil = ctrl.get('stop_coil')      # 线圈地址
            stop_register = ctrl.get('stop_register')  # 寄存器地址

            if stop_coil is not None and hasattr(client, 'write_single_coil'):
                success = client.write_single_coil(stop_coil, False)
            elif stop_register is not None and hasattr(client, 'write_single_register'):
                success = client.write_single_register(stop_register, 0)
            else:
                # 无控制配置时降级：coil 0 = False
                success = client.write_single_coil(0, False) if hasattr(client, 'write_single_coil') else False

            if success:
                logger.info(f"[真实] 设备 {device_id} 已停止")
            else:
                logger.error(f"[真实] 设备 {device_id} 停止命令发送失败")
                return False
        return True

    def start_device(self, device_id: str) -> bool:
        """启动指定设备（仅限机械类设备）"""
        config = self.devices.get(device_id)
        if not config:
            return False

        category = IDeviceManager.get_device_category(config)
        if category != 'mechanical':
            logger.warning(f"设备 {device_id} 类型为 {category}，不可启停")
            return False

        self._stopped_devices.discard(device_id)
        client = self.get_client(device_id)
        if client:
            ctrl = config.get('control', {})
            start_coil = ctrl.get('start_coil')
            start_register = ctrl.get('start_register')

            if start_coil is not None and hasattr(client, 'write_single_coil'):
                success = client.write_single_coil(start_coil, True)
            elif start_register is not None and hasattr(client, 'write_single_register'):
                success = client.write_single_register(start_register, 1)
            else:
                success = client.write_single_coil(0, True) if hasattr(client, 'write_single_coil') else False

            if success:
                logger.info(f"[真实] 设备 {device_id} 已启动")
            else:
                logger.error(f"[真实] 设备 {device_id} 启动命令发送失败")
                return False
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
        for device_id in self.devices:
            if not self.devices[device_id].get('enabled', True):
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
        """断开所有设备连接"""
        for device_id in list(self.clients.keys()):
            self.disconnect_device(device_id)

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

    def get_all_status(self) -> list[dict[str, Any]]:
        """获取所有设备状态"""
        return [self.get_device_status(did) for did in self.devices]

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
                if device_id in self.clients:
                    del self.clients[device_id]

            self.devices[device_id] = device_config
            self._save_config()

            logger.info(f"[真实] 添加设备: {device_id} [{protocol}]")
            return True

        except Exception as e:
            logger.error(f"添加设备异常: {e}")
            return False

    def remove_device(self, device_id: str) -> bool:
        """移除设备"""
        try:
            self.disconnect_device(device_id)
            self.devices.pop(device_id, None)
            self.clients.pop(device_id, None)
            self._save_config()
            logger.info(f"[真实] 移除设备: {device_id}")
            return True
        except Exception as e:
            logger.error(f"移除设备异常: {e}")
            return False

    def get_protocol_summary(self) -> dict[str, int]:
        """获取各协议设备数量统计"""
        summary = {}
        for d in self.devices.values():
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

    def _save_config(self):
        """保存设备配置到文件"""
        try:
            config = {'devices': list(self.devices.values())}
            config_file = Path(self.config_path)
            config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(config_file, 'w', encoding='utf-8') as f:
                yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
            logger.info("[真实] 设备配置已保存")
        except Exception as e:
            logger.error(f"保存配置文件异常: {e}")
