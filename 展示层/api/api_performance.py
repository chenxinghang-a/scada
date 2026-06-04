"""
性能指标API端点
提供JSON格式的系统性能指标，供前端仪表盘使用
"""
import os
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from flask import Blueprint, jsonify, request, current_app
from 用户层.auth import jwt_required
from core.service_response import api_error

performance_bp = Blueprint('performance', __name__, url_prefix='/api/performance')


class PerformanceCollector:
    """性能指标采集器"""

    def __init__(self):
        self._metrics_cache = {}
        self._cache_time = 0
        self._cache_ttl = 5  # 缓存5秒
        self._lock = threading.Lock()
        self._project_root = Path(__file__).parent.parent.parent
        self._data_dir = self._project_root / 'data'

    def _collect_system_metrics(self) -> dict:
        """采集系统指标"""
        metrics = {}

        # CPU使用率
        try:
            import psutil
            metrics['cpu_percent'] = psutil.cpu_percent(interval=0.5)
            metrics['cpu_count'] = psutil.cpu_count()
        except ImportError:
            metrics['cpu_percent'] = -1
            metrics['cpu_count'] = os.cpu_count() or -1

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
            total, used, free = shutil.disk_usage(str(self._project_root))
            metrics['disk_total_gb'] = round(total / (1024**3), 2)
            metrics['disk_used_gb'] = round(used / (1024**3), 2)
            metrics['disk_free_gb'] = round(free / (1024**3), 2)
            metrics['disk_percent'] = round(used / total * 100, 1)
        except Exception:
            metrics['disk_percent'] = -1

        # 线程数
        metrics['thread_count'] = threading.active_count()

        return metrics

    def _collect_database_metrics(self) -> dict:
        """采集数据库指标"""
        db_path = self._data_dir / 'scada.db'
        if not db_path.exists():
            return {'status': 'missing', 'size_mb': 0}

        metrics = {
            'size_mb': round(db_path.stat().st_size / (1024 * 1024), 2),
        }

        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            cursor = conn.cursor()

            # 表统计
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]

            table_counts = {}
            for table in tables:
                try:
                    cursor.execute(f"SELECT COUNT(*) FROM [{table}]")
                    table_counts[table] = cursor.fetchone()[0]
                except:
                    table_counts[table] = -1

            metrics['tables'] = table_counts
            metrics['total_records'] = sum(v for v in table_counts.values() if v > 0)

            # WAL大小
            wal_path = self._data_dir / 'scada.db-wal'
            if wal_path.exists():
                metrics['wal_size_mb'] = round(wal_path.stat().st_size / (1024 * 1024), 2)

            conn.close()
        except Exception as e:
            metrics['error'] = str(e)

        return metrics

    def _collect_application_metrics(self) -> dict:
        """采集应用指标"""
        metrics = {}

        # 日志统计
        log_dir = self._project_root / 'logs'
        if log_dir.exists():
            log_files = list(log_dir.glob('*.log'))
            metrics['log_count'] = len(log_files)
            metrics['log_total_mb'] = round(
                sum(f.stat().st_size for f in log_files) / (1024 * 1024), 2
            )

        # 配置文件统计
        config_dir = self._project_root / '配置'
        if config_dir.exists():
            config_files = list(config_dir.glob('*.yaml'))
            metrics['config_count'] = len(config_files)

        return metrics

    def collect_all(self, use_cache: bool = True) -> dict:
        """采集所有指标（带缓存）"""
        now = time.time()

        with self._lock:
            if use_cache and self._metrics_cache and (now - self._cache_time) < self._cache_ttl:
                return self._metrics_cache

        metrics = {
            'timestamp': datetime.now().isoformat(),
            'system': self._collect_system_metrics(),
            'database': self._collect_database_metrics(),
            'application': self._collect_application_metrics(),
        }

        with self._lock:
            self._metrics_cache = metrics
            self._cache_time = now

        return metrics

    def get_history(self, hours: int = 24) -> list:
        """获取历史指标"""
        metrics_file = self._project_root / 'metrics' / 'metrics.jsonl'
        if not metrics_file.exists():
            return []

        cutoff = datetime.now() - timedelta(hours=hours)
        history = []

        with open(metrics_file, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                    timestamp = datetime.fromisoformat(data['timestamp'])
                    if timestamp >= cutoff:
                        history.append(data)
                except:
                    continue

        return history


# 全局实例
collector = PerformanceCollector()


@performance_bp.route('/metrics/realtime', methods=['GET'])
@jwt_required
def get_realtime_metrics():
    """获取实时性能指标"""
    try:
        metrics = collector.collect_all()
        return jsonify(metrics)
    except Exception as e:
        return api_error(f'获取指标失败: {str(e)}', 500)


@performance_bp.route('/metrics/history', methods=['GET'])
@jwt_required
def get_metrics_history():
    """获取历史指标"""
    hours = request.args.get('hours', 24, type=int)
    try:
        history = collector.get_history(hours)
        return jsonify({
            'hours': hours,
            'samples': len(history),
            'data': history,
        })
    except Exception as e:
        return api_error(f'获取历史指标失败: {str(e)}', 500)


@performance_bp.route('/metrics/summary', methods=['GET'])
@jwt_required
def get_metrics_summary():
    """获取指标摘要（统计信息）"""
    hours = request.args.get('hours', 24, type=int)
    try:
        history = collector.get_history(hours)

        if not history:
            return jsonify({
                'hours': hours,
                'samples': 0,
                'message': '无历史数据',
            })

        cpu_values = [m.get('system', {}).get('cpu_percent', 0) for m in history if m.get('system', {}).get('cpu_percent', -1) > 0]
        memory_values = [m.get('system', {}).get('memory_percent', 0) for m in history if m.get('system', {}).get('memory_percent', -1) > 0]

        summary = {
            'hours': hours,
            'samples': len(history),
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
            'latest': history[-1] if history else None,
        }

        return jsonify(summary)
    except Exception as e:
        return api_error(f'获取指标摘要失败: {str(e)}', 500)


# 需要导入time模块
import time
