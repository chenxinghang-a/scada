"""
动态脱敏规则引擎
运行时可配置的脱敏规则，支持正则/关键字/字段名/自定义函数。

使用方式:
    from core.masking_rule_engine import MaskingRuleEngine
    engine = MaskingRuleEngine()
    engine.add_rule('credit_card', pattern=r'\d{4}[\s-]?\d{4}', strategy='mask_middle')
    masked = engine.mask_text('我的卡号是 1234 5678 9012 3456')
"""

import re
import json
import time
import logging
import threading
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class MaskStrategy(Enum):
    """脱敏策略"""
    FULL_MASK = 'full_mask'           # 完全遮蔽 → ***
    MASK_MIDDLE = 'mask_middle'       # 中间遮蔽 → 12**56
    MASK_END = 'mask_end'             # 尾部遮蔽 → 1234****
    MASK_START = 'mask_start'         # 头部遮蔽 → ****5678
    HASH = 'hash'                     # 哈希替换
    REDACT = 'redact'                 # 替换为 [REDACTED]
    TRUNCATE = 'truncate'             # 截断
    CUSTOM = 'custom'                 # 自定义函数


@dataclass
class MaskingRule:
    """脱敏规则"""
    name: str
    strategy: MaskStrategy
    pattern: Optional[str] = None       # 正则模式
    field_names: Optional[List[str]] = None  # 字段名匹配
    keywords: Optional[List[str]] = None    # 关键字匹配
    keep_prefix: int = 0                # 保留前N字符
    keep_suffix: int = 0                # 保留后N字符
    mask_char: str = '*'                # 遮蔽字符
    replacement: Optional[str] = None   # 替换文本
    priority: int = 0                   # 优先级（越大越先匹配）
    enabled: bool = True
    custom_fn: Optional[Callable] = None


