"""
预测性维护模块 (Predictive Maintenance)
========================================
工业4.0核心能力：基于数据驱动的设备健康预测

功能：
1. 趋势分析 — 移动平均 + 线性回归预测未来值
2. 异常检测 — Z-Score + IQR双算法
3. 设备健康评分 — 多维度加权评估 (0-100)
4. 故障预测 — 基于趋势外推预测何时超限
5. 维护建议 — 自动生成维护工单

依赖：numpy (已在requirements.txt中)
"""

import logging
import math
import time
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from collections import defaultdict, deque

logger = logging.getLogger(__name__)


class PredictiveMaintenance:
    """
    预测性维护引擎
    
    工作流程：
    1. 持续采集设备数据 → 存入滑动窗口
    2. 定期执行趋势分析 + 异常检测
    3. 计算设备健康评分
    4. 预测故障时间窗口
    5. 生成维护建议
    """
    
    def __init__(self, database, config: Dict = None):
        """
        Args:
            database: Database实例
            config: 配置字典
        """
        self.database = database
        self.config = config or {}
        
        # 滑动窗口：每个设备+寄存器保留最近N个数据点
        self.window_size = self.config.get('window_size', 500)
        self.data_windows: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=self.window_size)
        )
        
        # 设备健康评分缓存
        self.health_scores: Dict[str, Dict] = {}
        
        # 维护建议队列
        self.maintenance_alerts: List[Dict] = []
        
        # 设备阈值配置（从devices.yaml或手动配置）
        self.thresholds: Dict[str, Dict] = self.config.get('thresholds', {})
        
        # 如果没有配置阈值，设置常见参数的默认阈值
        if not self.thresholds:
            self.thresholds = {
                'temperature': {'upper': 150.0, 'lower': -10.0},
                'pressure': {'upper': 1.2, 'lower': 0.0},
                'voltage': {'upper': 250.0, 'lower': 180.0},
                'current': {'upper': 15.0, 'lower': 0.0},
                'speed': {'upper': 1800.0, 'lower': 0.0},
                'vibration': {'upper': 10.0, 'lower': 0.0},
                'temperature': {'upper': 150.0, 'lower': -10.0},
            }
        
        # 分析间隔（秒）
        self.analysis_interval = self.config.get('analysis_interval', 60)
        
        # 运行状态
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        
        logger.info("预测性维护模块初始化完成")
    
    def feed_data(self, device_id: str, register_name: str,
                  value: float, timestamp: datetime = None):
        """
        喂入实时数据（由DataCollector调用）
        
        Args:
            device_id: 设备ID
            register_name: 寄存器名称
            value: 数值
            timestamp: 时间戳
        """
        key = f"{device_id}:{register_name}"
        ts = timestamp or datetime.now()
        
        with self._lock:
            self.data_windows[key].append({
                'value': value,
                'timestamp': ts,
                'ts_epoch': ts.timestamp()
            })
    
    def start(self):
        """启动后台分析线程"""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._analysis_loop, daemon=True)
        self._thread.start()
        logger.info("预测性维护后台分析已启动")
    
    def stop(self):
        """停止后台分析"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("预测性维护后台分析已停止")
    
    def _analysis_loop(self):
        """后台分析主循环"""
        while self._running:
            try:
                self._run_analysis()
            except Exception as e:
                logger.error(f"预测性维护分析异常: {e}", exc_info=True)
            time.sleep(self.analysis_interval)
    
    def _run_analysis(self):
        """执行一轮完整分析"""
        with self._lock:
            keys = list(self.data_windows.keys())
        
        for key in keys:
            try:
                device_id, register_name = key.split(':', 1)
                with self._lock:
                    window = list(self.data_windows[key])
                
                if len(window) < 10:
                    continue
                
                # 1. 趋势分析
                trend = self._analyze_trend(window)
                
                # 2. 异常检测
                anomalies = self._detect_anomalies(window)
                
                # 3. 健康评分
                health = self._calculate_health_score(
                    device_id, register_name, window, trend, anomalies
                )
                
                # 4. 故障预测
                failure_pred = self._predict_failure(
                    device_id, register_name, window, trend
                )
                
                # 缓存结果
                score_key = f"{device_id}:{register_name}"
                with self._lock:
                    self.health_scores[score_key] = {
                        'device_id': device_id,
                        'register_name': register_name,
                        'health_score': health,
                        'trend': trend,
                        'anomaly_count': len(anomalies),
                        'failure_prediction': failure_pred,
                        'updated_at': datetime.now().isoformat(),
                    }
                    
                    # 5. 生成维护建议
                    if health < 60 or (failure_pred and failure_pred.get('days_to_limit', 999) < 7):
                        alert = self._generate_maintenance_alert(
                            device_id, register_name, health, trend, failure_pred
                        )
                        self.maintenance_alerts.append(alert)
                        # 保留最近100条
                        if len(self.maintenance_alerts) > 100:
                            self.maintenance_alerts = self.maintenance_alerts[-100:]
                
            except Exception as e:
                logger.warning(f"分析 {key} 时出错: {e}")
    
    def _analyze_trend(self, window: List[Dict]) -> Dict:
        """
        趋势分析 — 简单线性回归
        
        Returns:
            {
                'slope': 斜率（正值上升，负值下降）,
                'intercept': 截距,
                'r_squared': R²拟合度,
                'direction': 'rising'|'falling'|'stable',
                'change_rate': 变化率 (%/hour),
            }
        """
        n = len(window)
        if n < 2:
            return {'slope': 0, 'r_squared': 0, 'direction': 'stable', 'change_rate': 0}
        
        values = [d['value'] for d in window]
        # 用索引作为x轴（简化，避免时间戳精度问题）
        x = list(range(n))
        
        # 最小二乘法
        sum_x = sum(x)
        sum_y = sum(values)
        sum_xy = sum(xi * yi for xi, yi in zip(x, values))
        sum_x2 = sum(xi * xi for xi in x)
        
        denominator = n * sum_x2 - sum_x * sum_x
        if denominator == 0:
            return {'slope': 0, 'r_squared': 0, 'direction': 'stable', 'change_rate': 0}
        
        slope = (n * sum_xy - sum_x * sum_y) / denominator
        intercept = (sum_y - slope * sum_x) / n
        
        # R²
        y_mean = sum_y / n
        ss_tot = sum((yi - y_mean) ** 2 for yi in values)
        ss_res = sum((yi - (slope * xi + intercept)) ** 2 for xi, yi in zip(x, values))
        r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
        
        # 变化率（每小时）
        time_span_hours = (window[-1]['ts_epoch'] - window[0]['ts_epoch']) / 3600
        change_rate = (slope * n / time_span_hours) if time_span_hours > 0 else 0
        
        # 方向判定
        if abs(slope) < 1e-6:
            direction = 'stable'
        elif slope > 0:
            direction = 'rising'
        else:
            direction = 'falling'
        
        return {
            'slope': slope,
            'intercept': intercept,
            'r_squared': max(0, r_squared),
            'direction': direction,
            'change_rate': round(change_rate, 4),
        }
    
    def _detect_anomalies(self, window: List[Dict]) -> List[Dict]:
        """
        异常检测 — Z-Score + IQR双算法
        
        Returns:
            异常数据点列表
        """
        values = [d['value'] for d in window]
        n = len(values)
        
        if n < 5:
            return []
        
        anomalies = []
        
        # Z-Score方法
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / n
        std = math.sqrt(variance) if variance > 0 else 0
        
        if std > 0:
            z_threshold = self.config.get('z_threshold', 3.0)
            for i, point in enumerate(window):
                z_score = abs((point['value'] - mean) / std)
                if z_score > z_threshold:
                    anomalies.append({
                        'index': i,
                        'value': point['value'],
                        'timestamp': point['timestamp'].isoformat(),
                        'z_score': round(z_score, 2),
                        'method': 'z_score',
                    })
        
        # IQR方法
        sorted_vals = sorted(values)
        q1_idx = n // 4
        q3_idx = 3 * n // 4
        q1 = sorted_vals[q1_idx]
        q3 = sorted_vals[q3_idx]
        iqr = q3 - q1
        
        if iqr > 0:
            iqr_multiplier = self.config.get('iqr_multiplier', 1.5)
            lower_bound = q1 - iqr_multiplier * iqr
            upper_bound = q3 + iqr_multiplier * iqr
            
            for i, point in enumerate(window):
                if point['value'] < lower_bound or point['value'] > upper_bound:
                    # 避免重复标记
                    already_flaged = any(
                        a['index'] == i and a['method'] == 'z_score'
                        for a in anomalies
                    )
                    if not already_flaged:
                        anomalies.append({
                            'index': i,
                            'value': point['value'],
                            'timestamp': point['timestamp'].isoformat(),
                            'iqr_range': f"[{round(lower_bound, 2)}, {round(upper_bound, 2)}]",
                            'method': 'iqr',
                        })
        
        return anomalies
    
    def _calculate_health_score(self, device_id: str, register_name: str,
                                 window: List[Dict], trend: Dict,
                                 anomalies: List[Dict]) -> float:
        """
        设备健康评分 (0-100)
        
        评分维度：
        - 稳定性 (40分): 数据波动越小越好
        - 趋势 (30分): 趋势越平稳越好
        - 异常率 (30分): 异常点越少越好
        """
        n = len(window)
        if n < 5:
            return 100.0
        
        values = [d['value'] for d in window]
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / n
        std = math.sqrt(variance) if variance > 0 else 0
        
        # 变异系数 CV = std/mean
        cv = (std / abs(mean)) if mean != 0 else 0
        
        # 1. 稳定性评分 (0-40)
        # CV < 0.05 → 满分, CV > 0.5 → 0分
        stability_score = max(0, 40 * (1 - cv / 0.5))
        
        # 2. 趋势评分 (0-30)
        # R²越高且斜率越大，说明趋势越明显（可能有问题）
        r2 = trend.get('r_squared', 0)
        slope_abs = abs(trend.get('slope', 0))
        # 趋势越明显扣分越多
        trend_penalty = r2 * min(slope_abs * 100, 30)
        trend_score = max(0, 30 - trend_penalty)
        
        # 3. 异常率评分 (0-30)
        anomaly_rate = len(anomalies) / n
        # 异常率 < 1% → 满分, > 10% → 0分
        anomaly_score = max(0, 30 * (1 - anomaly_rate / 0.1))
        
        total = stability_score + trend_score + anomaly_score
        return round(min(100, max(0, total)), 1)
    
    def _predict_failure(self, device_id: str, register_name: str,
                          window: List[Dict], trend: Dict) -> Optional[Dict]:
        """
        故障预测 — 基于趋势外推
        
        Returns:
            {
                'predicted_limit': 预计超限值,
                'days_to_limit': 预计多少天后超限,
                'limit_type': 'upper'|'lower',
                'confidence': 置信度 (0-1),
            }
        """
        key = f"{device_id}:{register_name}"
        threshold = self.thresholds.get(key, self.thresholds.get(register_name, None))
        
        if not threshold:
            # 没有阈值配置，无法预测
            return None
        
        slope = trend.get('slope', 0)
        if abs(slope) < 1e-8:
            return None  # 无趋势
        
        current_value = window[-1]['value']
        upper = threshold.get('upper')
        lower = threshold.get('lower')
        
        results = []
        
        if upper is not None and slope > 0:
            # 上升趋势，预测何时超过上限
            samples_to_limit = (upper - current_value) / slope
            if samples_to_limit > 0:
                # 估算时间（假设每分钟1个采样点）
                interval = self.config.get('sample_interval', 60)  # 秒
                seconds_to_limit = samples_to_limit * interval
                days = seconds_to_limit / 86400
                results.append({
                    'predicted_limit': round(upper, 2),
                    'days_to_limit': round(days, 1),
                    'limit_type': 'upper',
                    'confidence': round(trend.get('r_squared', 0.5), 2),
                })
        
        if lower is not None and slope < 0:
            # 下降趋势，预测何时低于下限
            samples_to_limit = (current_value - lower) / abs(slope)
            if samples_to_limit > 0:
                interval = self.config.get('sample_interval', 60)
                seconds_to_limit = samples_to_limit * interval
                days = seconds_to_limit / 86400
                results.append({
                    'predicted_limit': round(lower, 2),
                    'days_to_limit': round(days, 1),
                    'limit_type': 'lower',
                    'confidence': round(trend.get('r_squared', 0.5), 2),
                })
        
        return results[0] if results else None
    
    def _generate_maintenance_alert(self, device_id: str, register_name: str,
                                      health: float, trend: Dict,
                                      failure_pred: Optional[Dict]) -> Dict:
        """生成维护建议"""
        severity = 'critical' if health < 40 else 'warning' if health < 60 else 'info'
        
        msg_parts = [f"设备 {device_id} 的 {register_name}"]
        
        if health < 40:
            msg_parts.append(f"健康评分极低({health}分)，建议立即检修")
        elif health < 60:
            msg_parts.append(f"健康评分偏低({health}分)，建议安排预防性维护")
        
        if failure_pred:
            days = failure_pred['days_to_limit']
            limit_type = '上限' if failure_pred['limit_type'] == 'upper' else '下限'
            msg_parts.append(f"预计{days}天后触及{limit_type}({failure_pred['predicted_limit']})")
        
        if trend.get('direction') == 'rising':
            msg_parts.append(f"数据持续上升(变化率{trend['change_rate']}/h)")
        elif trend.get('direction') == 'falling':
            msg_parts.append(f"数据持续下降(变化率{trend['change_rate']}/h)")
        
        return {
            'device_id': device_id,
            'register_name': register_name,
            'severity': severity,
            'health_score': health,
            'message': '，'.join(msg_parts),
            'failure_prediction': failure_pred,
            'created_at': datetime.now().isoformat(),
        }
    
    # ==================== 公共查询接口 ====================
    
    def get_health_scores(self) -> Dict[str, Dict]:
        """获取所有设备健康评分"""
        with self._lock:
            return dict(self.health_scores)
    
    def get_device_health(self, device_id: str) -> Dict:
        """获取指定设备的所有寄存器健康评分"""
        with self._lock:
            return {
                k: v for k, v in self.health_scores.items()
                if v['device_id'] == device_id
            }
    
    def get_maintenance_alerts(self, limit: int = 50) -> List[Dict]:
        """获取维护建议列表"""
        with self._lock:
            return list(self.maintenance_alerts[-limit:])
    
    def get_trend_data(self, device_id: str, register_name: str) -> Dict:
        """获取指定寄存器的趋势分析数据"""
        key = f"{device_id}:{register_name}"
        with self._lock:
            window = list(self.data_windows.get(key, []))
            score = self.health_scores.get(key, {})
        
        if not window:
            return {}
        
        return {
            'device_id': device_id,
            'register_name': register_name,
            'data_points': len(window),
            'current_value': window[-1]['value'] if window else None,
            'min_value': min(d['value'] for d in window) if window else None,
            'max_value': max(d['value'] for d in window) if window else None,
            'mean_value': sum(d['value'] for d in window) / len(window) if window else None,
            'health_score': score.get('health_score'),
            'trend': score.get('trend'),
            'failure_prediction': score.get('failure_prediction'),
        }
    
    def set_threshold(self, key: str, upper: float = None, lower: float = None):
        """设置设备阈值（用于故障预测）"""
        self.thresholds[key] = {}
        if upper is not None:
            self.thresholds[key]['upper'] = upper
        if lower is not None:
            self.thresholds[key]['lower'] = lower
        logger.info(f"设置阈值 {key}: upper={upper}, lower={lower}")
