"""
能耗优化建议模块
基于历史数据的能耗分析和优化建议

功能：
- 能耗模式分析
- 峰谷电价优化
- 节能建议生成
- 能效指标计算
"""

import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from collections import defaultdict

logger = logging.getLogger(__name__)


class EnergyRecord:
    """能耗记录"""

    def __init__(self, device_id: str, timestamp: float, energy_kwh: float,
                 power_kw: float = 0.0, cost_yuan: float = 0.0):
        self.device_id = device_id
        self.timestamp = timestamp
        self.energy_kwh = energy_kwh
        self.power_kw = power_kw
        self.cost_yuan = cost_yuan


class EnergyAnalyzer:
    """能耗分析器"""

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.records: List[EnergyRecord] = []
        self._lock = threading.Lock()

        # 电价配置（元/kWh）
        self.tariff = {
            'peak': self.config.get('peak_price', 1.2),    # 峰时
            'flat': self.config.get('flat_price', 0.8),     # 平时
            'valley': self.config.get('valley_price', 0.4), # 谷时
        }

        # 峰谷时段配置（24小时制）
        self.peak_hours = self.config.get('peak_hours', [8, 9, 10, 11, 18, 19, 20, 21])
        self.valley_hours = self.config.get('valley_hours', [0, 1, 2, 3, 4, 5, 22, 23])

        # 碳排放因子（kg CO2/kWh）
        self.carbon_factor = self.config.get('carbon_factor', 0.5)

    def add_record(self, device_id: str, timestamp: float, energy_kwh: float,
                   power_kw: float = 0.0):
        """添加能耗记录"""
        # 计算电费
        hour = datetime.fromtimestamp(timestamp).hour
        if hour in self.peak_hours:
            price = self.tariff['peak']
        elif hour in self.valley_hours:
            price = self.tariff['valley']
        else:
            price = self.tariff['flat']

        cost = energy_kwh * price

        with self._lock:
            self.records.append(EnergyRecord(
                device_id=device_id,
                timestamp=timestamp,
                energy_kwh=energy_kwh,
                power_kw=power_kw,
                cost_yuan=cost,
            ))

            # 只保留最近100万条记录
            if len(self.records) > 1000000:
                self.records = self.records[-1000000:]

    def get_device_consumption(self, device_id: str,
                               start_time: float = None,
                               end_time: float = None) -> Dict[str, Any]:
        """获取设备能耗统计"""
        with self._lock:
            filtered = [r for r in self.records if r.device_id == device_id]

            if start_time:
                filtered = [r for r in filtered if r.timestamp >= start_time]
            if end_time:
                filtered = [r for r in filtered if r.timestamp <= end_time]

        if not filtered:
            return {
                'device_id': device_id,
                'total_energy_kwh': 0,
                'total_cost_yuan': 0,
                'avg_power_kw': 0,
                'peak_energy_kwh': 0,
                'flat_energy_kwh': 0,
                'valley_energy_kwh': 0,
            }

        total_energy = sum(r.energy_kwh for r in filtered)
        total_cost = sum(r.cost_yuan for r in filtered)
        avg_power = sum(r.power_kw for r in filtered) / len(filtered)

        # 按时段统计
        peak_energy = 0
        flat_energy = 0
        valley_energy = 0

        for r in filtered:
            hour = datetime.fromtimestamp(r.timestamp).hour
            if hour in self.peak_hours:
                peak_energy += r.energy_kwh
            elif hour in self.valley_hours:
                valley_energy += r.energy_kwh
            else:
                flat_energy += r.energy_kwh

        return {
            'device_id': device_id,
            'total_energy_kwh': round(total_energy, 2),
            'total_cost_yuan': round(total_cost, 2),
            'avg_power_kw': round(avg_power, 2),
            'peak_energy_kwh': round(peak_energy, 2),
            'flat_energy_kwh': round(flat_energy, 2),
            'valley_energy_kwh': round(valley_energy, 2),
            'carbon_kg': round(total_energy * self.carbon_factor, 2),
        }

    def get_total_consumption(self, start_time: float = None,
                             end_time: float = None) -> Dict[str, Any]:
        """获取总能耗统计"""
        with self._lock:
            filtered = self.records

            if start_time:
                filtered = [r for r in filtered if r.timestamp >= start_time]
            if end_time:
                filtered = [r for r in filtered if r.timestamp <= end_time]

        if not filtered:
            return {
                'total_energy_kwh': 0,
                'total_cost_yuan': 0,
                'device_count': 0,
            }

        # 按设备统计
        device_energy: Dict[str, float] = defaultdict(float)
        for r in filtered:
            device_energy[r.device_id] += r.energy_kwh

        total_energy = sum(r.energy_kwh for r in filtered)
        total_cost = sum(r.cost_yuan for r in filtered)

        return {
            'total_energy_kwh': round(total_energy, 2),
            'total_cost_yuan': round(total_cost, 2),
            'device_count': len(device_energy),
            'top_consumers': sorted(
                [{'device_id': k, 'energy_kwh': round(v, 2)}
                 for k, v in device_energy.items()],
                key=lambda x: x['energy_kwh'],
                reverse=True
            )[:10],
            'carbon_kg': round(total_energy * self.carbon_factor, 2),
        }

    def analyze_peak_valley(self, device_id: str = None) -> Dict[str, Any]:
        """分析峰谷用电情况"""
        with self._lock:
            if device_id:
                records = [r for r in self.records if r.device_id == device_id]
            else:
                records = self.records

        if not records:
            return {'analysis': '无数据'}

        peak_energy = 0
        flat_energy = 0
        valley_energy = 0

        for r in records:
            hour = datetime.fromtimestamp(r.timestamp).hour
            if hour in self.peak_hours:
                peak_energy += r.energy_kwh
            elif hour in self.valley_hours:
                valley_energy += r.energy_kwh
            else:
                flat_energy += r.energy_kwh

        total = peak_energy + flat_energy + valley_energy

        if total == 0:
            return {'analysis': '无能耗数据'}

        peak_ratio = peak_energy / total * 100
        valley_ratio = valley_energy / total * 100

        # 计算如果将峰时用电转移到谷时可节省的费用
        potential_saving = peak_energy * (self.tariff['peak'] - self.tariff['valley'])

        return {
            'peak_energy_kwh': round(peak_energy, 2),
            'flat_energy_kwh': round(flat_energy, 2),
            'valley_energy_kwh': round(valley_energy, 2),
            'peak_ratio': round(peak_ratio, 1),
            'valley_ratio': round(valley_ratio, 1),
            'potential_saving_yuan': round(potential_saving, 2),
            'recommendation': self._generate_peak_valley_recommendation(
                peak_ratio, valley_ratio, potential_saving
            ),
        }

    def _generate_peak_valley_recommendation(self, peak_ratio: float,
                                            valley_ratio: float,
                                            potential_saving: float) -> str:
        """生成峰谷优化建议"""
        if peak_ratio > 60:
            return (
                f"峰时用电占比{peak_ratio:.1f}%过高。"
                f"建议将可调度负荷移至谷时，预计可节省{potential_saving:.0f}元/月。"
            )
        elif peak_ratio > 40:
            return (
                f"峰时用电占比{peak_ratio:.1f}%，有优化空间。"
                f"建议优化生产排班，增加谷时用电比例。"
            )
        else:
            return "峰谷用电比例合理，继续优化。"


