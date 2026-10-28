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
from typing import Any
from datetime import datetime

logger = logging.getLogger(__name__)

#: Modbus 连接/读写超时（秒）。**必须短** —— 闪烁线程每 250ms 写一次 DO，
#: 网关断电时默认超时（3~20s）会让 I/O 通道长期被占满（round 286）。
DEFAULT_MODBUS_TIMEOUT_S = 1.0

#: 连续多少次 DO 写失败后打开熔断
CIRCUIT_FAIL_THRESHOLD = 3

#: 熔断持续多久（秒）—— 期间不碰 socket、不重连
CIRCUIT_OPEN_SECONDS = 10.0


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

    def __init__(self, config: dict[str, Any] | None = None):
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
            'since': None,
            'acknowledged': False   # 操作员已确认（消音）标记，用于放行同级新报警
        }

        # Modbus客户端（延迟创建）
        self._modbus_client = None
        #: 状态锁 —— 只保护 `current_state` 这类内存字段，**绝不跨网络 I/O 持有**
        self._lock = threading.Lock()
        #: DO 写串行锁（round 286）。网络 I/O 只在这把锁里做，与状态锁分开：
        #: 网关断线时写操作会阻塞数秒，若还持着状态锁，`get_status()`（API）
        #: 和 `trigger_alarm()`（报警主链路）会一起被堵死。
        self._io_lock = threading.Lock()
        #: 连续 DO 写失败次数（成功即清零）
        self._fail_streak = 0
        #: 熔断到期时刻（**单调时钟**；None 表示未熔断）
        self._circuit_open_until: float | None = None

        # 闪烁线程（灯）
        self._flash_thread: threading.Thread | None = None
        self._flash_running = False
        self._flash_state = False  # 闪烁当前帧

        # 蜂鸣器脉冲线程
        self._buzzer_thread: threading.Thread | None = None
        self._buzzer_running = False

        logger.info(f"报警输出初始化: {'模拟模式' if self.simulation else '硬件模式'}")

    def _get_modbus_client(self):
        """获取或创建Modbus客户端（硬件模式）"""
        if self._modbus_client is None:
            modbus_cfg = self.config.get('modbus', {})
            host = modbus_cfg.get('host', '192.168.1.100')
            port = modbus_cfg.get('port', 502)
            timeout = modbus_cfg.get('timeout', DEFAULT_MODBUS_TIMEOUT_S)

            try:
                from pymodbus.client import ModbusTcpClient
                try:
                    # round 286：**必须显式给短超时**。默认超时下，
                    # 网关断电/网线拔掉时一次 connect 能卡 3~20 秒，
                    # 而闪烁线程每 250ms 就要写一次 DO —— 见模块顶部说明。
                    self._modbus_client = ModbusTcpClient(host, port=port,
                                                          timeout=timeout)
                except TypeError:
                    # 老版本 pymodbus 不接受 timeout 关键字
                    self._modbus_client = ModbusTcpClient(host, port=port)
                if not self._modbus_client.connect():
                    logger.error(f"报警输出Modbus连接失败: {host}:{port}")
                    # 连不上的 client 也要关掉，否则每次重试都漏一个 socket
                    try:
                        self._modbus_client.close()
                    except Exception as e:      # noqa: BLE001
                        logger.debug("关闭失败的 Modbus 客户端时异常（忽略）: %s", e)
                    self._modbus_client = None
                    return None
            except ImportError:
                logger.error("pymodbus未安装，报警输出硬件模式不可用")
                return None

        return self._modbus_client

    # ==================== DO 写失败熔断（round 286） ====================

    def _circuit_is_open(self) -> bool:
        """熔断是否处于打开状态（期间**不碰 socket**）。"""
        until = self._circuit_open_until
        if until is None:
            return False
        if time.monotonic() >= until:
            # 半开：允许下一次尝试（成功则关闭，失败则重新打开）
            self._circuit_open_until = None
            logger.info("报警输出 DO 通道熔断到期，恢复尝试（此前连续失败 %d 次）",
                        self._fail_streak)
            return False
        return True

    def _record_do_failure(self, detail: str) -> None:
        """记录一次 DO 写失败；连续失败达到阈值即打开熔断。"""
        self._fail_streak += 1
        if self._fail_streak >= CIRCUIT_FAIL_THRESHOLD and self._circuit_open_until is None:
            self._circuit_open_until = time.monotonic() + CIRCUIT_OPEN_SECONDS
            logger.error(
                "报警输出 DO 通道连续失败 %d 次（%s）—— 熔断 %.0f 秒，"
                "期间不再尝试连接网关；现场声光可能完全不动作，请检查 DO 通道",
                self._fail_streak, detail, CIRCUIT_OPEN_SECONDS)

    def _record_do_success(self) -> None:
        """DO 写成功：清零失败计数并关闭熔断。"""
        if self._fail_streak or self._circuit_open_until is not None:
            logger.warning("报警输出 DO 通道恢复（此前连续失败 %d 次）",
                           self._fail_streak)
        self._fail_streak = 0
        self._circuit_open_until = None

    def _write_do(self, address: int, value: bool):
        """写DO线圈（硬件/模拟双模式）"""
        if self.simulation:
            logger.debug(f"[模拟] DO{address} -> {'ON' if value else 'OFF'}")
            return True

        # 熔断打开时直接失败返回：不重连、不写 socket。
        # 这是现场最常见的情形（网关断电），而调用方（闪烁线程每 250ms 一次）
        # 如果每次都去 connect，会长时间占住 I/O 通道、把整个报警链路拖死。
        if self._circuit_is_open():
            logger.debug("报警输出 DO 通道熔断中，跳过写 DO%d", address)
            return False

        try:
            client = self._get_modbus_client()
            if client is None:
                self._record_do_failure('客户端不可用（连接失败）')
                return False
            slave_id = self.config.get('modbus', {}).get('slave_id', 1)
            result = client.write_coil(address, value, slave=slave_id)
            if result.isError():
                self._record_do_failure(f'DO{address} 写入返回错误: {result}')
                return False
            self._record_do_success()
            return True
        except Exception as e:
            self._record_do_failure(f'DO{address} 写入异常: {e}')
            logger.error(f"写DO{address}失败: {e}")
            return False

    def _write_all_do(self, red: bool, yellow: bool, green: bool, buzzer: bool) -> bool:
        """批量写DO线圈。

        Returns:
            bool: **四个通道是否全部写成功**。任一失败即 False。

        为什么要返回 bool：`reset()` 之前无论写没写成功都返回 True 并打印
        「报警输出已复位（绿灯正常）」，于是 Modbus DO 写失败时——
        **现场红灯/蜂鸣器还在响，界面却说已恢复正常**。声光报警是这个系统的
        安全出口，这种假成功比没有功能更危险。

        round 286：这里**不再持 `self._lock`**（状态锁），改用只串行化 I/O 的
        `_io_lock`。原来在状态锁内做 4 次网络写，网关一断线就把
        `trigger_alarm()` / `get_status()` / `acknowledge()` 全部堵在锁上 ——
        报警链路和 API 一起卡死。
        """
        with self._io_lock:
            results = [
                self._write_do(self.do_red, red),
                self._write_do(self.do_yellow, yellow),
                self._write_do(self.do_green, green),
                self._write_do(self.do_buzzer, buzzer),
            ]
        return all(results)

    # ==================== 报警输出 ====================

    def trigger_alarm(self, level: str, message: str, device_id: str = ''):
        """
        触发报警输出

        报警级别与输出映射：
        - critical: 红灯快闪 + 蜂鸣器常响
        - warning:  黄灯慢闪 + 蜂鸣器脉冲
        - info:     绿灯常亮（仅记录，无声光）

        安全设计：同级别报警不重复触发灯和蜂鸣器，避免抢灯。
        例外：操作员已确认（消音）后，同级新报警必须重新触发声光——
        否则一次消音会吞掉后续同级报警（漏报）。确认标记见 acknowledge()。

        Args:
            level: 报警级别 (critical/warning/info)
            message: 报警消息
            device_id: 设备ID

        Returns:
            str | bool: 正常路径返回本次输出的日志文案（供调用方记录/测试断言）；
            **DO 写入失败时返回 False**。

            为什么成功返回文案、失败返回 False：`AlarmManager._call_output_method`
            的契约是「显式 False / {'success': False} 才算失败」，返回 None 或任意
            字符串都当成功。此前本方法无论写没写成功都返回文案，于是
            **报警响了但声光没动，系统却认为输出成功**，`errors` 计数也不会涨 ——
            现场没有任何信号。失败必须显式返回 False 才能被计数与上报。
        """
        if not self.enabled:
            return

        # 同级别报警不重复触发灯控（避免抢灯）；但已确认(消音)后必须放行
        with self._lock:
            if (self.current_state.get('level') == level
                    and self.current_state.get('pattern') != AlarmLightPattern.STEADY
                    and not self.current_state.get('acknowledged', False)):
                # 只更新消息，不动灯
                self.current_state['message'] = message
                self.current_state['device_id'] = device_id
                return

            # 手动控制模式下不自动接管灯
            if self.current_state.get('level') == 'manual':
                return

            self.current_state.update({
                'level': level,
                'message': message,
                'device_id': device_id,
                'since': datetime.now().isoformat(),
                'acknowledged': False
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
            ok = self._write_all_do(red=True, yellow=False, green=False, buzzer=True)
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
            ok = self._write_all_do(red=False, yellow=True, green=False, buzzer=False)
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
            ok = self._write_all_do(red=False, yellow=False, green=True, buzzer=False)

            log_msg = f"【信息】🟢 {message} (设备: {device_id})"
            logger.info(log_msg)

        if not ok:
            logger.error(
                "报警输出失败[level=%s]: DO 写入未成功 —— 现场声光可能没有动作"
                "（红灯/黄灯/蜂鸣器），请检查 DO 通道与通信链路",
                level,
            )
            return False
        return log_msg

    def acknowledge(self):
        """
        确认/消音 — 关蜂鸣器（停脉冲线程 + **写 DO**），灯保持当前状态
        操作员到场后按"确认"按钮

        注意：确认只消音，不清除报警级别；但会打上 acknowledged 标记，
        使后续同级新报警能重新触发声光（避免一次消音吞掉后续漏报）。

        Returns:
            bool: 蜂鸣器 DO 是否**真的**写成功。False 表示软件状态已置位，
            但现场很可能仍在响 —— 调用方必须据此告知操作员。
        """
        self._stop_buzzer()
        with self._lock:
            self.current_state['buzzer'] = False
            self.current_state['acknowledged'] = True
            # 灯保持当前状态：确认只消音，不清除报警级别与灯色
            red = bool(self.current_state.get('red'))
            yellow = bool(self.current_state.get('yellow'))
            green = bool(self.current_state.get('green'))

        # round 302：**必须真的写 DO**。
        # critical 级的蜂鸣器是「DO 常 ON、不起脉冲线程」（见 trigger_alarm 的
        # critical 分支与它的注释），所以 `_stop_buzzer()` 只是 join 了一个 None
        # 线程、一个字节都没写；而这里原来无条件 `return True`。
        # 结果：界面回「已消音」、`get_status()['state']['buzzer']` 也是 False，
        # **现场蜂鸣器却一直常响到下一次 reset()**。操作员会以为界面卡了，
        # 或者去拔电源 —— 拔电源会让所有声光输出一起失效。
        # warning 级反而正常（脉冲线程退出时会写 False），所以这个缺陷只在
        # 最该消音的那一级出现。
        # 判据：`_write_all_do` 的返回值不是装饰品（round 281 修的正是这条契约）。
        ok = self._write_all_do(red=red, yellow=yellow, green=green, buzzer=False)
        if ok:
            logger.info("报警已确认（消音），指示灯保持；同级新报警仍会触发")
        else:
            logger.error(
                "报警确认：蜂鸣器 DO 写入失败 —— 现场很可能仍在响，"
                "请检查 DO 通道与通信链路，必要时到现场处置")
        return ok

    def reset(self):
        """
        复位 — 全部清零，恢复绿灯正常状态
        报警解除后调用

        Returns:
            bool: DO 是否**真的**全部写成功。False 表示软件状态已清零，
            但硬件可能没收到（红灯/蜂鸣器仍在），调用方必须据此告知操作员。
        """
        self._stop_flash()
        self._stop_buzzer()

        self.current_state.update({
            'red': False, 'yellow': False, 'green': True,
            'buzzer': False,
            'pattern': AlarmLightPattern.STEADY,
            'level': None, 'message': '', 'since': None,
            'acknowledged': False
        })

        ok = self._write_all_do(red=False, yellow=False, green=True, buzzer=False)
        if ok:
            logger.info("报警输出已复位（绿灯正常）")
        else:
            logger.error(
                "报警输出复位：DO 写入失败 —— 硬件可能仍处于报警态"
                "（红灯亮/蜂鸣器响），请检查 DO 通道与通信链路"
            )
        return ok

    # ==================== 手动控制 ====================

    def manual_control(self, red: bool | None = None, yellow: bool | None = None,
                       green: bool | None = None, buzzer: bool | None = None,
                       duration: int = 0) -> dict[str, Any]:
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
            dict: {'success': bool, 'output_ok': bool, 'state': {...}}
                `output_ok` 表示 DO 是否真的写成功（round 302：原来恒回 True，
                DO 断链时界面照样提示「指令已发送」）。
        """
        # 手动操作接管全部DO，停掉自动线程
        self._stop_flash()
        self._stop_buzzer()

        r = red if red is not None else self.current_state['red']
        y = yellow if yellow is not None else self.current_state['yellow']
        g = green if green is not None else self.current_state['green']
        b = buzzer if buzzer is not None else self.current_state['buzzer']

        # round 302：返回值不能丢 —— 这是调试/巡检接口，最需要「到底写没写进去」
        ok = self._write_all_do(r, y, g, b)

        self.current_state.update({
            'red': r, 'yellow': y, 'green': g, 'buzzer': b,
            'pattern': AlarmLightPattern.STEADY,
            'level': 'manual',
            'message': '手动控制',
            'since': datetime.now().isoformat(),
            'acknowledged': False
        })

        if duration > 0:
            timer = threading.Timer(duration, self.reset)
            timer.daemon = True
            timer.start()

        if not ok:
            logger.error("手动输出：DO 写入失败 —— 现场声光可能没有变化，"
                         "请检查 DO 通道与通信链路")
        return {'success': ok, 'output_ok': ok, 'state': self.current_state.copy()}

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
            # round 286：I/O 用 `_io_lock`，状态锁不跨网络调用持有
            with self._io_lock:
                self._write_do(self.do_buzzer, True)
            time.sleep(on_time)
            if not self._buzzer_running:
                break
            with self._io_lock:
                self._write_do(self.do_buzzer, False)
            time.sleep(off_time)
        # 退出时关蜂鸣器
        with self._io_lock:
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
            # round 286：I/O 用 `_io_lock`，状态锁不跨网络调用持有
            with self._io_lock:
                self._write_do(do_addr, self._flash_state)
            time.sleep(interval)

        # 退出时灭灯（加锁）
        with self._io_lock:
            self._write_do(do_addr, False)

    # ==================== 接口兼容 ====================

    def activate_alarm(self, level: str, message: str = '') -> bool:
        """
        激活报警（IAlarmOutput接口方法）

        Args:
            level: 报警级别 ('critical', 'warning', 'info')
            message: 报警消息

        Returns:
            是否成功
        """
        result = self.trigger_alarm(level=level, message=message)
        return result is not None

    # ==================== 状态查询 ====================

    def get_status(self) -> dict[str, Any]:
        """获取当前输出状态

        round 286：加上 `do_channel` —— 运维必须能看出"声光通道是不是坏了"。
        此前网关断线时，状态接口照样显示"红灯亮/蜂鸣器响"（那是**意图**，
        不是**事实**），没有任何地方能看出 DO 写其实一直在失败。
        """
        with self._lock:
            circuit_open = self._circuit_open_until is not None
            remaining = 0.0
            if circuit_open:
                remaining = max(0.0, self._circuit_open_until - time.monotonic())
            return {
                'enabled': self.enabled,
                'simulation': self.simulation,
                'state': self.current_state.copy(),
                'do_mapping': {
                    'red_light': self.do_red,
                    'yellow_light': self.do_yellow,
                    'green_light': self.do_green,
                    'buzzer': self.do_buzzer
                },
                #: DO 通道健康度（round 286）
                'do_channel': {
                    'fail_streak': self._fail_streak,
                    'circuit_open': circuit_open,
                    'circuit_remaining_seconds': round(remaining, 1),
                    'fail_threshold': CIRCUIT_FAIL_THRESHOLD,
                },
            }

    def disconnect(self):
        """断开连接（复位输出后断开Modbus）"""
        self.reset()
        if self._modbus_client:
            self._modbus_client.close()
            self._modbus_client = None
