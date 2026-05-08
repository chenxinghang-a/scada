"""
真实报警输出
完全独立的真实硬件实现，连接实际的声光报警器
"""

import time
import logging
import threading
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


class RealAlarmOutput(IAlarmOutput):
    """
    真实报警输出
    
    特点：
    - 完全独立，连接真实的声光报警器硬件
    - 通过Modbus DO线圈控制灯塔和蜂鸣器
    - 需要实际的硬件设备才能运行
    - 适用于生产环境
    """

    def __init__(self, config: dict[str, Any] | None = None):
        """
        初始化真实报警输出
        
        Args:
            config: 配置字典，必须包含modbus连接信息
        """
        self.config = config or {}
        self._enabled = self.config.get('enabled', True)
        
        # Modbus配置
        modbus_config = self.config.get('modbus', {})
        self.modbus_host = modbus_config.get('host', '192.168.1.100')
        self.modbus_port = modbus_config.get('port', 502)
        self.modbus_slave_id = modbus_config.get('slave_id', 1)
        
        # DO线圈地址映射
        do_map = self.config.get('do_mapping', {})
        self.do_red = do_map.get('red_light', 0)
        self.do_yellow = do_map.get('yellow_light', 1)
        self.do_green = do_map.get('green_light', 2)
        self.do_buzzer = do_map.get('buzzer', 3)
        
        # 蜂鸣器模式
        self.buzzer_mode = self.config.get('buzzer_mode', 'pulse')
        
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
        
        # Modbus客户端（延迟创建）
        self._modbus_client = None
        self._lock = threading.Lock()
        
        # 闪烁线程（灯）
        self._flash_thread: threading.Thread | None = None
        self._flash_running = False
        self._flash_state = False
        
        # 蜂鸣器脉冲线程
        self._buzzer_thread: threading.Thread | None = None
        self._buzzer_running = False
        
        # 报警历史
        self.history: list[dict[str, Any]] = []
        
        logger.info(f"[真实] 报警输出初始化完成: {self.modbus_host}:{self.modbus_port}")

    @property
    def enabled(self) -> bool:
        """是否启用"""
        return self._enabled

    def _get_modbus_client(self):
        """获取Modbus客户端（懒创建）"""
        if self._modbus_client is None:
            try:
                from pymodbus.client import ModbusTcpClient
                self._modbus_client = ModbusTcpClient(
                    host=self.modbus_host,
                    port=self.modbus_port
                )
                logger.info(f"[真实] Modbus客户端创建成功: {self.modbus_host}:{self.modbus_port}")
            except Exception as e:
                logger.error(f"[真实] Modbus客户端创建失败: {e}")
                return None
        return self._modbus_client

    def _write_do(self, address: int, value: bool) -> bool:
        """
        写入DO线圈
        
        Args:
            address: 线圈地址
            value: 值
            
        Returns:
            是否成功
        """
        client = self._get_modbus_client()
        if not client:
            return False
            
        try:
            with self._lock:
                result = client.write_coil(address, value, slave=self.modbus_slave_id)
                return not result.isError()
        except Exception as e:
            logger.error(f"[真实] 写入DO失败: {e}")
            return False

    def _set_lights(self, red: bool, yellow: bool, green: bool):
        """设置灯状态"""
        self._write_do(self.do_red, red)
        self._write_do(self.do_yellow, yellow)
        self._write_do(self.do_green, green)

    def _set_buzzer(self, on: bool):
        """设置蜂鸣器"""
        self._write_do(self.do_buzzer, on)

    def _start_flash(self, pattern: str):
        """启动闪烁线程"""
        self._flash_running = True
        self._flash_state = False
        
        def flash_loop():
            while self._flash_running:
                if pattern == AlarmLightPattern.FAST_FLASH:
                    interval = 0.25  # 2Hz
                elif pattern == AlarmLightPattern.SLOW_FLASH:
                    interval = 0.5   # 1Hz
                else:
                    interval = 1.0
                    
                self._flash_state = not self._flash_state
                self._set_lights(
                    red=self.current_state['red'] and self._flash_state,
                    yellow=self.current_state['yellow'] and self._flash_state,
                    green=self.current_state['green']
                )
                time.sleep(interval)
                
        self._flash_thread = threading.Thread(target=flash_loop, daemon=True)
        self._flash_thread.start()

    def _stop_flash(self):
        """停止闪烁"""
        self._flash_running = False
        if self._flash_thread:
            self._flash_thread.join(timeout=1)

    def _start_buzzer_pulse(self):
        """启动蜂鸣器脉冲"""
        self._buzzer_running = True
        
        def pulse_loop():
            while self._buzzer_running:
                self._set_buzzer(True)
                time.sleep(0.5)
                self._set_buzzer(False)
                time.sleep(0.5)
                
        self._buzzer_thread = threading.Thread(target=pulse_loop, daemon=True)
        self._buzzer_thread.start()

    def _stop_buzzer_pulse(self):
        """停止蜂鸣器脉冲"""
        self._buzzer_running = False
        if self._buzzer_thread:
            self._buzzer_thread.join(timeout=1)

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

        # 停止之前的闪烁和脉冲
        self._stop_flash()
        self._stop_buzzer_pulse()

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
            self._start_flash(AlarmLightPattern.FAST_FLASH)
            self._start_buzzer_pulse()
            logger.warning(f"[真实报警] 严重报警: 红灯快闪 + 蜂鸣器 | {message}")
            
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
            self._start_flash(AlarmLightPattern.SLOW_FLASH)
            self._start_buzzer_pulse()
            logger.warning(f"[真实报警] 警告: 黄灯慢闪 + 蜂鸣器 | {message}")
            
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
            self._set_lights(False, True, False)
            logger.info(f"[真实报警] 信息: 黄灯常亮 | {message}")

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

        self._stop_buzzer_pulse()
        self._set_buzzer(False)
        self.current_state['buzzer'] = False
        
        logger.info("[真实报警] 消音: 蜂鸣器关闭，报警灯保持")
        
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

        # 停止闪烁和脉冲
        self._stop_flash()
        self._stop_buzzer_pulse()
        
        # 恢复绿灯正常
        self._set_lights(False, False, True)
        self._set_buzzer(False)
        
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
        
        logger.info("[真实报警] 复位: 恢复绿灯正常状态")
        
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

        # 停止之前的闪烁和脉冲
        self._stop_flash()
        self._stop_buzzer_pulse()

        # 更新状态
        if 'red' in kwargs:
            self.current_state['red'] = kwargs['red']
        if 'yellow' in kwargs:
            self.current_state['yellow'] = kwargs['yellow']
        if 'green' in kwargs:
            self.current_state['green'] = kwargs['green']
        if 'buzzer' in kwargs:
            self.current_state['buzzer'] = kwargs['buzzer']

        # 设置硬件
        self._set_lights(
            self.current_state['red'],
            self.current_state['yellow'],
            self.current_state['green']
        )
        self._set_buzzer(self.current_state['buzzer'])

        logger.info(f"[真实报警] 手动控制: {kwargs}")
        
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
            'mode': 'real',
            'modbus_host': self.modbus_host,
            'modbus_port': self.modbus_port,
            'state': self.current_state.copy(),
            'history_count': len(self.history)
        }
