"""
容量规划工具
分析SCADA系统资源使用趋势，预测容量瓶颈

使用方法:
    python tools/capacity_planner.py analyze    # 分析当前容量
    python tools/capacity_planner.py predict    # 预测未来容量
    python tools/capacity_planner.py report     # 生成容量报告
"""

import os
import sys
import json
import sqlite3
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


class CapacityPlanner:
    """容量规划器"""

    def __init__(self):
        self.project_root = project_root
        self.db_path = project_root / 'data' / 'scada.db'
        self.thresholds = {
            'disk_warning': 80,      # 磁盘使用率警告阈值 (%)
            'disk_critical': 90,     # 磁盘使用率严重阈值 (%)
            'db_size_warning_mb': 1000,  # 数据库大小警告 (MB)
            'db_size_critical_mb': 5000, # 数据库大小严重 (MB)
            'record_count_warning': 10000000,  # 记录数警告
            'growth_rate_warning': 0.2,  # 增长率警告 (20%/天)
        }

    def analyze_capacity(self) -> Dict[str, Any]:
        """分析当前容量状态"""
        result = {
            'timestamp': datetime.now().isoformat(),
            'resources': {},
            'database': {},
            'alerts': [],
            'status': 'ok',
        }

        # 1. 磁盘容量
        result['resources']['disk'] = self._analyze_disk()

        # 2. 数据库容量
        result['database'] = self._analyze_database()

        # 3. 内存使用
        result['resources']['memory'] = self._analyze_memory()

        # 4. 生成告警
        result['alerts'] = self._generate_alerts(result)

        # 计算总体状态
        if any(a['severity'] == 'critical' for a in result['alerts']):
            result['status'] = 'critical'
        elif any(a['severity'] == 'warning' for a in result['alerts']):
            result['status'] = 'warning'

        return result

    def _analyze_disk(self) -> Dict[str, Any]:
        """分析磁盘容量"""
        try:
            import shutil
            total, used, free = shutil.disk_usage(str(self.project_root))
            return {
                'total_gb': round(total / (1024**3), 2),
                'used_gb': round(used / (1024**3), 2),
                'free_gb': round(free / (1024**3), 2),
                'usage_percent': round(used / total * 100, 1),
            }
        except Exception as e:
            return {'error': str(e)}

    def _analyze_database(self) -> Dict[str, Any]:
        """分析数据库容量"""
        if not self.db_path.exists():
            return {'error': '数据库不存在'}

        result = {
            'size_mb': round(self.db_path.stat().st_size / (1024 * 1024), 2),
            'tables': {},
        }

        try:
            conn = sqlite3.connect(str(self.db_path), timeout=5)
            cursor = conn.cursor()

            # 获取各表记录数
            tables = ['history_data', 'alarm_records', 'realtime_data', 'audit_log']
            for table in tables:
                try:
                    cursor.execute(f"SELECT COUNT(*) FROM {table}")
                    count = cursor.fetchone()[0]
                    result['tables'][table] = {'count': count}
                except Exception:
                    result['tables'][table] = {'count': 0}

            # WAL文件大小
            wal_path = self.db_path.parent / f"{self.db_path.name}-wal"
            if wal_path.exists():
                result['wal_size_mb'] = round(wal_path.stat().st_size / (1024 * 1024), 2)

            conn.close()
        except Exception as e:
            result['error'] = str(e)

        return result

    def _analyze_memory(self) -> Dict[str, Any]:
        """分析内存使用"""
        try:
            import psutil
            mem = psutil.virtual_memory()
            return {
                'total_gb': round(mem.total / (1024**3), 2),
                'available_gb': round(mem.available / (1024**3), 2),
                'usage_percent': mem.percent,
            }
        except ImportError:
            return {'error': 'psutil未安装'}

    def _generate_alerts(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """生成容量告警"""
        alerts = []

        # 磁盘告警
        disk = data.get('resources', {}).get('disk', {})
        if disk.get('usage_percent', 0) > self.thresholds['disk_critical']:
            alerts.append({
                'severity': 'critical',
                'resource': 'disk',
                'message': f"磁盘使用率 {disk['usage_percent']}% 超过严重阈值 {self.thresholds['disk_critical']}%",
                'recommendation': '立即清理磁盘空间或扩展存储',
            })
        elif disk.get('usage_percent', 0) > self.thresholds['disk_warning']:
            alerts.append({
                'severity': 'warning',
                'resource': 'disk',
                'message': f"磁盘使用率 {disk['usage_percent']}% 超过警告阈值 {self.thresholds['disk_warning']}%",
                'recommendation': '计划清理磁盘空间',
            })

        # 数据库大小告警
        db = data.get('database', {})
        if db.get('size_mb', 0) > self.thresholds['db_size_critical_mb']:
            alerts.append({
                'severity': 'critical',
                'resource': 'database',
                'message': f"数据库大小 {db['size_mb']}MB 超过严重阈值 {self.thresholds['db_size_critical_mb']}MB",
                'recommendation': '执行数据归档或清理',
            })
        elif db.get('size_mb', 0) > self.thresholds['db_size_warning_mb']:
            alerts.append({
                'severity': 'warning',
                'resource': 'database',
                'message': f"数据库大小 {db['size_mb']}MB 超过警告阈值 {self.thresholds['db_size_warning_mb']}MB",
                'recommendation': '计划数据归档',
            })

        # 记录数告警
        for table, info in db.get('tables', {}).items():
            if info.get('count', 0) > self.thresholds['record_count_warning']:
                alerts.append({
                    'severity': 'warning',
                    'resource': 'database',
                    'message': f"表 {table} 记录数 {info['count']} 超过警告阈值",
                    'recommendation': f'清理或归档 {table} 表数据',
                })

        return alerts

    def predict_capacity(self, days_ahead: int = 30) -> Dict[str, Any]:
        """预测未来容量需求"""
        current = self.analyze_capacity()
        db = current.get('database', {})

        prediction = {
            'timestamp': datetime.now().isoformat(),
            'days_ahead': days_ahead,
            'current': current,
            'predictions': {},
            'recommendations': [],
        }

        # 基于历史数据预测数据库增长
        if self.db_path.exists():
            try:
                conn = sqlite3.connect(str(self.db_path), timeout=5)
                cursor = conn.cursor()

                # 获取最近7天的数据增长
                week_ago = (datetime.now() - timedelta(days=7)).isoformat()
                cursor.execute("SELECT COUNT(*) FROM history_data WHERE timestamp > ?", (week_ago,))
                weekly_records = cursor.fetchone()[0]

                cursor.execute("SELECT COUNT(*) FROM history_data")
                total_records = cursor.fetchone()[0]

                conn.close()

                if weekly_records > 0:
                    daily_growth = weekly_records / 7
                    predicted_records = total_records + (daily_growth * days_ahead)
                    current_size_mb = db.get('size_mb', 0)
                    size_per_record = current_size_mb / max(total_records, 1)
                    predicted_size_mb = predicted_records * size_per_record

                    prediction['predictions'] = {
                        'current_records': total_records,
                        'weekly_growth': weekly_records,
                        'daily_growth': round(daily_growth),
                        'predicted_records': round(predicted_records),
                        'predicted_size_mb': round(predicted_size_mb, 2),
                        'days_until_warning': self._days_until_threshold(
                            current_size_mb, predicted_size_mb - current_size_mb, days_ahead,
                            self.thresholds['db_size_warning_mb']
                        ),
                    }

                    # 生成建议
                    if predicted_size_mb > self.thresholds['db_size_critical_mb']:
                        prediction['recommendations'].append({
                            'priority': 'high',
                            'action': '立即执行数据归档',
                            'reason': f"预计{days_ahead}天后数据库将达到{round(predicted_size_mb)}MB",
                        })
                    elif predicted_size_mb > self.thresholds['db_size_warning_mb']:
                        prediction['recommendations'].append({
                            'priority': 'medium',
                            'action': '计划数据归档',
                            'reason': f"预计{days_ahead}天后数据库将达到{round(predicted_size_mb)}MB",
                        })

            except Exception as e:
                prediction['error'] = str(e)

        return prediction

    def _days_until_threshold(self, current: float, growth: float, days: int, threshold: float) -> int:
        """计算达到阈值的天数"""
        if growth <= 0:
            return -1  # 不会达到
        daily_growth = growth / days
        remaining = threshold - current
        if remaining <= 0:
            return 0
        return int(remaining / daily_growth)

    def generate_report(self) -> Dict[str, Any]:
        """生成容量规划报告"""
        current = self.analyze_capacity()
        prediction = self.predict_capacity(30)

        return {
            'timestamp': datetime.now().isoformat(),
            'current_status': current,
            'thirty_day_prediction': prediction,
            'recommendations': self._generate_recommendations(current, prediction),
        }

    def _generate_recommendations(self, current: Dict, prediction: Dict) -> List[Dict[str, Any]]:
        """生成容量规划建议"""
        recommendations = []

        # 基于当前状态
        for alert in current.get('alerts', []):
            recommendations.append({
                'priority': 'high' if alert['severity'] == 'critical' else 'medium',
                'action': alert['recommendation'],
                'reason': alert['message'],
            })

        # 基于预测
        recommendations.extend(prediction.get('recommendations', []))

        # 通用建议
        if not recommendations:
            recommendations.append({
                'priority': 'low',
                'action': '继续监控',
                'reason': '当前容量状态良好',
            })

        return recommendations


def format_report(report: Dict[str, Any]) -> str:
    """格式化报告"""
    lines = []
    lines.append("=" * 60)
    lines.append("容量规划报告")
    lines.append(f"时间: {report['timestamp']}")
    lines.append("=" * 60)

    # 当前状态
    current = report.get('current_status', {})
    lines.append(f"\n当前状态: {current.get('status', 'unknown').upper()}")

    # 磁盘
    disk = current.get('resources', {}).get('disk', {})
    lines.append(f"\n磁盘:")
    lines.append(f"  总容量: {disk.get('total_gb', '?')}GB")
    lines.append(f"  已使用: {disk.get('used_gb', '?')}GB ({disk.get('usage_percent', '?')}%)")
    lines.append(f"  可用: {disk.get('free_gb', '?')}GB")

    # 数据库
    db = current.get('database', {})
    lines.append(f"\n数据库:")
    lines.append(f"  大小: {db.get('size_mb', '?')}MB")
    for table, info in db.get('tables', {}).items():
        lines.append(f"  {table}: {info.get('count', '?')} 条记录")

    # 告警
    alerts = current.get('alerts', [])
    if alerts:
        lines.append(f"\n告警:")
        for alert in alerts:
            lines.append(f"  [{alert['severity'].upper()}] {alert['message']}")

    # 预测
    prediction = report.get('thirty_day_prediction', {})
    predictions = prediction.get('predictions', {})
    if predictions:
        lines.append(f"\n30天预测:")
        lines.append(f"  当前记录: {predictions.get('current_records', '?')}")
        lines.append(f"  日增长: {predictions.get('daily_growth', '?')} 条")
        lines.append(f"  预计记录: {predictions.get('predicted_records', '?')}")
        lines.append(f"  预计大小: {predictions.get('predicted_size_mb', '?')}MB")
        days_until = predictions.get('days_until_warning', -1)
        if days_until >= 0:
            lines.append(f"  距离警告阈值: {days_until} 天")

    # 建议
    recommendations = report.get('recommendations', [])
    if recommendations:
        lines.append(f"\n建议:")
        for rec in recommendations:
            lines.append(f"  [{rec['priority'].upper()}] {rec['action']}")
            lines.append(f"    原因: {rec['reason']}")

    lines.append("=" * 60)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description='容量规划工具')
    parser.add_argument('command', choices=['analyze', 'predict', 'report'], help='命令')
    parser.add_argument('--days', type=int, default=30, help='预测天数')
    parser.add_argument('--output', help='输出文件路径')

    args = parser.parse_args()

    planner = CapacityPlanner()

    if args.command == 'analyze':
        result = planner.analyze_capacity()
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == 'predict':
        result = planner.predict_capacity(args.days)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == 'report':
        report = planner.generate_report()
        print(format_report(report))

        # 保存JSON
        if args.output:
            output_path = Path(args.output)
        else:
            output_path = project_root / 'logs' / f'capacity_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        print(f"\n报告已保存: {output_path}")


if __name__ == '__main__':
    main()
