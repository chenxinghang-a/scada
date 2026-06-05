"""
性能基线建立工具
建立和维护SCADA系统性能基线

使用方法:
    python tools/performance_baseline.py establish  # 建立基线
    python tools/performance_baseline.py compare    # 与基线对�?    python tools/performance_baseline.py report     # 生成报告
"""

import os
import sys
import json
import time
import sqlite3
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


class PerformanceBaseline:
    """性能基线管理�?""

    def __init__(self, baseline_file: str = None):
        self.baseline_file = baseline_file or str(project_root / '.performance_baseline.json')
        self.baseline = self._load_baseline()

    def _load_baseline(self) -> Dict[str, Any]:
        """加载基线"""
        try:
            with open(self.baseline_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_baseline(self):
        """保存基线"""
        Path(self.baseline_file).parent.mkdir(parents=True, exist_ok=True)
        with open(self.baseline_file, 'w', encoding='utf-8') as f:
            json.dump(self.baseline, f, indent=2, ensure_ascii=False)

    def establish_baseline(self, duration_minutes: int = 5) -> Dict[str, Any]:
        """建立性能基线"""
        print(f"建立性能基线（{duration_minutes}分钟�?..")

        samples = []
        interval = 10  # �?0秒采样一�?        num_samples = duration_minutes * 60 // interval

        for i in range(num_samples):
            sample = self._take_sample()
            samples.append(sample)
            print(f"  采样 {i+1}/{num_samples}: CPU {sample['cpu_percent']}%, 内存 {sample['memory_percent']}%")
            if i < num_samples - 1:
                time.sleep(interval)

        # 计算基线
        baseline = {
            'timestamp': datetime.now().isoformat(),
            'duration_minutes': duration_minutes,
            'samples_count': len(samples),
            'metrics': {},
        }

        # CPU基线
        cpu_values = [s['cpu_percent'] for s in samples]
        baseline['metrics']['cpu'] = {
            'avg': round(sum(cpu_values) / len(cpu_values), 1),
            'p50': round(sorted(cpu_values)[len(cpu_values)//2], 1),
            'p95': round(sorted(cpu_values)[int(len(cpu_values)*0.95)], 1),
            'max': max(cpu_values),
        }

        # 内存基线
        mem_values = [s['memory_percent'] for s in samples]
        baseline['metrics']['memory'] = {
            'avg': round(sum(mem_values) / len(mem_values), 1),
            'p50': round(sorted(mem_values)[len(mem_values)//2], 1),
            'p95': round(sorted(mem_values)[int(len(mem_values)*0.95)], 1),
            'max': max(mem_values),
        }

        # 数据库性能
        db_values = [s['db_query_time_ms'] for s in samples if s.get('db_query_time_ms')]
        if db_values:
            baseline['metrics']['database'] = {
                'avg_ms': round(sum(db_values) / len(db_values), 1),
                'p95_ms': round(sorted(db_values)[int(len(db_values)*0.95)], 1),
                'max_ms': max(db_values),
            }

        # 线程�?        thread_values = [s['thread_count'] for s in samples]
        baseline['metrics']['threads'] = {
            'avg': round(sum(thread_values) / len(thread_values), 0),
            'max': max(thread_values),
        }

        self.baseline = baseline
        self._save_baseline()

        print(f"\n基线已建�? {self.baseline_file}")
        return baseline

    def _take_sample(self) -> Dict[str, Any]:
        """采集一次性能样本"""
        sample = {
            'timestamp': time.time(),
            'cpu_percent': 0,
            'memory_percent': 0,
            'thread_count': 0,
            'db_query_time_ms': 0,
        }

        try:
            import psutil
            sample['cpu_percent'] = psutil.cpu_percent(interval=0.1)
            mem = psutil.virtual_memory()
            sample['memory_percent'] = mem.percent
            sample['thread_count'] = psutil.Process().num_threads()
        except ImportError:
            pass

        # 测试数据库查询性能
        try:
            db_path = project_root / 'data' / 'scada.db'
            if db_path.exists():
                start = time.time()
                conn = sqlite3.connect(str(db_path), timeout=5)
                conn.execute("SELECT COUNT(*) FROM history_data")
                conn.close()
                sample['db_query_time_ms'] = round((time.time() - start) * 1000, 1)
        except Exception:
            pass

        return sample

    def compare_with_baseline(self) -> Dict[str, Any]:
        """与基线对�?""
        if not self.baseline:
            return {'error': '没有基线数据，请先建立基�?}

        current = self._take_sample()
        baseline_metrics = self.baseline.get('metrics', {})

        comparison = {
            'timestamp': datetime.now().isoformat(),
            'current': current,
            'baseline': baseline_metrics,
            'deviations': [],
            'status': 'ok',
        }

        # CPU对比
        if 'cpu' in baseline_metrics:
            baseline_avg = baseline_metrics['cpu']['avg']
            deviation = current['cpu_percent'] - baseline_avg
            if abs(deviation) > baseline_avg * 0.5:  # 偏差超过50%
                comparison['deviations'].append({
                    'metric': 'cpu',
                    'baseline': baseline_avg,
                    'current': current['cpu_percent'],
                    'deviation': round(deviation, 1),
                    'severity': 'warning' if abs(deviation) < baseline_avg else 'critical',
                })

        # 内存对比
        if 'memory' in baseline_metrics:
            baseline_avg = baseline_metrics['memory']['avg']
            deviation = current['memory_percent'] - baseline_avg
            if abs(deviation) > 10:  # 偏差超过10%
                comparison['deviations'].append({
                    'metric': 'memory',
                    'baseline': baseline_avg,
                    'current': current['memory_percent'],
                    'deviation': round(deviation, 1),
                    'severity': 'warning' if deviation < 20 else 'critical',
                })

        # 数据库性能对比
        if 'database' in baseline_metrics and current.get('db_query_time_ms'):
            baseline_avg = baseline_metrics['database']['avg_ms']
            if current['db_query_time_ms'] > baseline_avg * 3:  # �?�?                comparison['deviations'].append({
                    'metric': 'database',
                    'baseline': baseline_avg,
                    'current': current['db_query_time_ms'],
                    'deviation': round(current['db_query_time_ms'] - baseline_avg, 1),
                    'severity': 'warning',
                })

        # 计算总体状�?        if any(d['severity'] == 'critical' for d in comparison['deviations']):
            comparison['status'] = 'critical'
        elif comparison['deviations']:
            comparison['status'] = 'warning'

        return comparison

    def generate_report(self) -> Dict[str, Any]:
        """生成报告"""
        comparison = self.compare_with_baseline()

        return {
            'timestamp': datetime.now().isoformat(),
            'baseline': self.baseline,
            'comparison': comparison,
            'recommendations': self._generate_recommendations(comparison),
        }

    def _generate_recommendations(self, comparison: Dict[str, Any]) -> List[str]:
        """生成建议"""
        recommendations = []

        for deviation in comparison.get('deviations', []):
            if deviation['metric'] == 'cpu':
                recommendations.append(f"CPU使用率偏高（{deviation['current']}% vs 基线{deviation['baseline']}%），建议检查高CPU进程")
            elif deviation['metric'] == 'memory':
                recommendations.append(f"内存使用率偏高（{deviation['current']}% vs 基线{deviation['baseline']}%），建议检查内存泄�?)
            elif deviation['metric'] == 'database':
                recommendations.append(f"数据库查询变慢（{deviation['current']}ms vs 基线{deviation['baseline']}ms），建议优化查询或清理数�?)

        return recommendations


def format_report(report: Dict[str, Any]) -> str:
    """格式化报�?""
    lines = []
    lines.append("=" * 60)
    lines.append("性能基线报告")
    lines.append(f"时间: {report['timestamp']}")
    lines.append("=" * 60)

    baseline = report.get('baseline', {})
    if baseline:
        lines.append(f"\n基线（{baseline.get('timestamp', 'N/A')}�?")
        for metric, values in baseline.get('metrics', {}).items():
            lines.append(f"  {metric}: {values}")

    comparison = report.get('comparison', {})
    if comparison:
        lines.append(f"\n当前状�? {comparison.get('status', 'unknown').upper()}")

        current = comparison.get('current', {})
        lines.append(f"  CPU: {current.get('cpu_percent', '?')}%")
        lines.append(f"  内存: {current.get('memory_percent', '?')}%")
        lines.append(f"  线程: {current.get('thread_count', '?')}")
        lines.append(f"  数据库查�? {current.get('db_query_time_ms', '?')}ms")

        if comparison.get('deviations'):
            lines.append(f"\n偏差:")
            for dev in comparison['deviations']:
                lines.append(f"  [{dev['severity'].upper()}] {dev['metric']}: {dev['current']} vs 基线 {dev['baseline']} (偏差 {dev['deviation']})")

    recommendations = report.get('recommendations', [])
    if recommendations:
        lines.append(f"\n建议:")
        for rec in recommendations:
            lines.append(f"  - {rec}")

    lines.append("=" * 60)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description='性能基线工具')
    parser.add_argument('command', choices=['establish', 'compare', 'report'], help='命令')
    parser.add_argument('--duration', type=int, default=5, help='基线建立时长（分钟）')

    args = parser.parse_args()

    baseline = PerformanceBaseline()

    if args.command == 'establish':
        result = baseline.establish_baseline(args.duration)
        print(f"\n基线指标:")
        for metric, values in result['metrics'].items():
            print(f"  {metric}: {values}")

    elif args.command == 'compare':
        result = baseline.compare_with_baseline()
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == 'report':
        report = baseline.generate_report()
        print(format_report(report))


if __name__ == '__main__':
    main()