class EnergyOptimizer:
    """能耗优化器"""

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.analyzer = EnergyAnalyzer(config)
        self._lock = threading.Lock()

        # 优化建议历史
        self.suggestions: List[Dict[str, Any]] = []

    def update_config(self, config: Dict[str, Any]):
        """更新配置"""
        with self._lock:
            self.config.update(config)
            if 'peak_price' in config:
                self.analyzer.tariff['peak'] = config['peak_price']
            if 'flat_price' in config:
                self.analyzer.tariff['flat'] = config['flat_price']
            if 'valley_price' in config:
                self.analyzer.tariff['valley'] = config['valley_price']

    def add_energy_data(self, device_id: str, timestamp: float,
                       energy_kwh: float, power_kw: float = 0.0):
        """添加能耗数据"""
        self.analyzer.add_record(device_id, timestamp, energy_kwh, power_kw)

    def generate_suggestions(self, device_id: str = None) -> List[Dict[str, Any]]:
        """生成优化建议"""
        suggestions = []

        # 1. 峰谷优化建议
        peak_valley = self.analyzer.analyze_peak_valley(device_id)
        if peak_valley.get('potential_saving_yuan', 0) > 100:
            suggestions.append({
                'type': 'peak_valley',
                'priority': 'high',
                'title': '峰谷电价优化',
                'description': peak_valley['recommendation'],
                'potential_saving': peak_valley['potential_saving_yuan'],
            })

        # 2. 设备能效建议
        if device_id:
            consumption = self.analyzer.get_device_consumption(device_id)
            if consumption['avg_power_kw'] > 50:  # 高功率设备
                suggestions.append({
                    'type': 'efficiency',
                    'priority': 'medium',
                    'title': '设备能效检查',
                    'description': f"设备平均功率{consumption['avg_power_kw']:.1f}kW，建议检查设备能效。",
                    'potential_saving': 0,
                })

        # 3. 碳排放建议
        total = self.analyzer.get_total_consumption()
        if total['carbon_kg'] > 1000:
            suggestions.append({
                'type': 'carbon',
                'priority': 'low',
                'title': '碳排放管理',
                'description': f"本月碳排放{total['carbon_kg']:.0f}kg，建议关注节能减排。",
                'potential_saving': 0,
            })

        # 记录建议
        with self._lock:
            for s in suggestions:
                s['timestamp'] = time.time()
                self.suggestions.append(s)

            # 只保留最近1000条建议
            if len(self.suggestions) > 1000:
                self.suggestions = self.suggestions[-1000:]

        return suggestions

    def get_energy_report(self, days: int = 30) -> Dict[str, Any]:
        """生成能耗报告"""
        end_time = time.time()
        start_time = end_time - (days * 86400)

        total = self.analyzer.get_total_consumption(start_time, end_time)
        peak_valley = self.analyzer.analyze_peak_valley()

        return {
            'period_days': days,
            'total_energy_kwh': total['total_energy_kwh'],
            'total_cost_yuan': total['total_cost_yuan'],
            'carbon_kg': total['carbon_kg'],
            'device_count': total['device_count'],
            'top_consumers': total['top_consumers'],
            'peak_valley_analysis': peak_valley,
            'suggestions': self.generate_suggestions(),
        }
