"""
数据聚合报告生成器
自动生成设备运行、报警统计、能耗分析等报告模板。

使用方式:
    from core.report_generator import ReportGenerator
    generator = ReportGenerator(db)
    report = generator.generate_device_report('pump_001', period='day')
"""

import logging
import sqlite3
import time
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class ReportTemplate:
    """报告模板"""

    def __init__(self, name: str, title: str, sections: List[Dict[str, Any]]):
        self.name = name
        self.title = title
        self.sections = sections

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'title': self.title,
            'sections': self.sections,
        }


class ReportGenerator:
    """报告生成器"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._templates: Dict[str, ReportTemplate] = {}
        self._register_default_templates()

    def _register_default_templates(self):
        """注册默认报告模板"""
        self._templates['device_daily'] = ReportTemplate(
            'device_daily',
            '设备日报',
            [
                {'type': 'summary', 'title': '运行概况'},
                {'type': 'chart', 'title': '数据趋势', 'chart_type': 'line'},
                {'type': 'table', 'title': '报警记录'},
                {'type': 'statistics', 'title': '统计数据'},
            ]
        )

        self._templates['alarm_summary'] = ReportTemplate(
            'alarm_summary',
            '报警汇总',
            [
                {'type': 'summary', 'title': '报警概况'},
                {'type': 'chart', 'title': '报警趋势', 'chart_type': 'bar'},
                {'type': 'table', 'title': '报警详情'},
                {'type': 'statistics', 'title': '报警统计'},
            ]
        )

        self._templates['energy_analysis'] = ReportTemplate(
            'energy_analysis',
            '能耗分析',
            [
                {'type': 'summary', 'title': '能耗概况'},
                {'type': 'chart', 'title': '能耗趋势', 'chart_type': 'line'},
                {'type': 'table', 'title': '设备能耗排名'},
                {'type': 'statistics', 'title': '能耗统计'},
            ]
        )

    def generate_device_report(
        self,
        device_id: str,
        period: str = 'day',
        start_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        生成设备报告

        Args:
            device_id: 设备ID
            period: 时间段（hour/day/week/month）
            start_date: 开始日期（ISO格式）

        Returns:
            报告数据
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row

        try:
            # 计算时间范围
            if start_date:
                end_time = datetime.fromisoformat(start_date)
            else:
                end_time = datetime.now()

            period_deltas = {
                'hour': timedelta(hours=1),
                'day': timedelta(days=1),
                'week': timedelta(weeks=1),
                'month': timedelta(days=30),
            }
            start_time = end_time - period_deltas.get(period, timedelta(days=1))

            # 获取数据统计
            stats = self._get_device_stats(conn, device_id, start_time, end_time)

            # 获取报警记录
            alarms = self._get_device_alarms(conn, device_id, start_time, end_time)

            # 获取数据趋势
            trend = self._get_device_trend(conn, device_id, start_time, end_time)

            return {
                'report_type': 'device_report',
                'device_id': device_id,
                'period': period,
                'start_time': start_time.isoformat(),
                'end_time': end_time.isoformat(),
                'generated_at': datetime.now().isoformat(),
                'summary': stats,
                'alarms': alarms,
                'trend': trend,
            }

        finally:
            conn.close()

    def generate_alarm_report(
        self,
        period: str = 'day',
        device_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """生成报警汇总报告"""
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row

        try:
            end_time = datetime.now()
            period_deltas = {
                'hour': timedelta(hours=1),
                'day': timedelta(days=1),
                'week': timedelta(weeks=1),
                'month': timedelta(days=30),
            }
            start_time = end_time - period_deltas.get(period, timedelta(days=1))

            # 报警统计
            stats = self._get_alarm_stats(conn, start_time, end_time, device_id)

            # 报警趋势
            trend = self._get_alarm_trend(conn, start_time, end_time, device_id)

            # Top报警设备
            top_devices = self._get_top_alarm_devices(conn, start_time, end_time)

            return {
                'report_type': 'alarm_report',
                'period': period,
                'start_time': start_time.isoformat(),
                'end_time': end_time.isoformat(),
                'generated_at': datetime.now().isoformat(),
                'summary': stats,
                'trend': trend,
                'top_devices': top_devices,
            }

        finally:
            conn.close()

    def _get_device_stats(
        self, conn: sqlite3.Connection,
        device_id: str, start: datetime, end: datetime
    ) -> Dict[str, Any]:
        """获取设备统计数据"""
        try:
            # 数据点数量
            cursor = conn.execute(
                'SELECT COUNT(*) as cnt FROM history_data WHERE device_id = ? AND timestamp BETWEEN ? AND ?',
                (device_id, start.isoformat(), end.isoformat())
            )
            data_count = cursor.fetchone()['cnt']

            # 报警数量
            cursor = conn.execute(
                'SELECT COUNT(*) as cnt FROM alarm_records WHERE device_id = ? AND timestamp BETWEEN ? AND ?',
                (device_id, start.isoformat(), end.isoformat())
            )
            alarm_count = cursor.fetchone()['cnt']

            return {
                'data_points': data_count,
                'alarm_count': alarm_count,
                'period_hours': (end - start).total_seconds() / 3600,
            }
        except Exception as e:
            logger.warning(f"获取设备统计失败: {e}")
            return {'data_points': 0, 'alarm_count': 0}

    def _get_device_alarms(
        self, conn: sqlite3.Connection,
        device_id: str, start: datetime, end: datetime
    ) -> List[Dict[str, Any]]:
        """获取设备报警记录"""
        try:
            cursor = conn.execute(
                'SELECT * FROM alarm_records WHERE device_id = ? AND timestamp BETWEEN ? AND ? ORDER BY timestamp DESC LIMIT 50',
                (device_id, start.isoformat(), end.isoformat())
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.warning(f"获取设备报警失败: {e}")
            return []

    def _get_device_trend(
        self, conn: sqlite3.Connection,
        device_id: str, start: datetime, end: datetime
    ) -> List[Dict[str, Any]]:
        """获取设备数据趋势"""
        try:
            cursor = conn.execute(
                '''SELECT register_name, COUNT(*) as count, AVG(value) as avg_value,
                   MIN(value) as min_value, MAX(value) as max_value
                   FROM history_data
                   WHERE device_id = ? AND timestamp BETWEEN ? AND ?
                   GROUP BY register_name''',
                (device_id, start.isoformat(), end.isoformat())
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.warning(f"获取设备趋势失败: {e}")
            return []

    def _get_alarm_stats(
        self, conn: sqlite3.Connection,
        start: datetime, end: datetime, device_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """获取报警统计"""
        try:
            if device_id:
                cursor = conn.execute(
                    'SELECT COUNT(*) as cnt FROM alarm_records WHERE device_id = ? AND timestamp BETWEEN ? AND ?',
                    (device_id, start.isoformat(), end.isoformat())
                )
            else:
                cursor = conn.execute(
                    'SELECT COUNT(*) as cnt FROM alarm_records WHERE timestamp BETWEEN ? AND ?',
                    (start.isoformat(), end.isoformat())
                )
            total = cursor.fetchone()['cnt']

            # 按级别统计
            cursor = conn.execute(
                '''SELECT alarm_level, COUNT(*) as cnt FROM alarm_records
                   WHERE timestamp BETWEEN ? AND ?
                   GROUP BY alarm_level''',
                (start.isoformat(), end.isoformat())
            )
            by_level = {row['alarm_level']: row['cnt'] for row in cursor.fetchall()}

            return {
                'total_alarms': total,
                'by_level': by_level,
            }
        except Exception as e:
            logger.warning(f"获取报警统计失败: {e}")
            return {'total_alarms': 0, 'by_level': {}}

    def _get_alarm_trend(
        self, conn: sqlite3.Connection,
        start: datetime, end: datetime, device_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """获取报警趋势"""
        try:
            cursor = conn.execute(
                '''SELECT DATE(timestamp) as date, COUNT(*) as count
                   FROM alarm_records
                   WHERE timestamp BETWEEN ? AND ?
                   GROUP BY DATE(timestamp) ORDER BY date''',
                (start.isoformat(), end.isoformat())
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.warning(f"获取报警趋势失败: {e}")
            return []

    def _get_top_alarm_devices(
        self, conn: sqlite3.Connection,
        start: datetime, end: datetime, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """获取报警最多的设备"""
        try:
            cursor = conn.execute(
                '''SELECT device_id, COUNT(*) as alarm_count
                   FROM alarm_records
                   WHERE timestamp BETWEEN ? AND ?
                   GROUP BY device_id ORDER BY alarm_count DESC LIMIT ?''',
                (start.isoformat(), end.isoformat(), limit)
            )
            return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.warning(f"获取Top报警设备失败: {e}")
            return []

    def get_templates(self) -> List[Dict[str, Any]]:
        """获取所有报告模板"""
        return [t.to_dict() for t in self._templates.values()]

    def get_template(self, name: str) -> Optional[Dict[str, Any]]:
        """获取指定模板"""
        template = self._templates.get(name)
        return template.to_dict() if template else None


def create_report_response(report: Dict[str, Any]) -> Dict[str, Any]:
    """创建报告响应"""
    return {
        'success': True,
        'report': report,
    }
