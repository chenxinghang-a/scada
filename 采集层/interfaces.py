"""
设备层抽象接口
定义模拟和真实设备的统一接口
"""

from abc import ABC, abstractmethod
from typing import Any


class IDeviceClient(ABC):
    """设备客户端抽象接口"""

    @abstractmethod
    def connect(self) -> bool:
        """连接设备"""
        pass

    @abstractmethod
    def disconnect(self):
        """断开连接"""
        pass

    @abstractmethod
    def read_holding_registers(self, address: int, count: int = 1) -> list[int] | None:
        """读取保持寄存器"""
        pass

    @abstractmethod
    def read_coils(self, address: int, count: int = 1) -> list[bool] | None:
        """读取线圈"""
        pass

    @abstractmethod
    def write_single_register(self, address: int, value: int) -> bool:
        """写入单个寄存器"""
        pass

    @abstractmethod
    def write_single_coil(self, address: int, value: bool) -> bool:
        """写入单个线圈"""
        pass

    @abstractmethod
    def get_latest_data(self) -> dict[str, Any]:
        """获取最新数据"""
        pass

    @property
    @abstractmethod
    def connected(self) -> bool:
        """是否已连接"""
        pass

    @property
    @abstractmethod
    def stats(self) -> dict[str, Any]:
        """获取统计信息"""
        pass


class IDeviceManager(ABC):
    """设备管理器抽象接口"""

    @abstractmethod
    def load_config(self):
        """加载设备配置"""
        pass

    @abstractmethod
    def get_client(self, device_id: str) -> IDeviceClient | None:
        """获取设备客户端"""
        pass

    @abstractmethod
    def connect_device(self, device_id: str) -> bool:
        """连接设备"""
        pass

    @abstractmethod
    def disconnect_device(self, device_id: str):
        """断开设备连接"""
        pass

    @abstractmethod
    def connect_all(self) -> dict[str, bool]:
        """连接所有设备"""
        pass

    @abstractmethod
    def disconnect_all(self):
        """断开所有设备连接"""
        pass

    @abstractmethod
    def get_device_status(self, device_id: str) -> dict[str, Any]:
        """获取设备状态"""
        pass

    @abstractmethod
    def get_all_status(self) -> list[dict[str, Any]]:
        """获取所有设备状态"""
        pass

    @abstractmethod
    def add_device(self, device_config: dict[str, Any]) -> bool:
        """添加设备"""
        pass

    @abstractmethod
    def remove_device(self, device_id: str) -> bool:
        """移除设备"""
        pass

    @abstractmethod
    def get_protocol_summary(self) -> dict[str, int]:
        """获取协议统计"""
        pass

    def set_estop_override(self, active: bool):
        """
        设置紧急停机覆盖状态（可选实现）
        
        模拟模式下，E-STOP 激活时停止仪表数据生成，保留安全类（灯/蜂鸣器/继电器）输出。
        真实模式下可以忽略此方法。
        """
        pass

    def stop_device(self, device_id: str) -> bool:
        """
        停止指定设备（仅 mechanical 类型有实际效果）
        模拟模式：停止数据生成，所有寄存器归零
        真实模式：向设备发送停止信号
        """
        return False

    def start_device(self, device_id: str) -> bool:
        """
        启动指定设备（恢复数据生成）
        """
        return False

    @staticmethod
    def get_device_category(device_config: dict) -> str:
        """
        根据配置推断设备类别
        - mechanical: 含速度/力/位置/计数等机械寄存器 → 可启停
        - instrument: 纯传感器/仪表 → 不可启停
        - safety: 灯/蜂鸣器/继电器 → 不可启停
        """
        # 优先使用显式声明
        category = device_config.get('device_category', '').lower()
        if category in ('mechanical', 'instrument', 'safety'):
            return category

        # 按设备名/寄存器名推断
        name = (device_config.get('name', '') or '').lower()
        desc = (device_config.get('description', '') or '').lower()

        # 安全设备
        safety_keywords = ['signal_tower', '信号灯塔', 'relay', '继电器',
                          'alarm', '报警', 'buzzer', '蜂鸣器', '警灯', '灯塔']
        for kw in safety_keywords:
            if kw in name or kw in desc:
                return 'safety'

        # 机械类：检查寄存器名
        from .simulated_client import _is_machinery
        for reg in device_config.get('registers', []):
            if _is_machinery(reg.get('name', '')):
                return 'mechanical'

        # 默认：仪表
        return 'instrument'
