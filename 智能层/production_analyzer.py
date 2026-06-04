"""
生产效率分析模块
基于OEE和生产数据的效率分析和优化建议

功能：
- OEE趋势分析
- 生产瓶颈识别
- 效率优化建议
- 产能规划
"""

import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from collections import defaultdict

logger = logging.getLogger(__name__)


class ProductionRecord:
    """生产记录"""

    def __init__(self, device_id: str, timestamp: float,
                 planned_quantity: float, actual_quantity: float,
                 good_quantity: float, downtime_minutes: float = 0.0):
        self.device_id = device_id
        self.timestamp = timestamp
        self.planned_quantity = planned_quantity
        self.actual_quantity = actual_quantity
        self.good_quantity = good_quantity
        self.downtime_minutes = downtime_minutes


class ProductionAnalyzer:
    """生产效率分析器"""

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.records: List[ProductionRecord] = []
        self._lock = threading.Lock()

        # OEE目标
        self.oee_targets = {
            'availability': self.config.get('target_availability', 0.90),
            'performance': self.config.get('target_performance', 0.95),
            'quality': self.config.get('target_quality', 0.99),
            'overall': self.config.get('target_oee', 0.85),
        }

    def add_record(self, device_id: str, timestamp: float,
                   planned_quantity: float, actual_quantity: float,
                   good_quantity: float, downtime_minutes: float = 0.0):
        """添加生产记录"""
        with self._lock:
            self.records.append(ProductionRecord(
                device_id=device_id,
                timestamp=timestamp,
                planned_quantity=planned_quantity,
                actual_quantity=actual_quantity,
                good_quantity=good_quantity,
                downtime_minutes=downtime_minutes,
            ))

            # 只保留最近10万条记录
            if len(self.records) > 100000:
                self.records = self.records[-100000:]

    def calculate_oee(self, device_id: str = None,
                     start_time: float = None,
                     end_time: float = None) -> Dict[str, Any]:
        """计算OEE（设备综合效率）

        OEE = 可用性 × 性能 × 质量

        可用性 = (计划运行时间 - 停机时间) / 计划运行时间
        性能 = 实际产量 / 理论产量
        质量 = 合格品数量 / 实际产量
        """
        with self._lock:
            filtered = self.records

            if device_id:
                filtered = [r for r in filtered if r.device_id == device_id]
            if start_time:
                filtered = [r for r in filtered if r.timestamp >= start_time]
            if end_time:
                filtered = [r for r in filtered if r.timestamp <= end_time]

        if not filtered:
            return {
                'availability': 0,
                'performance': 0,
                'quality': 0,
                'oee': 0,
                'record_count': 0,
            }

        # 计算各项指标
        total_planned = sum(r.planned_quantity for r in filtered)
        total_actual = sum(r.actual_quantity for r in filtered)
        total_good = sum(r.good_quantity for r in filtered)
        total_downtime = sum(r.downtime_minutes for r in filtered)

        # 假设每个记录代表1小时的生产
        planned_hours = len(filtered)
        actual_hours = planned_hours - (total_downtime / 60)

        # 可用性
        availability = actual_hours / planned_hours if planned_hours > 0 else 0

        # 性能
        performance = total_actual / total_planned if total_planned > 0 else 0

        # 质量
        quality = total_good / total_actual if total_actual > 0 else 0

        # OEE
        oee = availability * performance * quality

        return {
            'availability': round(availability, 4),
            'performance': round(performance, 4),
            'quality': round(quality, 4),
            'oee': round(oee, 4),
            'record_count': len(filtered),
            'total_planned': round(total_planned, 2),
            'total_actual': round(total_actual, 2),
            'total_good': round(total_good, 2),
            'total_downtime_hours': round(total_downtime / 60, 2),
        }

    def get_oee_trend(self, device_id: str = None, days: int = 30) -> List[Dict[str, Any]]:
        """获取OEE趋势"""
        end_time = time.time()
        start_time = end_time - (days * 86400)

        trend = []
        current = start_time

        while current < end_time:
            day_end = current + 86400
            oee = self.calculate_oee(device_id, current, day_end)

            trend.append({
                'date': datetime.fromtimestamp(current).strftime('%Y-%m-%d'),
                'oee': oee['oee'],
                'availability': oee['availability'],
                'performance': oee['performance'],
                'quality': oee['quality'],
            })

            current = day_end

        return trend

    def identify_bottlenecks(self, device_id: str = None) -> List[Dict[str, Any]]:
        """识别生产瓶颈"""
        oee = self.calculate_oee(device_id)
        bottlenecks = []

        # 可用性瓶颈
        if oee['availability'] < self.oee_targets['availability']:
            gap = self.oee_targets['availability'] - oee['availability']
            bottlenecks.append({
                'type': 'availability',
                'current': oee['availability'],
                'target': self.oee_targets['availability'],
                'gap': round(gap, 4),
                'impact': round(gap * oee['performance'] * oee['quality'], 4),
                'recommendation': '减少停机时间，优化设备维护计划',
            })

        # 性能瓶颈
        if oee['performance'] < self.oee_targets['performance']:
            gap = self.oee_targets['performance'] - oee['performance']
            bottlenecks.append({
                'type': 'performance',
                'current': oee['performance'],
                'target': self.oee_targets['performance'],
                'gap': round(gap, 4),
                'impact': round(oee['availability'] * gap * oee['quality'], 4),
                'recommendation': '提高设备运行速度，减少小停机',
            })

        # 质量瓶颈
        if oee['quality'] < self.oee_targets['quality']:
            gap = self.oee_targets['quality'] - oee['quality']
            bottlenecks.append({
                'type': 'quality',
                'current': oee['quality'],
                'target': self.oee_targets['quality'],
                'gap': round(gap, 4),
                'impact': round(oee['availability'] * oee['performance'] * gap, 4),
                'recommendation': '减少废品率，优化工艺参数',
            })

        # 按影响排序
        bottlenecks.sort(key=lambda x: x['impact'], reverse=True)

        return bottlenecks

    def generate_optimization_suggestions(self, device_id: str = None) -> List[Dict[str, Any]]:
        """生成优化建议"""
        suggestions = []

        # 获取OEE数据
        oee = self.calculate_oee(device_id)
        bottlenecks = self.identify_bottlenecks(device_id)

        # 基于瓶颈的建议
        for bottleneck in bottlenecks:
            suggestions.append({
                'type': f'oee_{bottleneck["type"]}',
                'priority': 'high' if bottleneck['impact'] > 0.1 else 'medium',
                'title': f'{bottleneck["type"].title()}优化',
                'description': bottleneck['recommendation'],
                'current_value': bottleneck['current'],
                'target_value': bottleneck['target'],
                'potential_improvement': round(bottleneck['impact'] * 100, 1),
            })

        # 停机分析
        if oee['total_downtime_hours'] > 0:
            suggestions.append({
                'type': 'downtime',
                'priority': 'high' if oee['total_downtime_hours'] > 10 else 'medium',
                'title': '停机时间优化',
                'description': f"总停机时间{oee['total_downtime_hours']:.1f}小时，建议分析停机原因并优化。",
                'current_value': oee['total_downtime_hours'],
                'target_value': 0,
                'potential_improvement': 0,
            })

        # 产能规划建议
        if oee['oee'] > 0.8:
            suggestions.append({
                'type': 'capacity',
                'priority': 'low',
                'title': '产能扩展',
                'description': 'OEE已达到80%以上，可考虑产能扩展。',
                'current_value': oee['oee'],
                'target_value': 0.85,
                'potential_improvement': 0,
            })

        return suggestions

    def get_production_summary(self, days: int = 30) -> Dict[str, Any]:
        """获取生产摘要"""
        end_time = time.time()
        start_time = end_time - (days * 86400)

        oee = self.calculate_oee(start_time=start_time, end_time=end_time)
        trend = self.get_oee_trend(days=days)
        bottlenecks = self.identify_bottlenecks()

        return {
            'period_days': days,
            'oee': oee,
            'trend': trend,
            'bottlenecks': bottlenecks,
            'suggestions': self.generate_optimization_suggestions(),
        }
