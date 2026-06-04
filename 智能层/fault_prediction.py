"""
设备故障预测模块
基于历史数据的设备故障预测和健康评估

功能：
- 设备健康评分
- 故障趋势预测
- 维护建议生成
- 异常检测
"""

import time
import logging
import threading
import math
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from collections import defaultdict

logger = logging.getLogger(__name__)


class HealthMetric:
    """健康指标"""

    def __init__(self, name: str, weight: float = 1.0):
        self.name = name
        self.weight = weight
        self.values: List[Tuple[float, float]] = []  # (timestamp, value)
        self.threshold_warning: float = 70.0
        self.threshold_critical: float = 50.0
        self._lock = threading.Lock()

    def add_value(self, timestamp: float, value: float):
        """添加指标值"""
        with self._lock:
            self.values.append((timestamp, value))
            # 只保留最近10000条
            if len(self.values) > 10000:
                self.values = self.values[-10000:]

    def get_trend(self, window_hours: int = 24) -> float:
        """获取趋势（正数表示恶化，负数表示改善）"""
        with self._lock:
            if len(self.values) < 2:
                return 0.0

            cutoff = time.time() - (window_hours * 3600)
            recent = [(t, v) for t, v in self.values if t > cutoff]

            if len(recent) < 2:
                return 0.0

            # 简单线性回归
            n = len(recent)
            sum_x = sum(t for t, _ in recent)
            sum_y = sum(v for _, v in recent)
            sum_xy = sum(t * v for t, v in recent)
            sum_x2 = sum(t * t for t, _ in recent)

            denominator = n * sum_x2 - sum_x * sum_x
            if denominator == 0:
                return 0.0

            slope = (n * sum_xy - sum_x * sum_y) / denominator

            # 归一化到每小时的变化率
            return slope * 3600

    def get_current(self) -> Optional[float]:
        """获取最新值"""
        with self._lock:
            if not self.values:
                return None
            return self.values[-1][1]

    def get_statistics(self, window_hours: int = 24) -> Dict[str, float]:
        """获取统计信息"""
        with self._lock:
            cutoff = time.time() - (window_hours * 3600)
            recent = [v for t, v in self.values if t > cutoff]

            if not recent:
                return {'avg': 0, 'min': 0, 'max': 0, 'stddev': 0}

            avg = sum(recent) / len(recent)
            min_val = min(recent)
            max_val = max(recent)

            variance = sum((x - avg) ** 2 for x in recent) / len(recent)
            stddev = math.sqrt(variance)

            return {
                'avg': round(avg, 2),
                'min': round(min_val, 2),
                'max': round(max_val, 2),
                'stddev': round(stddev, 2),
            }


