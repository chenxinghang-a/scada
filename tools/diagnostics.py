"""
运行时诊断工�?用于诊断SCADA系统运行状态和性能问题

使用方法:
    python tools/diagnostics.py full           # 完整诊断
    python tools/diagnostics.py connectivity   # 连接诊断
    python tools/diagnostics.py performance    # 性能诊断
    python tools/diagnostics.py memory         # 内存诊断
"""

import os
import sys
import json
import time
import sqlite3
import argparse
import platform
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


class SystemDiagnostics:
    """系统诊断工具"""

    def __init__(self):
        self.project_root = project_root
        self.data_dir = project_root / 'data'
        self.log_dir = project_root / 'logs'
        self.config_dir = project_root / '配置'

    def run_full_diagnostics(self) -> Dict[str, Any]:
        """运行完整诊断"""
        results = {
            'timestamp': datetime.now().isoformat(),
            'system': self._check_system(),
            'database': self._check_database(),
            'config': self._check_config(),
            'logs': self._check_logs(),
            'network': self._check_network(),
            'memory': self._check_memory(),
            'performance': self._check_performance(),
        }

        # 计算总体状�?        statuses = []
        for section in results.values():
            if isinstance(section, dict) and 'status' in section:
                statuses.append(section['status'])

        if 'error' in statuses:
            results['overall_status'] = 'error'
        elif 'warning' in statuses:
            results['overall_status'] = 'warning'
        else:
            results['overall_status'] = 'healthy'

        return results

    def _check_system(self) -> Dict[str, Any]:
        """检查系统信�?""
        return {
            'status': 'ok',
            'platform': platform.platform(),
            'python_version': platform.python_version(),
            'cpu_count': os.cpu_count(),
            'cwd': str(self.project_root),
            'pid': os.getpid(),
            'thread_count': threading.active_count(),
        }

    def _check_database(self) -> Dict[str, Any]:
        """检查数据库状�?""
        db_path = self.data_dir / 'scada.db'

        if not db_path.exists():
            return {'status': 'error', 'message': '数据库文件不存在'}

        try:
            conn = sqlite3.connect(str(db_path), timeout=5)

            # 检查表结构
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]

            # 检查记录数
            table_counts = {}
            for table in tables:
                try:
                    cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")
                    table_counts[table] = cursor.fetchone()[0]
                except Exception:
                    table_counts[table] = -1

            # 检查WAL状�?            cursor = conn.execute("PRAGMA wal_checkpoint")
            wal_status = cursor.fetchone()

            # 检查数据库大小
            db_size_mb = db_path.stat().st_size / (1024 * 1024)

            conn.close()

            return {
                'status': 'ok',
                'path': str(db_path),
                'size_mb': round(db_size_mb, 2),
                'tables': len(tables),
                'table_counts': table_counts,
                'wal_checkpoint': wal_status[0] if wal_status else 'unknown',
            }
        except Exception as e:
            return {'status': 'error', 'message': str(e)}

    def _check_config(self) -> Dict[str, Any]:
        """检查配置文�?""
        if not self.config_dir.exists():
            return {'status': 'warning', 'message': '配置目录不存�?}

        config_files = list(self.config_dir.glob('*.yaml'))

        results = {}
        for file in config_files:
            try:
                import yaml
                with open(file, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f)
                results[file.name] = {
                    'status': 'ok',
                    'size_kb': round(file.stat().st_size / 1024, 2),
                }
            except Exception as e:
                results[file.name] = {'status': 'error', 'message': str(e)}

        return {
            'status': 'ok',
            'files': len(config_files),
            'details': results,
        }

    def _check_logs(self) -> Dict[str, Any]:
        """检查日志状�?""
        if not self.log_dir.exists():
            return {'status': 'warning', 'message': '日志目录不存�?}

        log_files = list(self.log_dir.glob('*.log'))
        total_size = sum(f.stat().st_size for f in log_files)

        # 检查最近的错误
        recent_errors = 0
        for log_file in log_files[-5:]:  # 检查最�?个日志文�?            try:
                with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        if 'ERROR' in line or 'CRITICAL' in line:
                            recent_errors += 1
            except Exception:
                pass

        return {
            'status': 'warning' if recent_errors > 10 else 'ok',
            'file_count': len(log_files),
            'total_size_mb': round(total_size / (1024 * 1024), 2),
            'recent_errors': recent_errors,
        }

    def _check_network(self) -> Dict[str, Any]:
        """检查网络连�?""
        results = {
            'status': 'ok',
            'localhost': self._ping('localhost'),
        }

        # 检查常用端�?        ports_to_check = [5000, 502, 4840, 1883]
        port_results = {}

        for port in ports_to_check:
            try:
                import socket
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2)
                result = sock.connect_ex(('localhost', port))
                port_results[port] = 'open' if result == 0 else 'closed'
                sock.close()
            except Exception:
                port_results[port] = 'error'

        results['ports'] = port_results

        return results

    def _ping(self, host: str) -> bool:
        """Ping主机"""
        try:
            import subprocess
            param = '-n' if platform.system().lower() == 'windows' else '-c'
            command = ['ping', param, '1', host]
            result = subprocess.run(command, capture_output=True, timeout=5)
            return result.returncode == 0
        except Exception:
            return False

    def _check_memory(self) -> Dict[str, Any]:
        """检查内存使�?""
        try:
            import psutil
            memory = psutil.virtual_memory()
            return {
                'status': 'warning' if memory.percent > 80 else 'ok',
                'total_gb': round(memory.total / (1024**3), 2),
                'available_gb': round(memory.available / (1024**3), 2),
                'percent': memory.percent,
            }
        except ImportError:
            return {'status': 'unknown', 'message': 'psutil未安�?}

    def _check_performance(self) -> Dict[str, Any]:
        """检查性能指标"""
        # 测试数据库写入性能
        db_path = self.data_dir / 'scada.db'
        if not db_path.exists():
            return {'status': 'error', 'message': '数据库不存在'}

        try:
            conn = sqlite3.connect(str(db_path), timeout=5)

            # 写入测试
            start = time.time()
            conn.execute("CREATE TABLE IF NOT EXISTS _perf_test (id INTEGER, value TEXT)")
            for i in range(100):
                conn.execute("INSERT INTO _perf_test VALUES (?, ?)", (i, f"test_{i}"))
            conn.commit()
            write_time = time.time() - start

            # 读取测试
            start = time.time()
            cursor = conn.execute("SELECT COUNT(*) FROM _perf_test")
            read_time = time.time() - start

            # 清理
            conn.execute("DROP TABLE IF EXISTS _perf_test")
            conn.commit()
            conn.close()

            return {
                'status': 'ok',
                'write_100_rows_ms': round(write_time * 1000, 2),
                'read_count_ms': round(read_time * 1000, 2),
            }
        except Exception as e:
            return {'status': 'error', 'message': str(e)}

    def run_connectivity_diagnostics(self) -> Dict[str, Any]:
        """运行连接诊断"""
        return {
            'timestamp': datetime.now().isoformat(),
            'network': self._check_network(),
            'database': self._check_database(),
        }

    def run_performance_diagnostics(self) -> Dict[str, Any]:
        """运行性能诊断"""
        return {
            'timestamp': datetime.now().isoformat(),
            'performance': self._check_performance(),
            'memory': self._check_memory(),
            'system': self._check_system(),
        }

    def run_memory_diagnostics(self) -> Dict[str, Any]:
        """运行内存诊断"""
        return {
            'timestamp': datetime.now().isoformat(),
            'memory': self._check_memory(),
            'system': {
                'thread_count': threading.active_count(),
                'pid': os.getpid(),
            },
        }


def format_report(results: Dict[str, Any], indent: int = 2) -> str:
    """格式化诊断报�?""
    lines = []
    lines.append("=" * 60)
    lines.append("SCADA系统诊断报告")
    lines.append(f"时间: {results.get('timestamp', 'N/A')}")
    lines.append("=" * 60)

    for section, data in results.items():
        if section == 'timestamp':
            continue

        lines.append(f"\n[{section.upper()}]")
        if isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, dict):
                    lines.append(f"  {key}:")
                    for k, v in value.items():
                        lines.append(f"    {k}: {v}")
                else:
                    lines.append(f"  {key}: {value}")

    lines.append("\n" + "=" * 60)

    if 'overall_status' in results:
        status = results['overall_status']
        if status == 'healthy':
            lines.append("�?系统状�? 正常")
        elif status == 'warning':
            lines.append("�?系统状�? 警告")
        else:
            lines.append("�?系统状�? 异常")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description='SCADA系统诊断工具')
    parser.add_argument('command', choices=['full', 'connectivity', 'performance', 'memory'],
                       help='诊断类型')

    args = parser.parse_args()

    diagnostics = SystemDiagnostics()

    if args.command == 'full':
        results = diagnostics.run_full_diagnostics()
    elif args.command == 'connectivity':
        results = diagnostics.run_connectivity_diagnostics()
    elif args.command == 'performance':
        results = diagnostics.run_performance_diagnostics()
    elif args.command == 'memory':
        results = diagnostics.run_memory_diagnostics()

    # 输出报告
    print(format_report(results))

    # 保存JSON
    output_file = project_root / 'logs' / f'diagnostics_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\n详细报告已保�? {output_file}")


if __name__ == '__main__':
    main()
