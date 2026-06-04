"""
日志分析自动化工具
分析SCADA系统日志，识别异常模式和性能问题

使用方法:
    python tools/log_analyzer.py analyze   # 分析日志
    python tools/log_analyzer.py errors    # 提取错误
    python tools/log_analyzer.py report    # 生成报告
"""

import os
import re
import json
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any
from collections import defaultdict

# 项目根目录
project_root = Path(__file__).parent.parent


class LogAnalyzer:
    """日志分析器"""

    def __init__(self, log_dir: str = None):
        self.log_dir = Path(log_dir) if log_dir else project_root / 'logs'
        self.patterns = {
            'error': re.compile(r'ERROR|CRITICAL|Exception|Traceback', re.IGNORECASE),
            'warning': re.compile(r'WARNING|WARN', re.IGNORECASE),
            'connection': re.compile(r'connect|disconnect|timeout|refused', re.IGNORECASE),
            'alarm': re.compile(r'alarm|alert|warning|critical', re.IGNORECASE),
            'performance': re.compile(r'slow|timeout|latency|delay', re.IGNORECASE),
        }

    def analyze_logs(self, hours: int = 24) -> Dict[str, Any]:
        """分析日志"""
        results = {
            'timestamp': datetime.now().isoformat(),
            'period_hours': hours,
            'files': [],
            'summary': defaultdict(int),
            'errors': [],
            'warnings': [],
            'patterns': defaultdict(int),
        }

        cutoff = datetime.now() - timedelta(hours=hours)

        # 获取日志文件
        log_files = list(self.log_dir.glob('*.log'))
        if not log_files:
            results['error'] = '没有找到日志文件'
            return results

        for log_file in log_files:
            # 检查文件修改时间
            if datetime.fromtimestamp(log_file.stat().st_mtime) < cutoff:
                continue

            file_results = self._analyze_file(log_file, cutoff)
            results['files'].append({
                'name': log_file.name,
                'size_mb': round(log_file.stat().st_size / (1024 * 1024), 2),
                'lines': file_results['total_lines'],
                'errors': file_results['error_count'],
                'warnings': file_results['warning_count'],
            })

            results['errors'].extend(file_results['errors'][:100])  # 最多100条错误
            results['warnings'].extend(file_results['warnings'][:100])

            for key, count in file_results['patterns'].items():
                results['patterns'][key] += count

        # 计算摘要
        results['summary'] = {
            'total_files': len(results['files']),
            'total_errors': len(results['errors']),
            'total_warnings': len(results['warnings']),
            'error_rate': len(results['errors']) / max(sum(f['lines'] for f in results['files']), 1) * 100,
        }

        return results

    def _analyze_file(self, file_path: Path, cutoff: datetime) -> Dict[str, Any]:
        """分析单个日志文件"""
        results = {
            'total_lines': 0,
            'error_count': 0,
            'warning_count': 0,
            'errors': [],
            'warnings': [],
            'patterns': defaultdict(int),
        }

        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line_num, line in enumerate(f, 1):
                    results['total_lines'] += 1

                    # 检查错误
                    if self.patterns['error'].search(line):
                        results['error_count'] += 1
                        if len(results['errors']) < 100:
                            results['errors'].append({
                                'file': file_path.name,
                                'line': line_num,
                                'content': line.strip()[:200],
                            })

                    # 检查警告
                    elif self.patterns['warning'].search(line):
                        results['warning_count'] += 1
                        if len(results['warnings']) < 100:
                            results['warnings'].append({
                                'file': file_path.name,
                                'line': line_num,
                                'content': line.strip()[:200],
                            })

                    # 检查模式
                    for pattern_name, pattern in self.patterns.items():
                        if pattern.search(line):
                            results['patterns'][pattern_name] += 1

        except Exception as e:
            results['errors'].append({
                'file': file_path.name,
                'error': f'读取失败: {e}',
            })

        return results

    def extract_errors(self, hours: int = 24) -> List[Dict[str, Any]]:
        """提取错误信息"""
        results = self.analyze_logs(hours)
        return results.get('errors', [])

    def generate_report(self, hours: int = 24) -> Dict[str, Any]:
        """生成分析报告"""
        results = self.analyze_logs(hours)

        # 找出最常见的错误模式
        error_patterns = defaultdict(int)
        for error in results['errors']:
            content = error.get('content', '')
            # 简化错误消息
            simplified = re.sub(r'\d+', 'N', content)[:50]
            error_patterns[simplified] += 1

        # 按频率排序
        top_errors = sorted(error_patterns.items(), key=lambda x: x[1], reverse=True)[:10]

        return {
            'timestamp': datetime.now().isoformat(),
            'summary': results['summary'],
            'patterns': dict(results['patterns']),
            'top_errors': [{'pattern': p, 'count': c} for p, c in top_errors],
            'files': results['files'],
            'recommendations': self._generate_recommendations(results),
        }

    def _generate_recommendations(self, results: Dict[str, Any]) -> List[str]:
        """生成建议"""
        recommendations = []

        error_rate = results['summary'].get('error_rate', 0)
        if error_rate > 5:
            recommendations.append(f'错误率 {error_rate:.1f}% 过高，建议检查错误原因')

        if results['patterns'].get('connection', 0) > 100:
            recommendations.append('连接问题频繁，建议检查网络和设备状态')

        if results['patterns'].get('performance', 0) > 50:
            recommendations.append('性能问题频繁，建议优化查询和减少数据量')

        if len(results['errors']) > 50:
            recommendations.append(f'错误数量 {len(results["errors"])} 较多，建议优先处理')

        return recommendations


