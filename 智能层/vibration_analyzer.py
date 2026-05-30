"""
振动分析模块 (Vibration Analyzer)

工业设备振动监测与故障诊断，支持：
- FFT频谱分析（轴承故障频率检测）
- 频带能量分析
- 趋势监测
- 基于ISO 10816的振动等级评估

典型应用：
- 电机轴承磨损检测
- 齿轮箱故障诊断
- 泵/风机不平衡检测
- 联轴器对中不良检测

依赖：numpy（可选，无numpy时使用简化FFT）
"""

import math
import logging
import threading
from typing import Any
from datetime import datetime
from collections import deque

logger = logging.getLogger(__name__)

# 尝试导入numpy（可选）
try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    logger.debug("numpy未安装，振动分析使用简化模式")


# ISO 10816 振动等级（mm/s RMS）
VIBRATION_ZONES = {
    'A': {'max': 0.71, 'description': '良好', 'color': 'green'},
    'B': {'max': 1.8, 'description': '可接受', 'color': 'yellow'},
    'C': {'max': 4.5, 'description': '报警', 'color': 'orange'},
    'D': {'max': float('inf'), 'description': '危险', 'color': 'red'},
}

# 典型轴承故障频率系数（相对于转速频率）
BEARING_FAULT_COEFFICIENTS = {
    'BPFO': {'name': '外圈故障频率', 'typical': 3.5},  # Ball Pass Frequency Outer
    'BPFI': {'name': '内圈故障频率', 'typical': 5.5},  # Ball Pass Frequency Inner
    'BSF': {'name': '滚动体故障频率', 'typical': 2.3},  # Ball Spin Frequency
    'FTF': {'name': '保持架故障频率', 'typical': 0.4},  # Fundamental Train Frequency
}


class VibrationRecord:
    """振动记录"""
    def __init__(self, timestamp: float, value: float, unit: str = 'mm/s'):
        self.timestamp = timestamp
        self.value = value
        self.unit = unit

    def to_dict(self) -> dict:
        return {
            'timestamp': self.timestamp,
            'value': round(self.value, 4),
            'unit': self.unit
        }


class FFTResult:
    """FFT分析结果"""
    def __init__(self, frequencies: list[float], amplitudes: list[float],
                 dominant_freq: float, dominant_amp: float):
        self.frequencies = frequencies
        self.amplitudes = amplitudes
        self.dominant_freq = dominant_freq
        self.dominant_amp = dominant_amp

    def to_dict(self) -> dict:
        return {
            'frequencies': self.frequencies,
            'amplitudes': self.amplitudes,
            'dominant_frequency_hz': round(self.dominant_freq, 2),
            'dominant_amplitude': round(self.dominant_amp, 4),
            'frequency_bands': self._get_band_energies()
        }

    def _get_band_energies(self) -> dict[str, float]:
        """计算频带能量"""
        bands = {
            '0-100Hz': (0, 100),
            '100-500Hz': (100, 500),
            '500-1000Hz': (500, 1000),
            '1-5kHz': (1000, 5000),
            '5-10kHz': (5000, 10000),
        }
        result = {}
        for name, (low, high) in bands.items():
            energy = 0
            for f, a in zip(self.frequencies, self.amplitudes):
                if low <= f < high:
                    energy += a * a
            result[name] = round(math.sqrt(energy), 4)
        return result


