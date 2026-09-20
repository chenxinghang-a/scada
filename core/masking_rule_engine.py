"""
动态脱敏规则引擎
运行时可配置的脱敏规则，支持正则/关键字/字段名/自定义函数。

使用方式:
    from core.masking_rule_engine import MaskingRuleEngine
    engine = MaskingRuleEngine()
    engine.add_rule('credit_card', pattern=r'\\d{4}[\\s-]?\\d{4}', strategy='mask_middle')
    masked = engine.mask_text('我的卡号是 1234 5678 9012 3456')
"""

# ============================================================================
# 接线状态：未接线（WIRED = False）
# ============================================================================
# 本模块在生产代码（run.py / 各业务层 / 其它 core 模块）中**没有任何 import 引用**。
# 模块本身可用，但当前没有调用方 —— 也就是说它宣称的这项能力**当前并未生效**。
#
# 为什么保留而不删除：删掉即丢能力，模块本身有测试价值；这里只把「没接线」显式化、
# 可追踪，避免「代码在库里」被误读成「功能在跑」。
#
# 自动化复核（防止本标注过期）：
#   tests/test_core_regressions.py::test_unwired_marker_matches_reality
#   —— 该用例用 AST 扫描全仓库 import。一旦有人把本模块接进生产代码，
#      而这里仍写着 WIRED = False，用例即失败，强制文档与事实同步。
#
# 接线建议（需改 run.py / 各层，core 内部无权自行接线）：
#     在日志 filter 与 API 响应出口接入 MaskingRuleEngine 做动态脱敏。
# ============================================================================
WIRED = False


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

    #: 规则执行失败时的整段替换文本（fail-closed，杜绝明文外泄）
    FAILSAFE_MASK = '[REDACTED]'

    def __init__(self):
        self._rules: List[MaskingRule] = []
        self._lock = threading.Lock()
        self._stats = {
            'total_masks': 0,
            'by_rule': {},
            'rule_errors': 0,
        }
        self._rule_errors = 0
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
        """添加规则

        Raises:
            ValueError: 新增规则（含默认规则）的 pattern 或 keywords 无法作为合法正则
                编译时抛出。脱敏是安全控制，**必须在配置那一刻**就把坏正则拦住 ——
                旧实现不校验，坏正则要等到运行时 `re.sub` 才抛 `re.error`，
                而那时 except 分支直接把**未脱敏的原文**返回了（见 _apply_pattern_rule）。
        """
        rule = MaskingRule(
            name=name,
            strategy=MaskStrategy(strategy),
            **kwargs,
        )
        # 提前编译校验：坏正则在这里就被拒绝，不会带着"脱敏其实没生效"上线
        for attr in ('pattern',):
            pat = getattr(rule, attr)
            if pat:
                try:
                    re.compile(pat)
                except re.error as e:
                    raise ValueError(
                        f"脱敏规则 {name!r} 的 {attr} 不是合法正则: {e}") from e
        for kw in (rule.keywords or []):
            try:
                re.compile(re.escape(kw))
            except re.error as e:  # pragma: no cover - escape 后不应失败
                raise ValueError(f"脱敏规则 {name!r} 的关键字非法: {e}") from e

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
        """应用正则规则

        正则出错时必须 **fail-closed**：脱敏是安全控制，旧实现 `except re.error:
        return text` 会原样返回**未脱敏**的文本 —— 日志/响应里直接漏出密码、卡号，
        而且没有任何日志或计数，看起来"脱敏正常工作"。
        """
        try:
            def replacer(match):
                self._record_mask(rule.name)
                return self._apply_strategy(match.group(), rule)
            return re.sub(rule.pattern, replacer, text)
        except re.error as e:
            self._rule_errors += 1
            self._stats['rule_errors'] = self._rule_errors
            logger.error(
                "脱敏规则 %s 的正则执行失败，已整段遮蔽以避免明文泄漏"
                "（累计 %d 次）: pattern=%r: %s",
                rule.name, self._rule_errors, rule.pattern, e)
            # 整段遮蔽：宁可过度遮蔽，也不能把敏感原文漏出去
            return self.FAILSAFE_MASK

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
        """重置统计（保留 rule_errors 键，避免调用方按固定结构读时 KeyError）"""
        with self._lock:
            self._stats = {'total_masks': 0, 'by_rule': {}, 'rule_errors': 0}
            self._rule_errors = 0


# 全局实例
masking_engine = MaskingRuleEngine()
