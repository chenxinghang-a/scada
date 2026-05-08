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
