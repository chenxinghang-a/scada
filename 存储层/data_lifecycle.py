"""
数据生命周期管理模块
管理数据的创建、存储、归档、删除全流程

功能：
- 数据保留策略
- 自动归档
- 数据清理
- 生命周期报告

**接线状态：未接线（unwired）** —— 见 ``WIRED``。
2026-09 审计确认本模块在生产链路里**没有任何调用方**（只有测试引用），
实际的保留/归档由 ``Database.archive_old_data`` 经 ``core/maintenance.py``
的 ``data_archive`` 任务执行。因此这里只保证"行为正确且被测试固化"，
不声称已被使用；真要接入，请把 ``WIRED`` 改为 True 并补上调用方与监控。
"""

import time
import logging
import threading
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from pathlib import Path

from .database import ARCHIVE_TABLE

logger = logging.getLogger(__name__)

#: 是否已接入生产链路。False = 当前无人调用（仅有测试覆盖）。
#: 接入后请改成 True，并同步更新模块 docstring 与 tests/test_storage_regressions.py。
WIRED = False


class RetentionPolicy:
    """数据保留策略"""

    def __init__(self, name: str, table: str, retention_days: int,
                 archive_enabled: bool = True, archive_days: int = 7,
                 archive_table: Optional[str] = None):
        self.name = name
        self.table = table
        self.retention_days = retention_days
        self.archive_enabled = archive_enabled
        self.archive_days = archive_days
        #: 归档去向表名。None 表示由管理器推导（``{table}_archive``，
        #: ``history_data`` 例外，映射到全局唯一的 ``ARCHIVE_TABLE``）。
        self.archive_table = archive_table

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'table': self.table,
            'retention_days': self.retention_days,
            'archive_enabled': self.archive_enabled,
            'archive_days': self.archive_days,
            'archive_table': self.archive_table,
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
                retention_days=90, archive_enabled=True, archive_days=7,
                # 归档表必须是全局唯一那张（聚合结构），不能是 {table}_archive
                archive_table=ARCHIVE_TABLE,
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

    def execute_lifecycle(self, database=None) -> Dict[str, Any]:
        """执行生命周期管理

        Args:
            database: Database实例（可选，用于复用连接池）
        """
        results = {
            'timestamp': datetime.now().isoformat(),
            'actions': [],
        }

        try:
            if database:
                with database.get_connection() as conn:
                    for table, policy in self.policies.items():
                        try:
                            result = self._process_table(conn, table, policy)
                            results['actions'].append(result)
                        except Exception as e:
                            results['actions'].append({
                                'table': table,
                                'error': str(e),
                            })
            else:
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

    def resolve_archive_table(self, table: str) -> str:
        """推导某张表的归档去向表名。

        规则（顺序即优先级）：
        1. 策略里显式配置的 ``archive_table``；
        2. ``history_data`` -> ``ARCHIVE_TABLE``（全局唯一归档表，按天聚合结构）；
        3. 其余表沿用 ``{table}_archive``（这些表当前都没有归档表，不会命中）。

        2026-09 审计前这里无条件用 ``f"{table}_archive"``，于是
        ``history_data`` 的归档落在 ``history_data_archive``，
        和 ``Database.archive_old_data`` 写的 ``history_archive`` 不是一张表
        （而且它 ``SELECT *`` 的整行复制跟聚合结构也不兼容，
        真执行必报列数不符）—— 同一份归档散落三处。现在统一到唯一真源。
        """
        policy = self.policies.get(table)
        explicit = getattr(policy, 'archive_table', None)
        if explicit:
            return explicit
        if table == 'history_data':
            return ARCHIVE_TABLE
        return f"{table}_archive"

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
            archive_table = self.resolve_archive_table(table)
            result['archive_table'] = archive_table

            # 检查归档表是否存在
            cursor.execute("""
                SELECT name FROM sqlite_master
                WHERE type='table' AND name=?
            """, (archive_table,))
            archive_exists = cursor.fetchone() is not None

            if archive_exists:
                if archive_table == ARCHIVE_TABLE:
                    # 规范归档表存的是**按天聚合**值（与 Database.archive_old_data
                    # 同结构），不能整行复制，必须聚合后写入。
                    cursor.execute(f"""
                        INSERT OR IGNORE INTO {ARCHIVE_TABLE}
                            (device_id, register_name, avg_value, min_value,
                             max_value, sample_count, archive_date)
                        SELECT
                            device_id, register_name,
                            AVG(value), MIN(value), MAX(value), COUNT(*),
                            DATE(timestamp)
                        FROM {table}
                        WHERE timestamp < ?
                        GROUP BY device_id, register_name, DATE(timestamp)
                    """, (archive_cutoff.isoformat(sep=' '),))
                else:
                    # 通用表：整行复制，靠 id 去重（要求归档表与源表结构一致）
                    cursor.execute(f"""
                        INSERT INTO {archive_table}
                        SELECT * FROM {table}
                        WHERE timestamp < ?
                        AND id NOT IN (SELECT id FROM {archive_table})
                    """, (archive_cutoff.isoformat(sep=' '),))
                archived_count = cursor.rowcount
                result['archived_count'] = archived_count

        # 3. 删除过期数据
        delete_cutoff = datetime.now() - timedelta(days=policy.retention_days)
        cursor.execute(f"""
            DELETE FROM {table}
            WHERE timestamp < ?
        """, (delete_cutoff.isoformat(sep=' '),))
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
                except Exception as e:
                    # 查不到 ≠ 0 行。原先报 {'count': 0} 有两个问题：
                    #   1) 把「查询失败」显示成「这张表是空的」—— 生命周期报告
                    #      读起来像"没数据要清理"，实际是查不动；
                    #   2) 结构不一致：正常分支有 earliest/latest，这里没有，
                    #      任何按正常结构读报告的地方都会 KeyError。
                    table_stats[table] = {
                        'count': None,
                        'earliest': None,
                        'latest': None,
                        'error': str(e),
                    }

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
