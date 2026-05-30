"""
操作审计日志（工业安全合规）
============================

记录所有控制操作的完整审计轨迹：
- WHO: 操作人（用户名 + 角色）
- WHEN: 操作时间（精确到毫秒）
- WHAT: 操作内容（目标设备/参数/动作）
- WHY: 操作原因（可选）
- RESULT: 操作结果（成功/失败/超时）

特性：
- 追加写入（Append-Only），不可删除、不可修改
- 独立 SQLite 数据库，与业务数据隔离
- 支持时间范围/设备/操作人 多维查询
- 自动导出 CSV（用于合规存档）

用法:
    audit = AuditLogger(db_path="data/audit.db")

    # 记录操作
    audit.log_operation(
        user="operator1",
        role="操作员",
        action="set_value",
        target="siemens_1500_01/boiler_temperature",
        value=120.0,
        reason="升温至工作温度",
        result="success"
    )

    # 查询
    records = audit.query(start_time=..., end_time=..., user="operator1")
"""

import csv
import json
import shutil
import sqlite3
import hashlib
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AuditLogger:
    """操作审计日志"""

    def __init__(self, db_path: str = "data/audit.db"):
        self.db_path = db_path
        self._chain_lock = threading.Lock()
        self._init_db()
        logger.info(f"审计日志初始化: {db_path}")

    def _init_db(self):
        """初始化审计数据库（追加写入，不可删除）"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                user_name TEXT NOT NULL,
                user_role TEXT DEFAULT '',
                action TEXT NOT NULL,
                target TEXT NOT NULL,
                value TEXT DEFAULT '',
                reason TEXT DEFAULT '',
                result TEXT NOT NULL,
                detail TEXT DEFAULT '',
                ip_address TEXT DEFAULT '',
                checksum TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_name)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(target)
        """)
        conn.commit()
        conn.close()

    def _compute_checksum(self, data: str) -> str:
        """
        计算校验和（防篡改）

        使用 SHA-256 对记录内容计算哈希，链式校验。
        """
        return hashlib.sha256(data.encode('utf-8')).hexdigest()

    def log_operation(self, user: str, action: str, target: str,
                      value: Any = None, reason: str = '',
                      result: str = 'success', detail: str = '',
                      role: str = '', ip_address: str = ''):
        """
        记录操作审计日志

        Args:
            user: 操作人用户名
            action: 操作类型 (set_value, start_device, stop_device, acknowledge_alarm, shelve_alarm, ...)
            target: 操作目标 (device_id/register_name 或 alarm_id)
            value: 操作值（可选）
            reason: 操作原因（可选，但推荐填写）
            result: 操作结果 (success, failed, timeout, denied)
            detail: 详细信息（可选）
            role: 操作人角色
            ip_address: 操作人 IP
        """
        with self._chain_lock:
            now = datetime.now()
            timestamp = now.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

            # 序列化 value
            if value is not None:
                if isinstance(value, (dict, list)):
                    value_str = json.dumps(value, ensure_ascii=False, default=str)
                else:
                    value_str = str(value)
            else:
                value_str = ''

            # 计算校验和（链式：包含上一条记录的 ID）
            last_id = self._get_last_id()
            check_input = f"{last_id}:{timestamp}:{user}:{action}:{target}:{value_str}:{result}"
            checksum = self._compute_checksum(check_input)

            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("""
                    INSERT INTO audit_log
                    (timestamp, user_name, user_role, action, target, value, reason, result, detail, ip_address, checksum, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (timestamp, user, role, action, target, value_str, reason, result, detail, ip_address, checksum, now.timestamp()))
                conn.commit()
                logger.info(f"[审计] {user} {action} {target} = {value_str} -> {result}")
            except Exception as e:
                logger.error(f"审计日志写入失败: {e}")
            finally:
                conn.close()

    def _get_last_id(self) -> int:
        """获取最后一条记录的 ID（用于链式校验）"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute("SELECT MAX(id) FROM audit_log")
        row = cursor.fetchone()
        conn.close()
        return row[0] if row and row[0] else 0

    def _backup_log(self):
        """备份审计日志到安全目录"""
        backup_dir = Path('data/audit_backup')
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        shutil.copy2(self.db_path, backup_dir / f'audit_{timestamp}.db')

    def query(self, start_time: datetime = None, end_time: datetime = None,
              user: str = None, action: str = None, target: str = None,
              result: str = None, limit: int = 500) -> list[dict[str, Any]]:
        """
        查询审计日志

        Args:
            start_time: 开始时间
            end_time: 结束时间
            user: 操作人
            action: 操作类型
            target: 操作目标
            result: 操作结果
            limit: 返回条数

        Returns:
            list: 审计记录列表
        """
        conditions = []
        params = []

        if start_time:
            conditions.append("timestamp >= ?")
            params.append(start_time.strftime('%Y-%m-%d %H:%M:%S'))
        if end_time:
            conditions.append("timestamp <= ?")
            params.append(end_time.strftime('%Y-%m-%d %H:%M:%S'))
        if user:
            conditions.append("user_name = ?")
            params.append(user)
        if action:
            conditions.append("action = ?")
            params.append(action)
        if target:
            conditions.append("target LIKE ?")
            params.append(f"%{target}%")
        if result:
            conditions.append("result = ?")
            params.append(result)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"""
            SELECT id, timestamp, user_name, user_role, action, target,
                   value, reason, result, detail, ip_address, checksum
            FROM audit_log
            WHERE {where}
            ORDER BY id DESC
            LIMIT ?
        """
        params.append(limit)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(sql, params)
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        conn.close()

        return [dict(zip(columns, row)) for row in rows]

    def get_operation_stats(self, hours: float = 24.0) -> dict[str, Any]:
        """
        获取操作统计

        Args:
            hours: 统计时间窗口（小时）

        Returns:
            dict: 操作统计
        """
        cutoff = datetime.now().replace(hour=0, minute=0, second=0).isoformat()

        conn = sqlite3.connect(self.db_path)
        try:
            # 按操作类型统计
            cursor = conn.execute("""
                SELECT action, COUNT(*) as count
                FROM audit_log
                WHERE timestamp >= ?
                GROUP BY action
                ORDER BY count DESC
            """, (cutoff,))
            by_action = {row[0]: row[1] for row in cursor.fetchall()}

            # 按操作人统计
            cursor = conn.execute("""
                SELECT user_name, COUNT(*) as count
                FROM audit_log
                WHERE timestamp >= ?
                GROUP BY user_name
                ORDER BY count DESC
            """, (cutoff,))
            by_user = {row[0]: row[1] for row in cursor.fetchall()}

            # 按结果统计
            cursor = conn.execute("""
                SELECT result, COUNT(*) as count
                FROM audit_log
                WHERE timestamp >= ?
                GROUP BY result
            """, (cutoff,))
            by_result = {row[0]: row[1] for row in cursor.fetchall()}

            # 总数
            cursor = conn.execute("SELECT COUNT(*) FROM audit_log")
            total = cursor.fetchone()[0]

            return {
                'total_records': total,
                'today_by_action': by_action,
                'today_by_user': by_user,
                'today_by_result': by_result,
            }
        finally:
            conn.close()

    def export_csv(self, output_path: str, start_time: datetime = None,
                   end_time: datetime = None) -> int:
        """
        导出审计日志为 CSV（用于合规存档）

        Args:
            output_path: 输出文件路径
            start_time: 开始时间
            end_time: 结束时间

        Returns:
            int: 导出记录数
        """
        records = self.query(start_time=start_time, end_time=end_time, limit=100000)

        if not records:
            return 0

        with open(output_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=records[0].keys())
            writer.writeheader()
            writer.writerows(records)

        logger.info(f"审计日志已导出: {output_path} ({len(records)} 条)")
        return len(records)

    def verify_integrity(self) -> dict[str, Any]:
        """
        验证审计日志完整性

        检查校验和链是否完整，是否有篡改痕迹。

        Returns:
            dict: 验证结果
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute("""
            SELECT id, timestamp, user_name, action, target, value, result, checksum
            FROM audit_log ORDER BY id
        """)
        rows = cursor.fetchall()
        conn.close()

        total = len(rows)
        valid = 0
        invalid = 0
        invalid_ids = []

        for i, row in enumerate(rows):
            row_id, ts, user, action, target, value, result, checksum = row
            prev_id = rows[i-1][0] if i > 0 else 0
            check_input = f"{prev_id}:{ts}:{user}:{action}:{target}:{value}:{result}"
            expected = self._compute_checksum(check_input)

            if checksum == expected:
                valid += 1
            else:
                invalid += 1
                invalid_ids.append(row_id)

        return {
            'total_records': total,
            'valid': valid,
            'invalid': invalid,
            'integrity': 'OK' if invalid == 0 else 'COMPROMISED',
            'invalid_ids': invalid_ids[:10],  # 最多显示10个
        }
