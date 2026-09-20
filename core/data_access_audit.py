"""
数据访问审计
敏感数据访问日志记录，支持访问模式分析和异常检测。

使用方式:
    from core.data_access_audit import DataAccessAuditor
    auditor = DataAccessAuditor(db)
    auditor.log_access('user_001', 'devices', 'read', sensitive_fields=['password'])
"""

# ============================================================================
# 接线状态：未接线（WIRED = False）
# ============================================================================
# 本模块在生产代码（run.py / 各业务层 / 其它 core 模块）中**没有任何 import 引用**。
# 模块本身可用，但当前没有调用方 —— 也就是说它宣称的这项能力**当前并未生效**。
#
# 为什么保留而不删除：删掉即丢能力，模块本身有测试价值；这里只把「没接线」显式化、
# 可追踪，避免「代码在库里」被误读成「功能在跑」。
#
# 自动化复核（防止本标注过期）：
#   tests/test_core_regressions.py::test_unwired_marker_matches_reality
#   —— 该用例用 AST 扫描全仓库 import。一旦有人把本模块接进生产代码，
#      而这里仍写着 WIRED = False，用例即失败，强制文档与事实同步。
#
# 接线建议（需改 run.py / 各层，core 内部无权自行接线）：
#     在 展示层/api 的数据读取端点调用 `DataAccessAuditor.log_access(...)`（敏感表/字段已在模块内定义）。
# ============================================================================
WIRED = False


import logging
import sqlite3
import time
import threading
from typing import Any, Dict, List, Optional, Set
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _format_record_datetime(record: 'DataAccessRecord') -> Optional[str]:
    """把记录的时间戳格式化为 ISO 字符串（与 to_dict()['datetime'] 同格式）。

    DataAccessRecord 暴露的是 `timestamp`（秒级 float），**没有** `datetime` 属性；
    直接访问 `.datetime` 会 AttributeError。此处集中处理，便于测试锁定契约。
    """
    if record is None:
        return None
    return datetime.fromtimestamp(record.timestamp).isoformat()


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
        'users', 'audit_log', 'alarm_records',
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

        # 持久化失败计数（审计链断裂必须可观测，不能只靠日志刷屏）
        self._persist_failures = 0
        self._last_persist_error: Optional[str] = None

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
            # 审计日志写不进去 = 审计链断裂（合规问题），旧实现只在 DEBUG 级留痕，
            # 生产环境默认 INFO 级别下等于完全静默。必须 WARNING + 计数。
            self._persist_failures += 1
            self._last_persist_error = f"{type(e).__name__}: {e}"
            logger.warning(
                "数据访问审计持久化失败(user=%s, table=%s, op=%s)(累计 %d 次): %s",
                record.user_id, record.table, record.operation,
                self._persist_failures, e,
            )

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
                # 审计链健康度：持久化失败次数 > 0 说明落库链路坏了（合规风险）
                'persistence_enabled': bool(self._db_path),
                'persist_failures': self._persist_failures,
                'last_persist_error': self._last_persist_error,
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

            # 注意：DataAccessRecord 只有 timestamp（float），没有 datetime 属性。
            # 旧实现写 `user_records[0].datetime` → 只要该用户有访问记录就必抛
            # AttributeError，整个访问模式接口 500。这里统一走只读映射函数，
            # 保证与 DataAccessRecord.to_dict()['datetime'] 的格式完全一致。
            return {
                'user_id': user_id,
                'total_accesses': len(user_records),
                'sensitive_accesses': sensitive_count,
                'by_table': table_counts,
                'by_operation': op_counts,
                'first_access': _format_record_datetime(user_records[0]),
                'last_access': _format_record_datetime(user_records[-1]),
            }

    def reset_stats(self):
        """重置统计"""
        with self._lock:
            self._access_counts.clear()
            self._sensitive_access_counts.clear()


# 全局实例
data_access_auditor = DataAccessAuditor()
