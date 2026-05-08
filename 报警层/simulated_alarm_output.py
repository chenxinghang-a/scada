"""
模拟报警输出
完全独立的模拟实现，不依赖真实硬件
"""

import logging
from typing import Any
from datetime import datetime

from .interfaces import IAlarmOutput

logger = logging.getLogger(__name__)


class AlarmLightPattern:
    """报警灯闪烁模式"""
    OFF = 'off'
    STEADY = 'steady'
    SLOW_FLASH = 'slow'
    FAST_FLASH = 'fast'


class SimulatedAlarmOutput(IAlarmOutput):
    """
    模拟报警输出
    
    特点：
    - 完全独立，不依赖任何真实硬件
    - 所有操作输出到日志
    - 用于演示和测试环境
    - 可以独立运行，无需切换模式
    """

    def __init__(self, config: dict[str, Any] | None = None):
        """
        初始化模拟报警输出
        
        Args:
            config: 配置字典（可选）
        """
        self.config = config or {}
        self._enabled = self.config.get('enabled', True)
        
        # 当前输出状态
        self.current_state = {
            'red': False,
            'yellow': False,
            'green': True,   # 默认绿灯常亮=系统正常
            'buzzer': False,
            'pattern': AlarmLightPattern.STEADY,
            'level': None,
            'message': '',
            'since': None
        }
        
        # 报警历史
        self.history: list[dict[str, Any]] = []
        
        logger.info("[模拟] 报警输出初始化完成")

    @property
    def enabled(self) -> bool:
        """是否启用"""
        return self._enabled

    def activate_alarm(self, level: str, message: str = '') -> bool:
        """
        激活报警
        
        Args:
            level: 报警级别
            message: 报警消息
            
        Returns:
            是否成功
        """
        if not self._enabled:
            return False

        # 根据级别设置灯和蜂鸣器
        if level == 'critical':
            self.current_state.update({
                'red': True,
                'yellow': False,
                'green': False,
                'buzzer': True,
                'pattern': AlarmLightPattern.FAST_FLASH,
                'level': level,
                'message': message,
                'since': datetime.now().isoformat()
            })
            logger.warning(f"[模拟报警] 严重报警: 红灯快闪 + 蜂鸣器 | {message}")
            
        elif level == 'warning':
            self.current_state.update({
                'red': False,
                'yellow': True,
                'green': False,
                'buzzer': True,
                'pattern': AlarmLightPattern.SLOW_FLASH,
                'level': level,
                'message': message,
                'since': datetime.now().isoformat()
            })
            logger.warning(f"[模拟报警] 警告: 黄灯慢闪 + 蜂鸣器 | {message}")
            
        else:  # info
            self.current_state.update({
                'red': False,
                'yellow': True,
                'green': False,
                'buzzer': False,
                'pattern': AlarmLightPattern.STEADY,
                'level': level,
                'message': message,
                'since': datetime.now().isoformat()
            })
            logger.info(f"[模拟报警] 信息: 黄灯常亮 | {message}")

        # 记录历史
        self.history.append({
            'timestamp': datetime.now().isoformat(),
            'action': 'activate',
            'level': level,
            'message': message,
            'state': self.current_state.copy()
        })

        return True

    def acknowledge(self) -> bool:
        """
        消音（关闭蜂鸣器，灯保持）
        
        Returns:
            是否成功
        """
        if not self._enabled:
            return False

        self.current_state['buzzer'] = False
        
        logger.info("[模拟报警] 消音: 蜂鸣器关闭，报警灯保持")
        
        # 记录历史
        self.history.append({
            'timestamp': datetime.now().isoformat(),
            'action': 'acknowledge',
            'state': self.current_state.copy()
        })

        return True

    def reset(self) -> bool:
        """
        复位（全部清零，恢复绿灯正常）
        
        Returns:
            是否成功
        """
        if not self._enabled:
            return False

        self.current_state.update({
            'red': False,
            'yellow': False,
            'green': True,
            'buzzer': False,
            'pattern': AlarmLightPattern.STEADY,
            'level': None,
            'message': '',
            'since': None
        })
        
        logger.info("[模拟报警] 复位: 恢复绿灯正常状态")
        
        # 记录历史
        self.history.append({
            'timestamp': datetime.now().isoformat(),
            'action': 'reset',
            'state': self.current_state.copy()
        })

        return True

    def manual_control(self, **kwargs) -> dict[str, Any]:
        """
        手动控制
        
        Args:
            **kwargs: 控制参数 (red, yellow, green, buzzer, duration)
            
        Returns:
            控制结果
        """
        if not self._enabled:
            return {'success': False, 'message': '报警输出未启用'}

        # 更新状态
        if 'red' in kwargs:
            self.current_state['red'] = kwargs['red']
        if 'yellow' in kwargs:
            self.current_state['yellow'] = kwargs['yellow']
        if 'green' in kwargs:
            self.current_state['green'] = kwargs['green']
        if 'buzzer' in kwargs:
            self.current_state['buzzer'] = kwargs['buzzer']

        logger.info(f"[模拟报警] 手动控制: {kwargs}")
        
        # 记录历史
        self.history.append({
            'timestamp': datetime.now().isoformat(),
            'action': 'manual_control',
            'params': kwargs,
            'state': self.current_state.copy()
        })

        return {
            'success': True,
            'state': self.current_state.copy(),
            'message': '手动控制指令已执行'
        }

    def get_status(self) -> dict[str, Any]:
        """
        获取当前状态
        
        Returns:
            状态字典
        """
        return {
            'enabled': self._enabled,
            'mode': 'simulated',
            'state': self.current_state.copy(),
            'history_count': len(self.history)
        }