def format_report(report: Dict[str, Any]) -> str:
    """格式化报告"""
    lines = []
    lines.append("=" * 60)
    lines.append("日志分析报告")
    lines.append(f"时间: {report['timestamp']}")
    lines.append("=" * 60)

    summary = report['summary']
    lines.append(f"\n摘要:")
    lines.append(f"  文件数: {summary['total_files']}")
    lines.append(f"  错误数: {summary['total_errors']}")
    lines.append(f"  警告数: {summary['total_warnings']}")
    lines.append(f"  错误率: {summary['error_rate']:.2f}%")

    if report['patterns']:
        lines.append(f"\n模式统计:")
        for pattern, count in sorted(report['patterns'].items(), key=lambda x: x[1], reverse=True):
            lines.append(f"  {pattern}: {count}")

    if report['top_errors']:
        lines.append(f"\n常见错误:")
        for error in report['top_errors']:
            lines.append(f"  [{error['count']}次] {error['pattern']}")

    if report['recommendations']:
        lines.append(f"\n建议:")
        for rec in report['recommendations']:
            lines.append(f"  - {rec}")

    lines.append("=" * 60)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description='日志分析工具')
    parser.add_argument('command', choices=['analyze', 'errors', 'report'], help='命令')
    parser.add_argument('--hours', type=int, default=24, help='分析时间范围（小时）')
    parser.add_argument('--output', help='输出文件路径')

    args = parser.parse_args()

    analyzer = LogAnalyzer()

    if args.command == 'analyze':
        results = analyzer.analyze_logs(args.hours)
        print(json.dumps(results, indent=2, ensure_ascii=False, default=str))

    elif args.command == 'errors':
        errors = analyzer.extract_errors(args.hours)
        for error in errors:
            print(f"[{error['file']}:{error['line']}] {error['content']}")

    elif args.command == 'report':
        report = analyzer.generate_report(args.hours)
        print(format_report(report))

        # 保存JSON
        if args.output:
            output_path = Path(args.output)
        else:
            output_path = project_root / 'logs' / f'log_analysis_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        print(f"\n报告已保存: {output_path}")


if __name__ == '__main__':
    main()
