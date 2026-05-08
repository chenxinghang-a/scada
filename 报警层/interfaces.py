"""
报警层抽象接口
定义模拟和真实报警输出的统一接口
"""

from abc import ABC, abstractmethod
from typing import Any


class IAlarmOutput(ABC):
    """报警输出抽象接口"""

    @abstractmethod
    def activate_alarm(self, level: str, message: str = '') -> bool:
        """
        激活报警
        
        Args:
            level: 报警级别 ('critical', 'warning', 'info')
            message: 报警消息
            
        Returns:
            是否成功
        """
        pass

    @abstractmethod
    def acknowledge(self) -> bool:
        """
        消音（关闭蜂鸣器，灯保持）
        
        Returns:
            是否成功
        """
        pass

    @abstractmethod
    def reset(self) -> bool:
        """
        复位（全部清零，恢复绿灯正常）
        
        Returns:
            是否成功
        """
        pass

    @abstractmethod
    def manual_control(self, **kwargs) -> dict[str, Any]:
        """
        手动控制
        
        Args:
            **kwargs: 控制参数 (red, yellow, green, buzzer, duration)
            
        Returns:
            控制结果
        """
        pass

    @abstractmethod
    def get_status(self) -> dict[str, Any]:
        """
        获取当前状态
        
        Returns:
            状态字典
        """
        pass

    @property
    @abstractmethod
    def enabled(self) -> bool:
        """是否启用"""
        pass


class IBroadcastSystem(ABC):
    """广播系统抽象接口"""

    @abstractmethod
    def speak(self, text: str, level: str = 'info', area: str = None, source: str = 'manual') -> dict[str, Any]:
        """
        语音广播
        
        Args:
            text: 广播内容
            level: 级别 ('critical', 'warning', 'info')
            area: 广播区域
            source: 来源 ('manual', 'alarm', 'system')
            
        Returns:
            广播结果
        """
        pass

    @abstractmethod
    def get_areas(self) -> list[str]:
        """
        获取可用广播区域
        
        Returns:
            区域列表
        """
        pass

    @abstractmethod
    def get_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """
        获取广播历史
        
        Args:
            limit: 返回数量
            
        Returns:
            历史记录列表
        """
        pass

    @abstractmethod
    def get_status(self) -> dict[str, Any]:
        """
        获取系统状态
        
        Returns:
            状态字典
        """
        pass

    @property
    @abstractmethod
    def enabled(self) -> bool:
        """是否启用"""
        pass
