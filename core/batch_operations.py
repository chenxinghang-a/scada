"""
批量操作支持
批量创建/更新/删除，支持事务和回滚。

使用方式:
    from core.batch_operations import BatchOperator
    operator = BatchOperator(db)
    result = operator.batch_insert('devices', records)
"""

import logging
import sqlite3
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class BatchResult:
    """批量操作结果"""

    def __init__(self):
        self.success_count = 0
        self.error_count = 0
        self.errors: List[Dict[str, Any]] = []
        self.ids: List[int] = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            'success_count': self.success_count,
            'error_count': self.error_count,
            'total': self.success_count + self.error_count,
            'errors': self.errors[:10],  # 限制返回数量
            'ids': self.ids,
        }


class BatchOperator:
    """批量操作器"""

    def __init__(self, db_path: str, batch_size: int = 100):
        self.db_path = db_path
        self.batch_size = batch_size

    def batch_insert(
        self,
        table: str,
        records: List[Dict[str, Any]],
        on_conflict: str = 'error',
    ) -> Dict[str, Any]:
        """
        批量插入

        Args:
            table: 表名
            records: 记录列表
            on_conflict: 冲突处理（error/ignore/replace）

        Returns:
            操作结果
        """
        if not records:
            return {'success_count': 0, 'error_count': 0, 'message': '没有记录'}

        # 安全校验表名
        if not table.isalnum() and '_' not in table:
            raise ValueError(f"无效的表名: {table}")

        result = BatchResult()
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row

        try:
            # 获取列名（从第一条记录）
            columns = list(records[0].keys())
            cols_str = ', '.join(f'"{c}"' for c in columns)
            placeholders = ', '.join(['?' for _ in columns])

            # 构建SQL
            if on_conflict == 'ignore':
                sql = f'INSERT OR IGNORE INTO "{table}" ({cols_str}) VALUES ({placeholders})'
            elif on_conflict == 'replace':
                sql = f'INSERT OR REPLACE INTO "{table}" ({cols_str}) VALUES ({placeholders})'
            else:
                sql = f'INSERT INTO "{table}" ({cols_str}) VALUES ({placeholders})'

            # 分批执行
            for i in range(0, len(records), self.batch_size):
                batch = records[i:i + self.batch_size]

                for record in batch:
                    try:
                        values = [record.get(col) for col in columns]
                        cursor = conn.execute(sql, values)
                        if cursor.lastrowid:
                            result.ids.append(cursor.lastrowid)
                        result.success_count += 1
                    except Exception as e:
                        result.error_count += 1
                        result.errors.append({
                            'record': record,
                            'error': str(e),
                        })

                conn.commit()

            return result.to_dict()

        except Exception as e:
            conn.rollback()
            raise
        finally:
            conn.close()

    def batch_update(
        self,
        table: str,
        updates: List[Dict[str, Any]],
        key_column: str = 'id',
    ) -> Dict[str, Any]:
        """
        批量更新

        Args:
            table: 表名
            updates: 更新列表（每条必须包含key_column）
            key_column: 键列名

        Returns:
            操作结果
        """
        if not updates:
            return {'success_count': 0, 'error_count': 0, 'message': '没有记录'}

        if not table.isalnum() and '_' not in table:
            raise ValueError(f"无效的表名: {table}")

        result = BatchResult()
        conn = sqlite3.connect(self.db_path, timeout=30)

        try:
            # 获取更新列（排除键列）
            update_columns = [c for c in updates[0].keys() if c != key_column]

            for i in range(0, len(updates), self.batch_size):
                batch = updates[i:i + self.batch_size]

                for record in batch:
                    try:
                        key_value = record.get(key_column)
                        if key_value is None:
                            result.error_count += 1
                            result.errors.append({
                                'record': record,
                                'error': f'缺少键列: {key_column}',
                            })
                            continue

                        # 构建SET子句
                        set_clauses = []
                        params = []
                        for col in update_columns:
                            if col in record:
                                set_clauses.append(f'"{col}" = ?')
                                params.append(record[col])

                        if not set_clauses:
                            continue

                        params.append(key_value)
                        sql = f'UPDATE "{table}" SET {", ".join(set_clauses)} WHERE "{key_column}" = ?'
                        cursor = conn.execute(sql, params)

                        if cursor.rowcount > 0:
                            result.success_count += 1
                        else:
                            result.error_count += 1
                            result.errors.append({
                                'record': record,
                                'error': '记录不存在',
                            })

                    except Exception as e:
                        result.error_count += 1
                        result.errors.append({
                            'record': record,
                            'error': str(e),
                        })

                conn.commit()

            return result.to_dict()

        except Exception as e:
            conn.rollback()
            raise
        finally:
            conn.close()

    def batch_delete(
        self,
        table: str,
        ids: List[int],
        key_column: str = 'id',
    ) -> Dict[str, Any]:
        """
        批量删除

        Args:
            table: 表名
            ids: ID列表
            key_column: 键列名

        Returns:
            操作结果
        """
        if not ids:
            return {'success_count': 0, 'error_count': 0, 'message': '没有记录'}

        if not table.isalnum() and '_' not in table:
            raise ValueError(f"无效的表名: {table}")

        result = BatchResult()
        conn = sqlite3.connect(self.db_path, timeout=30)

        try:
            # 分批删除
            for i in range(0, len(ids), self.batch_size):
                batch = ids[i:i + self.batch_size]
                placeholders = ','.join(['?' for _ in batch])
                sql = f'DELETE FROM "{table}" WHERE "{key_column}" IN ({placeholders})'

                cursor = conn.execute(sql, batch)
                result.success_count += cursor.rowcount

            conn.commit()
            return result.to_dict()

        except Exception as e:
            conn.rollback()
            raise
        finally:
            conn.close()

    def batch_upsert(
        self,
        table: str,
        records: List[Dict[str, Any]],
        conflict_columns: List[str],
    ) -> Dict[str, Any]:
        """
        批量插入或更新（UPSERT）

        Args:
            table: 表名
            records: 记录列表
            conflict_columns: 冲突检测列

        Returns:
            操作结果
        """
        if not records:
            return {'success_count': 0, 'error_count': 0, 'message': '没有记录'}

        result = BatchResult()
        conn = sqlite3.connect(self.db_path, timeout=30)

        try:
            columns = list(records[0].keys())
            cols_str = ', '.join(f'"{c}"' for c in columns)
            placeholders = ', '.join(['?' for _ in columns])

            # 构建ON CONFLICT子句
            conflict_cols = ', '.join(f'"{c}"' for c in conflict_columns)
            update_cols = [c for c in columns if c not in conflict_columns]
            update_str = ', '.join(f'"{c}" = excluded."{c}"' for c in update_cols)

            sql = f'''
                INSERT INTO "{table}" ({cols_str})
                VALUES ({placeholders})
                ON CONFLICT({conflict_cols})
                DO UPDATE SET {update_str}
            '''

            for i in range(0, len(records), self.batch_size):
                batch = records[i:i + self.batch_size]

                for record in batch:
                    try:
                        values = [record.get(col) for col in columns]
                        conn.execute(sql, values)
                        result.success_count += 1
                    except Exception as e:
                        result.error_count += 1
                        result.errors.append({
                            'record': record,
                            'error': str(e),
                        })

                conn.commit()

            return result.to_dict()

        except Exception as e:
            conn.rollback()
            raise
        finally:
            conn.close()


def create_batch_response(result: Dict[str, Any]) -> Dict[str, Any]:
    """创建批量操作响应"""
    return {
        'success': True,
        'batch_result': result,
    }
