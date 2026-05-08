"""
OEE设备综合效率计算器 (Overall Equipment Effectiveness)
=========================================================
工业4.0核心指标：OEE = 可用率(A) × 性能率(P) × 质量率(Q)

世界级OEE标准：≥85%
- 可用率 ≥ 90%
- 性能率 ≥ 95%
- 质量率 ≥ 99.9%

本模块实现：
1. 实时OEE计算（基于设备运行状态）
2. OEE六大损失分析
3. 班次/日/周/月OEE统计
4. OEE趋势对比
"""

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any
from collections import defaultdict

logger = logging.getLogger(__name__)


class OEECalculator:
    """
    OEE设备综合效率计算器

    数据来源：
    - 设备运行状态（运行/停机/故障/待料）
    - 实际产量 vs 理论产量
    - 合格品数 vs 总产量
    """

    def __init__(self, database, config: dict[str, Any] | None = None):
        """
        Args:
            database: Database实例
            config: 配置字典，包含设备理论产能等
        """
        self.database = database
        self.config = config or {}

        # 设备理论产能配置（件/小时 或 产品/小时）
        self.theoretical_rates: dict[str, float] = self.config.get('theoretical_rates', {})

        # 设备运行状态追踪
        # device_id -> {'status': 'running'|'stopped'|'fault'|'idle', 'since': datetime}
        self.device_states: dict[str, dict[str, Any]] = {}

        # 上次产量记录（用于计算增量）
        self._last_production: dict[str, dict[str, Any]] = {}

        # 班次数据累积
        # device_id -> {'shift_start': datetime, 'planned_time': float,
        #               'actual_run_time': float, 'downtime': float,
        #               'total_count': int, 'good_count': int, 'ideal_cycle_time': float}
        self.shift_data: dict[str, dict[str, Any]] = defaultdict(lambda: {
            'shift_start': None,
            'planned_production_time': 0,  # 计划生产时间(秒)
            'actual_run_time': 0,          # 实际运行时间(秒)
            'downtime': 0,                 # 停机时间(秒)
            'total_count': 0,              # 总产量
            'good_count': 0,               # 合格品数
            'ideal_cycle_time': 60.0,      # 默认理想节拍60秒/件
        })

        # OEE历史记录
        self.oee_history: list[dict[str, Any]] = []

        # 锁（使用RLock避免死锁：get_all_oee会调用calculate_oee，两者都需要加锁）
        self._lock = threading.RLock()

        # 运行状态
        self._running = False
        self._thread = None

        logger.info("OEE计算器初始化完成")

    def start(self):
        """启动OEE计算"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._calc_loop, daemon=True)
        self._thread.start()
        logger.info("OEE计算器已启动")

    def stop(self):
        """停止OEE计算"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _calc_loop(self):
        """定时计算循环"""
        while self._running:
            try:
                self._periodic_calc()
            except Exception as e:
                logger.error(f"OEE计算异常: {e}", exc_info=True)
            time.sleep(300)  # 每5分钟计算一次

    def _periodic_calc(self):
        """定期计算并记录OEE"""
        with self._lock:
            for device_id in list(self.shift_data.keys()):
                oee = self.calculate_oee(device_id)
                if oee:
                    oee['calculated_at'] = datetime.now().isoformat()
                    self.oee_history.append(oee)
                    # 保留最近1000条
                    if len(self.oee_history) > 1000:
                        self.oee_history = self.oee_history[-1000:]

    # ==================== 数据输入接口 ====================

    def update_device_state(self, device_id: str, status: str):
        """
        更新设备运行状态

        Args:
            device_id: 设备ID
            status: 'running' | 'stopped' | 'fault' | 'idle'
        """
        now = datetime.now()

        with self._lock:
            old_state = self.device_states.get(device_id, {})
            old_status = old_state.get('status')
            old_since = old_state.get('since', now)

            # 计算上一个状态的持续时间
            duration = (now - old_since).total_seconds()

            # 更新状态时间统计
            sd = self.shift_data[device_id]
            if sd['shift_start'] is None:
                sd['shift_start'] = now
            # 如果没有设置计划生产时间，默认8小时
            if sd['planned_production_time'] <= 0:
                sd['planned_production_time'] = 8 * 3600  # 8小时 = 28800秒

            if old_status == 'running':
                sd['actual_run_time'] += duration
            elif old_status in ('stopped', 'fault', 'idle'):
                sd['downtime'] += duration

            # 更新状态
            self.device_states[device_id] = {
                'status': status,
                'since': now,
            }

    def record_production(self, device_id: str, count: int | None = None, good_count: int | None = None):
        """
        记录产量（支持绝对值和增量两种模式）

        Args:
            device_id: 设备ID
            count: 总产量（绝对值，自动计算增量）
            good_count: 合格品数（绝对值，自动计算增量）
        """
        with self._lock:
            sd = self.shift_data[device_id]
            # 确保_last_production中存在该设备的记录，且包含count和good两个键
            if device_id not in self._last_production:
                self._last_production[device_id] = {'count': 0, 'good': 0}
            last = self._last_production[device_id]

            if count is not None:
                # 计算增量（处理计数器归零的情况）
                delta = count - last.get('count', 0)
                if delta < 0:
                    delta = count  # 计数器归零
                sd['total_count'] += max(0, delta)
                self._last_production[device_id]['count'] = count

            if good_count is not None:
                delta = good_count - last.get('good', 0)
                if delta < 0:
                    delta = good_count
                sd['good_count'] += max(0, delta)
                self._last_production[device_id]['good'] = good_count

            # 如果没有传入任何参数，默认+1
            if count is None and good_count is None:
                sd['total_count'] += 1
                sd['good_count'] += 1

    def set_theoretical_rate(self, device_id: str, rate: float):
        """
        设置设备理论产能

        Args:
            device_id: 设备ID
            rate: 件/小时
        """
        with self._lock:
            self.theoretical_rates[device_id] = rate
            # 理想节拍时间 = 3600 / rate (秒/件)
            if rate > 0:
                self.shift_data[device_id]['ideal_cycle_time'] = 3600.0 / rate

    def start_shift(self, device_id: str, planned_hours: float = 8.0):
        """
        开始新班次

        Args:
            device_id: 设备ID
            planned_hours: 计划生产时间(小时)
        """
        with self._lock:
            self.shift_data[device_id] = {
                'shift_start': datetime.now(),
                'planned_production_time': planned_hours * 3600,
                'actual_run_time': 0,
                'downtime': 0,
                'total_count': 0,
                'good_count': 0,
                'ideal_cycle_time': self.shift_data[device_id].get('ideal_cycle_time', 0),
            }
            logger.info(f"设备 {device_id} 新班次开始，计划生产 {planned_hours} 小时")

    # ==================== OEE计算核心 ====================

    def calculate_oee(self, device_id: str) -> dict[str, Any] | None:
        """
        计算指定设备的OEE

        OEE = A × P × Q

        A (可用率) = 实际运行时间 / 计划生产时间
        P (性能率) = (总产量 × 理想节拍) / 实际运行时间
        Q (质量率) = 合格品数 / 总产量

        Returns:
            {
                'device_id': str,
                'availability': float,  # 可用率 (0-1)
                'performance': float,   # 性能率 (0-1)
                'quality': float,       # 质量率 (0-1)
                'oee': float,           # OEE (0-1)
                'oee_percent': float,   # OEE百分比
                'losses': dict[str, Any],         # 六大损失分析
            }
        """
        with self._lock:
            sd = self.shift_data.get(device_id)
            if not sd or sd['shift_start'] is None:
                return None

            # 计算当前运行时间（如果设备正在运行，加上当前状态的时间）
            now = datetime.now()
            current_state = self.device_states.get(device_id, {})
            extra_time = 0

            if current_state.get('status') == 'running':
                extra_time = (now - current_state.get('since', now)).total_seconds()

            actual_run = sd['actual_run_time'] + extra_time
            planned = sd['planned_production_time']

            if planned <= 0:
                return None

            # 可用率
            availability = min(1.0, actual_run / planned) if planned > 0 else 0

            # 性能率
            ideal_cycle = sd.get('ideal_cycle_time', 0)
            total_count = sd['total_count']

            if ideal_cycle > 0 and actual_run > 0:
                performance = min(1.0, (total_count * ideal_cycle) / actual_run)
            else:
                performance = 0

            # 质量率
            good_count = sd['good_count']
            quality = (good_count / total_count) if total_count > 0 else 1.0

            # OEE
            oee = availability * performance * quality

            # 六大损失分析
            losses = self._calculate_losses(device_id, sd, availability, performance, quality)

            return {
                'device_id': device_id,
                'shift_start': sd['shift_start'].isoformat(),
                'planned_production_time': round(planned, 1),
                'actual_run_time': round(actual_run, 1),
                'downtime': round(sd['downtime'], 1),
                'total_count': total_count,
                'good_count': good_count,
                'defect_count': total_count - good_count,
                'availability': round(availability, 4),
                'performance': round(performance, 4),
                'quality': round(quality, 4),
                'oee': round(oee, 4),
                'oee_percent': round(oee * 100, 1),
                'losses': losses,
                'grade': self._oee_grade(oee),
            }

    def _calculate_losses(self, device_id: str, sd: dict[str, Any],
                           availability: float, performance: float,
                           quality: float) -> dict[str, Any]:
        """
        六大损失分析

        1. 故障损失 (Availability) — 设备故障停机
        2. 换装调整损失 (Availability) — 换模/换线/调整
        3. 空转短暂停机损失 (Performance) — 小停机/空转
        4. 速度降低损失 (Performance) — 实际速度低于理论速度
        5. 不良品损失 (Quality) — 废品/返工
        6. 开机损失 (Quality) — 开机阶段的不良品
        """
        planned = sd['planned_production_time']
        actual_run = sd['actual_run_time']
        downtime = sd['downtime']
        total_count = sd['total_count']
        good_count = sd['good_count']
        ideal_cycle = sd.get('ideal_cycle_time', 0)

        # 可用率损失
        availability_loss = (1 - availability) * planned

        # 性能率损失
        if ideal_cycle > 0 and actual_run > 0:
            ideal_output = actual_run / ideal_cycle
            speed_loss_count = max(0, ideal_output - total_count)
            performance_loss = speed_loss_count * ideal_cycle
        else:
            performance_loss = 0

        # 质量率损失
        defect_count = total_count - good_count
        quality_loss = defect_count * ideal_cycle if ideal_cycle > 0 else 0

        return {
            '故障停机损失_秒': round(downtime, 1),
            '可用率损失占比_百分比': round((1 - availability) * 100, 1),
            '性能损失_秒': round(performance_loss, 1),
            '性能率损失占比_百分比': round((1 - performance) * 100, 1),
            '不良品数': defect_count,
            '质量损失_秒': round(quality_loss, 1),
            '质量率损失占比_百分比': round((1 - quality) * 100, 1),
        }

    def _oee_grade(self, oee: float) -> str:
        """OEE等级评定"""
        if oee >= 0.85:
            return '世界级'
        elif oee >= 0.75:
            return '优秀'
        elif oee >= 0.65:
            return '良好'
        elif oee >= 0.50:
            return '一般'
        else:
            return '需改进'

    # ==================== 查询接口 ====================

    def get_all_oee(self) -> dict[str, dict[str, Any]]:
        """获取所有设备的当前OEE"""
        results = {}
        with self._lock:
            for device_id in self.shift_data.keys():
                oee = self.calculate_oee(device_id)
                if oee:
                    results[device_id] = oee
        return results

    def get_device_oee(self, device_id: str) -> dict[str, Any] | None:
        """获取指定设备的OEE"""
        return self.calculate_oee(device_id)

    def get_oee_history(self, device_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """获取OEE历史记录"""
        with self._lock:
            if device_id:
                return [h for h in self.oee_history if h['device_id'] == device_id][-limit:]
            return self.oee_history[-limit:]

    def get_device_state(self, device_id: str) -> dict[str, Any] | None:
        """获取设备当前状态"""
        with self._lock:
            return self.device_states.get(device_id)

    def get_all_device_states(self) -> dict[str, dict[str, Any]]:
        """获取所有设备状态"""
        with self._lock:
            return dict(self.device_states)
