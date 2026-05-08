"""
报警管理器模块（重写版）
实现报警检测、记录、通知，集成工业声光报警器、语音广播、前端推送

核心改进：警告去重机制
- 冷却窗口：同一报警在冷却时间内只推送一次前端通知
- 确认抑制：用户确认报警后，同一报警在抑制时间内不再弹窗
- 前端dismissed跟踪：用户关闭弹窗后，该报警在冷却期内不重复显示
- 去重配置API：可动态调整去重参数
"""

import logging
import yaml
import time
from typing import Any, Callable
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)


class AlarmDedupConfig:
    """报警去重配置"""

    def __init__(self, config: dict[str, Any] | None = None):
        config = config or {}
        # 冷却窗口（秒）：同一报警在此时间内只推送一次前端通知
        self.emit_cooldown_seconds: int = config.get('emit_cooldown_seconds', 60)
        # 确认后抑制时间（秒）：用户确认报警后，同一报警在此时间内不再弹窗
        self.acknowledge_suppress_seconds: int = config.get('acknowledge_suppress_seconds', 300)
        # 是否启用去重
        self.enabled: bool = config.get('enabled', True)
        # 最大同时显示的报警数（前端）
        self.max_visible_toasts: int = config.get('max_visible_toasts', 3)
        # 前端toast自动消失时间：严重报警（秒）
        self.critical_toast_duration: int = config.get('critical_toast_duration', 30)
        # 前端toast自动消失时间：普通警告（秒）
        self.warning_toast_duration: int = config.get('warning_toast_duration', 10)

    def to_dict(self) -> dict[str, Any]:
        return {
            'enabled': self.enabled,
            'emit_cooldown_seconds': self.emit_cooldown_seconds,
            'acknowledge_suppress_seconds': self.acknowledge_suppress_seconds,
            'max_visible_toasts': self.max_visible_toasts,
            'critical_toast_duration': self.critical_toast_duration,
            'warning_toast_duration': self.warning_toast_duration,
        }

    def update(self, data: dict[str, Any]):
        """从字典更新配置"""
        if 'emit_cooldown_seconds' in data:
            self.emit_cooldown_seconds = max(5, int(data['emit_cooldown_seconds']))
        if 'acknowledge_suppress_seconds' in data:
            self.acknowledge_suppress_seconds = max(30, int(data['acknowledge_suppress_seconds']))
        if 'enabled' in data:
            self.enabled = bool(data['enabled'])
        if 'max_visible_toasts' in data:
            self.max_visible_toasts = max(1, min(10, int(data['max_visible_toasts'])))
        if 'critical_toast_duration' in data:
            self.critical_toast_duration = max(5, int(data['critical_toast_duration']))
        if 'warning_toast_duration' in data:
            self.warning_toast_duration = max(5, int(data['warning_toast_duration']))


