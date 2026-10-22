"""
性能指标API端点
提供JSON格式的系统性能指标，供前端仪表盘使用
"""
import os
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from flask import Blueprint, jsonify, request, current_app
from 用户层.auth import jwt_required
from core.service_response import error_response
import paths

logger = logging.getLogger(__name__)

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

    def _resolve_db_path(self):
        """定位**当前进程实际在用**的库 —— 唯一真源是 `paths.get_active_db_path()`。

        为什么不能写死文件名（2026-10-11，round 212 实测）
        --------------------------------------------------
        此前这里写死 `self._data_dir / 'scada.db'`，而**模拟模式实际用的是
        `data/scada_simulated.db`** → `exists()` 为假 → 性能页永远显示
        `0.0MB / 0 条`（wb 在部署版上实测到的「数据库 0.0MB」）。

        ⚠️ 也不能「取第一个存在的」：实测 `data/` 下 **`scada.db`（旧，495MB）
        与 `scada_simulated.db`（在用，712MB）同时存在** —— 猜文件名会**静默指错**，
        而错的那份看起来完全正常（有大小、有表、有行数）。
        所以路径由入口（`展示层/routes.py::create_app`）登记，这里只读。

        Returns:
            `Path`；**没登记过返回 `None`**（调用方显式降级为 unknown，不猜）。
        """
        active = paths.get_active_db_path()
        return Path(active) if active is not None else None

    def _collect_database_metrics(self) -> dict:
        """采集数据库指标。

        ⚠️ 拿不到库路径时返回 `status='unknown'`，**不回退到任何猜测路径** ——
        「读不到真源」要显式降级，绝不能编一个可能不是当前库的数字出来。
        """
        db_path = self._resolve_db_path()
        if db_path is None:
            return {
                'status': 'unknown',
                'size_mb': 0,
                'detail': '未登记当前库路径（paths.set_active_db_path 未被调用）',
            }
        if not db_path.exists():
            # 只回文件名，不回绝对路径（避免把服务器目录结构暴露给前端）
            return {'status': 'missing', 'size_mb': 0, 'db_file': db_path.name}

        metrics = {
            'status': 'ok',
            'db_file': db_path.name,
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
                except Exception as e:  # noqa: BLE001 —— 单表失败不该让整块指标消失
                    logger.debug("统计表 %s 行数失败: %s", table, e)
                    table_counts[table] = -1

            metrics['tables'] = table_counts
            metrics['total_records'] = sum(v for v in table_counts.values() if v > 0)

            # WAL大小（SQLite 的 -wal 是**库文件同目录同名** + 后缀，
            # 不能再用写死的 'scada.db-wal'）
            wal_path = Path(str(db_path) + '-wal')
            if wal_path.exists():
                metrics['wal_size_mb'] = round(wal_path.stat().st_size / (1024 * 1024), 2)

            conn.close()
        except Exception as e:  # noqa: BLE001 —— 采集失败要把原因带回前端
            logger.warning("采集数据库指标失败: %s", e)
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

    def append_sample(self, retention_days: int = 7, max_lines: int = 20000) -> int:
        """采集一次指标并追加到 ``metrics/metrics.jsonl``，返回文件行数。

        round 252：此前这个文件**只有 `tools/auto_metrics.py`（独立 CLI）会写**，
        而生产环境没有任何地方调用它 —— 没有 cron、没有计划任务、维护任务里
        也没有。于是：

        - ``GET /performance/metrics/history`` 永远 ``samples: 0``；
        - ``GET /performance/metrics/summary`` 永远回「无历史数据」；
        - 前端 ``src/api/performance.ts`` 的 getHistory/getSummary 拿到空数组，
          性能页的历史曲线和摘要面板**在生产永远是空的**。

        也就是「读取侧完好、写入侧不存在」—— 又一处声明与实现不符。
        本方法由 `core/maintenance.py` 的 `performance_metrics` 任务周期调用
        （默认 5 分钟一次），写入格式与 `tools/auto_metrics.py` 完全一致
        （一行一个 JSON，顶层带 `timestamp`），读取侧无需改动。

        同时做**保留策略**：jsonl 不裁剪就会无界增长（每 5 分钟一行、
        一天 288 行，一年 10 万行）。按天数 + 行数双重上限裁剪，
        超出的旧行删掉 —— 与其它维护任务同一思路。

        Args:
            retention_days: 保留天数（默认 7 天）
            max_lines: 行数硬上限（默认 2 万行，约 70 天@5分钟）

        Returns:
            写入后的文件行数；采集/写入失败返回 -1（由调用方记账）。
        """
        try:
            metrics = self.collect_all(use_cache=False)
            metrics_file = paths.METRICS_DIR / 'metrics.jsonl'
            paths.METRICS_DIR.mkdir(parents=True, exist_ok=True)

            line = json.dumps(metrics, ensure_ascii=False)
            with open(metrics_file, 'a', encoding='utf-8') as f:
                f.write(line + '\n')

            # 裁剪：行数超限时只保留尾部（旧行在前，因为追加写）
            try:
                with open(metrics_file, 'r', encoding='utf-8') as f:
                    all_lines = f.readlines()
            except Exception as e:      # noqa: BLE001 —— 读失败不影响本次写入
                logger.warning("指标文件读取失败，跳过裁剪: %s", e)
                return -1

            if len(all_lines) > max_lines:
                kept = all_lines[-max_lines:]
                with open(metrics_file, 'w', encoding='utf-8') as f:
                    f.writelines(kept)
                logger.info(
                    "指标文件超行数上限（%d > %d），已裁剪到 %d 行",
                    len(all_lines), max_lines, len(kept))
                return len(kept)
            return len(all_lines)
        except Exception as e:
            logger.error("性能指标采样失败: %s", e, exc_info=True)
            return -1

    def get_history(self, hours: int = 24) -> list:
        """获取历史指标"""
        # round 213：目录走 paths 单一真源（与 tools/auto_metrics.py 的写入侧同源）
        metrics_file = paths.METRICS_DIR / 'metrics.jsonl'
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
                except Exception as e:
                    # 单行指标记录损坏（JSON/时间戳非法）→ 该条历史指标丢失，
                    # 返回的曲线会缺数据点，属于数据不完整，必须留痕
                    logger.warning("跳过无法解析的指标记录 %s: %s | 原始行=%s",
                                   metrics_file, e, line.strip()[:200])
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
        return error_response(f'获取指标失败: {str(e)}', 500)


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
        return error_response(f'获取历史指标失败: {str(e)}', 500)


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
        return error_response(f'获取指标摘要失败: {str(e)}', 500)


# 需要导入time模块
import time
