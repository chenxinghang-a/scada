"""
数据一致性检查模�?检查数据库中数据的一致性和完整�?
功能�?- 外键一致性检�?- 数据格式验证
- 重复数据检�?- 一致性报�?"""

import time
import logging
import sqlite3
from datetime import datetime
from typing import Dict, List, Any, Optional
from collections import defaultdict

logger = logging.getLogger(__name__)


class ConsistencyChecker:
    """数据一致性检查器"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.checks: List[Dict[str, Any]] = []

    def run_all_checks(self) -> Dict[str, Any]:
        """运行所有一致性检�?""
        results = {
            'timestamp': datetime.now().isoformat(),
            'checks': [],
            'total_issues': 0,
            'status': 'healthy',
        }

        try:
            conn = sqlite3.connect(self.db_path, timeout=10)

            # 1. 检查表结构完整�?            results['checks'].append(self._check_table_integrity(conn))

            # 2. 检查数据格�?            results['checks'].append(self._check_data_formats(conn))

            # 3. 检查重复数�?            results['checks'].append(self._check_duplicates(conn))

            # 4. 检查孤立记�?            results['checks'].append(self._check_orphaned_records(conn))

            # 5. 检查时间戳一致�?            results['checks'].append(self._check_timestamps(conn))

            conn.close()

            # 统计问题总数
            for check in results['checks']:
                results['total_issues'] += check.get('issue_count', 0)

            if results['total_issues'] > 0:
                results['status'] = 'issues_found'

        except Exception as e:
            results['status'] = 'error'
            results['error'] = str(e)

        return results

    def _check_table_integrity(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """检查表结构完整�?""
        cursor = conn.cursor()
        issues = []

        # 获取所有表
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]

        expected_tables = ['realtime_data', 'history_data', 'alarm_records', 'users']

        for table in expected_tables:
            if table not in tables:
                issues.append(f"缺少必要的表: {table}")

        # 检查每个表的列
        for table in tables:
            try:
                cursor.execute(f"PRAGMA table_info({table})")
                columns = [row[1] for row in cursor.fetchall()]

                if 'id' not in columns and table not in ['sqlite_sequence']:
                    issues.append(f"�?{table} 缺少 id �?)

            except Exception as e:
                issues.append(f"检查表 {table} 结构失败: {e}")

        return {
            'check_name': 'table_integrity',
            'status': 'pass' if not issues else 'fail',
            'issue_count': len(issues),
            'issues': issues,
        }

    def _check_data_formats(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """检查数据格�?""
        cursor = conn.cursor()
        issues = []

        # 检�?realtime_data 的值格�?        try:
            cursor.execute("""
                SELECT COUNT(*) FROM realtime_data
                WHERE value IS NOT NULL AND typeof(value) != 'real' AND typeof(value) != 'integer'
            """)
            invalid_values = cursor.fetchone()[0]
            if invalid_values > 0:
                issues.append(f"realtime_data 中有 {invalid_values} 条无效数�?)
        except Exception:
            pass

        # 检�?history_data 的时间戳格式
        try:
            cursor.execute("""
                SELECT COUNT(*) FROM history_data
                WHERE timestamp IS NULL
            """)
            null_timestamps = cursor.fetchone()[0]
            if null_timestamps > 0:
                issues.append(f"history_data 中有 {null_timestamps} 条空时间�?)
        except Exception:
            pass

        return {
            'check_name': 'data_formats',
            'status': 'pass' if not issues else 'fail',
            'issue_count': len(issues),
            'issues': issues,
        }

    def _check_duplicates(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """检查重复数�?""
        cursor = conn.cursor()
        issues = []

        # 检�?realtime_data 的重复（device_id + register_name 应该唯一�?        try:
            cursor.execute("""
                SELECT device_id, register_name, COUNT(*) as cnt
                FROM realtime_data
                GROUP BY device_id, register_name
                HAVING cnt > 1
            """)
            duplicates = cursor.fetchall()
            if duplicates:
                issues.append(f"realtime_data 中有 {len(duplicates)} 组重复数�?)
        except Exception:
            pass

        # 检�?users 的重复用户名
        try:
            cursor.execute("""
                SELECT username, COUNT(*) as cnt
                FROM users
                GROUP BY username
                HAVING cnt > 1
            """)
            duplicates = cursor.fetchall()
            if duplicates:
                issues.append(f"users 表中�?{len(duplicates)} 个重复用户名")
        except Exception:
            pass

        return {
            'check_name': 'duplicates',
            'status': 'pass' if not issues else 'fail',
            'issue_count': len(issues),
            'issues': issues,
        }

    def _check_orphaned_records(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """检查孤立记�?""
        cursor = conn.cursor()
        issues = []

        # 检�?alarm_records 中的孤立记录（引用不存在的设备）
        try:
            cursor.execute("""
                SELECT COUNT(*) FROM alarm_records
                WHERE device_id NOT IN (SELECT DISTINCT device_id FROM realtime_data)
            """)
            orphaned = cursor.fetchone()[0]
            if orphaned > 0:
                issues.append(f"alarm_records 中有 {orphaned} 条孤立记录（设备不存在）")
        except Exception:
            pass

        return {
            'check_name': 'orphaned_records',
            'status': 'pass' if not issues else 'fail',
            'issue_count': len(issues),
            'issues': issues,
        }

    def _check_timestamps(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """检查时间戳一致�?""
        cursor = conn.cursor()
        issues = []

        # 检查未来时间戳
        try:
            now = datetime.now().isoformat()
            cursor.execute(f"""
                SELECT COUNT(*) FROM history_data
                WHERE timestamp > ?
            """, (now,))
            future_count = cursor.fetchone()[0]
            if future_count > 0:
                issues.append(f"history_data 中有 {future_count} 条未来时间戳")
        except Exception:
            pass

        # 检查过旧时间戳
        try:
            old = '2020-01-01'
            cursor.execute(f"""
                SELECT COUNT(*) FROM history_data
                WHERE timestamp < ?
            """, (old,))
            old_count = cursor.fetchone()[0]
            if old_count > 0:
                issues.append(f"history_data 中有 {old_count} 条过旧时间戳�?2020�?)
        except Exception:
            pass

        return {
            'check_name': 'timestamps',
            'status': 'pass' if not issues else 'fail',
            'issue_count': len(issues),
            'issues': issues,
        }

    def generate_report(self) -> Dict[str, Any]:
        """生成一致性报�?""
        results = self.run_all_checks()

        report = {
            'timestamp': results['timestamp'],
            'summary': {
                'status': results['status'],
                'total_issues': results['total_issues'],
                'checks_passed': sum(1 for c in results['checks'] if c['status'] == 'pass'),
                'checks_failed': sum(1 for c in results['checks'] if c['status'] == 'fail'),
            },
            'details': results['checks'],
        }

        return report
