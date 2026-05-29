"""
报警KPI监控模块
实现ISA-18.2标准的报警管理KPI指标

ISA-18.2标准要求：
1. 每小时平均报警数 < 6（理想）< 12（可接受）
2. 10分钟内峰值报警数 < 10
3. 常驻报警数 < 10
4. Top 10最频繁报警
5. 报警优先级分布：80%低，15%中，5%高
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from collections import defaultdict, Counter

logger = logging.getLogger(__name__)


class AlarmKPI:
    """
    报警KPI监控类
    
    实现ISA-18.2标准的报警管理KPI指标
    """
    
    def __init__(self, database, alarm_manager=None):
        """
        初始化报警KPI监控
        
        Args:
            database: 数据库实例
            alarm_manager: 报警管理器实例
        """
        self.database = database
        self.alarm_manager = alarm_manager
        
        # KPI阈值配置
        self.thresholds = {
            'avg_alarms_per_hour': {
                'ideal': 6,
                'acceptable': 12,
                'poor': 30
            },
            'peak_alarms_10min': {
                'ideal': 10,
                'acceptable': 20,
                'poor': 50
            },
            'standing_alarms': {
                'ideal': 10,
                'acceptable': 20,
                'poor': 100
            },
            'priority_distribution': {
                'low': 0.80,      # 80%低优先级
                'medium': 0.15,   # 15%中优先级
                'high': 0.05      # 5%高优先级
            }
        }
        
        # 报警历史缓存
        self._alarm_history = []
        self._max_history = 10000
        
        logger.info("报警KPI监控初始化完成")

    def calculate_kpis(self, hours: int = 24) -> Dict[str, Any]:
        """
        计算报警KPI指标
        
        Args:
            hours: 计算时间范围（小时）
            
        Returns:
            KPI指标字典
        """
        end_time = datetime.now()
        start_time = end_time - timedelta(hours=hours)
        
        # 获取报警记录
        alarms = self._get_alarms_in_period(start_time, end_time)
        
        # 计算各项KPI
        avg_alarms_per_hour = self._calculate_avg_alarms_per_hour(alarms, hours)
        peak_alarms_10min = self._calculate_peak_alarms_10min(alarms)
        standing_alarms = self._get_standing_alarms()
        priority_distribution = self._calculate_priority_distribution(alarms)
        top_10_alarms = self._get_top_10_alarms(alarms)
        alarm_rate_trend = self._calculate_alarm_rate_trend(alarms, hours)
        
        # 评估KPI状态
        kpi_status = self._evaluate_kpi_status(
            avg_alarms_per_hour, peak_alarms_10min, standing_alarms, priority_distribution
        )
        
        return {
            'period': {
                'start': start_time.isoformat(),
                'end': end_time.isoformat(),
                'hours': hours
            },
            'avg_alarms_per_hour': {
                'value': avg_alarms_per_hour,
                'status': kpi_status['avg_alarms_per_hour'],
                'thresholds': self.thresholds['avg_alarms_per_hour']
            },
            'peak_alarms_10min': {
                'value': peak_alarms_10min,
                'status': kpi_status['peak_alarms_10min'],
                'thresholds': self.thresholds['peak_alarms_10min']
            },
            'standing_alarms': {
                'value': standing_alarms,
                'status': kpi_status['standing_alarms'],
                'thresholds': self.thresholds['standing_alarms']
            },
            'priority_distribution': {
                'distribution': priority_distribution,
                'status': kpi_status['priority_distribution'],
                'target': self.thresholds['priority_distribution']
            },
            'top_10_alarms': top_10_alarms,
            'alarm_rate_trend': alarm_rate_trend,
            'overall_status': kpi_status['overall'],
            'recommendations': self._generate_recommendations(kpi_status, avg_alarms_per_hour, peak_alarms_10min, standing_alarms)
        }

    def _get_alarms_in_period(self, start_time: datetime, end_time: datetime) -> List[Dict]:
        """获取指定时间段内的报警记录"""
        try:
            return self.database.get_alarm_records(
                start_time=start_time,
                end_time=end_time,
                limit=10000
            )
        except Exception as e:
            logger.error(f"获取报警记录失败: {e}")
            return []

    def _calculate_avg_alarms_per_hour(self, alarms: List[Dict], hours: int) -> float:
        """计算每小时平均报警数"""
        if not alarms or hours <= 0:
            return 0.0
        
        return len(alarms) / hours

    def _calculate_peak_alarms_10min(self, alarms: List[Dict]) -> int:
        """计算10分钟内峰值报警数"""
        if not alarms:
            return 0
        
        # 按时间排序
        sorted_alarms = sorted(alarms, key=lambda x: x.get('timestamp', ''))
        
        max_count = 0
        window_start = 0
        
        for i, alarm in enumerate(sorted_alarms):
            # 移动窗口起点
            alarm_time = datetime.fromisoformat(alarm['timestamp'].replace('Z', '+00:00'))
            window_end = alarm_time
            window_start_time = window_end - timedelta(minutes=10)
            
            # 计算窗口内的报警数
            count = 0
            for j in range(i, -1, -1):
                prev_alarm = sorted_alarms[j]
                prev_time = datetime.fromisoformat(prev_alarm['timestamp'].replace('Z', '+00:00'))
                if prev_time >= window_start_time:
                    count += 1
                else:
                    break
            
            max_count = max(max_count, count)
        
        return max_count

    def _get_standing_alarms(self) -> int:
        """获取常驻报警数"""
        if not self.alarm_manager:
            return 0
        
        try:
            active_alarms = self.alarm_manager.get_active_alarms()
            return len(active_alarms)
        except Exception as e:
            logger.error(f"获取常驻报警数失败: {e}")
            return 0

    def _calculate_priority_distribution(self, alarms: List[Dict]) -> Dict[str, float]:
        """计算报警优先级分布"""
        if not alarms:
            return {'low': 0, 'medium': 0, 'high': 0, 'critical': 0}
        
        priority_count = Counter()
        for alarm in alarms:
            level = alarm.get('alarm_level', 'low').lower()
            priority_count[level] += 1
        
        total = len(alarms)
        return {
            'low': priority_count.get('low', 0) / total,
            'medium': priority_count.get('medium', 0) / total,
            'high': priority_count.get('high', 0) / total,
            'critical': priority_count.get('critical', 0) / total
        }

    def _get_top_10_alarms(self, alarms: List[Dict]) -> List[Dict]:
        """获取Top 10最频繁报警"""
        if not alarms:
            return []
        
        # 统计每个报警ID的出现次数
        alarm_count = Counter()
        alarm_info = {}
        
        for alarm in alarms:
            alarm_id = alarm.get('alarm_id', 'unknown')
            alarm_count[alarm_id] += 1
            
            if alarm_id not in alarm_info:
                alarm_info[alarm_id] = {
                    'alarm_id': alarm_id,
                    'device_id': alarm.get('device_id', ''),
                    'register_name': alarm.get('register_name', ''),
                    'alarm_level': alarm.get('alarm_level', ''),
                    'alarm_message': alarm.get('alarm_message', '')
                }
        
        # 获取Top 10
        top_10 = []
        for alarm_id, count in alarm_count.most_common(10):
            info = alarm_info.get(alarm_id, {})
            info['count'] = count
            top_10.append(info)
        
        return top_10

    def _calculate_alarm_rate_trend(self, alarms: List[Dict], hours: int) -> List[Dict]:
        """计算报警率趋势"""
        if not alarms:
            return []
        
        # 按小时分组
        hourly_count = defaultdict(int)
        
        for alarm in alarms:
            timestamp = alarm.get('timestamp', '')
            if timestamp:
                try:
                    dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    hour_key = dt.strftime('%Y-%m-%d %H:00')
                    hourly_count[hour_key] += 1
                except:
                    pass
        
        # 生成趋势数据
        trend = []
        for i in range(hours):
            hour = datetime.now() - timedelta(hours=hours - 1 - i)
            hour_key = hour.strftime('%Y-%m-%d %H:00')
            trend.append({
                'hour': hour_key,
                'count': hourly_count.get(hour_key, 0)
            })
        
        return trend

    def _evaluate_kpi_status(self, avg_alarms: float, peak_alarms: int, 
                            standing_alarms: int, priority_dist: Dict) -> Dict[str, str]:
        """评估KPI状态"""
        thresholds = self.thresholds
        
        # 评估每小时平均报警数
        if avg_alarms <= thresholds['avg_alarms_per_hour']['ideal']:
            avg_status = 'ideal'
        elif avg_alarms <= thresholds['avg_alarms_per_hour']['acceptable']:
            avg_status = 'acceptable'
        else:
            avg_status = 'poor'
        
        # 评估10分钟峰值报警数
        if peak_alarms <= thresholds['peak_alarms_10min']['ideal']:
            peak_status = 'ideal'
        elif peak_alarms <= thresholds['peak_alarms_10min']['acceptable']:
            peak_status = 'acceptable'
        else:
            peak_status = 'poor'
        
        # 评估常驻报警数
        if standing_alarms <= thresholds['standing_alarms']['ideal']:
            standing_status = 'ideal'
        elif standing_alarms <= thresholds['standing_alarms']['acceptable']:
            standing_status = 'acceptable'
        else:
            standing_status = 'poor'
        
        # 评估优先级分布
        target = thresholds['priority_distribution']
        low_diff = abs(priority_dist.get('low', 0) - target['low'])
        medium_diff = abs(priority_dist.get('medium', 0) - target['medium'])
        high_diff = abs(priority_dist.get('high', 0) - target['high'])
        
        if low_diff < 0.1 and medium_diff < 0.1 and high_diff < 0.1:
            priority_status = 'ideal'
        elif low_diff < 0.2 and medium_diff < 0.2 and high_diff < 0.2:
            priority_status = 'acceptable'
        else:
            priority_status = 'poor'
        
        # 整体状态
        statuses = [avg_status, peak_status, standing_status, priority_status]
        if 'poor' in statuses:
            overall = 'poor'
        elif 'acceptable' in statuses:
            overall = 'acceptable'
        else:
            overall = 'ideal'
        
        return {
            'avg_alarms_per_hour': avg_status,
            'peak_alarms_10min': peak_status,
            'standing_alarms': standing_status,
            'priority_distribution': priority_status,
            'overall': overall
        }

    def _generate_recommendations(self, kpi_status: Dict, avg_alarms: float, 
                                 peak_alarms: int, standing_alarms: int) -> List[str]:
        """生成改进建议"""
        recommendations = []
        
        if kpi_status['avg_alarms_per_hour'] == 'poor':
            recommendations.append(
                f"每小时平均报警数({avg_alarms:.1f})过高，建议进行报警合理化审查，减少不必要的报警"
            )
        
        if kpi_status['peak_alarms_10min'] == 'poor':
            recommendations.append(
                f"10分钟峰值报警数({peak_alarms})过高，建议实施报警抑制和延迟策略"
            )
        
        if kpi_status['standing_alarms'] == 'poor':
            recommendations.append(
                f"常驻报警数({standing_alarms})过多，建议处理积压报警，建立报警响应流程"
            )
        
        if kpi_status['priority_distribution'] == 'poor':
            recommendations.append(
                "报警优先级分布不符合ISA-18.2标准，建议重新评估报警优先级"
            )
        
        if not recommendations:
            recommendations.append("报警系统运行良好，符合ISA-18.2标准")
        
        return recommendations

    def get_alarm_statistics_by_device(self, hours: int = 24) -> Dict[str, Any]:
        """按设备统计报警"""
        end_time = datetime.now()
        start_time = end_time - timedelta(hours=hours)
        
        alarms = self._get_alarms_in_period(start_time, end_time)
        
        device_stats = defaultdict(lambda: {'count': 0, 'levels': Counter()})
        
        for alarm in alarms:
            device_id = alarm.get('device_id', 'unknown')
            level = alarm.get('alarm_level', 'low')
            
            device_stats[device_id]['count'] += 1
            device_stats[device_id]['levels'][level] += 1
        
        # 转换为可序列化格式
        result = {}
        for device_id, stats in device_stats.items():
            result[device_id] = {
                'total_count': stats['count'],
                'by_level': dict(stats['levels'])
            }
        
        return result

    def get_alarm_statistics_by_type(self, hours: int = 24) -> Dict[str, Any]:
        """按报警类型统计"""
        end_time = datetime.now()
        start_time = end_time - timedelta(hours=hours)
        
        alarms = self._get_alarms_in_period(start_time, end_time)
        
        type_stats = defaultdict(lambda: {'count': 0, 'devices': set()})
        
        for alarm in alarms:
            alarm_type = alarm.get('alarm_id', 'unknown')
            device_id = alarm.get('device_id', '')
            
            type_stats[alarm_type]['count'] += 1
            type_stats[alarm_type]['devices'].add(device_id)
        
        # 转换为可序列化格式
        result = {}
        for alarm_type, stats in type_stats.items():
            result[alarm_type] = {
                'total_count': stats['count'],
                'affected_devices': len(stats['devices']),
                'device_list': list(stats['devices'])
            }
        
        return result

    def export_kpi_report(self, hours: int = 24, format: str = 'json') -> Any:
        """
        导出KPI报告
        
        Args:
            hours: 时间范围
            format: 导出格式 ('json', 'text')
            
        Returns:
            报告内容
        """
        kpis = self.calculate_kpis(hours)
        
        if format == 'json':
            return kpis
        
        # 文本格式
        report = []
        report.append("=" * 60)
        report.append("ISA-18.2 报警KPI报告")
        report.append("=" * 60)
        report.append(f"报告时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append(f"统计周期: 过去 {hours} 小时")
        report.append("")
        
        report.append("1. 每小时平均报警数")
        report.append(f"   当前值: {kpis['avg_alarms_per_hour']['value']:.1f}")
        report.append(f"   状态: {kpis['avg_alarms_per_hour']['status']}")
        report.append(f"   目标: < {self.thresholds['avg_alarms_per_hour']['ideal']} (理想)")
        report.append("")
        
        report.append("2. 10分钟峰值报警数")
        report.append(f"   当前值: {kpis['peak_alarms_10min']['value']}")
        report.append(f"   状态: {kpis['peak_alarms_10min']['status']}")
        report.append(f"   目标: < {self.thresholds['peak_alarms_10min']['ideal']} (理想)")
        report.append("")
        
        report.append("3. 常驻报警数")
        report.append(f"   当前值: {kpis['standing_alarms']['value']}")
        report.append(f"   状态: {kpis['standing_alarms']['status']}")
        report.append(f"   目标: < {self.thresholds['standing_alarms']['ideal']} (理想)")
        report.append("")
        
        report.append("4. 报警优先级分布")
        dist = kpis['priority_distribution']['distribution']
        report.append(f"   低优先级: {dist.get('low', 0):.1%} (目标: 80%)")
        report.append(f"   中优先级: {dist.get('medium', 0):.1%} (目标: 15%)")
        report.append(f"   高优先级: {dist.get('high', 0):.1%} (目标: 5%)")
        report.append(f"   状态: {kpis['priority_distribution']['status']}")
        report.append("")
        
        report.append("5. Top 10 最频繁报警")
        for i, alarm in enumerate(kpis['top_10_alarms'], 1):
            report.append(f"   {i}. {alarm.get('alarm_id', 'N/A')} - {alarm.get('count', 0)} 次")
            report.append(f"      设备: {alarm.get('device_id', 'N/A')}")
            report.append(f"      消息: {alarm.get('alarm_message', 'N/A')}")
        report.append("")
        
        report.append("6. 改进建议")
        for i, rec in enumerate(kpis['recommendations'], 1):
            report.append(f"   {i}. {rec}")
        report.append("")
        
        report.append("=" * 60)
        report.append(f"整体状态: {kpis['overall_status'].upper()}")
        report.append("=" * 60)
        
        return "\n".join(report)