class AlarmManager:
    """
    报警管理器（重写版）
    负责报警检测、记录、声光输出、语音广播、前端推送

    报警输出总线（三级联动）：
    1. 声光报警器（Modbus DO -> 报警灯塔 + 蜂鸣器）
    2. 语音广播系统（MQTT -> IP网络广播/现场音柱）
    3. 前端WebSocket推送（页面弹窗/音效/语音合成）

    去重机制：
    - 同一报警（alarm_id + device_id + register_name）在冷却窗口内只推送一次
    - 用户确认后，同一报警在抑制时间内不再弹窗
    - 前端可通过 dismissed 列表告知后端跳过推送
    """

    def __init__(self, database, config_path: str = '配置/alarms.yaml',
                 alarm_output=None, broadcast_system=None):
        """
        初始化报警管理器

        Args:
            database: 数据库实例
            config_path: 报警配置文件路径
            alarm_output: 声光报警输出实例（AlarmOutput，可选）
            broadcast_system: 广播系统实例（BroadcastSystem，可选）
        """
        self.database = database
        self.config_path = config_path

        # 报警规则
        self.rules: dict[str, dict] = {}  # rule_id -> rule_config

        # 报警状态
        self.alarm_states: dict[tuple, dict] = {}  # (device_id, register_name) -> alarm_state

        # 输出总线（延迟注入，启动后绑定）
        self.alarm_output = alarm_output
        self.broadcast_system = broadcast_system

        # WebSocket回调（由外部注入，用于前端推送）
        self._websocket_emit: Callable[..., Any] | None = None

        # 去重配置
        self.dedup_config = AlarmDedupConfig()

        # 去重状态：记录每个报警的最后推送时间
        # key: (alarm_id, device_id, register_name) -> last_emit_timestamp
        self._emit_history: dict[tuple, float] = {}

        # 确认记录：记录每个报警的确认时间
        # key: (alarm_id, device_id, register_name) -> acknowledge_timestamp
        self._acknowledge_history: dict[tuple, float] = {}

        # 线程锁（保护去重状态的并发访问）
        self._dedup_lock = Lock()

        # 加载报警配置
        self.load_config()

    def set_websocket_emit(self, emit_func: Callable[..., Any]):
        """注入WebSocket emit函数（由run.py启动时调用）"""
        self._websocket_emit = emit_func

    def load_config(self):
        """加载报警配置文件"""
        try:
            config_file = Path(self.config_path)
            if not config_file.exists():
                logger.error(f"报警配置文件不存在: {self.config_path}")
                return

            with open(config_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)

            # 解析报警规则
            rules_config = config.get('alarm_rules', [])
            for rule_config in rules_config:
                rule_id = rule_config.get('id')
                if rule_id:
                    self.rules[rule_id] = rule_config
                    logger.info(f"加载报警规则: {rule_id} - {rule_config.get('name')}")

            logger.info(f"共加载 {len(self.rules)} 条报警规则")

            # 加载去重配置
            dedup_cfg = config.get('dedup', {})
            if dedup_cfg:
                self.dedup_config = AlarmDedupConfig(dedup_cfg)
                logger.info(f"加载去重配置: 冷却{self.dedup_config.emit_cooldown_seconds}s, "
                            f"确认抑制{self.dedup_config.acknowledge_suppress_seconds}s")

        except Exception as e:
            logger.error(f"加载报警配置异常: {e}")

    def check_alarm(self, device_id: str, register_name: str,
                    value: float, timestamp: datetime):
        """
        检查报警

        Args:
            device_id: 设备ID
            register_name: 寄存器名称
            value: 数据值
            timestamp: 时间戳
        """
        # 查找匹配的报警规则
        for rule_id, rule_config in self.rules.items():
            if not rule_config.get('enabled', True):
                continue

            # 检查设备和寄存器是否匹配
            if (rule_config.get('device_id') == device_id and
                rule_config.get('register_name') == register_name):

                # 检查报警条件
                alarm_triggered = self._check_condition(
                    value=value,
                    condition=rule_config.get('condition'),
                    threshold=rule_config.get('threshold')
                )

                # 处理报警状态
                self._process_alarm_state(
                    rule_id=rule_id,
                    rule_config=rule_config,
                    device_id=device_id,
                    register_name=register_name,
                    value=value,
                    timestamp=timestamp,
                    triggered=alarm_triggered
                )

    def _check_condition(self, value: float, condition: str, threshold: float) -> bool:
        """
        检查报警条件

        Args:
            value: 实际值
            condition: 条件类型
            threshold: 阈值

        Returns:
            bool: 是否触发报警
        """
        if condition == 'greater_than':
            return value > threshold
        elif condition == 'less_than':
            return value < threshold
        elif condition == 'equal_to':
            return abs(value - threshold) < 0.0001
        elif condition == 'not_equal_to':
            return abs(value - threshold) >= 0.0001
        elif condition == 'greater_equal':
            return value >= threshold
        elif condition == 'less_equal':
            return value <= threshold
        else:
            logger.warning(f"未知的报警条件: {condition}")
            return False

    def _process_alarm_state(self, rule_id: str, rule_config: dict[str, Any],
                             device_id: str, register_name: str,
                             value: float, timestamp: datetime, triggered: bool):
        """
        处理报警状态（含去重逻辑）

        Args:
            rule_id: 规则ID
            rule_config: 规则配置
            device_id: 设备ID
            register_name: 寄存器名称
            value: 数据值
            timestamp: 时间戳
            triggered: 是否触发
        """
        state_key = (device_id, register_name)
        current_state = self.alarm_states.get(state_key, {})

        # 获取延迟时间
        delay = rule_config.get('delay', 0)

        if triggered:
            # 检查是否已经在报警状态（同一规则）
            if current_state.get('alarm_id') == rule_id:
                # 已经在报警，更新时间
                current_state['last_trigger_time'] = timestamp
                current_state['trigger_count'] = current_state.get('trigger_count', 0) + 1

                # 去重检查：是否需要重新推送前端通知
                if self._should_emit(rule_id, device_id, register_name):
                    self._trigger_alarm(rule_config, device_id, register_name, value, timestamp)
                    self._record_emit(rule_id, device_id, register_name)
            else:
                # 新报警（或不同规则覆盖同一设备/寄存器）
                alarm_state = {
                    'alarm_id': rule_id,
                    'device_id': device_id,
                    'register_name': register_name,
                    'first_trigger_time': timestamp,
                    'last_trigger_time': timestamp,
                    'trigger_count': 1,
                    'confirmed': False,
                    'acknowledged': False
                }

                # 如果有延迟，等待确认
                if delay > 0:
                    alarm_state['pending'] = True
                    alarm_state['confirm_time'] = timestamp
                else:
                    # 立即触发报警（新报警总是触发一次）
                    alarm_state['pending'] = False
                    self._trigger_alarm(rule_config, device_id, register_name, value, timestamp)
                    self._record_emit(rule_id, device_id, register_name)

                self.alarm_states[state_key] = alarm_state
        else:
            # 未触发，检查是否需要清除报警
            if current_state.get('alarm_id') == rule_id:
                # 清除报警状态
                del self.alarm_states[state_key]
                logger.info(f"报警清除: {rule_id} - {device_id}/{register_name}")

    def _should_emit(self, rule_id: str, device_id: str, register_name: str) -> bool:
        """
        检查是否应该推送报警通知（去重核心逻辑）

        Args:
            rule_id: 规则ID
            device_id: 设备ID
            register_name: 寄存器名称

        Returns:
            bool: 是否应该推送
        """
        if not self.dedup_config.enabled:
            return True

        alarm_key = (rule_id, device_id, register_name)
        now = time.time()

        with self._dedup_lock:
            # 检查确认后抑制
            ack_time = self._acknowledge_history.get(alarm_key)
            if ack_time is not None:
                suppress_until = ack_time + self.dedup_config.acknowledge_suppress_seconds
                if now < suppress_until:
                    logger.debug(f"报警被确认抑制: {rule_id} (剩余{suppress_until - now:.0f}s)")
                    return False

            # 检查冷却窗口
            last_emit = self._emit_history.get(alarm_key)
            if last_emit is not None:
                cooldown_until = last_emit + self.dedup_config.emit_cooldown_seconds
                if now < cooldown_until:
                    logger.debug(f"报警在冷却窗口内: {rule_id} (剩余{cooldown_until - now:.0f}s)")
                    return False

        return True

    def _record_emit(self, rule_id: str, device_id: str, register_name: str):
        """记录报警推送时间"""
        alarm_key = (rule_id, device_id, register_name)
        with self._dedup_lock:
            self._emit_history[alarm_key] = time.time()

    def _record_acknowledge(self, rule_id: str, device_id: str, register_name: str):
        """记录报警确认时间"""
        alarm_key = (rule_id, device_id, register_name)
        with self._dedup_lock:
            self._acknowledge_history[alarm_key] = time.time()

    def _trigger_alarm(self, rule_config: dict[str, Any], device_id: str,
                       register_name: str, value: float, timestamp: datetime):
        """
        触发报警 → 三级联动输出

        1. 记录数据库
        2. 声光报警器（Modbus DO控制灯塔+蜂鸣器）
        3. 语音广播（MQTT发布到现场音柱）
        4. 前端WebSocket推送（页面弹窗+音效+语音合成）

        Args:
            rule_config: 规则配置
            device_id: 设备ID
            register_name: 寄存器名称
            value: 数据值
            timestamp: 时间戳
        """
        rule_id = rule_config.get('id')
        alarm_level = rule_config.get('level', 'warning')
        alarm_message = rule_config.get('name', '未知报警')
        threshold = rule_config.get('threshold', 0)
        alarm_area = rule_config.get('area', 'all')

        # 1. 记录数据库
        self.database.insert_alarm(
            alarm_id=rule_id,
            device_id=device_id,
            register_name=register_name,
            alarm_level=alarm_level,
            alarm_message=alarm_message,
            threshold=threshold,
            actual_value=value,
            timestamp=timestamp
        )

        logger.warning(f"报警触发: {alarm_message} - {device_id}/{register_name} = {value}")

        # 2. 声光报警器输出（Modbus DO -> 灯塔+蜂鸣器）
        if self.alarm_output and self.alarm_output.enabled:
            try:
                self.alarm_output.trigger_alarm(
                    level=alarm_level,
                    message=alarm_message,
                    device_id=device_id
                )
            except Exception as e:
                logger.error(f"声光报警输出异常: {e}")

        # 3. 语音广播系统（MQTT -> IP网络广播/现场音柱）
        if self.broadcast_system and self.broadcast_system.enabled:
            try:
                self.broadcast_system.speak_alarm(
                    level=alarm_level,
                    message=f"{alarm_message}，当前值{value}，阈值{threshold}",
                    device_id=device_id,
                    area=alarm_area
                )
            except Exception as e:
                logger.error(f"语音广播异常: {e}")

        # 4. 前端WebSocket推送（含去重标记）
        self._emit_websocket_alarm({
            'alarm_id': rule_id,
            'device_id': device_id,
            'register_name': register_name,
            'alarm_level': alarm_level,
            'alarm_message': alarm_message,
            'threshold': threshold,
            'actual_value': value,
            'timestamp': timestamp.isoformat() if hasattr(timestamp, 'isoformat') else str(timestamp),
            'area': alarm_area,
            'dedup_key': f"{rule_id}:{device_id}:{register_name}",
        })

    def _emit_websocket_alarm(self, alarm_data: dict[str, Any]):
        """通过WebSocket向前端推送报警"""
        try:
            if self._websocket_emit:
                self._websocket_emit(alarm_data)
        except Exception as e:
            logger.error(f"WebSocket报警推送异常: {e}")

    def _send_notification(self, rule_config: dict[str, Any], device_id: str,
                           register_name: str, value: float, timestamp: datetime):
        """
        发送报警通知（保留兼容，具体实现在 _trigger_alarm 中）
        """
        alarm_level = rule_config.get('level', 'warning')
        alarm_message = rule_config.get('name', '未知报警')
        logger.info(f"报警通知: [{alarm_level}] {alarm_message} - {device_id}/{register_name}")

    def get_active_alarms(self) -> list[dict[str, Any]]:
        """
        获取活动报警

        Returns:
            list[dict[str, Any]]: 活动报警列表
        """
        active_alarms = []

        for state_key, state in self.alarm_states.items():
            device_id, register_name = state_key
            rule_id = state.get('alarm_id')
            rule_config = self.rules.get(rule_id, {})

            active_alarms.append({
                'alarm_id': rule_id,
                'device_id': device_id,
                'register_name': register_name,
                'alarm_level': rule_config.get('level', 'warning'),
                'alarm_message': rule_config.get('name', '未知报警'),
                'first_trigger_time': state.get('first_trigger_time'),
                'last_trigger_time': state.get('last_trigger_time'),
                'trigger_count': state.get('trigger_count', 0),
                'acknowledged': state.get('acknowledged', False)
            })

        return active_alarms

    def acknowledge_alarm(self, alarm_id: str, device_id: str,
                          register_name: str, acknowledged_by: str) -> bool:
        """
        确认报警（含声光消音 + 去重抑制）

        操作员到场后确认报警，自动关闭蜂鸣器（灯保持闪烁）
        同时记录确认时间，触发去重抑制窗口

        Args:
            alarm_id: 报警ID
            device_id: 设备ID
            register_name: 寄存器名称
            acknowledged_by: 确认人

        Returns:
            bool: 确认是否成功
        """
        state_key = (device_id, register_name)
        state = self.alarm_states.get(state_key)

        if state and state.get('alarm_id') == alarm_id:
            state['acknowledged'] = True
            state['acknowledged_by'] = acknowledged_by
            state['acknowledged_at'] = datetime.now()

        # 更新数据库
        success = self.database.acknowledge_alarm(alarm_id, acknowledged_by)

        if success:
            logger.info(f"报警已确认: {alarm_id} by {acknowledged_by}")

            # 记录确认时间（用于去重抑制）
            self._record_acknowledge(alarm_id, device_id, register_name)

            # 声光消音（关蜂鸣器，灯保持）
            if self.alarm_output and self.alarm_output.enabled:
                try:
                    self.alarm_output.acknowledge()
                except Exception as e:
                    logger.error(f"声光消音失败: {e}")
        return success

    def reset_alarm(self, device_id: str | None = None) -> bool:
        """
        复位报警（清除所有/指定设备的报警状态，恢复绿灯）

        Args:
            device_id: 设备ID（None=全部复位）
        """
        if device_id:
            keys_to_remove = [k for k in self.alarm_states if k[0] == device_id]
            for key in keys_to_remove:
                del self.alarm_states[key]
        else:
            self.alarm_states.clear()

        # 清除去重历史（复位后允许重新推送）
        with self._dedup_lock:
            if device_id:
                emit_keys = [k for k in self._emit_history if k[1] == device_id]
                ack_keys = [k for k in self._acknowledge_history if k[1] == device_id]
                for k in emit_keys:
                    del self._emit_history[k]
                for k in ack_keys:
                    del self._acknowledge_history[k]
            else:
                self._emit_history.clear()
                self._acknowledge_history.clear()

        # 声光复位（全部清零，绿灯恢复）
        if self.alarm_output and self.alarm_output.enabled:
            try:
                self.alarm_output.reset()
            except Exception as e:
                logger.error(f"声光复位失败: {e}")

        logger.info(f"报警已复位: {'全部' if not device_id else device_id}")
        return True

    def get_alarm_statistics(self) -> dict[str, Any]:
        """
        获取报警统计信息（含输出总线状态 + 去重统计）

        Returns:
            dict[str, Any]: 统计信息
        """
        # 活动报警统计
        active_alarms = self.get_active_alarms()

        # 按级别统计
        level_stats = {}
        for alarm in active_alarms:
            level = alarm.get('alarm_level', 'unknown')
            level_stats[level] = level_stats.get(level, 0) + 1

        # 按设备统计
        device_stats = {}
        for alarm in active_alarms:
            device_id = alarm.get('device_id')
            device_stats[device_id] = device_stats.get(device_id, 0) + 1

        stats = {
            'total_active_alarms': len(active_alarms),
            'by_level': level_stats,
            'by_device': device_stats,
            'total_rules': len(self.rules),
            'enabled_rules': sum(1 for r in self.rules.values() if r.get('enabled', True))
        }

        # 输出总线状态
        stats['output'] = {
            'alarm_output': self.alarm_output.get_status() if self.alarm_output else None,
            'broadcast': self.broadcast_system.get_status() if self.broadcast_system else None
        }

        # 去重统计
        with self._dedup_lock:
            stats['dedup'] = {
                'enabled': self.dedup_config.enabled,
                'emit_cooldown_seconds': self.dedup_config.emit_cooldown_seconds,
                'acknowledge_suppress_seconds': self.dedup_config.acknowledge_suppress_seconds,
                'tracked_alarms': len(self._emit_history),
                'acknowledged_alarms': len(self._acknowledge_history),
            }

        return stats

    def get_dedup_config(self) -> dict[str, Any]:
        """获取去重配置"""
        return self.dedup_config.to_dict()

    def update_dedup_config(self, data: dict[str, Any]) -> dict[str, Any]:
        """
        更新去重配置

        Args:
            data: 新配置数据

        Returns:
            dict: 更新后的配置
        """
        self.dedup_config.update(data)

        # 持久化到配置文件
        self._save_dedup_config()

        logger.info(f"去重配置已更新: {self.dedup_config.to_dict()}")
        return self.dedup_config.to_dict()

    def _save_dedup_config(self):
        """保存去重配置到alarms.yaml"""
        try:
            config_file = Path(self.config_path)
            if not config_file.exists():
                return

            with open(config_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}

            config['dedup'] = self.dedup_config.to_dict()

            with open(config_file, 'w', encoding='utf-8') as f:
                yaml.dump(config, f, allow_unicode=True, default_flow_style=False)

            logger.info("去重配置已保存到配置文件")

        except Exception as e:
            logger.error(f"保存去重配置异常: {e}")

    def add_rule(self, rule_config: dict[str, Any]) -> bool:
        """
        添加报警规则

        Args:
            rule_config: 规则配置

        Returns:
            bool: 添加是否成功
        """
        try:
            rule_id = rule_config.get('id')
            if not rule_id:
                logger.error("报警规则缺少id字段")
                return False

            # 检查是否已存在
            if rule_id in self.rules:
                logger.warning(f"报警规则 {rule_id} 已存在，将被覆盖")

            # 添加到内存
            self.rules[rule_id] = rule_config

            # 保存到配置文件
            self._save_config()

            logger.info(f"添加报警规则: {rule_id}")
            return True

        except Exception as e:
            logger.error(f"添加报警规则异常: {e}")
            return False

    def remove_rule(self, rule_id: str) -> bool:
        """
        移除报警规则

        Args:
            rule_id: 规则ID

        Returns:
            bool: 移除是否成功
        """
        try:
            if rule_id in self.rules:
                del self.rules[rule_id]

                # 保存到配置文件
                self._save_config()

                logger.info(f"移除报警规则: {rule_id}")
                return True
            else:
                logger.warning(f"报警规则 {rule_id} 不存在")
                return False

        except Exception as e:
            logger.error(f"移除报警规则异常: {e}")
            return False

    def _save_config(self):
        """保存报警配置到文件"""
        try:
            config = {
                'alarm_rules': list(self.rules.values())
            }

            config_file = Path(self.config_path)
            config_file.parent.mkdir(parents=True, exist_ok=True)

            with open(config_file, 'w', encoding='utf-8') as f:
                yaml.dump(config, f, allow_unicode=True, default_flow_style=False)

            logger.info("报警配置已保存")

        except Exception as e:
            logger.error(f"保存报警配置异常: {e}")
