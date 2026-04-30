"""
报警管理器模块
实现报警检测、记录和通知
"""

import logging
import yaml
from typing import Dict, List, Any, Optional
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class AlarmManager:
    """
    报警管理器
    负责报警检测、记录和通知
    """
    
    def __init__(self, database, config_path: str = '配置/alarms.yaml'):
        """
        初始化报警管理器
        
        Args:
            database: 数据库实例
            config_path: 报警配置文件路径
        """
        self.database = database
        self.config_path = config_path
        
        # 报警规则
        self.rules = {}  # rule_id -> rule_config
        
        # 报警状态
        self.alarm_states = {}  # (device_id, register_name) -> alarm_state
        
        # 加载报警配置
        self.load_config()
    
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
    
    def _process_alarm_state(self, rule_id: str, rule_config: Dict,
                             device_id: str, register_name: str,
                             value: float, timestamp: datetime, triggered: bool):
        """
        处理报警状态
        
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
            # 检查是否已经在报警状态
            if current_state.get('alarm_id') == rule_id:
                # 已经在报警，更新时间
                current_state['last_trigger_time'] = timestamp
                current_state['trigger_count'] = current_state.get('trigger_count', 0) + 1
            else:
                # 新报警
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
                    # 立即触发报警
                    alarm_state['pending'] = False
                    self._trigger_alarm(rule_config, device_id, register_name, value, timestamp)
                
                self.alarm_states[state_key] = alarm_state
        else:
            # 未触发，检查是否需要清除报警
            if current_state.get('alarm_id') == rule_id:
                # 清除报警状态
                del self.alarm_states[state_key]
                logger.info(f"报警清除: {rule_id} - {device_id}/{register_name}")
    
    def _trigger_alarm(self, rule_config: Dict, device_id: str,
                       register_name: str, value: float, timestamp: datetime):
        """
        触发报警
        
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
        
        # 记录报警
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
        
        # 发送通知（如果配置了）
        self._send_notification(rule_config, device_id, register_name, value, timestamp)
    
    def _send_notification(self, rule_config: Dict, device_id: str,
                           register_name: str, value: float, timestamp: datetime):
        """
        发送报警通知
        
        Args:
            rule_config: 规则配置
            device_id: 设备ID
            register_name: 寄存器名称
            value: 数据值
            timestamp: 时间戳
        """
        # 这里可以实现邮件、短信等通知
        # 暂时只记录日志
        alarm_level = rule_config.get('level', 'warning')
        alarm_message = rule_config.get('name', '未知报警')
        
        logger.info(f"报警通知: [{alarm_level}] {alarm_message} - {device_id}/{register_name}")
    
    def get_active_alarms(self) -> List[Dict]:
        """
        获取活动报警
        
        Returns:
            List[Dict]: 活动报警列表
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
        确认报警
        
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
        
        # 更新数据库（无论内存状态是否存在都尝试更新）
        success = self.database.acknowledge_alarm(alarm_id, acknowledged_by)
        
        if success:
            logger.info(f"报警已确认: {alarm_id} by {acknowledged_by}")
        return success
    
    def get_alarm_statistics(self) -> Dict[str, Any]:
        """
        获取报警统计信息
        
        Returns:
            Dict: 统计信息
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
        
        return {
            'total_active_alarms': len(active_alarms),
            'by_level': level_stats,
            'by_device': device_stats,
            'total_rules': len(self.rules),
            'enabled_rules': sum(1 for r in self.rules.values() if r.get('enabled', True))
        }
    
    def add_rule(self, rule_config: Dict) -> bool:
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
