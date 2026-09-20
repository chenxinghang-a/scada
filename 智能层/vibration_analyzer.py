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

采样率（关键前提）：
    FFT 的频率轴 = k * fs / N，fs 错则整条频谱错。振动数据源（PLC 轮询/寄存器
    采集）通常**没有固定采样率**——采集周期受轮询、网络、调度抖动影响，
    因此本模块不再假定任何默认 fs：

    - 采样率来源优先级：feed_data(sample_rate=...) > config['device_sample_rates']
      [device_id] > config['sample_rate'] 全局配置；
    - 三者都取不到时 → `get_spectrum()` 返回 available=False 并给出原因，
      **不产出频谱**（宁可不给，也不给出无物理意义的假频谱）；
    - `_do_fft()` 的 sample_rate 为必填位置参数，从签名上杜绝"默认 100Hz"复用。

依赖：numpy（可选，无numpy时使用简化DFT）
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

        # 采样率（Hz）：None 表示"数据源未声明采样率"——此时频谱不可用，
        # 绝不用假定值（如 100Hz）代替。见模块 docstring。
        self._sample_rate = self._validate_sample_rate(self.config.get('sample_rate'))

        # 每设备采样率（Hz）：device_id -> float，形如
        #   {'device_sample_rates': {'vibration_sensor_01': 2000}}
        self._device_sample_rates: dict[str, float] = {}
        for dev_id, rate in (self.config.get('device_sample_rates') or {}).items():
            valid = self._validate_sample_rate(rate)
            if valid is not None:
                self._device_sample_rates[dev_id] = valid

        # feed_data 时随数据点显式上报的采样率（最高优先级）
        self._feed_sample_rates: dict[str, float] = {}

        # 阈值配置
        self._warning_threshold = self.config.get('warning_threshold', 1.8)  # mm/s
        self._alarm_threshold = self.config.get('alarm_threshold', 4.5)  # mm/s

        # 运行状态与后台分析线程
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._analysis_interval = self.config.get('analysis_interval_s', 5.0)
        # 已告警过的设备（避免后台线程重复刷同一条告警）
        self._alerted: set[str] = set()

        logger.info(
            "振动分析器初始化完成（采样率=%s）",
            f"{self._sample_rate}Hz" if self._sample_rate else "未配置，频谱功能将降级为不可用"
        )

    @staticmethod
    def _validate_sample_rate(rate: Any) -> float | None:
        """校验采样率：非数值或 <=0 一律视为"未知采样率"（返回 None）"""
        if rate is None or isinstance(rate, bool):
            return None
        try:
            value = float(rate)
        except (TypeError, ValueError):
            return None
        if value <= 0 or math.isnan(value) or math.isinf(value):
            return None
        return value

    def resolve_sample_rate(self, device_id: str) -> float | None:
        """
        解析设备的真实采样率（Hz）

        优先级：feed_data 显式上报 > config['device_sample_rates'] > 全局 config['sample_rate']
        全部取不到返回 None（调用方必须降级，不得假定）。
        """
        with self._lock:
            rate = self._feed_sample_rates.get(device_id)
            if rate is None:
                rate = self._device_sample_rates.get(device_id)
        if rate is not None:
            return rate
        return self._sample_rate

    def start(self):
        """启动分析器（含后台分析线程）"""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._analysis_loop, name='vibration-analyzer', daemon=True
        )
        self._thread.start()
        logger.info("振动分析器已启动（分析周期 %.1fs）", self._analysis_interval)

    def stop(self, timeout: float = 5.0):
        """停止分析器并回收分析线程"""
        self._running = False
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning("振动分析线程未在 %.1fs 内退出", timeout)
        self._thread = None
        logger.info("振动分析器已停止")

    def _analysis_loop(self):
        """后台分析循环：周期性重算振动评分并做阈值告警"""
        while not self._stop_event.wait(self._analysis_interval):
            if not self._running:
                break
            try:
                self.refresh_scores()
            except Exception as e:  # 后台线程不能因单次异常退出
                logger.error(f"振动分析周期异常: {e}", exc_info=True)

    def refresh_scores(self) -> list[str]:
        """
        重算所有已缓存设备的振动评分/趋势，并对越过阈值的设备告警一次

        Returns:
            本次刷新过的设备ID列表
        """
        with self._lock:
            device_ids = list(self._buffers.keys())

        for device_id in device_ids:
            with self._lock:
                self._update_score(device_id)
                score = self._scores.get(device_id)

            if not score:
                continue
            rms = score['rms']
            if rms >= self._alarm_threshold and device_id not in self._alerted:
                self._alerted.add(device_id)
                logger.error(
                    "振动超标告警: %s RMS=%.2fmm/s (阈值 %.2f) 等级=%s",
                    device_id, rms, self._alarm_threshold, score['zone'],
                )
            elif rms < self._warning_threshold:
                self._alerted.discard(device_id)
        return device_ids

    def feed_data(self, device_id: str, register_name: str,
                  value: float, timestamp: datetime = None,
                  sample_rate: float | None = None):
        """
        喂入振动数据

        Args:
            device_id: 设备ID
            register_name: 寄存器名（包含vibration关键字的会被处理）
            value: 振动值
            timestamp: 时间戳
            sample_rate: 该数据点所属波形的采样率(Hz)。只有数据源真实提供采样率时
                才应传入；本值一旦注册即用于该设备的 FFT 频率轴。
        """
        if not self._running:
            return

        # 只处理振动相关数据
        if 'vibration' not in register_name.lower():
            return

        if timestamp is None:
            timestamp = datetime.now()

        ts = timestamp.timestamp() if hasattr(timestamp, 'timestamp') else float(timestamp)

        rate = self._validate_sample_rate(sample_rate)

        record = VibrationRecord(ts, value)

        with self._lock:
            if rate is not None:
                self._feed_sample_rates[device_id] = rate
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
            - 采样率已知：{'available': True, 'spectrum': {...}, 'sample_rate': fs, ...}
            - 采样率未知：{'available': False, 'spectrum': None, 'reason': '...', ...}
              （FFT 频率轴依赖真实 fs，未知则频谱无物理意义 → 降级，不产出假频谱）
            - 数据不足（<64点）或无该设备：None
        """
        with self._lock:
            buffer = self._buffers.get(device_id)
            if not buffer or len(buffer) < 64:
                return None

            values = [r.value for r in buffer]

        sample_rate = self.resolve_sample_rate(device_id)
        now_iso = datetime.now().isoformat()

        if sample_rate is None:
            logger.warning(
                "振动频谱不可用: 设备 %s 未提供采样率，FFT 频率轴无法确定（已降级）",
                device_id,
            )
            return {
                'device_id': device_id,
                'available': False,
                'reason': (
                    '采样率未知：数据源未声明固定采样率，FFT 频率轴无法确定，'
                    '频谱结果无物理意义，已降级为不可用。'
                    '请通过 feed_data(sample_rate=...) 或配置 device_sample_rates/sample_rate 提供'
                ),
                'spectrum': None,
                'sample_count': len(values),
                'sample_rate': None,
                'updated_at': now_iso,
            }

        # 执行FFT（显式传入真实采样率）
        fft_result = self._do_fft(values, sample_rate)

        return {
            'device_id': device_id,
            'available': True,
            'spectrum': fft_result.to_dict(),
            'sample_count': len(values),
            'sample_rate': sample_rate,
            'updated_at': now_iso,
        }

    def _do_fft(self, values: list[float], sample_rate: float) -> FFTResult:
        """
        执行FFT分析

        Args:
            values: 时域采样序列
            sample_rate: **真实**采样率(Hz)。必填且必须 >0 —— 频率轴 = k*fs/N，
                没有任何合理的"默认值"，因此本参数刻意不设默认（防止误用假定频率）。
        """
        if sample_rate is None or sample_rate <= 0:
            raise ValueError("FFT 需要有效的真实采样率(Hz)，收到: %r" % (sample_rate,))

        n = len(values)

        if HAS_NUMPY:
            # 使用numpy的FFT
            fft_vals = np.fft.rfft(values)
            fft_amps = np.abs(fft_vals) / n * 2
            fft_freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)

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
                freq = k * sample_rate / n
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
            - 频谱可用：故障特征检测结果
            - 频谱不可用（采样率未知）：'diagnosis' 明确说明判定已跳过，不做假判断
            - 无数据：None
        """
        spectrum = self.get_spectrum(device_id)
        if not spectrum:
            return None

        if not spectrum.get('available', True):
            # 没有可信频谱 → 不做轴承故障判定（避免基于假频率给出"正常/故障"结论）
            logger.warning("轴承故障判定跳过（设备 %s）: %s", device_id, spectrum.get('reason'))
            return {
                'device_id': device_id,
                'rpm': rpm,
                'available': False,
                'reason': spectrum.get('reason'),
                'bearing_faults': {},
                'fault_count': 0,
                'diagnosis': '频谱不可用（采样率未知），轴承故障判定已跳过',
                'updated_at': datetime.now().isoformat(),
            }

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