class VibrationAnalyzer:
    """
    振动分析器

    功能：
    1. 接收振动传感器数据
    2. 计算RMS、峰值、峰峰值
    3. FFT频谱分析
    4. ISO 10816等级评估
    5. 趋势监测与预警

    使用方法：
        analyzer = VibrationAnalyzer(database)
        analyzer.start()

        # 喂入数据
        analyzer.feed_data(device_id, register_name, value, timestamp)

        # 查询结果
        scores = analyzer.get_vibration_scores()
        spectrum = analyzer.get_spectrum(device_id)
    """

    def __init__(self, database=None, config: dict[str, Any] = None):
        self.database = database
        self.config = config or {}
        self._lock = threading.Lock()

        # 振动数据缓存（每个设备保留最近1024个采样点用于FFT）
        self._buffer_size = self.config.get('buffer_size', 1024)
        self._buffers: dict[str, deque] = {}  # device_id -> deque[VibrationRecord]

        # 振动评分
        self._scores: dict[str, dict[str, Any]] = {}  # device_id -> score_info

        # 采样率（Hz）
        self._sample_rate = self.config.get('sample_rate', 100)

        # 阈值配置
        self._warning_threshold = self.config.get('warning_threshold', 1.8)  # mm/s
        self._alarm_threshold = self.config.get('alarm_threshold', 4.5)  # mm/s

        self._running = False
        logger.info("振动分析器初始化完成")

    def start(self):
        """启动分析器"""
        self._running = True
        logger.info("振动分析器已启动")

    def stop(self):
        """停止分析器"""
        self._running = False
        logger.info("振动分析器已停止")

    def feed_data(self, device_id: str, register_name: str,
                  value: float, timestamp: datetime = None):
        """
        喂入振动数据

        Args:
            device_id: 设备ID
            register_name: 寄存器名（包含vibration关键字的会被处理）
            value: 振动值
            timestamp: 时间戳
        """
        if not self._running:
            return

        # 只处理振动相关数据
        if 'vibration' not in register_name.lower():
            return

        if timestamp is None:
            timestamp = datetime.now()

        ts = timestamp.timestamp() if hasattr(timestamp, 'timestamp') else float(timestamp)

        record = VibrationRecord(ts, value)

        with self._lock:
            if device_id not in self._buffers:
                self._buffers[device_id] = deque(maxlen=self._buffer_size)
            self._buffers[device_id].append(record)

            # 更新振动评分
            self._update_score(device_id)

    def _update_score(self, device_id: str):
        """更新设备振动评分"""
        buffer = self._buffers.get(device_id)
        if not buffer or len(buffer) < 10:
            return

        values = [r.value for r in buffer]

        # 计算统计指标
        rms = math.sqrt(sum(v * v for v in values) / len(values))
        peak = max(abs(v) for v in values)
        peak_to_peak = max(values) - min(values)

        # ISO 10816等级评估
        zone = self._evaluate_zone(rms)

        # 健康评分（0-100，RMS越大分数越低）
        if rms <= 0.71:
            health = 100
        elif rms <= 1.8:
            health = 100 - (rms - 0.71) / (1.8 - 0.71) * 30
        elif rms <= 4.5:
            health = 70 - (rms - 1.8) / (4.5 - 1.8) * 40
        else:
            health = max(0, 30 - (rms - 4.5) / 4.5 * 30)

        # 趋势检测
        trend = self._detect_trend(values)

        self._scores[device_id] = {
            'device_id': device_id,
            'rms': round(rms, 4),
            'peak': round(peak, 4),
            'peak_to_peak': round(peak_to_peak, 4),
            'zone': zone['name'],
            'zone_color': zone['color'],
            'zone_description': zone['description'],
            'health_score': round(health, 1),
            'trend': trend,
            'sample_count': len(values),
            'updated_at': datetime.now().isoformat(),
        }

    def _evaluate_zone(self, rms: float) -> dict:
        """根据ISO 10816评估振动等级"""
        for zone_name, zone_info in VIBRATION_ZONES.items():
            if rms <= zone_info['max']:
                return {'name': zone_name, **zone_info}
        return {'name': 'D', **VIBRATION_ZONES['D']}

    def _detect_trend(self, values: list[float]) -> str:
        """检测振动趋势"""
        if len(values) < 20:
            return 'stable'

        # 简单线性回归检测趋势
        n = len(values)
        x_mean = (n - 1) / 2
        y_mean = sum(values) / n

        numerator = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
        denominator = sum((i - x_mean) ** 2 for i in range(n))

        if denominator == 0:
            return 'stable'

        slope = numerator / denominator

        # 归一化斜率（相对于均值的变化率）
        if y_mean != 0:
            normalized_slope = slope / abs(y_mean) * n
        else:
            normalized_slope = 0

        if normalized_slope > 0.1:
            return 'rising'
        elif normalized_slope < -0.1:
            return 'falling'
        else:
            return 'stable'

    def get_vibration_scores(self) -> dict[str, dict[str, Any]]:
        """获取所有设备振动评分"""
        with self._lock:
            return dict(self._scores)

    def get_device_vibration(self, device_id: str) -> dict[str, Any] | None:
        """获取指定设备振动评分"""
        with self._lock:
            return self._scores.get(device_id)

    def get_spectrum(self, device_id: str) -> dict[str, Any] | None:
        """
        获取设备振动频谱（FFT分析）

        Args:
            device_id: 设备ID

        Returns:
            频谱分析结果
        """
        with self._lock:
            buffer = self._buffers.get(device_id)
            if not buffer or len(buffer) < 64:
                return None

            values = [r.value for r in buffer]

        # 执行FFT
        fft_result = self._do_fft(values)

        return {
            'device_id': device_id,
            'spectrum': fft_result.to_dict(),
            'sample_count': len(values),
            'sample_rate': self._sample_rate,
            'updated_at': datetime.now().isoformat(),
        }

    def _do_fft(self, values: list[float]) -> FFTResult:
        """执行FFT分析"""
        n = len(values)

        if HAS_NUMPY:
            # 使用numpy的FFT
            fft_vals = np.fft.rfft(values)
            fft_amps = np.abs(fft_vals) / n * 2
            fft_freqs = np.fft.rfftfreq(n, 1.0 / self._sample_rate)

            # 找到主频
            # 跳过DC分量（index=0）
            dominant_idx = np.argmax(fft_amps[1:]) + 1
            dominant_freq = fft_freqs[dominant_idx]
            dominant_amp = fft_amps[dominant_idx]

            return FFTResult(
                frequencies=fft_freqs.tolist(),
                amplitudes=fft_amps.tolist(),
                dominant_freq=float(dominant_freq),
                dominant_amp=float(dominant_amp)
            )
        else:
            # 简化FFT（DFT，只计算前N/2个频率分量）
            n_half = n // 2
            amplitudes = []
            frequencies = []

            for k in range(n_half):
                freq = k * self._sample_rate / n
                real = sum(values[m] * math.cos(2 * math.pi * k * m / n) for m in range(n))
                imag = sum(values[m] * math.sin(2 * math.pi * k * m / n) for m in range(n))
                amp = math.sqrt(real * real + imag * imag) / n * 2

                frequencies.append(freq)
                amplitudes.append(amp)

            # 找到主频（跳过DC）
            dominant_idx = 1
            dominant_amp = 0
            for i in range(1, len(amplitudes)):
                if amplitudes[i] > dominant_amp:
                    dominant_amp = amplitudes[i]
                    dominant_idx = i

            return FFTResult(
                frequencies=frequencies,
                amplitudes=amplitudes,
                dominant_freq=frequencies[dominant_idx] if dominant_idx < len(frequencies) else 0,
                dominant_amp=dominant_amp
            )

    def check_bearing_fault(self, device_id: str, rpm: float) -> dict[str, Any] | None:
        """
        轴承故障频率检测

        Args:
            device_id: 设备ID
            rpm: 转速（RPM）

        Returns:
            轴承故障检测结果
        """
        spectrum = self.get_spectrum(device_id)
        if not spectrum:
            return None

        # 转速频率（Hz）
        shaft_freq = rpm / 60.0

        fft_data = spectrum['spectrum']
        frequencies = fft_data.get('frequencies', [])
        amplitudes = fft_data.get('amplitudes', [])

        if not frequencies or not amplitudes:
            return None

        # 检测各故障频率
        fault_results = {}
        for fault_type, coeff_info in BEARING_FAULT_COEFFICIENTS.items():
            expected_freq = shaft_freq * coeff_info['typical']

            # 在期望频率附近（±10%）找最大幅值
            freq_range = expected_freq * 0.1
            max_amp = 0
            found_freq = 0

            for f, a in zip(frequencies, amplitudes):
                if abs(f - expected_freq) <= freq_range and a > max_amp:
                    max_amp = a
                    found_freq = f

            # 判断是否有故障特征（幅值超过阈值）
            threshold = self.config.get('bearing_fault_threshold', 0.1)
            has_fault = max_amp > threshold

            fault_results[fault_type] = {
                'name': coeff_info['name'],
                'expected_frequency_hz': round(expected_freq, 2),
                'detected_frequency_hz': round(found_freq, 2),
                'amplitude': round(max_amp, 4),
                'has_fault_signature': has_fault,
            }

        # 综合判断
        fault_count = sum(1 for r in fault_results.values() if r['has_fault_signature'])

        return {
            'device_id': device_id,
            'rpm': rpm,
            'shaft_frequency_hz': round(shaft_freq, 2),
            'bearing_faults': fault_results,
            'fault_count': fault_count,
            'diagnosis': '轴承可能存在故障' if fault_count >= 2 else '轴承状态正常',
            'updated_at': datetime.now().isoformat(),
        }

    def get_trend_data(self, device_id: str, hours: int = 24) -> list[dict[str, Any]]:
        """获取振动趋势数据"""
        with self._lock:
            buffer = self._buffers.get(device_id)
            if not buffer:
                return []

            # 返回最近N小时的数据
            cutoff = datetime.now().timestamp() - hours * 3600
            return [r.to_dict() for r in buffer if r.timestamp >= cutoff]
