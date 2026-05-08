"""
设备管理器工厂
根据配置自动选择模拟或真实设备管理器
"""

import logging
import yaml
from pathlib import Path
from typing import Any

from .interfaces import IDeviceManager
from .simulated_device_manager import SimulatedDeviceManager
from .real_device_manager import RealDeviceManager

logger = logging.getLogger(__name__)


class DeviceManagerFactory:
    """
    设备管理器工厂
    
    根据系统配置自动创建对应的设备管理器：
    - simulation_mode=True: 创建模拟设备管理器（使用仿真数据）
    - simulation_mode=False: 创建真实设备管理器（连接实际硬件）
    
    特点：
    - 两个管理器完全独立，互不干扰
    - 真实模式不显示"真实模式"标识
    - 支持运行时切换模式
    """
    
    @staticmethod
    def create(config_path: str = '配置/system.yaml') -> IDeviceManager:
        """
        根据配置创建设备管理器
        
        Args:
            config_path: 系统配置文件路径
            
        Returns:
            设备管理器实例（模拟或真实）
        """
        # 读取系统配置
        simulation_mode = True  # 默认模拟模式
        
        config_file = Path(config_path)
        if config_file.exists():
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f) or {}
                simulation_mode = config.get('system', {}).get('simulation_mode', True)
            except Exception as e:
                logger.warning(f"读取配置文件失败: {e}，使用默认模拟模式")
        
        # 根据模式创建对应的管理器
        if simulation_mode:
            logger.info("创建模拟设备管理器（仿真数据模式）")
            return SimulatedDeviceManager()
        else:
            logger.info("创建真实设备管理器（连接实际硬件）")
            return RealDeviceManager()
    
    @staticmethod
    def create_simulated() -> SimulatedDeviceManager:
        """直接创建模拟设备管理器"""
        return SimulatedDeviceManager()
    
    @staticmethod
    def create_real() -> RealDeviceManager:
        """直接创建真实设备管理器"""
        return RealDeviceManager()


def get_device_manager(config_path: str = '配置/system.yaml') -> IDeviceManager:
    """
    获取设备管理器实例（工厂方法的便捷封装）
    
    Args:
        config_path: 系统配置文件路径
        
    Returns:
        设备管理器实例
    """
    return DeviceManagerFactory.create(config_path)