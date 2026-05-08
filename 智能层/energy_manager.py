"""
能源管理模块 (Energy Management)
==================================
工业4.0绿色制造核心：能源消耗监控、分析与优化

功能：
1. 实时能耗监控 — 电力/水/气/蒸汽分项计量
2. 能耗统计 — 班次/日/周/月能耗汇总
3. 峰谷平电价分析 — 分时电价成本核算（支持动态配置）
4. 碳排放计算 — 基于国家排放因子（支持动态配置）
5. 能效指标 — 单位产品能耗、万元产值能耗
6. 能耗异常检测 — 突增/泄漏预警

重写说明：
- 电价配置支持通过YAML文件持久化和前端API动态调整
- 峰谷平时段定义支持动态调整
- 碳排放因子支持动态调整
- 所有配置变更实时生效，无需重启
"""

import logging
import math
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from collections import defaultdict

import yaml

logger = logging.getLogger(__name__)

# 默认配置（当YAML文件不存在时使用）
DEFAULT_CONFIG = {
    'tariff': {
        'peak': 1.2,
        'flat': 0.7,
        'valley': 0.35,
    },
    'tariff_periods': {
        'peak': [(8, 11), (18, 23)],
        'valley': [(0, 7), (23, 24)],
    },
    'carbon_factor': 0.581,
    'anomaly': {
        'threshold_multiplier': 2.0,
        'warning_multiplier': 1.5,
    },
}


