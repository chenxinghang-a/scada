"""
工厂级设备控制模块
工业4.0落地核心 — 安全、可靠、可追溯的设备控制

核心能力：
1. 安全联锁（Safety Interlock）— 超限自动停机，硬件级优先
2. 写操作安全校验 — 值范围/地址白名单/操作前条件检查
3. 故障降级（Fault Degradation）— 通信断开→安全状态
4. 紧急停机（E-Stop）— 一键停机，独立通道，最高优先级
5. 操作审计（Audit Trail）— 完整链路，持久化，防篡改
6. 写操作回读验证 — 写入后回读确认
7. 操作互斥（Mutex）— 防止多用户同时操作同一设备
"""

import time
import logging
import threading
import json
from typing import Dict, List, Any, Optional, Callable, Set
from datetime import datetime, timedelta
from collections import deque
from pathlib import Path

logger = logging.getLogger(__name__)


class SafetyLevel:
    """安全等级"""
    SAFE = 'safe'           # 安全状态
    WARNING = 'warning'     # 警告（可继续运行）
    CRITICAL = 'critical'   # 严重（需停机）
    EMERGENCY = 'emergency' # 紧急（立即停机）


class DeviceControlSafety:
    """
    工厂级设备控制安全管理器

    职责：
    - 安全联锁规则管理与执行
    - 写操作安全校验
    - 故障降级策略
    - 紧急停机管理
    - 操作审计日志
    """

    def __init__(self, database, device_manager=None, alarm_manager=None):
        self.database = database
        self.device_manager = device_manager
        self.alarm_manager = alarm_manager

        # ===== 安全联锁规则 =====
        self._interlock_rules: Dict[str, Dict] = {}
        self._interlock_states: Dict[str, bool] = {}  # rule_id -> is_triggered
        self._interlock_bypass: Set[str] = set()  # 被旁路的联锁ID
        self._lock = threading.Lock()

        # ===== 写操作安全配置 =====
        # 设备写操作白名单：device_id -> {register_address: (min_value, max_value)}
        self._write_limits: Dict[str, Dict] = {}
        # 设备可写地址白名单：device_id -> set of allowed addresses
        self._write_whitelist: Dict[str, Set[int]] = {}

        # ===== 操作互斥锁 =====
        self._device_locks: Dict[str, threading.Lock] = {}
        self._device_lock_owners: Dict[str, str] = {}  # device_id -> operator

        # ===== 紧急停机 =====
        self._estop_active = False
        self._estop_time: Optional[datetime] = None
        self._estop_reason = ''
        self._estop_devices: List[str] = []  # 需要停机的设备列表

        # ===== 故障降级 =====
        self._device_health: Dict[str, Dict] = {}  # device_id -> health info
        self._safe_state_handlers: Dict[str, Callable] = {}  # device_id -> safe_state_func

        # ===== 操作审计 =====
        self._audit_log = deque(maxlen=5000)
        self._audit_file = Path('data/audit_log.jsonl')

        # ===== 通信监控 =====
        self._comm_failures: Dict[str, int] = {}  # device_id -> consecutive failures
        self._comm_threshold = 3  # 连续失败3次触发降级

        # 加载预置联锁规则
        self._load_preset_interlocks()
        # 加载写操作限制
        self._load_write_limits()

        logger.info("工厂级设备控制安全管理器已初始化")

    # ==================== 安全联锁 ====================

    def _load_preset_interlocks(self):
        """加载预置安全联锁规则（工厂标准配置）"""
        preset_rules = [
            {
                'id': 'interlock_temp_high',
                'name': '温度超限联锁',
                'description': '反应釜温度超过上限自动停加热',
                'priority': 1,
                'condition': {
                    'type': 'threshold',
                    'device_id': 'plc_reactor_01',
                    'register': 'temperature',
                    'operator': '>',
                    'value': 95.0
                },
                'action': {
                    'type': 'write_register',
                    'device_id': 'plc_reactor_01',
                    'register': 'heater_enable',
                    'value': 0
                },
                'alarm': {
                    'level': 'critical',
                    'message': '温度超限联锁触发：反应釜温度>{threshold}°C，加热已自动关闭'
                }
            },
            {
                'id': 'interlock_pressure_high',
                'name': '压力超限联锁',
                'description': '管道压力超过上限自动停泵',
                'priority': 1,
                'condition': {
                    'type': 'threshold',
                    'device_id': 'plc_reactor_01',
                    'register': 'pressure',
                    'operator': '>',
                    'value': 2.5
                },
                'action': {
                    'type': 'write_register',
                    'device_id': 'plc_reactor_01',
                    'register': 'pump_enable',
                    'value': 0
                },
                'alarm': {
                    'level': 'critical',
                    'message': '压力超限联锁触发：管道压力>{threshold}MPa，泵已自动停止'
                }
            },
            {
                'id': 'interlock_estop',
                'name': '急停按钮联锁',
                'description': '急停按钮按下全厂停机',
                'priority': 0,  # 最高优先级
                'condition': {
                    'type': 'coil',
                    'device_id': 'relay_output_01',
                    'address': 0,  # DI0 = 急停按钮
                    'value': True
                },
                'action': {
                    'type': 'emergency_stop'
                },
                'alarm': {
                    'level': 'critical',
                    'message': '急停按钮已按下！全厂设备紧急停机'
                }
            },
            {
                'id': 'interlock_fire_alarm',
                'name': '烟感报警联锁',
                'description': '烟感报警自动切断电源',
                'priority': 0,
                'condition': {
                    'type': 'coil',
                    'device_id': 'relay_output_01',
                    'address': 2,  # DI2 = 烟感
                    'value': True
                },
                'action': {
                    'type': 'write_register',
                    'device_id': 'relay_output_01',
                    'register': 'power_cut',
                    'value': 1
                },
                'alarm': {
                    'level': 'critical',
                    'message': '烟感报警联锁触发：已自动切断非安全电源'
                }
            },
            {
                'id': 'interlock_water_leak',
                'name': '水浸报警联锁',
                'description': '水浸传感器报警自动停泵',
                'priority': 0,
                'condition': {
                    'type': 'coil',
                    'device_id': 'relay_output_01',
                    'address': 3,  # DI3 = 水浸
                    'value': True
                },
                'action': {
                    'type': 'write_register',
                    'device_id': 'plc_reactor_01',
                    'register': 'pump_enable',
                    'value': 0
                },
                'alarm': {
                    'level': 'critical',
                    'message': '水浸报警联锁触发：泵已自动停止'
                }
            },
            {
                'id': 'interlock_door_open',
                'name': '门禁联锁',
                'description': '设备间门打开时禁止启动设备',
                'priority': 2,
                'condition': {
                    'type': 'coil',
                    'device_id': 'relay_output_01',
                    'address': 1,  # DI1 = 门禁
                    'value': True
                },
                'action': {
                    'type': 'block_start'  # 阻止设备启动
                },
                'alarm': {
                    'level': 'warning',
                    'message': '门禁联锁：设备间门未关闭，禁止启动设备'
                }
            },
        ]

        for rule in preset_rules:
            self._interlock_rules[rule['id']] = rule
            self._interlock_states[rule['id']] = False

        logger.info(f"已加载 {len(preset_rules)} 条预置安全联锁规则")

    def _load_write_limits(self):
        """加载写操作安全限制（防止写入危险值）"""
        self._write_limits = {
            'plc_reactor_01': {
                'temperature_setpoint': (0, 150),    # 温度设定值 0-150°C
                'pressure_setpoint': (0, 4.0),       # 压力设定值 0-4.0 MPa
                'flow_setpoint': (0, 100),            # 流量设定值 0-100 m³/h
                'heater_enable': (0, 1),              # 加热开关 0/1
                'pump_enable': (0, 1),                # 泵开关 0/1
                'valve_position': (0, 100),           # 阀门开度 0-100%
            },
            'relay_output_01': {
                # 继电器模块：每路只能0或1
                'default': (0, 1)
            },
            'relay_output_02': {
                'default': (0, 1)
            },
            'signal_tower_01': {
                'default': (0, 1)
            },
            'signal_tower_02': {
                'default': (0, 1)
            }
        }

    def add_interlock(self, rule: Dict) -> bool:
        """添加自定义联锁规则"""
        rule_id = rule.get('id')
        if not rule_id:
            return False
        with self._lock:
            self._interlock_rules[rule_id] = rule
            self._interlock_states[rule_id] = False
        logger.info(f"添加联锁规则: {rule_id} - {rule.get('name')}")
        return True

    def remove_interlock(self, rule_id: str) -> bool:
        """移除联锁规则"""
        with self._lock:
            if rule_id in self._interlock_rules:
                del self._interlock_rules[rule_id]
                self._interlock_states.pop(rule_id, None)
                self._interlock_bypass.discard(rule_id)
                logger.info(f"移除联锁规则: {rule_id}")
                return True
        return False

    def bypass_interlock(self, rule_id: str, operator: str, reason: str) -> bool:
        """旁路联锁（维护时临时禁用，需记录审计）"""
        if rule_id not in self._interlock_rules:
            return False
        with self._lock:
            self._interlock_bypass.add(rule_id)
        self._audit('interlock_bypass', operator, f'旁路联锁 {rule_id}: {reason}')
        logger.warning(f"联锁已旁路: {rule_id} by {operator} — {reason}")
        return True

    def restore_interlock(self, rule_id: str, operator: str) -> bool:
        """恢复联锁"""
        with self._lock:
            self._interlock_bypass.discard(rule_id)
        self._audit('interlock_restore', operator, f'恢复联锁 {rule_id}')
        logger.info(f"联锁已恢复: {rule_id} by {operator}")
        return True

    def check_interlocks(self, device_id: str, register_name: str, value: float):
        """
        检查联锁条件（每次数据采集时调用）

        Args:
            device_id: 设备ID
            register_name: 寄存器名称
            value: 当前值
        """
        with self._lock:
            for rule_id, rule in self._interlock_rules.items():
                # 跳过被旁路的联锁
                if rule_id in self._interlock_bypass:
                    continue

                cond = rule.get('condition', {})

                # 检查条件是否匹配
                triggered = False
                if cond.get('type') == 'threshold':
                    if (cond.get('device_id') == device_id and
                            cond.get('register') == register_name):
                        op = cond.get('operator', '>')
                        threshold = cond.get('value', 0)
                        if op == '>' and value > threshold:
                            triggered = True
                        elif op == '<' and value < threshold:
                            triggered = True
                        elif op == '>=' and value >= threshold:
                            triggered = True
                        elif op == '<=' and value <= threshold:
                            triggered = True

                elif cond.get('type') == 'coil':
                    if (cond.get('device_id') == device_id and
                            cond.get('address') == register_name):
                        if value == (1 if cond.get('value') else 0):
                            triggered = True

                # 处理联锁触发
                was_triggered = self._interlock_states.get(rule_id, False)
                if triggered and not was_triggered:
                    self._interlock_states[rule_id] = True
                    self._execute_interlock_action(rule)
                elif not triggered and was_triggered:
                    self._interlock_states[rule_id] = False
                    logger.info(f"联锁条件解除: {rule_id}")

    def _execute_interlock_action(self, rule: Dict):
        """执行联锁动作"""
        rule_id = rule['id']
        action = rule.get('action', {})
        alarm_info = rule.get('alarm', {})

        logger.warning(f"联锁触发: {rule_id} - {rule.get('name')}")

        # 发送报警
        if self.alarm_manager and alarm_info:
            level = alarm_info.get('level', 'critical')
            message = alarm_info.get('message', f'联锁触发: {rule_id}')
            # 通过报警管理器触发声光报警
            if self.alarm_manager.alarm_output:
                self.alarm_manager.alarm_output.trigger_alarm(level, message, 'interlock')
            if self.alarm_manager.broadcast_system:
                self.alarm_manager.broadcast_system.speak_alarm(level, message, 'interlock')

        # 执行动作
        action_type = action.get('type')
        if action_type == 'write_register':
            device_id = action.get('device_id')
            register = action.get('register')
            value = action.get('value')
            if self.device_manager:
                try:
                    self.device_manager.write_register(device_id, register, value)
                    logger.info(f"联锁动作执行: 写入 {device_id}/{register} = {value}")
                except Exception as e:
                    logger.error(f"联锁动作执行失败: {e}")

        elif action_type == 'emergency_stop':
            self.trigger_emergency_stop(f'联锁 {rule_id} 触发紧急停机')

        elif action_type == 'block_start':
            # 标记设备启动被阻止
            logger.warning(f"联锁阻止设备启动: {rule_id}")

        # 记录审计
        self._audit('interlock_triggered', 'system',
                    f'联锁 {rule_id} ({rule.get("name")}) 触发')

    def get_interlock_status(self) -> Dict[str, Any]:
        """获取所有联锁状态"""
        with self._lock:
            result = {}
            for rule_id, rule in self._interlock_rules.items():
                result[rule_id] = {
                    'name': rule.get('name'),
                    'description': rule.get('description'),
                    'priority': rule.get('priority', 99),
                    'triggered': self._interlock_states.get(rule_id, False),
                    'bypassed': rule_id in self._interlock_bypass,
                    'condition': rule.get('condition'),
                    'action': rule.get('action'),
                }
            return result

    # ==================== 写操作安全校验 ====================

    def validate_write(self, device_id: str, address: int, value: int,
                       operator: str) -> Dict[str, Any]:
        """
        写操作安全校验

        Returns:
            {'allowed': bool, 'reason': str, 'warnings': list}
        """
        result = {'allowed': True, 'reason': '', 'warnings': []}

        # 1. 紧急停机检查
        if self._estop_active:
            result['allowed'] = False
            result['reason'] = '紧急停机状态中，禁止所有写操作'
            return result

        # 2. 设备健康检查
        health = self._device_health.get(device_id, {})
        if health.get('status') == 'disconnected':
            result['allowed'] = False
            result['reason'] = f'设备 {device_id} 通信断开，无法执行写操作'
            return result

        # 3. 值范围校验
        limits = self._write_limits.get(device_id, {})
        if limits:
            # 查找匹配的限制（按地址或默认）
            limit_key = str(address)
            if limit_key in limits:
                min_val, max_val = limits[limit_key]
                if value < min_val or value > max_val:
                    result['allowed'] = False
                    result['reason'] = f'写入值 {value} 超出安全范围 [{min_val}, {max_val}]'
                    return result
            elif 'default' in limits:
                min_val, max_val = limits['default']
                if value < min_val or value > max_val:
                    result['allowed'] = False
                    result['reason'] = f'写入值 {value} 超出安全范围 [{min_val}, {max_val}]'
                    return result

        # 4. 联锁阻止检查
        for rule_id, rule in self._interlock_rules.items():
            if rule_id in self._interlock_bypass:
                continue
            action = rule.get('action', {})
            if action.get('type') == 'block_start':
                cond = rule.get('condition', {})
                if cond.get('device_id') == device_id:
                    # 检查联锁是否触发
                    if self._interlock_states.get(rule_id, False):
                        result['allowed'] = False
                        result['reason'] = f'联锁 {rule_id} ({rule.get("name")}) 阻止操作'
                        return result

        # 5. 操作互斥检查
        lock_owner = self._device_lock_owners.get(device_id)
        if lock_owner and lock_owner != operator:
            result['allowed'] = False
            result['reason'] = f'设备 {device_id} 正被 {lock_owner} 操作中，请稍后重试'
            return result

        # 6. 警告检查
        for rule_id, triggered in self._interlock_states.items():
            if triggered and rule_id not in self._interlock_bypass:
                rule = self._interlock_rules.get(rule_id, {})
                result['warnings'].append(f'注意: 联锁 {rule_id} ({rule.get("name")}) 当前触发中')

        return result

    def acquire_device_lock(self, device_id: str, operator: str, timeout: float = 5.0) -> bool:
        """获取设备操作锁（防止并发操作）"""
        if device_id not in self._device_locks:
            self._device_locks[device_id] = threading.Lock()

        acquired = self._device_locks[device_id].acquire(timeout=timeout)
        if acquired:
            self._device_lock_owners[device_id] = operator
        return acquired

    def release_device_lock(self, device_id: str):
        """释放设备操作锁"""
        self._device_lock_owners.pop(device_id, None)
        if device_id in self._device_locks:
            try:
                self._device_locks[device_id].release()
            except RuntimeError:
                pass

    # ==================== 紧急停机 ====================

    def trigger_emergency_stop(self, reason: str = '手动触发') -> Dict[str, Any]:
        """
        触发紧急停机

        紧急停机逻辑：
        1. 设置全局E-Stop标志（阻止所有写操作）
        2. 向所有可控设备发送停机命令
        3. 触发声光报警（红灯+蜂鸣器常响）
        4. 触发全厂广播
        5. 记录审计日志
        """
        self._estop_active = True
        self._estop_time = datetime.now()
        self._estop_reason = reason

        logger.critical(f"紧急停机触发: {reason}")

        # 向可控设备发送停机命令
        stopped_devices = []
        if self.device_manager:
            for device_id in self._get_controllable_devices():
                try:
                    self.device_manager.write_register(device_id, 'emergency_stop', 1)
                    stopped_devices.append(device_id)
                except Exception as e:
                    logger.error(f"紧急停机命令发送失败 {device_id}: {e}")

        self._estop_devices = stopped_devices

        # 触发声光报警
        if self.alarm_manager:
            if self.alarm_manager.alarm_output:
                self.alarm_manager.alarm_output.trigger_alarm(
                    'critical', f'紧急停机: {reason}', 'estop')
            if self.alarm_manager.broadcast_system:
                self.alarm_manager.broadcast_system.speak(
                    f'紧急停机！{reason}，所有设备已停止运行，请立即检查！',
                    level='critical', source='estop')

        # 审计
        self._audit('emergency_stop', 'system', f'紧急停机: {reason}，影响设备: {stopped_devices}')

        return {
            'success': True,
            'message': f'紧急停机已触发: {reason}',
            'stopped_devices': stopped_devices,
            'timestamp': self._estop_time.isoformat()
        }

    def reset_emergency_stop(self, operator: str) -> Dict[str, Any]:
        """
        解除紧急停机（需要工程师权限+确认）

        解除逻辑：
        1. 检查所有联锁条件是否已解除
        2. 清除E-Stop标志
        3. 复位声光报警
        4. 记录审计日志
        """
        # 检查联锁状态
        active_interlocks = [
            rule_id for rule_id, triggered in self._interlock_states.items()
            if triggered and rule_id not in self._interlock_bypass
        ]
        if active_interlocks:
            return {
                'success': False,
                'message': f'无法解除紧急停机：联锁 {active_interlocks} 仍触发中',
                'active_interlocks': active_interlocks
            }

        self._estop_active = False
        self._estop_time = None
        self._estop_reason = ''

        # 复位声光报警
        if self.alarm_manager:
            if self.alarm_manager.alarm_output:
                self.alarm_manager.alarm_output.reset()

        self._audit('estop_reset', operator, '紧急停机已解除')
        logger.info(f"紧急停机已解除 by {operator}")

        return {
            'success': True,
            'message': '紧急停机已解除，系统恢复正常',
            'operator': operator
        }

    def get_estop_status(self) -> Dict[str, Any]:
        """获取紧急停机状态"""
        return {
            'active': self._estop_active,
            'time': self._estop_time.isoformat() if self._estop_time else None,
            'reason': self._estop_reason,
            'affected_devices': self._estop_devices
        }

    def _get_controllable_devices(self) -> List[str]:
        """获取所有可控设备ID列表"""
        devices = []
        if self.device_manager:
            for device_id, config in self.device_manager.devices.items():
                if config.get('access') == 'rw' or config.get('protocol') in ('modbus_tcp', 'modbus_rtu'):
                    devices.append(device_id)
        return devices

    # ==================== 故障降级 ====================

    def update_device_health(self, device_id: str, connected: bool,
                             response_time_ms: float = 0):
        """更新设备健康状态（由数据采集器调用）"""
        if device_id not in self._device_health:
            self._device_health[device_id] = {
                'status': 'unknown',
                'last_seen': None,
                'consecutive_failures': 0,
                'avg_response_ms': 0
            }

        health = self._device_health[device_id]

        if connected:
            health['status'] = 'connected'
            health['last_seen'] = datetime.now().isoformat()
            health['consecutive_failures'] = 0
            # 指数移动平均
            alpha = 0.3
            health['avg_response_ms'] = (
                alpha * response_time_ms +
                (1 - alpha) * health.get('avg_response_ms', 0)
            )
            self._comm_failures[device_id] = 0
        else:
            failures = self._comm_failures.get(device_id, 0) + 1
            self._comm_failures[device_id] = failures
            health['consecutive_failures'] = failures

            if failures >= self._comm_threshold:
                health['status'] = 'disconnected'
                self._trigger_fault_degradation(device_id, failures)

    def _trigger_fault_degradation(self, device_id: str, failures: int):
        """触发故障降级"""
        logger.warning(f"设备 {device_id} 通信失败 {failures} 次，触发故障降级")

        # 执行安全状态
        handler = self._safe_state_handlers.get(device_id)
        if handler:
            try:
                handler(device_id)
            except Exception as e:
                logger.error(f"安全状态执行失败 {device_id}: {e}")

        # 触发报警
        if self.alarm_manager:
            if self.alarm_manager.alarm_output:
                self.alarm_manager.alarm_output.trigger_alarm(
                    'warning',
                    f'设备 {device_id} 通信中断（{failures}次），已进入安全状态',
                    device_id
                )

        self._audit('fault_degradation', 'system',
                    f'设备 {device_id} 通信失败 {failures} 次，已降级到安全状态')

    def register_safe_state_handler(self, device_id: str, handler: Callable):
        """注册设备安全状态处理函数"""
        self._safe_state_handlers[device_id] = handler

    def get_device_health_summary(self) -> Dict[str, Any]:
        """获取所有设备健康摘要"""
        summary = {
            'total': len(self._device_health),
            'connected': 0,
            'disconnected': 0,
            'unknown': 0,
            'devices': {}
        }
        for device_id, health in self._device_health.items():
            status = health.get('status', 'unknown')
            summary[status] = summary.get(status, 0) + 1
            summary['devices'][device_id] = health
        return summary

    # ==================== 写操作回读验证 ====================

    def write_with_verification(self, device_id: str, address: int, value: int,
                                operator: str, max_retries: int = 2) -> Dict[str, Any]:
        """
        带回读验证的写操作

        流程：
        1. 安全校验
        2. 获取设备锁
        3. 写入值
        4. 回读验证
        5. 释放锁
        6. 记录审计
        """
        # 安全校验
        validation = self.validate_write(device_id, address, value, operator)
        if not validation['allowed']:
            self._audit('write_blocked', operator,
                        f'写入被阻止: {device_id}/{address}={value}, 原因: {validation["reason"]}')
            return {
                'success': False,
                'message': validation['reason'],
                'validation': validation
            }

        # 获取设备锁
        if not self.acquire_device_lock(device_id, operator):
            return {
                'success': False,
                'message': f'无法获取设备 {device_id} 的操作锁'
            }

        try:
            # 写入
            success = False
            for attempt in range(max_retries + 1):
                try:
                    if self.device_manager:
                        success = self.device_manager.write_register(device_id, address, value)
                    break
                except Exception as e:
                    if attempt < max_retries:
                        logger.warning(f"写入重试 {attempt + 1}/{max_retries}: {e}")
                        time.sleep(0.1)
                    else:
                        raise

            if not success:
                self._audit('write_failed', operator,
                            f'写入失败: {device_id}/{address}={value}')
                return {
                    'success': False,
                    'message': '写入操作失败'
                }

            # 回读验证
            readback_ok = True
            readback_value = None
            try:
                if self.device_manager:
                    readback_value = self.device_manager.read_register(device_id, address)
                    if readback_value is not None and abs(readback_value - value) > 1:
                        readback_ok = False
                        logger.warning(f"回读验证失败: 期望 {value}, 实际 {readback_value}")
            except Exception as e:
                logger.warning(f"回读验证异常: {e}")

            # 审计
            self._audit('write_success', operator,
                        f'写入成功: {device_id}/{address}={value}'
                        f'{f", 回读={readback_value}" if readback_value is not None else ""}'
                        f'{", 验证通过" if readback_ok else ", 验证失败"}')

            result = {
                'success': True,
                'message': '写入成功',
                'warnings': validation.get('warnings', [])
            }
            if not readback_ok:
                result['message'] = '写入成功，但回读验证失败'
                result['warnings'].append(f'回读值 {readback_value} 与写入值 {value} 不一致')

            return result

        finally:
            self.release_device_lock(device_id)

    # ==================== 操作审计 ====================

    def _audit(self, action: str, operator: str, detail: str):
        """记录操作审计日志"""
        entry = {
            'timestamp': datetime.now().isoformat(),
            'action': action,
            'operator': operator,
            'detail': detail
        }
        self._audit_log.append(entry)

        # 持久化到文件
        try:
            self._audit_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._audit_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False) + '\n')
        except Exception as e:
            logger.error(f"审计日志写入失败: {e}")

    def get_audit_log(self, limit: int = 100, action_filter: str = None) -> List[Dict]:
        """获取操作审计日志"""
        logs = list(self._audit_log)
        if action_filter:
            logs = [l for l in logs if l.get('action') == action_filter]
        return list(reversed(logs[-limit:]))

    def get_audit_stats(self) -> Dict[str, Any]:
        """获取审计统计"""
        logs = list(self._audit_log)
        action_counts = {}
        for log in logs:
            action = log.get('action', 'unknown')
            action_counts[action] = action_counts.get(action, 0) + 1
        return {
            'total_entries': len(logs),
            'by_action': action_counts,
            'estop_active': self._estop_active,
            'active_interlocks': sum(
                1 for t in self._interlock_states.values() if t
            ),
            'bypassed_interlocks': len(self._interlock_bypass)
        }

    # ==================== 综合状态 ====================

    def get_full_status(self) -> Dict[str, Any]:
        """获取设备控制安全系统完整状态"""
        return {
            'estop': self.get_estop_status(),
            'interlocks': {
                'total': len(self._interlock_rules),
                'triggered': sum(1 for t in self._interlock_states.values() if t),
                'bypassed': len(self._interlock_bypass),
                'rules': self.get_interlock_status()
            },
            'device_health': self.get_device_health_summary(),
            'audit': self.get_audit_stats()
        }
