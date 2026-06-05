"""
数据访问审计
敏感数据访问日志记录，支持访问模式分析和异常检测。

使用方式:
    from core.data_access_audit import DataAccessAuditor
    auditor = DataAccessAuditor(db)
    auditor.log_access('user_001', 'devices', 'read', sensitive_fields=['password'])
"""

import logging
import sqlite3
import time
import threading
from typing import Any, Dict, List, Optional, Set
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class DataAccessRecord:
    """数据访问记录"""

    def __init__(
        self,
        user_id: str,
        table: str,
        operation: str,
        record_id: Optional[str] = None,
        sensitive_fields: List[str] = None,
        ip_address: str = None,
        user_agent: str = None,
    ):
        self.user_id = user_id
        self.table = table
        self.operation = operation
        self.record_id = record_id
        self.sensitive_fields = sensitive_fields or []
        self.ip_address = ip_address
        self.user_agent = user_agent
        self.timestamp = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            'user_id': self.user_id,
            'table': self.table,
            'operation': self.operation,
            'record_id': self.record_id,
            'sensitive_fields': self.sensitive_fields,
            'ip_address': self.ip_address,
            'user_agent': self.user_agent,
            'timestamp': self.timestamp,
            'datetime': datetime.fromtimestamp(self.timestamp).isoformat(),
        }


class DataAccessAuditor:
    """数据访问审计器"""

    # 敏感表定义
    SENSITIVE_TABLES = {
        'users', 'audit_logs', 'alarm_records',
        'device_config', 'system_config',
    }

    # 敏感字段模式
    SENSITIVE_FIELD_PATTERNS = {
        'password', 'secret', 'token', 'key', 'credential',
        'ssn', 'credit_card', 'phone', 'email', 'address',
    }

    def __init__(self, db_path: str = None):
        self._records: List[DataAccessRecord] = []
        self._lock = threading.Lock()
        self._max_records = 10000
        self._db_path = db_path

        # 访问统计
        self._access_counts: Dict[str, Dict[str, int]] = {}  # user -> {table: count}
        self._sensitive_access_counts: Dict[str, int] = {}  # user -> count

        # 异常检测阈值
        self._high_volume_threshold = 100  # 单用户单表每小时最大访问次数
        self._sensitive_threshold = 50  # 单用户每小时敏感数据访问次数

    def log_access(
        self,
        user_id: str,
        table: str,
        operation: str,
        record_id: Optional[str] = None,
        sensitive_fields: List[str] = None,
        ip_address: str = None,
        user_agent: str = None,
    ):
        """记录数据访问"""
        record = DataAccessRecord(
            user_id=user_id,
            table=table,
            operation=operation,
            record_id=record_id,
            sensitive_fields=sensitive_fields,
            ip_address=ip_address,
            user_agent=user_agent,
        )

        with self._lock:
            self._records.append(record)

            # 限制记录数量
            if len(self._records) > self._max_records:
                self._records = self._records[-self._max_records:]

            # 更新统计
            if user_id not in self._access_counts:
                self._access_counts[user_id] = {}
            self._access_counts[user_id][table] = self._access_counts[user_id].get(table, 0) + 1

            # 敏感数据访问统计
            if sensitive_fields or table in self.SENSITIVE_TABLES:
                self._sensitive_access_counts[user_id] = self._sensitive_access_counts.get(user_id, 0) + 1

        # 检查异常
        self._check_anomalies(record)

        # 持久化到数据库
        if self._db_path:
            self._persist_record(record)

    def _check_anomalies(self, record: DataAccessRecord):
        """检查访问异常"""
        with self._lock:
            # 高频访问检测
            user_table_count = self._access_counts.get(record.user_id, {}).get(record.table, 0)
            if user_table_count > self._high_volume_threshold:
                logger.warning(
                    f"高频数据访问: user={record.user_id}, table={record.table}, "
                    f"count={user_table_count}"
                )

            # 敏感数据大量访问检测
            sensitive_count = self._sensitive_access_counts.get(record.user_id, 0)
            if sensitive_count > self._sensitive_threshold:
                logger.warning(
                    f"敏感数据大量访问: user={record.user_id}, count={sensitive_count}"
                )

    def _persist_record(self, record: DataAccessRecord):
        """持久化记录到数据库"""
        try:
            conn = sqlite3.connect(self._db_path, timeout=5)
            conn.execute(
                '''INSERT INTO data_access_log
                   (user_id, table_name, operation, record_id, sensitive_fields,
                    ip_address, user_agent, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (
                    record.user_id,
                    record.table,
                    record.operation,
                    record.record_id,
                    ','.join(record.sensitive_fields),
                    record.ip_address,
                    record.user_agent,
                    record.timestamp,
                )
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.debug(f"数据访问日志持久化失败: {e}")

    def get_recent_access(
        self,
        user_id: Optional[str] = None,
        table: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """获取最近的访问记录"""
        with self._lock:
            records = self._records

            if user_id:
                records = [r for r in records if r.user_id == user_id]
            if table:
                records = [r for r in records if r.table == table]

            return [r.to_dict() for r in records[-limit:]]

    def get_sensitive_access_summary(self) -> Dict[str, Any]:
        """获取敏感数据访问摘要"""
        with self._lock:
            return {
                'total_sensitive_accesses': sum(self._sensitive_access_counts.values()),
                'by_user': dict(self._sensitive_access_counts),
                'sensitive_tables': list(self.SENSITIVE_TABLES),
            }

    def get_access_stats(self) -> Dict[str, Any]:
        """获取访问统计"""
        with self._lock:
            return {
                'total_records': len(self._records),
                'unique_users': len(self._access_counts),
                'sensitive_users': len(self._sensitive_access_counts),
                'high_volume_threshold': self._high_volume_threshold,
                'sensitive_threshold': self._sensitive_threshold,
            }

    def get_user_access_pattern(self, user_id: str) -> Dict[str, Any]:
        """获取用户访问模式"""
        with self._lock:
            user_records = [r for r in self._records if r.user_id == user_id]

            if not user_records:
                return {'user_id': user_id, 'total_accesses': 0}

            # 按表统计
            table_counts = {}
            for r in user_records:
                table_counts[r.table] = table_counts.get(r.table, 0) + 1

            # 按操作统计
            op_counts = {}
            for r in user_records:
                op_counts[r.operation] = op_counts.get(r.operation, 0) + 1

            # 敏感访问
            sensitive_count = sum(1 for r in user_records if r.sensitive_fields or r.table in self.SENSITIVE_TABLES)

            return {
                'user_id': user_id,
                'total_accesses': len(user_records),
                'sensitive_accesses': sensitive_count,
                'by_table': table_counts,
                'by_operation': op_counts,
                'first_access': user_records[0].datetime if user_records else None,
                'last_access': user_records[-1].datetime if user_records else None,
            }

    def reset_stats(self):
        """重置统计"""
        with self._lock:
            self._access_counts.clear()
            self._sensitive_access_counts.clear()


# 全局实例
data_access_auditor = DataAccessAuditor()