class EnergyManager:
    """
    能源管理引擎

    数据来源：
    - 电力仪表（功率、电量、功率因数）
    - 水表/气表/蒸汽表（流量、累积量）
    - 设备运行状态（关联能耗与产出）

    配置管理：
    - 从 配置/energy.yaml 加载配置
    - 支持通过API动态修改电价、时段、碳排放因子
    - 配置变更自动持久化到YAML文件
    """

    def __init__(self, database, config: dict[str, Any] | None = None,
                 config_path: str = '配置/energy.yaml'):
        """
        Args:
            database: Database实例
            config: 配置字典（优先级高于配置文件）
            config_path: YAML配置文件路径
        """
        self.database = database
        self.config_path = Path(config_path)
        self._lock = threading.Lock()

        # 加载配置（YAML文件 -> 默认值 -> 外部config覆盖）
        self._load_config(config)

        # 实时功率数据
        # device_id -> {'power_kw': float, 'timestamp': datetime}
        self.realtime_power: dict[str, dict[str, Any]] = {}

        # 能耗累积数据
        self.energy_accumulated: dict[str, dict[str, Any]] = defaultdict(lambda: {
            'energy_kwh': 0, 'water_m3': 0, 'gas_m3': 0, 'steam_ton': 0,
            'peak_kwh': 0, 'flat_kwh': 0, 'valley_kwh': 0,
        })

        # 班次能耗记录
        self.shift_records: list[dict[str, Any]] = []

        # 能耗基线（用于异常检测）
        self.energy_baseline: dict[str, float] = {}

        # 运行状态
        self._running = False
        self._thread = None

        logger.info("能源管理模块初始化完成")

    def _load_config(self, override: dict[str, Any] | None = None):
        """
        加载配置：YAML文件 -> 默认值 -> 外部覆盖

        Args:
            override: 外部配置字典（优先级最高）
        """
        # 1. 从默认值开始
        merged = {
            'tariff': dict(DEFAULT_CONFIG['tariff']),
            'tariff_periods': {
                'peak': [list(p) for p in DEFAULT_CONFIG['tariff_periods']['peak']],
                'valley': [list(p) for p in DEFAULT_CONFIG['tariff_periods']['valley']],
            },
            'carbon_factor': DEFAULT_CONFIG['carbon_factor'],
            'anomaly': dict(DEFAULT_CONFIG['anomaly']),
        }

        # 2. 从YAML文件加载（覆盖默认值）
        if self.config_path.exists():
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    file_config = yaml.safe_load(f) or {}
                if 'tariff' in file_config:
                    merged['tariff'].update(file_config['tariff'])
                if 'tariff_periods' in file_config:
                    for period_type, periods in file_config['tariff_periods'].items():
                        merged['tariff_periods'][period_type] = [
                            list(p) for p in periods
                        ]
                if 'carbon_factor' in file_config:
                    merged['carbon_factor'] = file_config['carbon_factor']
                if 'anomaly' in file_config:
                    merged['anomaly'].update(file_config['anomaly'])
                logger.info(f"从 {self.config_path} 加载能源配置")
            except Exception as e:
                logger.warning(f"加载能源配置文件失败: {e}，使用默认配置")
        else:
            logger.info(f"能源配置文件不存在: {self.config_path}，使用默认配置")

        # 3. 外部config覆盖
        if override:
            if 'tariff' in override:
                merged['tariff'].update(override['tariff'])
            if 'tariff_periods' in override:
                for period_type, periods in override['tariff_periods'].items():
                    merged['tariff_periods'][period_type] = periods
            if 'carbon_factor' in override:
                merged['carbon_factor'] = override['carbon_factor']
            if 'anomaly' in override:
                merged['anomaly'].update(override['anomaly'])

        # 应用配置
        self.tariff = merged['tariff']
        self.tariff_periods = merged['tariff_periods']
        self.carbon_factor = merged['carbon_factor']
        self.anomaly_config = merged['anomaly']

    def _save_config(self) -> bool:
        """将当前配置持久化到YAML文件"""
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            config_data = {
                'tariff': self.tariff,
                'tariff_periods': {
                    'peak': [list(p) for p in self.tariff_periods.get('peak', [])],
                    'valley': [list(p) for p in self.tariff_periods.get('valley', [])],
                },
                'carbon_factor': self.carbon_factor,
                'anomaly': self.anomaly_config,
            }
            with open(self.config_path, 'w', encoding='utf-8') as f:
                yaml.dump(config_data, f, allow_unicode=True, default_flow_style=False)
            logger.info(f"能源配置已保存到 {self.config_path}")
            return True
        except Exception as e:
            logger.error(f"保存能源配置失败: {e}")
            return False

    # ==================== 配置管理API ====================

    def get_tariff_config(self) -> dict[str, Any]:
        """
        获取当前电价配置

        Returns:
            {
                'tariff': {'peak': 1.2, 'flat': 0.7, 'valley': 0.35},
                'tariff_periods': {'peak': [[8,11],[18,23]], 'valley': [[0,7],[23,24]]},
                'carbon_factor': 0.581,
            }
        """
        with self._lock:
            return {
                'tariff': dict(self.tariff),
                'tariff_periods': {
                    k: [list(p) for p in v]
                    for k, v in self.tariff_periods.items()
                },
                'carbon_factor': self.carbon_factor,
            }

    def update_tariff(self, tariff: dict[str, float] | None = None,
                      tariff_periods: dict[str, list] | None = None,
                      carbon_factor: float | None = None) -> dict[str, Any]:
        """
        更新电价配置（实时生效 + 持久化）

        Args:
            tariff: 电价字典 {'peak': 1.2, 'flat': 0.7, 'valley': 0.35}
            tariff_periods: 时段定义 {'peak': [[8,11],[18,23]], 'valley': [[0,7],[23,24]]}
            carbon_factor: 碳排放因子 (kgCO2/kWh)

        Returns:
            {'success': True, 'config': {...}} 或 {'success': False, 'message': '...'}
        """
        with self._lock:
            changes = []

            if tariff is not None:
                # 验证电价
                for key in ('peak', 'flat', 'valley'):
                    if key in tariff:
                        val = tariff[key]
                        if not isinstance(val, (int, float)) or val < 0:
                            return {
                                'success': False,
                                'message': f'电价 {key} 必须为非负数，收到: {val}'
                            }
                self.tariff.update(tariff)
                changes.append(f"电价: {self.tariff}")

            if tariff_periods is not None:
                # 验证时段
                for period_type, periods in tariff_periods.items():
                    if period_type not in ('peak', 'valley'):
                        return {
                            'success': False,
                            'message': f'不支持的时段类型: {period_type}，仅支持 peak/valley'
                        }
                    for period in periods:
                        if not isinstance(period, (list, tuple)) or len(period) != 2:
                            return {
                                'success': False,
                                'message': f'时段格式错误: {period}，应为 [start_hour, end_hour]'
                            }
                        start, end = period
                        if not (0 <= start < 24 and 0 < end <= 24 and start < end):
                            return {
                                'success': False,
                                'message': f'时段范围错误: [{start},{end}]，小时范围0-24'
                            }
                self.tariff_periods.update(tariff_periods)
                changes.append(f"时段: {self.tariff_periods}")

            if carbon_factor is not None:
                if not isinstance(carbon_factor, (int, float)) or carbon_factor < 0:
                    return {
                        'success': False,
                        'message': f'碳排放因子必须为非负数，收到: {carbon_factor}'
                    }
                self.carbon_factor = carbon_factor
                changes.append(f"碳排放因子: {carbon_factor}")

            # 持久化
            saved = self._save_config()

            logger.info(f"电价配置已更新: {'; '.join(changes)}")
            return {
                'success': True,
                'message': f'配置已更新并{"已" if saved else "未"}持久化',
                'config': self.get_tariff_config(),
            }

    def get_anomaly_config(self) -> dict[str, Any]:
        """获取异常检测配置"""
        with self._lock:
            return dict(self.anomaly_config)

    def update_anomaly_config(self, config: dict[str, Any]) -> dict[str, Any]:
        """更新异常检测配置"""
        with self._lock:
            if 'threshold_multiplier' in config:
                val = config['threshold_multiplier']
                if not isinstance(val, (int, float)) or val <= 0:
                    return {'success': False, 'message': f'阈值倍数必须为正数: {val}'}
                self.anomaly_config['threshold_multiplier'] = val

            if 'warning_multiplier' in config:
                val = config['warning_multiplier']
                if not isinstance(val, (int, float)) or val <= 0:
                    return {'success': False, 'message': f'警告倍数必须为正数: {val}'}
                self.anomaly_config['warning_multiplier'] = val

            self._save_config()
            return {
                'success': True,
                'message': '异常检测配置已更新',
                'config': dict(self.anomaly_config),
            }

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
                'tariff_rates': dict,  # 当前电价
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
            'tariff_rates': dict(self.tariff),
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
            'equivalent_trees': round(summary['carbon_emission_kg'] / 21.77, 1),
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
            'unit_product_energy': None,
            'unit_value_energy': None,
        }

        if production_count > 0:
            result['unit_product_energy'] = round(energy / production_count, 4)

        if production_value > 0:
            result['unit_value_energy'] = round(energy / production_value, 2)

        return result

    # ==================== 异常检测 ====================

    def _check_anomalies(self):
        """能耗异常检测"""
        threshold = self.anomaly_config.get('threshold_multiplier', 2.0)
        with self._lock:
            for device_id, data in self.energy_accumulated.items():
                baseline = self.energy_baseline.get(device_id)
                if baseline and baseline > 0:
                    current = data.get('energy_kwh', 0)
                    if current > baseline * threshold:
                        logger.warning(
                            f"能耗异常: {device_id} 当前{current:.1f}kWh, "
                            f"基线{baseline:.1f}kWh, 超出{(current/baseline-1)*100:.0f}%"
                        )

    def get_anomalies(self) -> list[dict[str, Any]]:
        """获取能耗异常列表"""
        warning_threshold = self.anomaly_config.get('warning_multiplier', 1.5)
        anomalies = []
        with self._lock:
            for device_id, data in self.energy_accumulated.items():
                baseline = self.energy_baseline.get(device_id)
                if baseline and baseline > 0:
                    current = data.get('energy_kwh', 0)
                    ratio = current / baseline
                    if ratio > warning_threshold:
                        anomalies.append({
                            'device_id': device_id,
                            'current_kwh': round(current, 2),
                            'baseline_kwh': baseline,
                            'ratio': round(ratio, 2),
                            'severity': 'critical' if ratio > self.anomaly_config.get('threshold_multiplier', 2.0) else 'warning',
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