class DeviceHealthProfile:
    """设备健康档案"""

    def __init__(self, device_id: str, device_name: str = ''):
        self.device_id = device_id
        self.device_name = device_name or device_id
        self.metrics: Dict[str, HealthMetric] = {}
        self._lock = threading.Lock()

        # 健康评分权重
        self.metric_weights: Dict[str, float] = {
            'temperature': 0.25,
            'vibration': 0.25,
            'current': 0.15,
            'pressure': 0.15,
            'flow': 0.10,
            'level': 0.10,
        }

        # 故障历史
        self.fault_history: List[Dict[str, Any]] = []

        # 维护记录
        self.maintenance_history: List[Dict[str, Any]] = []

    def add_metric_value(self, metric_name: str, timestamp: float, value: float):
        """添加指标值"""
        with self._lock:
            if metric_name not in self.metrics:
                self.metrics[metric_name] = HealthMetric(metric_name)
            self.metrics[metric_name].add_value(timestamp, value)

    def get_health_score(self) -> float:
        """计算综合健康评分 (0-100)"""
        with self._lock:
            if not self.metrics:
                return 100.0  # 无数据时假设健康

            total_score = 0.0
            total_weight = 0.0

            for name, metric in self.metrics.items():
                weight = self.metric_weights.get(name, 0.1)

                # 基于当前值和趋势计算分数
                current = metric.get_current()
                trend = metric.get_trend()

                if current is None:
                    continue

                # 基础分数（基于当前值与阈值的关系）
                base_score = 100.0
                if name in ['temperature', 'vibration', 'current']:
                    # 越高越差的指标
                    if current > 80:
                        base_score = max(0, 100 - (current - 80) * 5)
                    elif current > 60:
                        base_score = max(50, 100 - (current - 60) * 2.5)
                elif name in ['pressure', 'flow', 'level']:
                    # 需要在合理范围内的指标
                    if current < 20 or current > 80:
                        base_score = max(0, 100 - abs(current - 50) * 2)

                # 趋势调整（恶化扣分）
                trend_penalty = min(20, max(-20, trend * 10))

                final_score = max(0, min(100, base_score - trend_penalty))
                total_score += final_score * weight
                total_weight += weight

            if total_weight == 0:
                return 100.0

            return round(total_score / total_weight, 1)

    def get_health_status(self) -> str:
        """获取健康状态"""
        score = self.get_health_score()

        if score >= 80:
            return 'healthy'
        elif score >= 60:
            return 'warning'
        elif score >= 40:
            return 'degraded'
        else:
            return 'critical'

    def predict_failure(self, days_ahead: int = 7) -> Dict[str, Any]:
        """预测未来故障风险

        Args:
            days_ahead: 预测天数

        Returns:
            预测结果
        """
        risk_factors = []
        overall_risk = 0.0

        with self._lock:
            for name, metric in self.metrics.items():
                trend = metric.get_trend(days_ahead * 24)
                current = metric.get_current()

                if current is None:
                    continue

                # 预测未来值
                predicted = current + trend * days_ahead * 24

                # 计算风险
                risk = 0.0
                reason = ''

                if name in ['temperature', 'vibration', 'current']:
                    if predicted > 90:
                        risk = 0.9
                        reason = f'{name}预计将达到{predicted:.1f}，超过危险阈值'
                    elif predicted > 70:
                        risk = 0.5
                        reason = f'{name}预计将达到{predicted:.1f}，接近警告阈值'
                elif name in ['pressure', 'flow', 'level']:
                    if predicted < 10 or predicted > 90:
                        risk = 0.8
                        reason = f'{name}预计将达到{predicted:.1f}，超出安全范围'

                if risk > 0:
                    risk_factors.append({
                        'metric': name,
                        'current': current,
                        'predicted': round(predicted, 1),
                        'risk': risk,
                        'reason': reason,
                    })
                    overall_risk = max(overall_risk, risk)

        return {
            'device_id': self.device_id,
            'days_ahead': days_ahead,
            'overall_risk': round(overall_risk, 2),
            'risk_factors': risk_factors,
            'recommendation': self._generate_recommendation(overall_risk, risk_factors),
        }

    def _generate_recommendation(self, risk: float, factors: List[Dict]) -> str:
        """生成维护建议"""
        if risk >= 0.8:
            return '建议立即安排预防性维护，存在高故障风险'
        elif risk >= 0.5:
            return '建议本周内安排检查，关注异常指标'
        elif risk >= 0.2:
            return '建议定期监测，暂无紧急风险'
        else:
            return '设备运行正常，继续常规维护'

    def add_fault_record(self, fault_type: str, description: str, timestamp: float = None):
        """记录故障"""
        with self._lock:
            self.fault_history.append({
                'type': fault_type,
                'description': description,
                'timestamp': timestamp or time.time(),
            })

    def add_maintenance_record(self, maintenance_type: str, description: str,
                              performed_by: str = '', timestamp: float = None):
        """记录维护"""
        with self._lock:
            self.maintenance_history.append({
                'type': maintenance_type,
                'description': description,
                'performed_by': performed_by,
                'timestamp': timestamp or time.time(),
            })


