"""
性能指标自动采集工具
自动收集和分析SCADA系统性能指标

使用方法:
    python tools/auto_metrics.py collect   # 采集指标
    python tools/auto_metrics.py report    # 生成报告
    python tools/auto_metrics.py alert     # 检查告警
"""

import os
import sys
import json
import time
import sqlite3
import argparse
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import paths  # round 213：库名/库模式的单一真源（禁止再写死 scada*.db）


class MetricsCollector:
    """性能指标采集器"""

    def __init__(self, db_mode: str = None):
        self.project_root = project_root
        # round 213：目录走 paths 单一真源（与展示层 api_performance.py 的读取侧同源）
        self.metrics_dir = paths.METRICS_DIR
        self.metrics_dir.mkdir(exist_ok=True)

        # round 213：库路径走单一真源解析（原先写死 data/scada.db，
        # 指向 4.5 个月没人用的旧库，指标全采错了还不报错）
        self.db_mode = paths.resolve_cli_db_mode(db_mode)
        self.db_path = Path(paths.get_db_path(self.db_mode))

        # 指标存储
        self.metrics_file = self.metrics_dir / 'metrics.jsonl'

    def collect_system_metrics(self) -> Dict[str, Any]:
        """采集系统指标"""
        metrics = {
            'timestamp': datetime.now().isoformat(),
            'type': 'system',
        }

        # CPU使用率
        try:
            import psutil
            metrics['cpu_percent'] = psutil.cpu_percent(interval=1)
            metrics['cpu_count'] = psutil.cpu_count()
        except ImportError:
            metrics['cpu_percent'] = -1

        # 内存使用
        try:
            import psutil
            memory = psutil.virtual_memory()
            metrics['memory_total_gb'] = round(memory.total / (1024**3), 2)
            metrics['memory_used_gb'] = round(memory.used / (1024**3), 2)
            metrics['memory_percent'] = memory.percent
        except ImportError:
            metrics['memory_percent'] = -1

        # 磁盘使用
        try:
            import shutil
            total, used, free = shutil.disk_usage(str(self.project_root))
            metrics['disk_total_gb'] = round(total / (1024**3), 2)
            metrics['disk_used_gb'] = round(used / (1024**3), 2)
            metrics['disk_free_gb'] = round(free / (1024**3), 2)
            metrics['disk_percent'] = round(used / total * 100, 1)
        except Exception:
            metrics['disk_percent'] = -1

        # 线程数
        import threading
        metrics['thread_count'] = threading.active_count()

        return metrics

    def collect_database_metrics(self) -> Dict[str, Any]:
        """采集数据库指标"""
        db_path = self.db_path

        if not db_path.exists():
            return {'timestamp': datetime.now().isoformat(), 'type': 'database', 'status': 'missing'}

        metrics = {
            'timestamp': datetime.now().isoformat(),
            'type': 'database',
            'size_mb': round(db_path.stat().st_size / (1024 * 1024), 2),
        }

        try:
            conn = sqlite3.connect(str(db_path), timeout=5)

            # 表统计
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]

            table_counts = {}
            for table in tables:
                try:
                    cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
                    table_counts[table] = cursor.fetchone()[0]
                except Exception:
                    table_counts[table] = -1

            metrics['tables'] = table_counts
            metrics['total_records'] = sum(v for v in table_counts.values() if v > 0)

            # WAL大小
            wal_path = self.db_path.with_name(self.db_path.name + '-wal')
            if wal_path.exists():
                metrics['wal_size_mb'] = round(wal_path.stat().st_size / (1024 * 1024), 2)

            conn.close()
        except Exception as e:
            metrics['error'] = str(e)

        return metrics

    def collect_application_metrics(self) -> Dict[str, Any]:
        """采集应用指标"""
        metrics = {
            'timestamp': datetime.now().isoformat(),
            'type': 'application',
        }

        # 日志统计
        log_dir = project_root / 'logs'
        if log_dir.exists():
            log_files = list(log_dir.glob('*.log'))
            metrics['log_count'] = len(log_files)
            metrics['log_total_mb'] = round(
                sum(f.stat().st_size for f in log_files) / (1024 * 1024), 2
            )

        # 配置文件统计
        config_dir = project_root / '配置'
        if config_dir.exists():
            config_files = list(config_dir.glob('*.yaml'))
            metrics['config_count'] = len(config_files)

        return metrics

    def collect_all(self) -> Dict[str, Any]:
        """采集所有指标"""
        return {
            'timestamp': datetime.now().isoformat(),
            'system': self.collect_system_metrics(),
            'database': self.collect_database_metrics(),
            'application': self.collect_application_metrics(),
        }

    def save_metrics(self, metrics: Dict[str, Any]):
        """保存指标到文件"""
        with open(self.metrics_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(metrics, ensure_ascii=False) + '\n')

    def load_metrics(self, hours: int = 24) -> List[Dict[str, Any]]:
        """加载历史指标"""
        if not self.metrics_file.exists():
            return []

        cutoff = datetime.now() - timedelta(hours=hours)
        metrics = []

        skipped = 0
        with open(self.metrics_file, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line.strip())
                    timestamp = datetime.fromisoformat(data['timestamp'])
                except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                    # 窄化到「这一条记录坏了」这一类可枚举异常。
                    # 原为 `except Exception: continue` —— 连 OSError / MemoryError
                    # 这类"整份文件都读不下去"的错误也会被当成"这行格式不对"吞掉，
                    # 表现为指标条数莫名其妙变少，而没有任何提示。
                    skipped += 1
                    continue
                if timestamp >= cutoff:
                    metrics.append(data)

        if skipped:
            print(f'[auto_metrics] 加载指标时跳过 {skipped} 条损坏记录'
                  f'（有效 {len(metrics)} 条）', file=sys.stderr)

        return metrics

    def check_alerts(self, metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
        """检查告警条件"""
        alerts = []

        # CPU告警
        cpu = metrics.get('system', {}).get('cpu_percent', 0)
        if cpu > 90:
            alerts.append({
                'level': 'critical',
                'type': 'cpu',
                'message': f'CPU使用率过高: {cpu}%',
                'value': cpu,
                'threshold': 90,
            })
        elif cpu > 70:
            alerts.append({
                'level': 'warning',
                'type': 'cpu',
                'message': f'CPU使用率较高: {cpu}%',
                'value': cpu,
                'threshold': 70,
            })

        # 内存告警
        memory = metrics.get('system', {}).get('memory_percent', 0)
        if memory > 90:
            alerts.append({
                'level': 'critical',
                'type': 'memory',
                'message': f'内存使用率过高: {memory}%',
                'value': memory,
                'threshold': 90,
            })
        elif memory > 80:
            alerts.append({
                'level': 'warning',
                'type': 'memory',
                'message': f'内存使用率较高: {memory}%',
                'value': memory,
                'threshold': 80,
            })

        # 磁盘告警
        disk = metrics.get('system', {}).get('disk_percent', 0)
        if disk > 90:
            alerts.append({
                'level': 'critical',
                'type': 'disk',
                'message': f'磁盘使用率过高: {disk}%',
                'value': disk,
                'threshold': 90,
            })
        elif disk > 80:
            alerts.append({
                'level': 'warning',
                'type': 'disk',
                'message': f'磁盘使用率较高: {disk}%',
                'value': disk,
                'threshold': 80,
            })

        return alerts

    def generate_report(self, hours: int = 24) -> Dict[str, Any]:
        """生成指标报告"""
        metrics_list = self.load_metrics(hours)

        if not metrics_list:
            return {'message': '无历史数据'}

        # 计算统计
        cpu_values = [m.get('system', {}).get('cpu_percent', 0) for m in metrics_list]
        memory_values = [m.get('system', {}).get('memory_percent', 0) for m in metrics_list]

        return {
            'period_hours': hours,
            'samples': len(metrics_list),
            'cpu': {
                'avg': round(sum(cpu_values) / len(cpu_values), 1) if cpu_values else 0,
                'max': max(cpu_values) if cpu_values else 0,
                'min': min(cpu_values) if cpu_values else 0,
            },
            'memory': {
                'avg': round(sum(memory_values) / len(memory_values), 1) if memory_values else 0,
                'max': max(memory_values) if memory_values else 0,
                'min': min(memory_values) if memory_values else 0,
            },
            'latest': metrics_list[-1] if metrics_list else None,
        }


def main():
    parser = argparse.ArgumentParser(description='性能指标自动采集工具')
    parser.add_argument('command', choices=['collect', 'report', 'alert'],
                       help='执行的命令')
    parser.add_argument('--hours', type=int, default=24, help='报告时间范围（小时）')
    # round 213：显式选库（原先写死 scada.db，指到旧库还不报错）。
    # 不传则读 SCADA_DB_MODE env，再没有回退 simulated（与 run.py 无参默认一致）。
    parser.add_argument('--db-mode', default=None,
                        choices=['simulated', 'real', 'simulator', 'default'],
                        help='数据库模式（默认读 SCADA_DB_MODE env，再回退 simulated）')

    args = parser.parse_args()

    collector = MetricsCollector(db_mode=args.db_mode)
    # 把「这次分析的是哪个库」打到明面上 —— 静默选库正是这类缺陷的温床
    print(f"[db] {paths.describe_db_choice(collector.db_mode)}", file=sys.stderr)

    if args.command == 'collect':
        metrics = collector.collect_all()
        collector.save_metrics(metrics)
        print(json.dumps(metrics, indent=2, ensure_ascii=False))

    elif args.command == 'report':
        report = collector.generate_report(args.hours)
        print(json.dumps(report, indent=2, ensure_ascii=False))

    elif args.command == 'alert':
        metrics = collector.collect_all()
        alerts = collector.check_alerts(metrics)

        if alerts:
            print("告警:")
            for alert in alerts:
                print(f"  [{alert['level'].upper()}] {alert['message']}")
        else:
            print("无告警")


if __name__ == '__main__':
    main()
