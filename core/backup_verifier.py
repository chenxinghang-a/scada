"""
数据库备份验证增强
备份完整性检查+恢复测试+验证报告。

使用方式:
    from core.backup_verifier import BackupVerifier
    verifier = BackupVerifier(db_path, backup_dir)
    result = verifier.verify_backup('backup.db')
"""

import os
import time
import shutil
import sqlite3
import hashlib
import logging
import tempfile
from typing import Any, Dict, List, Optional
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class BackupVerifier:
    """备份验证器"""

    def __init__(self, db_path: str, backup_dir: str = 'backups'):
        self.db_path = db_path
        self.backup_dir = Path(backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def verify_backup(self, backup_path: str) -> Dict[str, Any]:
        """
        验证备份文件完整性

        Args:
            backup_path: 备份文件路径

        Returns:
            验证结果
        """
        result = {
            'backup_path': backup_path,
            'verified_at': datetime.now().isoformat(),
            'checks': {},
            'overall_status': 'unknown',
        }

        try:
            # 1. 文件存在性检查
            if not os.path.exists(backup_path):
                result['checks']['file_exists'] = {'status': 'fail', 'message': '备份文件不存在'}
                result['overall_status'] = 'fail'
                return result

            result['checks']['file_exists'] = {'status': 'pass'}

            # 2. 文件大小检查
            file_size = os.path.getsize(backup_path)
            result['checks']['file_size'] = {
                'status': 'pass' if file_size > 0 else 'fail',
                'size_mb': round(file_size / 1024 / 1024, 2),
            }

            # 3. SQLite完整性检查
            integrity_result = self._check_integrity(backup_path)
            result['checks']['integrity'] = integrity_result

            # 4. 表结构检查
            schema_result = self._check_schema(backup_path)
            result['checks']['schema'] = schema_result

            # 5. 数据一致性检查
            consistency_result = self._check_consistency(backup_path)
            result['checks']['consistency'] = consistency_result

            # 6. 文件哈希
            file_hash = self._compute_hash(backup_path)
            result['file_hash'] = file_hash

            # 计算总体状态
            statuses = [c['status'] for c in result['checks'].values()]
            if all(s == 'pass' for s in statuses):
                result['overall_status'] = 'pass'
            elif any(s == 'fail' for s in statuses):
                result['overall_status'] = 'fail'
            else:
                result['overall_status'] = 'warning'

        except Exception as e:
            result['overall_status'] = 'error'
            result['error'] = str(e)
            logger.error(f"备份验证失败: {e}")

        return result

    def _check_integrity(self, backup_path: str) -> Dict[str, Any]:
        """SQLite完整性检查"""
        try:
            conn = sqlite3.connect(backup_path, timeout=10)
            cursor = conn.execute("PRAGMA integrity_check")
            result = cursor.fetchone()[0]
            conn.close()

            return {
                'status': 'pass' if result == 'ok' else 'fail',
                'result': result,
            }
        except Exception as e:
            return {'status': 'fail', 'error': str(e)}

    def _check_schema(self, backup_path: str) -> Dict[str, Any]:
        """表结构检查"""
        try:
            conn = sqlite3.connect(backup_path, timeout=10)
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]
            conn.close()

            # 检查关键表是否存在
            required_tables = ['devices', 'history_data', 'alarm_records']
            missing = [t for t in required_tables if t not in tables]

            return {
                'status': 'pass' if not missing else 'warning',
                'tables': tables,
                'missing_required': missing,
            }
        except Exception as e:
            return {'status': 'fail', 'error': str(e)}

    def _check_consistency(self, backup_path: str) -> Dict[str, Any]:
        """数据一致性检查"""
        try:
            conn = sqlite3.connect(backup_path, timeout=10)

            # 检查外键约束
            cursor = conn.execute("PRAGMA foreign_key_check")
            fk_errors = cursor.fetchall()

            # 检查索引完整性
            cursor = conn.execute("PRAGMA quick_check")
            quick_check = cursor.fetchone()[0]

            conn.close()

            return {
                'status': 'pass' if not fk_errors and quick_check == 'ok' else 'fail',
                'foreign_key_errors': len(fk_errors),
                'quick_check': quick_check,
            }
        except Exception as e:
            return {'status': 'fail', 'error': str(e)}

    def _compute_hash(self, file_path: str) -> str:
        """计算文件SHA256哈希"""
        sha256 = hashlib.sha256()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                sha256.update(chunk)
        return sha256.hexdigest()

    @staticmethod
    def _count_table_rows(conn: sqlite3.Connection, table_name: str) -> int:
        """
        安全统计表行数（表名已验证来自sqlite_master）

        SECURITY: 表名通过sqlite_master系统表获取（可信源），
        使用quote()函数安全引用标识符防止SQL注入。
        """
        quoted = conn.execute("SELECT quote(?)", (table_name,)).fetchone()[0]
        sql = "SELECT COUNT(*) FROM " + quoted
        cursor = conn.execute(sql)
        return cursor.fetchone()[0]

    def test_restore(self, backup_path: str) -> Dict[str, Any]:
        """
        测试备份恢复（不修改原数据库）

        Args:
            backup_path: 备份文件路径

        Returns:
            恢复测试结果
        """
        result = {
            'backup_path': backup_path,
            'tested_at': datetime.now().isoformat(),
            'status': 'unknown',
        }

        try:
            # 复制到临时文件
            with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
                tmp_path = tmp.name

            shutil.copy2(backup_path, tmp_path)

            # 测试打开和查询
            conn = sqlite3.connect(tmp_path, timeout=10)

            # 检查是否可以读取
            cursor = conn.execute("SELECT COUNT(*) FROM sqlite_master")
            table_count = cursor.fetchone()[0]

            # 检查每个表的行数
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [row[0] for row in cursor.fetchall()]

            table_stats = {}
            for table in tables:
                try:
                    # SECURITY: 表名来自sqlite_master系统表（可信源），非用户输入
                    count = self._count_table_rows(conn, table)
                    table_stats[table] = count
                except:
                    table_stats[table] = -1

            conn.close()

            # 清理临时文件
            os.unlink(tmp_path)

            result['status'] = 'pass'
            result['table_count'] = table_count
            result['table_stats'] = table_stats

        except Exception as e:
            result['status'] = 'fail'
            result['error'] = str(e)
            # 清理
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

        return result

    def get_backup_info(self, backup_path: str) -> Dict[str, Any]:
        """获取备份文件信息"""
        if not os.path.exists(backup_path):
            return {'error': '备份文件不存在'}

        stat = os.stat(backup_path)
        return {
            'path': backup_path,
            'size_mb': round(stat.st_size / 1024 / 1024, 2),
            'created': datetime.fromtimestamp(stat.st_ctime).isoformat(),
            'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
            'hash': self._compute_hash(backup_path),
        }

    def list_backups(self) -> List[Dict[str, Any]]:
        """列出所有备份文件"""
        backups = []
        for path in self.backup_dir.glob('*.db'):
            backups.append(self.get_backup_info(str(path)))
        backups.sort(key=lambda x: x.get('modified', ''), reverse=True)
        return backups
