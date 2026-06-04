"""
数据生命周期管理模块
管理数据的创建、存储、归档、删除全流程

功能：
- 数据保留策略
- 自动归档
- 数据清理
- 生命周期报告
"""

import time
import logging
import threading
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class RetentionPolicy:
    """数据保留策略"""

    def __init__(self, name: str, table: str, retention_days: int,
                 archive_enabled: bool = True, archive_days: int = 7):
        self.name = name
        self.table = table
        self.retention_days = retention_days
        self.archive_enabled = archive_enabled
        self.archive_days = archive_days

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'table': self.table,
            'retention_days': self.retention_days,
            'archive_enabled': self.archive_enabled,
            'archive_days': self.archive_days,
        }


class DataLifecycleManager:
    """数据生命周期管理器"""

    def __init__(self, db_path: str, config: Dict[str, Any] = None):
        self.db_path = db_path
        self.config = config or {}
        self._lock = threading.Lock()

        # 默认保留策略
        self.policies: Dict[str, RetentionPolicy] = {
            'history_data': RetentionPolicy(
                '历史数据', 'history_data',
                retention_days=90, archive_enabled=True, archive_days=7
            ),
            'alarm_records': RetentionPolicy(
                '报警记录', 'alarm_records',
                retention_days=180, archive_enabled=True, archive_days=30
            ),
            'audit_log': RetentionPolicy(
                '审计日志', 'audit_log',
                retention_days=365, archive_enabled=False
            ),
        }

        # 生命周期统计
        self.stats: Dict[str, Dict[str, Any]] = {}

    def get_policy(self, table: str) -> Optional[RetentionPolicy]:
        """获取表的保留策略"""
        return self.policies.get(table)

    def set_policy(self, policy: RetentionPolicy):
        """设置保留策略"""
        with self._lock:
            self.policies[policy.table] = policy

    def execute_lifecycle(self) -> Dict[str, Any]:
        """执行生命周期管理"""
        results = {
            'timestamp': datetime.now().isoformat(),
            'actions': [],
        }

        try:
            conn = sqlite3.connect(self.db_path, timeout=30)

            for table, policy in self.policies.items():
                try:
                    result = self._process_table(conn, table, policy)
                    results['actions'].append(result)
                except Exception as e:
                    results['actions'].append({
                        'table': table,
                        'error': str(e),
                    })

            conn.close()

        except Exception as e:
            results['error'] = str(e)

        return results

    def _process_table(self, conn: sqlite3.Connection, table: str,
                      policy: RetentionPolicy) -> Dict[str, Any]:
        """处理单个表的生命周期"""
        result = {
            'table': table,
            'policy': policy.to_dict(),
        }

        cursor = conn.cursor()

        # 1. 统计当前数据量
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        total_count = cursor.fetchone()[0]
        result['total_records'] = total_count

        # 2. 归档旧数据
        if policy.archive_enabled:
            archive_cutoff = datetime.now() - timedelta(days=policy.archive_days)
            archive_table = f"{table}_archive"

            # 检查归档表是否存在
            cursor.execute(f"""
                SELECT name FROM sqlite_master
                WHERE type='table' AND name='{archive_table}'
            """)
            archive_exists = cursor.fetchone() is not None

            if archive_exists:
                # 归档数据
                cursor.execute(f"""
                    INSERT INTO {archive_table}
                    SELECT * FROM {table}
                    WHERE timestamp < ?
                    AND id NOT IN (SELECT id FROM {archive_table})
                """, (archive_cutoff.isoformat(),))
                archived_count = cursor.rowcount
                result['archived_count'] = archived_count

        # 3. 删除过期数据
        delete_cutoff = datetime.now() - timedelta(days=policy.retention_days)
        cursor.execute(f"""
            DELETE FROM {table}
            WHERE timestamp < ?
        """, (delete_cutoff.isoformat(),))
        deleted_count = cursor.rowcount
        result['deleted_count'] = deleted_count

        conn.commit()

        # 4. 更新统计
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        remaining_count = cursor.fetchone()[0]
        result['remaining_records'] = remaining_count

        with self._lock:
            self.stats[table] = {
                'total': total_count,
                'deleted': deleted_count,
                'remaining': remaining_count,
                'last_run': datetime.now().isoformat(),
            }

        return result

    def get_statistics(self) -> Dict[str, Any]:
        """获取生命周期统计"""
        with self._lock:
            return dict(self.stats)

    def generate_report(self) -> Dict[str, Any]:
        """生成生命周期报告"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=5)
            cursor = conn.cursor()

            table_stats = {}
            for table in self.policies.keys():
                try:
                    cursor.execute(f"SELECT COUNT(*) FROM {table}")
                    count = cursor.fetchone()[0]

                    # 获取最早和最新记录
                    cursor.execute(f"SELECT MIN(timestamp), MAX(timestamp) FROM {table}")
                    min_ts, max_ts = cursor.fetchone()

                    table_stats[table] = {
                        'count': count,
                        'earliest': min_ts,
                        'latest': max_ts,
                    }
                except:
                    table_stats[table] = {'count': 0}

            conn.close()

            return {
                'timestamp': datetime.now().isoformat(),
                'policies': {k: v.to_dict() for k, v in self.policies.items()},
                'table_stats': table_stats,
                'execution_stats': self.get_statistics(),
            }

        except Exception as e:
            return {
                'timestamp': datetime.now().isoformat(),
                'error': str(e),
            }
