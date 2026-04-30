"""
报警规则模块
定义报警规则和条件
"""

import logging
from typing import Dict, List, Any, Optional
from enum import Enum

logger = logging.getLogger(__name__)


class AlarmLevel(Enum):
    """报警级别"""
    CRITICAL = "critical"  # 严重
    WARNING = "warning"    # 警告
    INFO = "info"          # 信息


class AlarmCondition(Enum):
    """报警条件"""
    GREATER_THAN = "greater_than"
    LESS_THAN = "less_than"
    EQUAL_TO = "equal_to"
    NOT_EQUAL_TO = "not_equal_to"
    GREATER_EQUAL = "greater_equal"
    LESS_EQUAL = "less_equal"


class AlarmRule:
    """
    报警规则类
    定义单个报警规则
    """
    
    def __init__(self, rule_id: str, name: str, device_id: str,
                 register_name: str, condition: str, threshold: float,
                 level: str = "warning", enabled: bool = True,
                 delay: int = 0, description: str = ""):
        """
        初始化报警规则
        
        Args:
            rule_id: 规则ID
            name: 规则名称
            device_id: 设备ID
            register_name: 寄存器名称
            condition: 报警条件
            threshold: 阈值
            level: 报警级别
            enabled: 是否启用
            delay: 延迟时间（秒）
            description: 描述
        """
        self.rule_id = rule_id
        self.name = name
        self.device_id = device_id
        self.register_name = register_name
        self.condition = condition
        self.threshold = threshold
        self.level = level
        self.enabled = enabled
        self.delay = delay
        self.description = description
    
    def check(self, value: float) -> bool:
        """
        检查是否触发报警
        
        Args:
            value: 实际值
            
        Returns:
            bool: 是否触发报警
        """
        if not self.enabled:
            return False
        
        if self.condition == AlarmCondition.GREATER_THAN.value:
            return value > self.threshold
        elif self.condition == AlarmCondition.LESS_THAN.value:
            return value < self.threshold
        elif self.condition == AlarmCondition.EQUAL_TO.value:
            return abs(value - self.threshold) < 0.0001
        elif self.condition == AlarmCondition.NOT_EQUAL_TO.value:
            return abs(value - self.threshold) >= 0.0001
        elif self.condition == AlarmCondition.GREATER_EQUAL.value:
            return value >= self.threshold
        elif self.condition == AlarmCondition.LESS_EQUAL.value:
            return value <= self.threshold
        else:
            logger.warning(f"未知的报警条件: {self.condition}")
            return False
    
    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典
        
        Returns:
            Dict: 规则字典
        """
        return {
            'id': self.rule_id,
            'name': self.name,
            'device_id': self.device_id,
            'register_name': self.register_name,
            'condition': self.condition,
            'threshold': self.threshold,
            'level': self.level,
            'enabled': self.enabled,
            'delay': self.delay,
            'description': self.description
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AlarmRule':
        """
        从字典创建规则
        
        Args:
            data: 规则字典
            
        Returns:
            AlarmRule: 规则实例
        """
        return cls(
            rule_id=data.get('id'),
            name=data.get('name'),
            device_id=data.get('device_id'),
            register_name=data.get('register_name'),
            condition=data.get('condition'),
            threshold=data.get('threshold'),
            level=data.get('level', 'warning'),
            enabled=data.get('enabled', True),
            delay=data.get('delay', 0),
            description=data.get('description', '')
        )


class AlarmRules:
    """
    报警规则管理类
    管理所有报警规则
    """
    
    def __init__(self):
        """初始化报警规则管理"""
        self.rules = {}  # rule_id -> AlarmRule
    
    def add_rule(self, rule: AlarmRule):
        """
        添加规则
        
        Args:
            rule: 报警规则
        """
        self.rules[rule.rule_id] = rule
        logger.info(f"添加报警规则: {rule.rule_id}")
    
    def remove_rule(self, rule_id: str):
        """
        移除规则
        
        Args:
            rule_id: 规则ID
        """
        if rule_id in self.rules:
            del self.rules[rule_id]
            logger.info(f"移除报警规则: {rule_id}")
    
    def get_rule(self, rule_id: str) -> Optional[AlarmRule]:
        """
        获取规则
        
        Args:
            rule_id: 规则ID
            
        Returns:
            AlarmRule: 规则实例
        """
        return self.rules.get(rule_id)
    
    def get_rules_for_device(self, device_id: str) -> List[AlarmRule]:
        """
        获取设备的所有规则
        
        Args:
            device_id: 设备ID
            
        Returns:
            List[AlarmRule]: 规则列表
        """
        return [rule for rule in self.rules.values() 
                if rule.device_id == device_id and rule.enabled]
    
    def check_value(self, device_id: str, register_name: str, 
                    value: float) -> List[AlarmRule]:
        """
        检查值是否触发报警
        
        Args:
            device_id: 设备ID
            register_name: 寄存器名称
            value: 实际值
            
        Returns:
            List[AlarmRule]: 触发的规则列表
        """
        triggered = []
        
        for rule in self.rules.values():
            if (rule.device_id == device_id and 
                rule.register_name == register_name and
                rule.enabled and rule.check(value)):
                triggered.append(rule)
        
        return triggered
    
    def load_from_dict(self, rules_data: List[Dict[str, Any]]):
        """
        从字典加载规则
        
        Args:
            rules_data: 规则字典列表
        """
        for rule_data in rules_data:
            rule = AlarmRule.from_dict(rule_data)
            self.add_rule(rule)
    
    def to_dict(self) -> List[Dict[str, Any]]:
        """
        转换为字典列表
        
        Returns:
            List[Dict]: 规则字典列表
        """
        return [rule.to_dict() for rule in self.rules.values()]
