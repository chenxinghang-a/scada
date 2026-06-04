"""
数据质量监控模块
监控SCADA系统数据质量，检测异常值、缺失值、重复值

功能：
- 数据完整性检查
- 异常值检测
- 数据一致性验证
- 质量报告生成
"""

import time
import logging
import threading
import math
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from collections import defaultdict

logger = logging.getLogger(__name__)


class DataQualityRule:
    """数据质量规则"""

    def __init__(self, rule_id: str, name: str, rule_type: str, config: Dict[str, Any] = None):
        self.rule_id = rule_id
        self.name = name
        self.rule_type = rule_type  # range, consistency, completeness, timeliness
        self.config = config or {}
        self.enabled = True

    def validate(self, value: float, metadata: Dict[str, Any] = None) -> Tuple[bool, str]:
        """验证数据质量

        Returns:
            (passed, message)
        """
        if not self.enabled:
            return True, "规则已禁用"

        if self.rule_type == 'range':
            return self._validate_range(value)
        elif self.rule_type == 'consistency':
            return self._validate_consistency(value, metadata)
        elif self.rule_type == 'completeness':
            return self._validate_completeness(metadata)
        elif self.rule_type == 'timeliness':
            return self._validate_timeliness(metadata)
        else:
            return True, "未知规则类型"

    def _validate_range(self, value: float) -> Tuple[bool, str]:
        """范围检查"""
        min_val = self.config.get('min')
        max_val = self.config.get('max')

        if min_val is not None and value < min_val:
            return False, f"值 {value} 低于最小值 {min_val}"
        if max_val is not None and value > max_val:
            return False, f"值 {value} 超过最大值 {max_val}"

        return True, "范围检查通过"

    def _validate_consistency(self, value: float, metadata: Dict[str, Any] = None) -> Tuple[bool, str]:
        """一致性检查（与历史值比较）"""
        if not metadata:
            return True, "无历史数据"

        last_value = metadata.get('last_value')
        max_change = self.config.get('max_change_rate', 0.5)  # 最大变化率50%

        if last_value is not None and last_value != 0:
            change_rate = abs(value - last_value) / abs(last_value)
            if change_rate > max_change:
                return False, f"值变化率 {change_rate:.1%} 超过阈值 {max_change:.1%}"

        return True, "一致性检查通过"

    def _validate_completeness(self, metadata: Dict[str, Any] = None) -> Tuple[bool, str]:
        """完整性检查"""
        if not metadata:
            return True, "无元数据"

        expected_interval = self.config.get('expected_interval', 60)  # 预期间隔（秒）
        last_timestamp = metadata.get('last_timestamp')

        if last_timestamp:
            elapsed = time.time() - last_timestamp
            if elapsed > expected_interval * 3:  # 超过3倍间隔
                return False, f"数据缺失 {elapsed:.0f} 秒"

        return True, "完整性检查通过"

    def _validate_timeliness(self, metadata: Dict[str, Any] = None) -> Tuple[bool, str]:
        """时效性检查"""
        if not metadata:
            return True, "无元数据"

        max_delay = self.config.get('max_delay', 300)  # 最大延迟（秒）
        timestamp = metadata.get('timestamp')

        if timestamp:
            delay = time.time() - timestamp
            if delay > max_delay:
                return False, f"数据延迟 {delay:.0f} 秒超过阈值 {max_delay} 秒"

        return True, "时效性检查通过"


