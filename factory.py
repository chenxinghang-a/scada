"""
系统工厂模块（备用）
根据配置创建独立的模拟或真实组件

注意：当前run.py未使用此模块，直接在main()中创建组件。
此模块保留供run_v2.py或未来重构使用。
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class SystemFactory:
    """
    系统工厂
    
    根据配置创建独立的模拟或真实组件，实现完全分离。
    
    使用方式：
        factory = SystemFactory(mode='simulated')  # 或 'real'
        device_manager = factory.create_device_manager()
        alarm_output = factory.create_alarm_output()
        broadcast_system = factory.create_broadcast_system()
    """

    def __init__(self, mode: str = 'simulated', config: dict[str, Any] | None = None):
        """
        初始化系统工厂
        
        Args:
            mode: 运行模式 ('simulated' 或 'real')
            config: 配置字典
        """
        if mode not in ('simulated', 'real'):
            raise ValueError(f"不支持的模式: {mode}，必须是 'simulated' 或 'real'")
        
        self.mode = mode
        self.config = config or {}
        
        logger.info(f"系统工厂初始化完成，模式: {mode}")

    def create_device_manager(self, config_path: str = None):
        """
        创建设备管理器
        
        Args:
            config_path: 设备配置文件路径
            
        Returns:
            设备管理器实例
        """
        if self.mode == 'simulated':
            from 采集层.simulated_device_manager import SimulatedDeviceManager
            logger.info("创建模拟设备管理器")
            return SimulatedDeviceManager(config_path)
        else:
            from 采集层.real_device_manager import RealDeviceManager
            logger.info("创建真实设备管理器")
            return RealDeviceManager(config_path)

    def create_alarm_output(self, config: dict[str, Any] = None):
        """
        创建报警输出
        
        Args:
            config: 报警输出配置
            
        Returns:
            报警输出实例
        """
        if self.mode == 'simulated':
            from 报警层.simulated_alarm_output import SimulatedAlarmOutput
            logger.info("创建模拟报警输出")
            return SimulatedAlarmOutput(config)
        else:
            from 报警层.real_alarm_output import RealAlarmOutput
            logger.info("创建真实报警输出")
            return RealAlarmOutput(config)

    def create_broadcast_system(self, config: dict[str, Any] = None):
        """
        创建广播系统
        
        Args:
            config: 广播系统配置
            
        Returns:
            广播系统实例
        """
        if self.mode == 'simulated':
            from 报警层.simulated_broadcast import SimulatedBroadcastSystem
            logger.info("创建模拟广播系统")
            return SimulatedBroadcastSystem(config)
        else:
            from 报警层.real_broadcast import RealBroadcastSystem
            logger.info("创建真实广播系统")
            return RealBroadcastSystem(config)

    def get_mode(self) -> str:
        """获取当前模式"""
        return self.mode

    def is_simulated(self) -> bool:
        """是否是模拟模式"""
        return self.mode == 'simulated'

    def is_real(self) -> bool:
        """是否是真实模式"""
        return self.mode == 'real'
