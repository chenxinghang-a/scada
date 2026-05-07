"""
报警输出模块 — 工业声光报警器驱动
通过Modbus DO线圈控制报警灯塔（红/黄/绿灯）和蜂鸣器

硬件接线示例（典型工业灯塔）：
  PLC DO0 -> 红色报警灯（常亮/闪烁）
  PLC DO1 -> 黄色警告灯（常亮/闪烁）
  PLC DO2 -> 绿色正常灯（常亮）
  PLC DO3 -> 蜂鸣器（脉冲/常响）

支持：
  - Modbus TCP/RTU写DO线圈控制真实硬件
  - 模拟模式下输出到日志（无硬件也能演示）
  - 报警级别自动映射灯色和蜂鸣
  - 闪烁模式（严重报警快闪，警告慢闪）
  - 消音功能（关蜂鸣器，灯保持）
  - 复位功能（全部清零）
"""

import time
import logging
import threading
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class AlarmLightPattern:
    """报警灯闪烁模式"""
    OFF = 'off'           # 灭
    STEADY = 'steady'     # 常亮
    SLOW_FLASH = 'slow'   # 慢闪（1Hz，警告）
    FAST_FLASH = 'fast'   # 快闪（2Hz，严重）


class AlarmOutput:
    """
    工业报警输出驱动
    
    通过Modbus DO线圈控制报警灯塔和蜂鸣器。
    模拟模式下输出到日志，可用于无硬件的开发/演示环境。
    
    配置示例（config dict）:
    {
        'enabled': True,
        'simulation': True,
        'modbus': {
            'host': '192.168.1.100',
            'port': 502,
            'slave_id': 1,
        },
        'do_mapping': {
            'red_light': 0,      # DO0 = 红灯
            'yellow_light': 1,   # DO1 = 黄灯
            'green_light': 2,    # DO2 = 绿灯
            'buzzer': 3,         # DO3 = 蜂鸣器
        },
        'buzzer_mode': 'pulse'   # pulse=脉冲 / steady=常响
    }
    """
    
    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.enabled = self.config.get('enabled', True)
        self.simulation = self.config.get('simulation', True)
        
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
        self._flash_thread: Optional[threading.Thread] = None
        self._flash_running = False
        self._flash_state = False  # 闪烁当前帧
        
        # 蜂鸣器脉冲线程
        self._buzzer_thread: Optional[threading.Thread] = None
        self._buzzer_running = False
        
        logger.info(f"报警输出初始化: {'模拟模式' if self.simulation else '硬件模式'}")
    
    def _get_modbus_client(self):
        """获取或创建Modbus客户端（硬件模式）"""
        if self._modbus_client is None:
            modbus_cfg = self.config.get('modbus', {})
            host = modbus_cfg.get('host', '192.168.1.100')
            port = modbus_cfg.get('port', 502)
            slave_id = modbus_cfg.get('slave_id', 1)
            
            try:
                from pymodbus.client import ModbusTcpClient
                self._modbus_client = ModbusTcpClient(host, port=port)
                if not self._modbus_client.connect():
                    logger.error(f"报警输出Modbus连接失败: {host}:{port}")
                    self._modbus_client = None
                    return None
            except ImportError:
                logger.error("pymodbus未安装，报警输出硬件模式不可用")
                return None
        
        return self._modbus_client
    
    def _write_do(self, address: int, value: bool):
        """写DO线圈（硬件/模拟双模式）"""
        if self.simulation:
            logger.debug(f"[模拟] DO{address} -> {'ON' if value else 'OFF'}")
            return True
        
        try:
            client = self._get_modbus_client()
            if client is None:
                return False
            slave_id = self.config.get('modbus', {}).get('slave_id', 1)
            result = client.write_coil(address, value, slave=slave_id)
            return not result.isError()
        except Exception as e:
            logger.error(f"写DO{address}失败: {e}")
            return False
    
    def _write_all_do(self, red: bool, yellow: bool, green: bool, buzzer: bool):
        """批量写DO线圈"""
        with self._lock:
            self._write_do(self.do_red, red)
            self._write_do(self.do_yellow, yellow)
            self._write_do(self.do_green, green)
            self._write_do(self.do_buzzer, buzzer)
    
    # ==================== 报警输出 ====================
    
    def trigger_alarm(self, level: str, message: str, device_id: str = ''):
        """
        触发报警输出
        
        报警级别与输出映射：
        - critical: 红灯快闪 + 蜂鸣器常响
        - warning:  黄灯慢闪 + 蜂鸣器脉冲
        - info:     绿灯常亮（仅记录，无声光）
        
        Args:
            level: 报警级别 (critical/warning/info)
            message: 报警消息
            device_id: 设备ID
        """
        if not self.enabled:
            return
        
        self.current_state.update({
            'level': level,
            'message': message,
            'device_id': device_id,
            'since': datetime.now().isoformat()
        })
        
        # 先停掉旧的闪烁线程和蜂鸣器
        self._stop_flash()
        self._stop_buzzer()
        
        if level == 'critical':
            # 严重报警：红灯快闪 + 蜂鸣器常响
            self.current_state.update({
                'red': True, 'yellow': False, 'green': False,
                'buzzer': True,
                'pattern': AlarmLightPattern.FAST_FLASH
            })
            # 先写入目标灯ON + 蜂鸣器ON（避免闪烁线程启动前的黑灯间隙）
            self._write_all_do(red=True, yellow=False, green=False, buzzer=True)
            self._start_flash('red', 0.25)  # 250ms间隔=快闪
            # critical级别：蜂鸣器常响（不启脉冲线程，保持DO=True）
            
            log_msg = f"【严重报警】🔴 {message} (设备: {device_id})"
            logger.warning(log_msg)
            
        elif level == 'warning':
            # 警告：黄灯慢闪 + 蜂鸣器脉冲（响0.3s停0.7s）
            self.current_state.update({
                'red': False, 'yellow': True, 'green': False,
                'buzzer': True,
                'pattern': AlarmLightPattern.SLOW_FLASH
            })
            # 先写入目标灯ON（避免闪烁线程启动前的黑灯间隙）
            self._write_all_do(red=False, yellow=True, green=False, buzzer=False)
            self._start_flash('yellow', 0.5)  # 500ms间隔=慢闪
            # warning级别：蜂鸣器脉冲响（响0.3s停0.7s）
            self._start_buzzer_pulse(on_time=0.3, off_time=0.7)
            
            log_msg = f"【警告】🟡 {message} (设备: {device_id})"
            logger.warning(log_msg)
            
        else:
            # 信息级：只记日志，绿灯保持
            self.current_state.update({
                'red': False, 'yellow': False, 'green': True,
                'buzzer': False,
                'pattern': AlarmLightPattern.STEADY
            })
            self._write_all_do(red=False, yellow=False, green=True, buzzer=False)
            
            log_msg = f"【信息】🟢 {message} (设备: {device_id})"
            logger.info(log_msg)
        
        return log_msg
    
    def acknowledge(self):
        """
        确认/消音 — 关蜂鸣器（停脉冲线程+写DO），灯保持闪烁
        操作员到场后按"确认"按钮
        """
        self._stop_buzzer()
        self.current_state['buzzer'] = False
        logger.info("报警已确认（消音），指示灯保持")
        return True
    
    def reset(self):
        """
        复位 — 全部清零，恢复绿灯正常状态
        报警解除后调用
        """
        self._stop_flash()
        self._stop_buzzer()
        
        self.current_state.update({
            'red': False, 'yellow': False, 'green': True,
            'buzzer': False,
            'pattern': AlarmLightPattern.STEADY,
            'level': None, 'message': '', 'since': None
        })
        
        self._write_all_do(red=False, yellow=False, green=True, buzzer=False)
        logger.info("报警输出已复位（绿灯正常）")
        return True
    
    # ==================== 手动控制 ====================
    
    def manual_control(self, red: bool = None, yellow: bool = None,
                       green: bool = None, buzzer: bool = None,
                       duration: int = 0) -> dict:
        """
        手动控制灯和蜂鸣器（用于调试/测试）
        
        逻辑：
        - 手动操作会接管全部DO（停掉自动闪烁/脉冲线程）
        - level 标记为 'manual'，区分报警自动模式
        - duration > 0 时定时复位到正常状态
        
        Args:
            red: 红灯（None=不变）
            yellow: 黄灯（None=不变）
            green: 绿灯（None=不变）
            buzzer: 蜂鸣器（None=不变）
            duration: 持续时间（秒，0=持续）
        
        Returns:
            dict: {'success': True, 'state': {...}}
        """
        # 手动操作接管全部DO，停掉自动线程
        self._stop_flash()
        self._stop_buzzer()
        
        r = red if red is not None else self.current_state['red']
        y = yellow if yellow is not None else self.current_state['yellow']
        g = green if green is not None else self.current_state['green']
        b = buzzer if buzzer is not None else self.current_state['buzzer']
        
        self._write_all_do(r, y, g, b)
        
        self.current_state.update({
            'red': r, 'yellow': y, 'green': g, 'buzzer': b,
            'pattern': AlarmLightPattern.STEADY,
            'level': 'manual',
            'message': '手动控制',
            'since': datetime.now().isoformat()
        })
        
        if duration > 0:
            timer = threading.Timer(duration, self.reset)
            timer.daemon = True
            timer.start()
        
        return {'success': True, 'state': self.current_state.copy()}
    
    # ==================== 蜂鸣器脉冲控制 ====================
    
    def _start_buzzer_pulse(self, on_time: float = 0.5, off_time: float = 0.5):
        """启动蜂鸣器脉冲线程（间歇响）"""
        self._buzzer_running = True
        self._buzzer_thread = threading.Thread(
            target=self._buzzer_pulse_loop, args=(on_time, off_time), daemon=True
        )
        self._buzzer_thread.start()
    
    def _stop_buzzer(self):
        """停止蜂鸣器脉冲"""
        self._buzzer_running = False
        if self._buzzer_thread and self._buzzer_thread.is_alive():
            self._buzzer_thread.join(timeout=1)
    
    def _buzzer_pulse_loop(self, on_time: float, off_time: float):
        """蜂鸣器脉冲循环（响-停-响-停...）"""
        while self._buzzer_running:
            with self._lock:
                self._write_do(self.do_buzzer, True)
            time.sleep(on_time)
            if not self._buzzer_running:
                break
            with self._lock:
                self._write_do(self.do_buzzer, False)
            time.sleep(off_time)
        # 退出时关蜂鸣器
        with self._lock:
            self._write_do(self.do_buzzer, False)
    
    # ==================== 闪烁控制 ====================
    
    def _start_flash(self, color: str, interval: float):
        """启动闪烁线程"""
        self._flash_running = True
        self._flash_state = False  # 重置状态，第一次toggle后=ON
        self._flash_thread = threading.Thread(
            target=self._flash_loop, args=(color, interval), daemon=True
        )
        self._flash_thread.start()
    
    def _stop_flash(self):
        """停止闪烁"""
        self._flash_running = False
        if self._flash_thread and self._flash_thread.is_alive():
            self._flash_thread.join(timeout=1)
    
    def _flash_loop(self, color: str, interval: float):
        """闪烁循环（已加锁，避免和手动控制竞态）"""
        do_addr = {
            'red': self.do_red,
            'yellow': self.do_yellow,
            'green': self.do_green
        }.get(color)
        
        if do_addr is None:
            return
        
        while self._flash_running:
            self._flash_state = not self._flash_state
            with self._lock:
                self._write_do(do_addr, self._flash_state)
            time.sleep(interval)
        
        # 退出时灭灯（加锁）
        with self._lock:
            self._write_do(do_addr, False)
    
    # ==================== 状态查询 ====================
    
    def get_status(self) -> Dict[str, Any]:
        """获取当前输出状态"""
        return {
            'enabled': self.enabled,
            'simulation': self.simulation,
            'state': self.current_state.copy(),
            'do_mapping': {
                'red_light': self.do_red,
                'yellow_light': self.do_yellow,
                'green_light': self.do_green,
                'buzzer': self.do_buzzer
            }
        }
    
    def disconnect(self):
        """断开连接（复位输出后断开Modbus）"""
        self.reset()
        if self._modbus_client:
            self._modbus_client.close()
            self._modbus_client = None