class DataQualityMonitor:
    """数据质量监控器"""

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.rules: Dict[str, DataQualityRule] = {}
        self._lock = threading.Lock()

        # 质量统计
        self.stats: Dict[str, Dict[str, int]] = defaultdict(lambda: {
            'total': 0, 'passed': 0, 'failed': 0
        })

        # 历史值缓存（用于一致性检查）
        self._history_cache: Dict[str, Dict[str, Any]] = {}

        # 默认规则
        self._init_default_rules()

    def _init_default_rules(self):
        """初始化默认规则"""
        # 温度范围规则
        self.add_rule(DataQualityRule(
            'temp_range', '温度范围检查', 'range',
            {'min': -50, 'max': 200}
        ))

        # 压力范围规则
        self.add_rule(DataQualityRule(
            'pressure_range', '压力范围检查', 'range',
            {'min': 0, 'max': 50}
        ))

        # 一致性规则
        self.add_rule(DataQualityRule(
            'value_consistency', '值一致性检查', 'consistency',
            {'max_change_rate': 0.5}
        ))

        # 时效性规则
        self.add_rule(DataQualityRule(
            'data_timeliness', '数据时效性检查', 'timeliness',
            {'max_delay': 300}
        ))

    def add_rule(self, rule: DataQualityRule):
        """添加规则"""
        with self._lock:
            self.rules[rule.rule_id] = rule

    def remove_rule(self, rule_id: str):
        """移除规则"""
        with self._lock:
            if rule_id in self.rules:
                del self.rules[rule_id]

    def validate_data(self, device_id: str, register_name: str,
                     value: float, timestamp: float = None) -> Dict[str, Any]:
        """验证数据质量

        Returns:
            {
                'passed': bool,
                'checks': [{'rule': str, 'passed': bool, 'message': str}],
                'quality_score': float  # 0-100
            }
        """
        timestamp = timestamp or time.time()
        cache_key = f"{device_id}:{register_name}"

        # 获取历史元数据
        with self._lock:
            metadata = self._history_cache.get(cache_key, {})
            metadata['timestamp'] = timestamp
            metadata['current_value'] = value

        checks = []
        passed_count = 0
        total_count = 0

        with self._lock:
            for rule_id, rule in self.rules.items():
                if not rule.enabled:
                    continue

                total_count += 1
                passed, message = rule.validate(value, metadata)

                checks.append({
                    'rule': rule_id,
                    'rule_name': rule.name,
                    'passed': passed,
                    'message': message,
                })

                if passed:
                    passed_count += 1

                # 更新统计
                self.stats[cache_key]['total'] += 1
                if passed:
                    self.stats[cache_key]['passed'] += 1
                else:
                    self.stats[cache_key]['failed'] += 1

        # 更新历史缓存
        with self._lock:
            self._history_cache[cache_key] = {
                'last_value': value,
                'last_timestamp': timestamp,
            }

        # 计算质量分数
        quality_score = (passed_count / total_count * 100) if total_count > 0 else 100

        return {
            'passed': passed_count == total_count,
            'checks': checks,
            'quality_score': round(quality_score, 1),
        }

    def get_statistics(self, device_id: str = None) -> Dict[str, Any]:
        """获取质量统计"""
        with self._lock:
            if device_id:
                # 返回指定设备的统计
                device_stats = {}
                for key, stats in self.stats.items():
                    if key.startswith(device_id + ':'):
                        register = key.split(':')[1]
                        device_stats[register] = stats
                return device_stats
            else:
                # 返回总体统计
                total = sum(s['total'] for s in self.stats.values())
                passed = sum(s['passed'] for s in self.stats.values())
                failed = sum(s['failed'] for s in self.stats.values())

                return {
                    'total_checks': total,
                    'passed': passed,
                    'failed': failed,
                    'pass_rate': round(passed / total * 100, 1) if total > 0 else 100,
                    'devices': len(set(k.split(':')[0] for k in self.stats.keys())),
                }

    def generate_report(self) -> Dict[str, Any]:
        """生成质量报告"""
        stats = self.get_statistics()

        # 找出质量最差的设备
        worst_devices = []
        device_scores = defaultdict(lambda: {'total': 0, 'passed': 0})

        for key, stat in self.stats.items():
            device_id = key.split(':')[0]
            device_scores[device_id]['total'] += stat['total']
            device_scores[device_id]['passed'] += stat['passed']

        for device_id, scores in device_scores.items():
            if scores['total'] > 0:
                pass_rate = scores['passed'] / scores['total'] * 100
                worst_devices.append({
                    'device_id': device_id,
                    'pass_rate': round(pass_rate, 1),
                    'total_checks': scores['total'],
                })

        worst_devices.sort(key=lambda x: x['pass_rate'])

        return {
            'timestamp': datetime.now().isoformat(),
            'summary': stats,
            'worst_devices': worst_devices[:10],
            'rules_count': len(self.rules),
            'enabled_rules': sum(1 for r in self.rules.values() if r.enabled),
        }

    def cleanup_cache(self, max_age_seconds: int = 3600):
        """清理过期的历史缓存"""
        with self._lock:
            now = time.time()
            expired = [
                key for key, data in self._history_cache.items()
                if now - data.get('last_timestamp', 0) > max_age_seconds
            ]
            for key in expired:
                del self._history_cache[key]

            if expired:
                logger.info(f"清理了 {len(expired)} 个过期的历史缓存")
