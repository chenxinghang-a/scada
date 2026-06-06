"""
内存泄漏自动检测工具
监控SCADA系统内存使用，检测潜在泄漏

使用方法:
    python tools/memory_monitor.py monitor   # 持续监控
    python tools/memory_monitor.py snapshot  # 内存快照
    python tools/memory_monitor.py report    # 生成报告
"""

import os
import sys
import json
import time
import threading
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


class MemoryMonitor:
    """内存监控器"""

    def __init__(self, interval: int = 60):
        self.interval = interval
        self.snapshots: List[Dict[str, Any]] = []
        self._running = False
        self._thread = None

    def take_snapshot(self) -> Dict[str, Any]:
        """获取内存快照"""
        snapshot = {
            'timestamp': datetime.now().isoformat(),
            'pid': os.getpid(),
        }

        try:
            import psutil
            process = psutil.Process()

            # 进程内存
            mem_info = process.memory_info()
            snapshot['process'] = {
                'rss_mb': round(mem_info.rss / (1024 * 1024), 2),  # 物理内存
                'vms_mb': round(mem_info.vms / (1024 * 1024), 2),  # 虚拟内存
                'percent': process.memory_percent(),
            }

            # 系统内存
            sys_mem = psutil.virtual_memory()
            snapshot['system'] = {
                'total_gb': round(sys_mem.total / (1024**3), 2),
                'available_gb': round(sys_mem.available / (1024**3), 2),
                'percent': sys_mem.percent,
            }

            # 线程数
            snapshot['threads'] = process.num_threads()

            # 文件描述符
            try:
                snapshot['fds'] = process.num_fds()
            except Exception:
                snapshot['fds'] = -1

        except ImportError:
            # psutil不可用时使用基础方法
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF)
            snapshot['process'] = {
                'rss_mb': round(usage.ru_maxrss / 1024, 2),
                'vms_mb': -1,
                'percent': -1,
            }

        return snapshot

    def detect_leak(self, window_size: int = 10) -> Dict[str, Any]:
        """检测内存泄漏"""
        if len(self.snapshots) < window_size:
            return {
                'status': 'insufficient_data',
                'message': f'需要至少 {window_size} 个快照，当前 {len(self.snapshots)} 个',
            }

        # 取最近的快照
        recent = self.snapshots[-window_size:]
        rss_values = [s['process']['rss_mb'] for s in recent if 'process' in s]

        if len(rss_values) < window_size:
            return {'status': 'insufficient_data'}

        # 计算趋势
        first_half = rss_values[:len(rss_values)//2]
        second_half = rss_values[len(rss_values)//2:]

        avg_first = sum(first_half) / len(first_half)
        avg_second = sum(second_half) / len(second_half)

        growth_rate = (avg_second - avg_first) / avg_first * 100 if avg_first > 0 else 0

        # 判断是否泄漏
        if growth_rate > 20:  # 增长超过20%
            return {
                'status': 'leak_detected',
                'growth_rate': round(growth_rate, 1),
                'avg_first_mb': round(avg_first, 2),
                'avg_second_mb': round(avg_second, 2),
                'message': f'内存增长 {growth_rate:.1f}%，可能存在泄漏',
            }
        elif growth_rate > 10:  # 增长超过10%
            return {
                'status': 'warning',
                'growth_rate': round(growth_rate, 1),
                'message': f'内存增长 {growth_rate:.1f}%，需要关注',
            }
        else:
            return {
                'status': 'ok',
                'growth_rate': round(growth_rate, 1),
                'message': f'内存稳定，增长率 {growth_rate:.1f}%',
            }

    def start_monitoring(self):
        """启动持续监控"""
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        print(f"内存监控已启动（间隔 {self.interval} 秒）")

    def stop_monitoring(self):
        """停止监控"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        print("内存监控已停止")

    def _monitor_loop(self):
        """监控循环"""
        while self._running:
            try:
                snapshot = self.take_snapshot()
                self.snapshots.append(snapshot)

                # 只保留最近1000个快照
                if len(self.snapshots) > 1000:
                    self.snapshots = self.snapshots[-1000:]

                # 检测泄漏
                if len(self.snapshots) >= 10:
                    leak_result = self.detect_leak()
                    if leak_result['status'] == 'leak_detected':
                        print(f"[警告] {leak_result['message']}")
                        print(f"  平均内存: {leak_result['avg_first_mb']}MB -> {leak_result['avg_second_mb']}MB")

            except Exception as e:
                print(f"监控异常: {e}")

            time.sleep(self.interval)

    def generate_report(self) -> Dict[str, Any]:
        """生成报告"""
        if not self.snapshots:
            return {'message': '无数据'}

        latest = self.snapshots[-1]
        leak_result = self.detect_leak() if len(self.snapshots) >= 10 else {'status': 'insufficient_data'}

        return {
            'timestamp': datetime.now().isoformat(),
            'snapshots_count': len(self.snapshots),
            'latest': latest,
            'leak_detection': leak_result,
            'memory_trend': self._get_trend(),
        }

    def _get_trend(self) -> Dict[str, Any]:
        """获取内存趋势"""
        if len(self.snapshots) < 2:
            return {}

        rss_values = [s['process']['rss_mb'] for s in self.snapshots if 'process' in s]

        if not rss_values:
            return {}

        return {
            'min_mb': round(min(rss_values), 2),
            'max_mb': round(max(rss_values), 2),
            'avg_mb': round(sum(rss_values) / len(rss_values), 2),
            'current_mb': round(rss_values[-1], 2),
        }

    def save_report(self, filepath: str = None):
        """保存报告"""
        if filepath is None:
            filepath = str(project_root / 'logs' / f'memory_report_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json')

        report = self.generate_report()

        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        print(f"报告已保存: {filepath}")
        return filepath


def main():
    parser = argparse.ArgumentParser(description='内存泄漏检测工具')
    parser.add_argument('command', choices=['monitor', 'snapshot', 'report'], help='命令')
    parser.add_argument('--interval', type=int, default=60, help='监控间隔（秒）')
    parser.add_argument('--output', help='输出文件路径')

    args = parser.parse_args()

    monitor = MemoryMonitor(interval=args.interval)

    if args.command == 'snapshot':
        snapshot = monitor.take_snapshot()
        print(json.dumps(snapshot, indent=2, ensure_ascii=False))

    elif args.command == 'monitor':
        try:
            monitor.start_monitoring()
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            monitor.stop_monitoring()
            monitor.save_report(args.output)

    elif args.command == 'report':
        # 快速采集几个快照
        for _ in range(5):
            monitor.snapshots.append(monitor.take_snapshot())
            time.sleep(1)

        report = monitor.generate_report()
        print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
