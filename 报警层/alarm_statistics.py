"""
报警统计分析（ISA-18.2 / IEC 62682）
=====================================

指标：
- 报警率 (Alarm Rate): 每小时报警数
- Standing Alarm: 长时间未确认的报警
- Chattering Alarm: 短时间内反复触发/清除的报警（抖动报警）
- Flood Alarm: 报警洪峰（短时间内大量报警）
- Suppressed Alarm: 被抑制的报警统计

行业基准（ISA-18.2）:
- 理想报警率: ≤6 次/小时 (每操作员)
- 可接受: ≤12 次/小时
- 需要审查: ≤24 次/小时
- 不可接受: >24 次/小时
"""

import logging
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)


class AlarmStatistics:
    """报警统计分析器"""

    def __init__(self, alarm_manager=None, database=None):
        """
        Args:
            alarm_manager: AlarmManager 实例
            database: 数据库实例（用于查询历史报警）
        """
        self.alarm_manager = alarm_manager
        self.database = database

        # 报警触发记录（用于计算报警率和抖动检测）
        # key: (alarm_id, device_id, register_name) -> list of timestamps
        self._alarm_history: dict[tuple, list[datetime]] = defaultdict(list)
        self._max_history = 10000

        # ISA-18.2 基准
        self.RATE_IDEAL = 6      # 理想报警率 (次/小时)
        self.RATE_ACCEPTABLE = 12  # 可接受
        self.RATE_REVIEW = 24     # 需要审查

        # 抖动检测参数
        self.CHATTER_WINDOW = 10    # 秒：检测窗口
        self.CHATTER_THRESHOLD = 3  # 次：窗口内触发次数达到此值判定为抖动

        # Standing Alarm 参数
        self.STANDING_THRESHOLD = timedelta(minutes=30)  # 未确认超过此时间

        # Flood Alarm 参数
        self.FLOOD_WINDOW = 60     # 秒
        self.FLOOD_THRESHOLD = 20  # 次

    def record_alarm_trigger(self, alarm_id: str, device_id: str,
                              register_name: str, timestamp: datetime = None):
        """
        记录报警触发事件

        Args:
            alarm_id: 报警ID
            device_id: 设备ID
            register_name: 寄存器名
            timestamp: 触发时间
        """
        key = (alarm_id, device_id, register_name)
        ts = timestamp or datetime.now()
        self._alarm_history[key].append(ts)

        # 裁剪历史
        if len(self._alarm_history[key]) > self._max_history:
            self._alarm_history[key] = self._alarm_history[key][-self._max_history:]

    def get_alarm_rate(self, hours: float = 1.0) -> dict[str, Any]:
        """
        计算报警率

        Args:
            hours: 统计时间窗口（小时）

        Returns:
            dict: 报警率统计
        """
        cutoff = datetime.now() - timedelta(hours=hours)
        total_count = 0

        for key, timestamps in self._alarm_history.items():
            count = sum(1 for ts in timestamps if ts > cutoff)
            total_count += count

        rate = total_count / hours if hours > 0 else 0

        # 评级
        if rate <= self.RATE_IDEAL:
            rating = '理想'
            color = 'green'
        elif rate <= self.RATE_ACCEPTABLE:
            rating = '可接受'
            color = 'yellow'
        elif rate <= self.RATE_REVIEW:
            rating = '需要审查'
            color = 'orange'
        else:
            rating = '不可接受'
            color = 'red'

        return {
            'rate_per_hour': round(rate, 2),
            'total_count': total_count,
            'window_hours': hours,
            'rating': rating,
            'color': color,
            'benchmarks': {
                'ideal': self.RATE_IDEAL,
                'acceptable': self.RATE_ACCEPTABLE,
                'review': self.RATE_REVIEW,
            }
        }

    def detect_chattering_alarms(self) -> list[dict[str, Any]]:
        """
        检测抖动报警（Chattering Alarm）

        短时间内反复触发/清除的报警，通常由阈值附近信号抖动引起。
        应该通过增大死区(Deadband)来解决。

        Returns:
            list: 抖动报警列表
        """
        cutoff = datetime.now() - timedelta(minutes=30)
        chattering = []

        for key, timestamps in self._alarm_history.items():
            recent = [ts for ts in timestamps if ts > cutoff]
            if len(recent) < self.CHATTER_THRESHOLD:
                continue

            # 检测窗口内的触发次数
            for i in range(len(recent) - self.CHATTER_THRESHOLD + 1):
                window_start = recent[i]
                window_end = recent[i + self.CHATTER_THRESHOLD - 1]
                if (window_end - window_start).total_seconds() <= self.CHATTER_WINDOW:
                    chattering.append({
                        'alarm_id': key[0],
                        'device_id': key[1],
                        'register_name': key[2],
                        'trigger_count': len(recent),
                        'window_seconds': (window_end - window_start).total_seconds(),
                        'suggestion': '增大死区(Deadband)或增加延迟(Delay)',
                    })
                    break

        return sorted(chattering, key=lambda x: x['trigger_count'], reverse=True)

    def detect_standing_alarms(self) -> list[dict[str, Any]]:
        """
        检测 Standing Alarm（长时间未确认的报警）

        Returns:
            list: Standing Alarm 列表
        """
        if not self.alarm_manager:
            return []

        standing = []
        active_alarms = self.alarm_manager.get_active_alarms()

        for alarm in active_alarms:
            if alarm.get('acknowledged'):
                continue

            first_trigger = alarm.get('first_trigger_time')
            if first_trigger is None:
                continue

            if isinstance(first_trigger, str):
                first_trigger = datetime.fromisoformat(first_trigger)

            duration = datetime.now() - first_trigger
            if duration > self.STANDING_THRESHOLD:
                standing.append({
                    'alarm_id': alarm['alarm_id'],
                    'device_id': alarm['device_id'],
                    'register_name': alarm['register_name'],
                    'alarm_level': alarm.get('alarm_level'),
                    'alarm_message': alarm.get('alarm_message'),
                    'duration_minutes': int(duration.total_seconds() / 60),
                    'first_trigger': first_trigger.isoformat(),
                })

        return sorted(standing, key=lambda x: x['duration_minutes'], reverse=True)

    def detect_flood(self) -> dict[str, Any]:
        """
        检测报警洪峰（Flood Alarm）

        Returns:
            dict: 洪峰状态
        """
        cutoff = datetime.now() - timedelta(seconds=self.FLOOD_WINDOW)
        recent_count = 0

        for key, timestamps in self._alarm_history.items():
            recent_count += sum(1 for ts in timestamps if ts > cutoff)

        return {
            'is_flooding': recent_count >= self.FLOOD_THRESHOLD,
            'count': recent_count,
            'window_seconds': self.FLOOD_WINDOW,
            'threshold': self.FLOOD_THRESHOLD,
        }

    def get_comprehensive_report(self) -> dict[str, Any]:
        """
        获取综合报警统计报告

        Returns:
            dict: 包含所有指标的报告
        """
        return {
            'timestamp': datetime.now().isoformat(),
            'alarm_rate': self.get_alarm_rate(hours=1.0),
            'alarm_rate_8h': self.get_alarm_rate(hours=8.0),
            'chattering': self.detect_chattering_alarms(),
            'standing': self.detect_standing_alarms(),
            'flood': self.detect_flood(),
            'active_alarm_count': len(self.alarm_manager.get_active_alarms()) if self.alarm_manager else 0,
            'total_tracked_rules': len(self._alarm_history),
        }
