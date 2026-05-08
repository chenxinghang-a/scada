"""
能源管理模块 (Energy Management)
==================================
工业4.0绿色制造核心：能源消耗监控、分析与优化

功能：
1. 实时能耗监控 — 电力/水/气/蒸汽分项计量
2. 能耗统计 — 班次/日/周/月能耗汇总
3. 峰谷平电价分析 — 分时电价成本核算
4. 碳排放计算 — 基于国家排放因子
5. 能效指标 — 单位产品能耗、万元产值能耗
6. 能耗异常检测 — 突增/泄漏预警
"""

import logging
import math
import threading
import time
from datetime import datetime, timedelta
from typing import Any
from collections import defaultdict

logger = logging.getLogger(__name__)


class EnergyManager:
    """
    能源管理引擎
    
    数据来源：
    - 电力仪表（功率、电量、功率因数）
    - 水表/气表/蒸汽表（流量、累积量）
    - 设备运行状态（关联能耗与产出）
    """
    
    def __init__(self, database, config: dict[str, Any] | None = None):
        """
        Args:
            database: Database实例
            config: 配置字典
        """
        self.database = database
        self.config = config or {}
        
        # 分时电价配置 (元/kWh)
        self.tariff = self.config.get('tariff', {
            'peak': 1.2,      # 峰时电价 (8:00-11:00, 18:00-23:00)
            'flat': 0.7,       # 平时电价 (7:00-8:00, 11:00-18:00)
            'valley': 0.35,    # 谷时电价 (23:00-7:00)
        })
        
        # 峰谷平时段定义 (小时)
        self.tariff_periods = self.config.get('tariff_periods', {
            'peak': [(8, 11), (18, 23)],
            'valley': [(0, 7), (23, 24)],
            # 其余为平
        })
        
        # 碳排放因子 (kgCO2/kWh)
        # 中国电网平均排放因子约0.5810 tCO2/MWh (2023年)
        self.carbon_factor = self.config.get('carbon_factor', 0.581)
        
        # 实时功率数据
        # device_id -> {'power_kw': float, 'timestamp': datetime}
        self.realtime_power: dict[str, dict[str, Any]] = {}
        
        # 能耗累积数据
        # device_id -> {'energy_kwh': float, 'water_m3': float, 'gas_m3': float, ...}
        self.energy_accumulated: dict[str, dict[str, Any]] = defaultdict(lambda: {
            'energy_kwh': 0, 'water_m3': 0, 'gas_m3': 0, 'steam_ton': 0,
            'peak_kwh': 0, 'flat_kwh': 0, 'valley_kwh': 0,
        })
        
        # 班次能耗记录
        self.shift_records: list[dict[str, Any]] = []
        
        # 能耗基线（用于异常检测）
        self.energy_baseline: dict[str, float] = {}
        
        # 锁
        self._lock = threading.Lock()
        
        # 运行状态
        self._running = False
        self._thread = None
        
        logger.info("能源管理模块初始化完成")
    
    def start(self):
        """启动能源管理"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.info("能源管理已启动")
    
    def stop(self):
        """停止能源管理"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
    
    def _monitor_loop(self):
        """监控主循环"""
        while self._running:
            try:
                self._check_anomalies()
            except Exception as e:
                logger.error(f"能源监控异常: {e}", exc_info=True)
            time.sleep(60)  # 每分钟检查一次
    
    # ==================== 数据输入接口 ====================
    
    def feed_power_data(self, device_id: str, power_kw: float,
                         energy_kwh: float | None = None, timestamp: datetime | None = None):
        """
        喂入电力数据
        
        Args:
            device_id: 设备ID
            power_kw: 实时功率 (kW)
            energy_kwh: 累积电量 (kWh)，可选
            timestamp: 时间戳
        """
        now = timestamp or datetime.now()
        
        with self._lock:
            # 更新实时功率
            old = self.realtime_power.get(device_id, {})
            old_power = old.get('power_kw', 0)
            
            self.realtime_power[device_id] = {
                'power_kw': power_kw,
                'timestamp': now,
            }
            
            # 计算电量增量（梯形积分）
            if old_power > 0 and 'timestamp' in old:
                dt_hours = (now - old['timestamp']).total_seconds() / 3600
                avg_power = (old_power + power_kw) / 2
                delta_kwh = avg_power * dt_hours
                
                # 分时累加
                tariff_type = self._get_tariff_type(now.hour)
                self.energy_accumulated[device_id]['energy_kwh'] += delta_kwh
                self.energy_accumulated[device_id][f'{tariff_type}_kwh'] += delta_kwh
            elif energy_kwh is not None:
                # 直接使用累积电量
                self.energy_accumulated[device_id]['energy_kwh'] = energy_kwh
    
    def feed_water_data(self, device_id: str, flow_m3h: float, timestamp: datetime | None = None):
        """喂入水表数据"""
        now = timestamp or datetime.now()
        with self._lock:
            old = self.realtime_power.get(f"{device_id}_water", {})
            if old and 'timestamp' in old:
                dt_hours = (now - old['timestamp']).total_seconds() / 3600
                self.energy_accumulated[device_id]['water_m3'] += flow_m3h * dt_hours
            self.realtime_power[f"{device_id}_water"] = {
                'flow_m3h': flow_m3h,
                'timestamp': now,
            }
    
    def feed_gas_data(self, device_id: str, flow_m3h: float, timestamp: datetime | None = None):
        """喂入气表数据"""
        now = timestamp or datetime.now()
        with self._lock:
            old = self.realtime_power.get(f"{device_id}_gas", {})
            if old and 'timestamp' in old:
                dt_hours = (now - old['timestamp']).total_seconds() / 3600
                self.energy_accumulated[device_id]['gas_m3'] += flow_m3h * dt_hours
            self.realtime_power[f"{device_id}_gas"] = {
                'flow_m3h': flow_m3h,
                'timestamp': now,
            }
    
    def set_baseline(self, device_id: str, daily_kwh: float):
        """设置能耗基线（用于异常检测）"""
        with self._lock:
            self.energy_baseline[device_id] = daily_kwh
    
    def _get_tariff_type(self, hour: int) -> str:
        """根据小时判断电价类型"""
        for start, end in self.tariff_periods.get('peak', []):
            if start <= hour < end:
                return 'peak'
        for start, end in self.tariff_periods.get('valley', []):
            if start <= hour < end:
                return 'valley'
        return 'flat'
    
    # ==================== 能耗分析 ====================
    
    def get_realtime_power(self, device_id: str | None = None) -> dict[str, Any]:
        """获取实时功率"""
        with self._lock:
            if device_id:
                return self.realtime_power.get(device_id, {})
            return dict(self.realtime_power)
    
    def get_total_power(self) -> float:
        """获取全厂总功率 (kW)"""
        with self._lock:
            total = 0
            for k, v in self.realtime_power.items():
                if 'power_kw' in v:
                    total += v['power_kw']
            return round(total, 2)
    
    def get_energy_summary(self, device_id: str | None = None) -> dict[str, Any]:
        """
        获取能耗汇总
        
        Returns:
            {
                'total_energy_kwh': float,
                'peak_kwh': float,
                'flat_kwh': float,
                'valley_kwh': float,
                'electricity_cost': float,  # 电费(元)
                'carbon_emission_kg': float,  # 碳排放(kg)
                'water_m3': float,
                'gas_m3': float,
            }
        """
        with self._lock:
            if device_id:
                data = dict(self.energy_accumulated.get(device_id, {}))
            else:
                # 汇总所有设备
                data = {'energy_kwh': 0, 'peak_kwh': 0, 'flat_kwh': 0,
                        'valley_kwh': 0, 'water_m3': 0, 'gas_m3': 0, 'steam_ton': 0}
                for d in self.energy_accumulated.values():
                    for k in data:
                        data[k] += d.get(k, 0)
        
        # 计算电费
        cost = (data.get('peak_kwh', 0) * self.tariff['peak'] +
                data.get('flat_kwh', 0) * self.tariff['flat'] +
                data.get('valley_kwh', 0) * self.tariff['valley'])
        
        # 碳排放
        carbon = data.get('energy_kwh', 0) * self.carbon_factor
        
        return {
            'total_energy_kwh': round(data.get('energy_kwh', 0), 2),
            'peak_kwh': round(data.get('peak_kwh', 0), 2),
            'flat_kwh': round(data.get('flat_kwh', 0), 2),
            'valley_kwh': round(data.get('valley_kwh', 0), 2),
            'electricity_cost': round(cost, 2),
            'carbon_emission_kg': round(carbon, 2),
            'water_m3': round(data.get('water_m3', 0), 2),
            'gas_m3': round(data.get('gas_m3', 0), 2),
            'tariff_rates': self.tariff,
        }
    
    def get_energy_cost_breakdown(self) -> dict[str, Any]:
        """获取电费分时明细"""
        with self._lock:
            peak_total = sum(d.get('peak_kwh', 0) for d in self.energy_accumulated.values())
            flat_total = sum(d.get('flat_kwh', 0) for d in self.energy_accumulated.values())
            valley_total = sum(d.get('valley_kwh', 0) for d in self.energy_accumulated.values())
        
        return {
            'peak': {
                'kwh': round(peak_total, 2),
                'rate': self.tariff['peak'],
                'cost': round(peak_total * self.tariff['peak'], 2),
            },
            'flat': {
                'kwh': round(flat_total, 2),
                'rate': self.tariff['flat'],
                'cost': round(flat_total * self.tariff['flat'], 2),
            },
            'valley': {
                'kwh': round(valley_total, 2),
                'rate': self.tariff['valley'],
                'cost': round(valley_total * self.tariff['valley'], 2),
            },
            'total_cost': round(
                peak_total * self.tariff['peak'] +
                flat_total * self.tariff['flat'] +
                valley_total * self.tariff['valley'], 2
            ),
        }
    
    def get_carbon_emission(self) -> dict[str, Any]:
        """获取碳排放数据"""
        summary = self.get_energy_summary()
        energy_mwh = summary['total_energy_kwh'] / 1000
        
        return {
            'total_emission_kg': summary['carbon_emission_kg'],
            'total_emission_ton': round(summary['carbon_emission_kg'] / 1000, 4),
            'energy_mwh': round(energy_mwh, 4),
            'factor_kg_per_kwh': self.carbon_factor,
            'equivalent_trees': round(summary['carbon_emission_kg'] / 21.77, 1),  # 1棵树年吸收21.77kg CO2
        }
    
    def get_energy_efficiency(self, production_count: int = 0,
                                production_value: float = 0) -> dict[str, Any]:
        """
        能效指标计算
        
        Args:
            production_count: 产品数量
            production_value: 产值(万元)
        """
        summary = self.get_energy_summary()
        energy = summary['total_energy_kwh']
        
        result = {
            'total_energy_kwh': energy,
            'unit_product_energy': None,  # 单位产品能耗 (kWh/件)
            'unit_value_energy': None,    # 万元产值能耗 (kWh/万元)
        }
        
        if production_count > 0:
            result['unit_product_energy'] = round(energy / production_count, 4)
        
        if production_value > 0:
            result['unit_value_energy'] = round(energy / production_value, 2)
        
        return result
    
    # ==================== 异常检测 ====================
    
    def _check_anomalies(self):
        """能耗异常检测"""
        with self._lock:
            for device_id, data in self.energy_accumulated.items():
                baseline = self.energy_baseline.get(device_id)
                if baseline and baseline > 0:
                    # 当前能耗超过基线200%则报警
                    current = data.get('energy_kwh', 0)
                    if current > baseline * 2:
                        logger.warning(
                            f"能耗异常: {device_id} 当前{current:.1f}kWh, "
                            f"基线{baseline:.1f}kWh, 超出{(current/baseline-1)*100:.0f}%"
                        )
    
    def get_anomalies(self) -> list[dict[str, Any]]:
        """获取能耗异常列表"""
        anomalies = []
        with self._lock:
            for device_id, data in self.energy_accumulated.items():
                baseline = self.energy_baseline.get(device_id)
                if baseline and baseline > 0:
                    current = data.get('energy_kwh', 0)
                    ratio = current / baseline
                    if ratio > 1.5:
                        anomalies.append({
                            'device_id': device_id,
                            'current_kwh': round(current, 2),
                            'baseline_kwh': baseline,
                            'ratio': round(ratio, 2),
                            'severity': 'critical' if ratio > 2 else 'warning',
                            'message': f'{device_id}能耗异常：当前{current:.1f}kWh，'
                                       f'基线{baseline:.1f}kWh，超出{(ratio-1)*100:.0f}%',
                        })
        return anomalies
    
    def reset_accumulated(self, device_id: str | None = None):
        """重置能耗累积（新班次/新日）"""
        with self._lock:
            if device_id:
                self.energy_accumulated[device_id] = {
                    'energy_kwh': 0, 'water_m3': 0, 'gas_m3': 0, 'steam_ton': 0,
                    'peak_kwh': 0, 'flat_kwh': 0, 'valley_kwh': 0,
                }
            else:
                self.energy_accumulated.clear()