class MaskingRuleEngine:
    """脱敏规则引擎"""

    def __init__(self):
        self._rules: List[MaskingRule] = []
        self._lock = threading.Lock()
        self._stats = {
            'total_masks': 0,
            'by_rule': {},
        }
        self._load_defaults()

    def _load_defaults(self):
        """加载默认规则"""
        defaults = [
            MaskingRule(
                name='password',
                strategy=MaskStrategy.FULL_MASK,
                field_names=['password', 'passwd', 'pwd', 'secret', 'token', 'api_key', 'api_secret'],
                priority=100,
            ),
            MaskingRule(
                name='credit_card',
                strategy=MaskStrategy.MASK_MIDDLE,
                pattern=r'\b(?:\d{4}[\s-]?){3}\d{4}\b',
                keep_prefix=4,
                keep_suffix=4,
                priority=90,
            ),
            MaskingRule(
                name='phone',
                strategy=MaskStrategy.MASK_MIDDLE,
                pattern=r'\b1[3-9]\d{9}\b',
                keep_prefix=3,
                keep_suffix=4,
                priority=80,
            ),
            MaskingRule(
                name='email',
                strategy=MaskStrategy.MASK_START,
                pattern=r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
                keep_suffix=0,
                replacement=None,
                priority=70,
            ),
            MaskingRule(
                name='id_card',
                strategy=MaskStrategy.MASK_MIDDLE,
                pattern=r'\b\d{17}[\dXx]\b',
                keep_prefix=3,
                keep_suffix=4,
                priority=85,
            ),
            MaskingRule(
                name='ip_private',
                strategy=MaskStrategy.MASK_END,
                pattern=r'\b(?:10|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b',
                keep_prefix=7,
                priority=50,
            ),
        ]
        self._rules.extend(defaults)

    def add_rule(self, name: str, strategy: str = 'full_mask', **kwargs) -> MaskingRule:
        """添加规则"""
        rule = MaskingRule(
            name=name,
            strategy=MaskStrategy(strategy),
            **kwargs,
        )
        with self._lock:
            self._rules.append(rule)
            self._rules.sort(key=lambda r: r.priority, reverse=True)
        logger.info(f"添加脱敏规则: {name} ({strategy})")
        return rule

    def remove_rule(self, name: str) -> bool:
        """移除规则"""
        with self._lock:
            before = len(self._rules)
            self._rules = [r for r in self._rules if r.name != name]
            return len(self._rules) < before

    def mask_text(self, text: str) -> str:
        """对文本进行脱敏"""
        if not text or not isinstance(text, str):
            return text

        result = text
        with self._lock:
            rules = [r for r in self._rules if r.enabled]

        for rule in rules:
            if rule.pattern:
                result = self._apply_pattern_rule(result, rule)
            elif rule.keywords:
                result = self._apply_keyword_rule(result, rule)

        return result

    def mask_dict(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """对字典进行脱敏"""
        if not isinstance(data, dict):
            return data

        result = {}
        with self._lock:
            field_rules = [r for r in self._rules if r.enabled and r.field_names]

        for key, value in data.items():
            masked = False
            for rule in field_rules:
                if key.lower() in [f.lower() for f in rule.field_names]:
                    result[key] = self._apply_strategy(str(value), rule)
                    masked = True
                    self._record_mask(rule.name)
                    break

            if not masked:
                if isinstance(value, str):
                    result[key] = self.mask_text(value)
                elif isinstance(value, dict):
                    result[key] = self.mask_dict(value)
                elif isinstance(value, list):
                    result[key] = [self.mask_dict(i) if isinstance(i, dict) else self.mask_text(str(i)) if isinstance(i, str) else i for i in value]
                else:
                    result[key] = value

        return result

    def _apply_pattern_rule(self, text: str, rule: MaskingRule) -> str:
        """应用正则规则"""
        try:
            def replacer(match):
                self._record_mask(rule.name)
                return self._apply_strategy(match.group(), rule)
            return re.sub(rule.pattern, replacer, text)
        except re.error:
            return text

    def _apply_keyword_rule(self, text: str, rule: MaskingRule) -> str:
        """应用关键字规则"""
        result = text
        for keyword in rule.keywords:
            if keyword.lower() in result.lower():
                result = result.replace(keyword, rule.replacement or '***')
                self._record_mask(rule.name)
        return result

    def _apply_strategy(self, value: str, rule: MaskingRule) -> str:
        """应用脱敏策略"""
        if rule.strategy == MaskStrategy.FULL_MASK:
            return rule.mask_char * 3
        elif rule.strategy == MaskStrategy.MASK_MIDDLE:
            prefix = value[:rule.keep_prefix]
            suffix = value[-rule.keep_suffix:] if rule.keep_suffix else ''
            middle_len = len(value) - rule.keep_prefix - rule.keep_suffix
            if middle_len <= 0:
                return value
            return prefix + rule.mask_char * min(middle_len, 6) + suffix
        elif rule.strategy == MaskStrategy.MASK_END:
            prefix = value[:rule.keep_prefix]
            return prefix + rule.mask_char * 4
        elif rule.strategy == MaskStrategy.MASK_START:
            suffix = value[-rule.keep_suffix:] if rule.keep_suffix else ''
            return rule.mask_char * 4 + suffix
        elif rule.strategy == MaskStrategy.HASH:
            import hashlib
            return hashlib.sha256(value.encode()).hexdigest()[:12]
        elif rule.strategy == MaskStrategy.REDACT:
            return rule.replacement or '[REDACTED]'
        elif rule.strategy == MaskStrategy.TRUNCATE:
            return value[:rule.keep_prefix] + '...'
        elif rule.strategy == MaskStrategy.CUSTOM and rule.custom_fn:
            return rule.custom_fn(value)
        return value

    def _record_mask(self, rule_name: str):
        """记录脱敏统计"""
        with self._lock:
            self._stats['total_masks'] += 1
            self._stats['by_rule'][rule_name] = self._stats['by_rule'].get(rule_name, 0) + 1

    def get_rules(self) -> List[Dict[str, Any]]:
        """获取所有规则"""
        with self._lock:
            return [{
                'name': r.name,
                'strategy': r.strategy.value,
                'pattern': r.pattern,
                'field_names': r.field_names,
                'keywords': r.keywords,
                'priority': r.priority,
                'enabled': r.enabled,
            } for r in self._rules]

    def get_stats(self) -> Dict[str, Any]:
        """获取统计"""
        with self._lock:
            return dict(self._stats)

    def reset_stats(self):
        """重置统计"""
        with self._lock:
            self._stats = {'total_masks': 0, 'by_rule': {}}


# 全局实例
masking_engine = MaskingRuleEngine()