class FaultPredictionEngine:
    """故障预测引擎"""

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.profiles: Dict[str, DeviceHealthProfile] = {}
        self._lock = threading.Lock()

        # 配置
        self.prediction_interval = self.config.get('prediction_interval', 3600)  # 预测间隔（秒）
        self.alert_threshold = self.config.get('alert_threshold', 0.7)  # 告警阈值

        self._running = False
        self._prediction_thread: Optional[threading.Thread] = None

        # 预测结果缓存
        self._predictions: Dict[str, Dict[str, Any]] = {}

    def start(self):
        """启动预测引擎"""
        self._running = True
        self._prediction_thread = threading.Thread(target=self._prediction_loop, daemon=True)
        self._prediction_thread.start()
        logger.info("故障预测引擎已启动")

    def stop(self):
        """停止预测引擎"""
        self._running = False
        if self._prediction_thread:
            self._prediction_thread.join(timeout=5)
        logger.info("故障预测引擎已停止")

    def _prediction_loop(self):
        """预测循环"""
        while self._running:
            try:
                self._run_predictions()
                time.sleep(self.prediction_interval)
            except Exception as e:
                logger.error(f"预测循环异常: {e}")

    def _run_predictions(self):
        """执行预测"""
        with self._lock:
            profiles = dict(self.profiles)

        for device_id, profile in profiles.items():
            try:
                prediction = profile.predict_failure()
                self._predictions[device_id] = prediction

                # 如果风险超过阈值，记录日志
                if prediction['overall_risk'] >= self.alert_threshold:
                    logger.warning(
                        f"设备 {device_id} 故障风险高: {prediction['overall_risk']:.0%} - "
                        f"{prediction['recommendation']}"
                    )
            except Exception as e:
                logger.error(f"设备 {device_id} 预测失败: {e}")

    def get_or_create_profile(self, device_id: str, device_name: str = '') -> DeviceHealthProfile:
        """获取或创建设备健康档案"""
        with self._lock:
            if device_id not in self.profiles:
                self.profiles[device_id] = DeviceHealthProfile(device_id, device_name)
            return self.profiles[device_id]

    def update_metric(self, device_id: str, metric_name: str, value: float,
                     timestamp: float = None):
        """更新设备指标"""
        profile = self.get_or_create_profile(device_id)
        profile.add_metric_value(metric_name, timestamp or time.time(), value)

    def get_device_health(self, device_id: str) -> Dict[str, Any]:
        """获取设备健康状态"""
        with self._lock:
            if device_id not in self.profiles:
                return {'device_id': device_id, 'health_score': 100, 'status': 'unknown'}

            profile = self.profiles[device_id]
            return {
                'device_id': device_id,
                'device_name': profile.device_name,
                'health_score': profile.get_health_score(),
                'status': profile.get_health_status(),
                'prediction': self._predictions.get(device_id),
            }

    def get_all_health(self) -> List[Dict[str, Any]]:
        """获取所有设备健康状态"""
        with self._lock:
            results = []
            for device_id, profile in self.profiles.items():
                results.append({
                    'device_id': device_id,
                    'device_name': profile.device_name,
                    'health_score': profile.get_health_score(),
                    'status': profile.get_health_status(),
                })
            return results

    def get_predictions(self) -> Dict[str, Dict[str, Any]]:
        """获取所有预测结果"""
        return dict(self._predictions)

    def get_high_risk_devices(self, threshold: float = 0.7) -> List[Dict[str, Any]]:
        """获取高风险设备"""
        results = []
        for device_id, prediction in self._predictions.items():
            if prediction.get('overall_risk', 0) >= threshold:
                results.append({
                    'device_id': device_id,
                    'risk': prediction['overall_risk'],
                    'recommendation': prediction['recommendation'],
                    'factors': prediction.get('risk_factors', []),
                })
        return sorted(results, key=lambda x: x['risk'], reverse=True)
