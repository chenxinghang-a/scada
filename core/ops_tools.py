"""
运维工具集
提供运行时配置热更新、系统诊断导出、数据库维护等运维能力。

功能:
  - 配置热更新（不重启修改运行参数）
  - 系统诊断一键导出（打包日志+配置+状态）
  - 数据库维护（VACUUM/REINDEX/ANALYZE）
  - 过期数据自动清理
  - 运维操作审计日志
"""

import os
import json
import time
import shutil
import sqlite3
import logging
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable
from datetime import datetime, timedelta
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class OpsAuditLogger:
    """运维操作审计日志"""

    def __init__(self, log_dir: str = "logs"):
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._audit_file = self._log_dir / "ops_audit.jsonl"
        self._lock = threading.Lock()

    def log_operation(
        self,
        operation: str,
        operator: str = "system",
        target: str = "",
        details: Dict[str, Any] = None,
        result: str = "success",
        error: str = "",
    ):
        """记录运维操作"""
        entry = {
            'timestamp': datetime.now().isoformat(),
            'operation': operation,
            'operator': operator,
            'target': target,
            'result': result,
            'error': error,
            'details': details or {},
        }
        with self._lock:
            try:
                with open(self._audit_file, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + '\n')
            except Exception as e:
                logger.debug(f"运维审计写入失败: {e}")

    def get_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        """获取最近的运维操作记录"""
        entries = []
        try:
            if self._audit_file.exists():
                with open(self._audit_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                entries.append(json.loads(line))
                            except json.JSONDecodeError as e:
                                # 安全忽略：审计日志为追加写入，进程被中断时会残留半行，
                                # 单行解析失败跳过即可，不应影响其余记录的读取。
                                logger.debug(f"运维审计日志单行解析失败(跳过该行): {e}")
        except Exception as e:
            # 整文件读取失败 → get_recent 返回空/不完整列表，会被误读为"无运维操作记录"
            logger.warning(f"读取运维审计日志失败(返回结果可能不完整): file={self._audit_file}: {e}")
        return entries[-limit:]


class RuntimeConfigManager:
    """
    运行时配置热更新管理器

    支持在不重启的情况下修改运行参数。
    变更通过回调通知相关模块。
    """

    def __init__(self):
        self._config: Dict[str, Any] = {}
        self._callbacks: Dict[str, List[Callable]] = {}
        self._lock = threading.RLock()
        self._history: List[Dict[str, Any]] = []
        self._max_history = 100

        # 加载默认配置
        self._load_defaults()

    def _load_defaults(self):
        """加载默认运行时配置"""
        self._config = {
            'alarm_dedup_window_sec': 30,
            'alarm_flood_threshold': 100,
            'alarm_flood_window_sec': 60,
            'data_retention_days': 90,
            'log_retention_days': 30,
            'websocket_push_interval_ms': 1000,
            'device_poll_interval_sec': 5,
            'health_check_interval_sec': 30,
            'max_concurrent_requests': 100,
            'request_timeout_sec': 30,
            'backup_enabled': True,
            'backup_interval_hours': 24,
            'metrics_enabled': True,
            'tracing_enabled': False,
        }

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值"""
        with self._lock:
            return self._config.get(key, default)

    def set(self, key: str, value: Any, operator: str = "system") -> bool:
        """
        设置配置值（热更新）

        Args:
            key: 配置键
            value: 配置值
            operator: 操作者

        Returns:
            True 如果值已变更
        """
        with self._lock:
            old_value = self._config.get(key)
            if old_value == value:
                return False

            self._config[key] = value
            self._history.append({
                'time': datetime.now().isoformat(),
                'key': key,
                'old': old_value,
                'new': value,
                'operator': operator,
            })
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

            # 通知回调
            for cb in self._callbacks.get(key, []):
                try:
                    cb(key, value, old_value)
                except Exception as e:
                    logger.error(f"配置回调异常 [{key}]: {e}")

            logger.info(f"运行时配置变更: {key} = {value} (原值: {old_value})")
            return True

    def update_batch(self, updates: Dict[str, Any], operator: str = "system") -> int:
        """批量更新配置"""
        changed = 0
        for key, value in updates.items():
            if self.set(key, value, operator):
                changed += 1
        return changed

    def register_callback(self, key: str, callback: Callable):
        """注册配置变更回调"""
        with self._lock:
            if key not in self._callbacks:
                self._callbacks[key] = []
            self._callbacks[key].append(callback)

    def get_all(self) -> Dict[str, Any]:
        """获取所有配置"""
        with self._lock:
            return dict(self._config)

    def get_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """获取配置变更历史"""
        with self._lock:
            return self._history[-limit:]

    def reset(self, key: str = None, operator: str = "system"):
        """重置配置到默认值"""
        defaults = {}
        self._load_defaults()
        defaults = dict(self._config)
        self._load_defaults()  # 重新加载默认值

        if key:
            default_val = defaults.get(key)
            if default_val is not None:
                self.set(key, default_val, operator)
        else:
            self._config = defaults


class DatabaseMaintainer:
    """
    数据库维护工具

    提供 VACUUM、REINDEX、ANALYZE 等维护操作。
    """

    def __init__(self, db_path: str = None):
        self._db_path = db_path

    def set_db_path(self, path: str):
        """设置数据库路径"""
        self._db_path = path

    @staticmethod
    def _validate_table_name(conn: sqlite3.Connection, table: str) -> str:
        """
        验证表名安全性（防SQL注入）

        1. 检查表名仅含安全字符 [a-zA-Z0-9_]
        2. 确认表存在于 sqlite_master
        3. 返回安全的标识符（双引号引用）

        Raises:
            ValueError: 表名不安全或不存在
        """
        import re
        if not table or not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', table):
            raise ValueError(f"不安全的表名: {table!r}")
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            raise ValueError(f"表不存在: {table!r}")
        return f'"{table}"'

    @contextmanager
    def _get_conn(self):
        """获取维护用连接"""
        if not self._db_path:
            raise ValueError("数据库路径未设置")
        conn = sqlite3.connect(self._db_path, timeout=60)
        try:
            yield conn
        finally:
            conn.close()

    def vacuum(self) -> Dict[str, Any]:
        """
        VACUUM - 回收已删除数据的空间

        对于大数据库可能耗时较长。
        """
        start = time.time()
        try:
            # 获取压缩前大小
            size_before = os.path.getsize(self._db_path)

            with self._get_conn() as conn:
                conn.execute('VACUUM')

            size_after = os.path.getsize(self._db_path)
            elapsed = time.time() - start

            result = {
                'operation': 'VACUUM',
                'status': 'success',
                'size_before_mb': round(size_before / 1024 / 1024, 2),
                'size_after_mb': round(size_after / 1024 / 1024, 2),
                'freed_mb': round((size_before - size_after) / 1024 / 1024, 2),
                'duration_sec': round(elapsed, 2),
            }
            logger.info(f"VACUUM 完成: {result['size_before_mb']}MB → {result['size_after_mb']}MB, 释放 {result['freed_mb']}MB")
            return result
        except Exception as e:
            return {'operation': 'VACUUM', 'status': 'error', 'error': str(e), 'duration_sec': time.time() - start}

    def reindex(self, table: str = None) -> Dict[str, Any]:
        """
        REINDEX - 重建索引

        Args:
            table: 指定表名（None 则重建所有）
        """
        start = time.time()
        try:
            with self._get_conn() as conn:
                if table:
                    safe_name = self._validate_table_name(conn, table)
                    sql = 'REINDEX ' + safe_name
                    conn.execute(sql)
                else:
                    conn.execute('REINDEX')

            elapsed = time.time() - start
            result = {
                'operation': 'REINDEX',
                'target': table or 'ALL',
                'status': 'success',
                'duration_sec': round(elapsed, 2),
            }
            logger.info(f"REINDEX 完成: {table or 'ALL'}, 耗时 {elapsed:.2f}s")
            return result
        except Exception as e:
            return {'operation': 'REINDEX', 'target': table or 'ALL', 'status': 'error', 'error': str(e), 'duration_sec': time.time() - start}

    def analyze(self) -> Dict[str, Any]:
        """
        ANALYZE - 更新查询优化器统计信息
        """
        start = time.time()
        try:
            with self._get_conn() as conn:
                conn.execute('ANALYZE')

            elapsed = time.time() - start
            result = {
                'operation': 'ANALYZE',
                'status': 'success',
                'duration_sec': round(elapsed, 2),
            }
            logger.info(f"ANALYZE 完成, 耗时 {elapsed:.2f}s")
            return result
        except Exception as e:
            return {'operation': 'ANALYZE', 'status': 'error', 'error': str(e), 'duration_sec': time.time() - start}

    def integrity_check(self) -> Dict[str, Any]:
        """
        完整性检查
        """
        start = time.time()
        try:
            with self._get_conn() as conn:
                cursor = conn.execute('PRAGMA integrity_check')
                result_text = cursor.fetchone()[0]

            elapsed = time.time() - start
            return {
                'operation': 'integrity_check',
                'status': 'ok' if result_text == 'ok' else 'error',
                'result': result_text,
                'duration_sec': round(elapsed, 2),
            }
        except Exception as e:
            return {'operation': 'integrity_check', 'status': 'error', 'error': str(e), 'duration_sec': time.time() - start}

    def get_table_stats(self) -> List[Dict[str, Any]]:
        """获取各表统计信息"""
        try:
            with self._get_conn() as conn:
                cursor = conn.execute("""
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name NOT LIKE 'sqlite_%'
                    ORDER BY name
                """)
                tables = [row[0] for row in cursor.fetchall()]

                stats = []
                for table in tables:
                    try:
                        safe_name = self._validate_table_name(conn, table)
                        sql = 'SELECT COUNT(*) FROM ' + safe_name
                        count = conn.execute(sql).fetchone()[0]
                        stats.append({'table': table, 'row_count': count})
                    except Exception:
                        stats.append({'table': table, 'row_count': -1})
                return stats
        except Exception as e:
            return [{'error': str(e)}]


class DataCleaner:
    """
    过期数据自动清理

    清理过期的历史数据、日志文件、备份文件。
    """

    def __init__(self, db_path: str = None):
        self._db_path = db_path

    def set_db_path(self, path: str):
        self._db_path = path

    def _require_db_path(self) -> str:
        """返回已配置的库路径；未配置时给出**明确**报错。

        round 162 修复。原先这里直接把 `self._db_path` 交给 `sqlite3.connect()`：
        未接线时抛的是 `TypeError: expected str, bytes or os.PathLike object,
        not NoneType`，被下面的 `except Exception` 收成 `{'status': 'error'}`，
        再被 `展示层/api/api_ops.py` 无条件包进 `success_response` →
        **HTTP 200 + body 里 `status:'error'`**，即本项目最典型的"静默假成功"：
        管理员点「清理历史数据」，接口回 200，界面显示成功，而一行都没删。

        实测（修复前）：`DataCleaner().clean_history_data(90)` →
        `{'status': 'error', 'error': 'expected str, bytes or os.PathLike object,
        not NoneType'}`，且 `set_db_path()` 全仓库**无任何调用方**。
        生产接线见 `run.py` 建库之后。
        """
        if not self._db_path:
            raise RuntimeError(
                'DataCleaner 未配置数据库路径 —— 请先调用 set_db_path()。'
                '（生产环境由 run.py 在建库后接线；测试请显式传 db_path）'
            )
        return self._db_path

    def clean_history_data(self, retention_days: int = 90) -> Dict[str, Any]:
        """清理过期历史数据"""
        start = time.time()
        try:
            # sep=' ' 与库内写入格式一致；用默认的 'T' 分隔会多删同日的记录
            cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat(sep=' ')
            conn = sqlite3.connect(self._require_db_path(), timeout=30)
            cursor = conn.execute(
                "DELETE FROM history_data WHERE timestamp < ?",
                (cutoff,)
            )
            deleted = cursor.rowcount
            conn.commit()
            conn.close()

            elapsed = time.time() - start
            result = {
                'operation': 'clean_history_data',
                'retention_days': retention_days,
                'deleted_rows': deleted,
                'cutoff_date': cutoff,
                'status': 'success',
                'duration_sec': round(elapsed, 2),
            }
            logger.info(f"清理历史数据: 删除 {deleted} 条, 保留 {retention_days} 天")
            return result
        except Exception as e:
            return {'operation': 'clean_history_data', 'status': 'error', 'error': str(e), 'duration_sec': time.time() - start}

    def clean_audit_logs(self, retention_days: int = 30) -> Dict[str, Any]:
        """清理过期审计日志

        ⚠️ 注意：审计日志在**独立**的库（`用户层/audit_logger.py`，
        默认 `data/audit.db`），不是业务库。本方法用的是同一个 `_db_path`，
        所以生产环境直接调用它会报 `no such table: audit_log` ——
        这是**如实失败**，好过静默假成功。
        审计系统该收敛到哪一套属 P2-8 的产品口径，见队列「待主人确认」第 8 条。
        """
        start = time.time()
        try:
            # sep=' ' 与库内写入格式一致；用默认的 'T' 分隔会多删同日的记录
            cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat(sep=' ')
            conn = sqlite3.connect(self._require_db_path(), timeout=30)
            cursor = conn.execute(
                "DELETE FROM audit_log WHERE timestamp < ?",
                (cutoff,)
            )
            deleted = cursor.rowcount
            conn.commit()
            conn.close()

            elapsed = time.time() - start
            return {
                'operation': 'clean_audit_logs',
                'retention_days': retention_days,
                'deleted_rows': deleted,
                'status': 'success',
                'duration_sec': round(elapsed, 2),
            }
        except Exception as e:
            return {'operation': 'clean_audit_logs', 'status': 'error', 'error': str(e), 'duration_sec': time.time() - start}

    def clean_old_backups(self, backup_dir: str = "data", keep_count: int = 5) -> Dict[str, Any]:
        """清理旧备份文件，只保留最近 N 个"""
        start = time.time()
        try:
            backup_path = Path(backup_dir)
            backups = sorted(
                backup_path.glob("scada_backup_*.db"),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )
            deleted = []
            for backup in backups[keep_count:]:
                backup.unlink()
                deleted.append(backup.name)

            return {
                'operation': 'clean_old_backups',
                'keep_count': keep_count,
                'deleted_files': deleted,
                'deleted_count': len(deleted),
                'status': 'success',
                'duration_sec': round(time.time() - start, 2),
            }
        except Exception as e:
            return {'operation': 'clean_old_backups', 'status': 'error', 'error': str(e), 'duration_sec': time.time() - start}

    def clean_log_files(self, log_dir: str = "logs", retention_days: int = 30) -> Dict[str, Any]:
        """清理过期日志文件"""
        start = time.time()
        try:
            log_path = Path(log_dir)
            cutoff_ts = time.time() - (retention_days * 86400)
            deleted = []

            for log_file in log_path.glob("*.log*"):
                if log_file.stat().st_mtime < cutoff_ts:
                    log_file.unlink()
                    deleted.append(log_file.name)

            return {
                'operation': 'clean_log_files',
                'retention_days': retention_days,
                'deleted_files': deleted,
                'deleted_count': len(deleted),
                'status': 'success',
                'duration_sec': round(time.time() - start, 2),
            }
        except Exception as e:
            return {'operation': 'clean_log_files', 'status': 'error', 'error': str(e), 'duration_sec': time.time() - start}


class DiagnosticExporter:
    """
    系统诊断一键导出

    打包日志、配置、系统状态为诊断文件。
    """

    def __init__(self, output_dir: str = "data/diagnostics"):
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def export_diagnostics(
        self,
        include_logs: bool = True,
        include_config: bool = True,
        include_db_stats: bool = True,
        include_system_state: bool = True,
    ) -> Dict[str, Any]:
        """
        导出系统诊断信息

        Returns:
            导出结果和文件路径
        """
        start = time.time()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        diag_dir = self._output_dir / f"diag_{timestamp}"
        diag_dir.mkdir(parents=True, exist_ok=True)

        files_created = []

        try:
            # 1. 系统状态
            if include_system_state:
                state = self._collect_system_state()
                state_file = diag_dir / "system_state.json"
                with open(state_file, 'w', encoding='utf-8') as f:
                    json.dump(state, f, ensure_ascii=False, indent=2, default=str)
                files_created.append("system_state.json")

            # 2. 配置信息
            if include_config:
                config = self._collect_config()
                config_file = diag_dir / "config.json"
                with open(config_file, 'w', encoding='utf-8') as f:
                    json.dump(config, f, ensure_ascii=False, indent=2, default=str)
                files_created.append("config.json")

            # 3. 数据库统计
            if include_db_stats:
                db_stats = self._collect_db_stats()
                db_file = diag_dir / "db_stats.json"
                with open(db_file, 'w', encoding='utf-8') as f:
                    json.dump(db_stats, f, ensure_ascii=False, indent=2, default=str)
                files_created.append("db_stats.json")

            # 4. 最近日志（最后1000行）
            if include_logs:
                self._copy_recent_logs(diag_dir)
                files_created.append("logs/")

            # 打包为zip
            zip_path = self._output_dir / f"diag_{timestamp}.zip"
            shutil.make_archive(str(zip_path.with_suffix('')), 'zip', diag_dir)
            shutil.rmtree(diag_dir, ignore_errors=True)

            elapsed = time.time() - start
            return {
                'status': 'success',
                'zip_path': str(zip_path),
                'zip_size_mb': round(zip_path.stat().st_size / 1024 / 1024, 2),
                'files_included': files_created,
                'duration_sec': round(elapsed, 2),
            }
        except Exception as e:
            return {'status': 'error', 'error': str(e), 'duration_sec': time.time() - start}

    def _collect_system_state(self) -> Dict[str, Any]:
        """收集系统状态"""
        state = {
            'timestamp': datetime.now().isoformat(),
            'platform': os.name,
            'cwd': os.getcwd(),
        }
        try:
            import psutil
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage('.')
            state['memory'] = {
                'total_gb': round(mem.total / 1024**3, 2),
                'used_gb': round(mem.used / 1024**3, 2),
                'percent': mem.percent,
            }
            state['disk'] = {
                'total_gb': round(disk.total / 1024**3, 2),
                'used_gb': round(disk.used / 1024**3, 2),
                'free_gb': round(disk.free / 1024**3, 2),
                'percent': round(disk.percent, 1),
            }
            state['cpu_percent'] = psutil.cpu_percent(interval=0.5)
            state['pid'] = os.getpid()
        except ImportError:
            state['note'] = 'psutil未安装，部分系统信息不可用'

        # 尝试获取模块状态
        try:
            from core.module_registry import ModuleRegistry
            state['modules'] = ModuleRegistry.get_status()
        except Exception as e:
            # 该段失败会使诊断包缺少 modules 段，导出结果不完整
            logger.warning(f"诊断导出: 收集模块状态失败(诊断包将缺少 modules 段): {e}")

        try:
            from core.health_checker import HealthChecker
            state['health'] = HealthChecker.get_status()
        except Exception as e:
            # 该段失败会使诊断包缺少 health 段，导出结果不完整
            logger.warning(f"诊断导出: 收集健康状态失败(诊断包将缺少 health 段): {e}")

        return state

    def _collect_config(self) -> Dict[str, Any]:
        """收集配置信息"""
        config = {}
        config_dir = Path('配置')
        if config_dir.exists():
            for f in config_dir.glob('*.yaml'):
                try:
                    import yaml
                    with open(f, 'r', encoding='utf-8') as fh:
                        config[f.name] = yaml.safe_load(fh)
                except Exception:
                    config[f.name] = '(读取失败)'

        # 运行时配置
        try:
            from core.ops_tools import runtime_config_manager
            config['runtime'] = runtime_config_manager.get_all()
        except Exception as e:
            # 该段失败会使诊断包缺少运行时配置，导出结果不完整
            logger.warning(f"诊断导出: 收集运行时配置失败(诊断包将缺少 runtime 配置): {e}")

        return config

    def _collect_db_stats(self) -> Dict[str, Any]:
        """收集数据库统计"""
        stats = {}
        data_dir = Path('data')
        if data_dir.exists():
            for db_file in data_dir.glob('*.db'):
                try:
                    stats[db_file.name] = {
                        'size_mb': round(db_file.stat().st_size / 1024 / 1024, 2),
                        'modified': datetime.fromtimestamp(db_file.stat().st_mtime).isoformat(),
                    }
                    # 获取表统计
                    conn = sqlite3.connect(str(db_file), timeout=5)
                    tables = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                    table_stats = {}
                    for (table,) in tables:
                        try:
                            import re as _re
                            if not _re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', table):
                                continue
                            safe_id = '"' + table.replace('"', '""') + '"'
                            sql = 'SELECT COUNT(*) FROM ' + safe_id
                            count = conn.execute(sql).fetchone()[0]
                            table_stats[table] = count
                        except Exception as e:
                            # 预期内：个别表可能损坏或不可读，跳过该表不影响其余统计
                            logger.debug(f"诊断导出: 统计表行数失败(跳过该表): db={db_file.name}, table={table}: {e}")
                    stats[db_file.name]['tables'] = table_stats
                    conn.close()
                except Exception as e:
                    stats[db_file.name] = {'error': str(e)}
        return stats

    def _copy_recent_logs(self, diag_dir: Path):
        """复制最近日志"""
        log_dir = Path('logs')
        if not log_dir.exists():
            return

        out_dir = diag_dir / 'logs'
        out_dir.mkdir(exist_ok=True)

        for log_file in log_dir.glob('*.log*'):
            try:
                # 只复制最近修改的（24小时内）
                if time.time() - log_file.stat().st_mtime < 86400:
                    shutil.copy2(log_file, out_dir / log_file.name)
            except Exception as e:
                # 预期内：单文件复制失败（如文件被占用）不影响其余日志，诊断包将少该文件
                logger.debug(f"诊断导出: 复制日志文件失败(该文件将缺失): {log_file.name}: {e}")


# 全局实例
ops_audit = OpsAuditLogger()
runtime_config_manager = RuntimeConfigManager()
db_maintainer = DatabaseMaintainer()
data_cleaner = DataCleaner()
diagnostic_exporter = DiagnosticExporter()
