"""
数据压缩存储优化
历史数据压缩归档，减少存储空间。

使用方式:
    from core.data_compressor import DataCompressor
    compressor = DataCompressor(db_path)
    compressor.compress_old_data(days=90)
"""

import gzip
import json
import time
import sqlite3
import logging
import threading
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


class DataCompressor:
    """数据压缩存储管理器"""

    def __init__(self, db_path: str, archive_dir: str = None):
        self.db_path = db_path
        if archive_dir is None:
            archive_dir = str(Path(db_path).parent / 'archive')
        self.archive_dir = Path(archive_dir)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def compress_old_data(
        self,
        table: str = 'history_data',
        days: int = 90,
        batch_size: int = 10000,
        timestamp_col: str = 'timestamp',
    ) -> Dict[str, Any]:
        """
        压缩归档旧数据

        Args:
            table: 表名
            days: 保留天数
            batch_size: 每批处理行数
            timestamp_col: 时间戳列名

        Returns:
            压缩结果统计
        """
        cutoff = datetime.now() - timedelta(days=days)
        cutoff_str = cutoff.isoformat()

        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row

        try:
            # 统计待压缩数据量
            count_sql = f'SELECT COUNT(*) as cnt FROM "{table}" WHERE "{timestamp_col}" < ?'
            row = conn.execute(count_sql, (cutoff_str,)).fetchone()
            total_rows = row[0] if row else 0

            if total_rows == 0:
                return {
                    'table': table,
                    'rows_compressed': 0,
                    'archive_file': None,
                    'message': '无数据需要压缩',
                }

            # 获取表结构
            columns = self._get_columns(conn, table)

            # 分批读取并压缩
            archive_file = self.archive_dir / f'{table}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.gz'
            compressed_size = 0
            rows_archived = 0

            with gzip.open(archive_file, 'wt', encoding='utf-8') as gz:
                # 写入表结构头部
                gz.write(f'-- Table: {table}\n')
                gz.write(f'-- Columns: {",".join(columns)}\n')
                gz.write(f'-- Archived: {datetime.now().isoformat()}\n')
                gz.write(f'-- Cutoff: {cutoff_str}\n')
                gz.write('--\n')

                offset = 0
                while True:
                    select_sql = f'SELECT * FROM "{table}" WHERE "{timestamp_col}" < ? ORDER BY "{timestamp_col}" LIMIT ? OFFSET ?'
                    rows = conn.execute(select_sql, (cutoff_str, batch_size, offset)).fetchall()

                    if not rows:
                        break

                    for row in rows:
                        row_data = dict(row)
                        gz.write(json.dumps(row_data, default=str) + '\n')
                        rows_archived += 1

                    offset += batch_size

            compressed_size = archive_file.stat().st_size

            # 删除已归档数据
            delete_sql = f'DELETE FROM "{table}" WHERE "{timestamp_col}" < ?'
            cursor = conn.execute(delete_sql, (cutoff_str,))
            deleted_count = cursor.rowcount
            conn.commit()

            # 记录归档元数据
            self._save_archive_metadata(archive_file, table, rows_archived, cutoff_str)

            result = {
                'table': table,
                'rows_compressed': rows_archived,
                'rows_deleted': deleted_count,
                'archive_file': str(archive_file),
                'compressed_size_mb': round(compressed_size / 1024 / 1024, 2),
                'cutoff_date': cutoff_str,
            }

            logger.info(f"数据压缩归档完成: {table} {rows_archived}行 → {archive_file.name}")
            return result

        finally:
            conn.close()

    def restore_archive(self, archive_path: str, table: str = None) -> Dict[str, Any]:
        """
        从归档恢复数据

        Args:
            archive_path: 归档文件路径
            table: 目标表名（None则从归档头部读取）

        Returns:
            恢复结果
        """
        archive = Path(archive_path)
        if not archive.exists():
            return {'error': f'归档文件不存在: {archive_path}'}

        conn = sqlite3.connect(self.db_path, timeout=30)

        try:
            rows_restored = 0
            target_table = table

            with gzip.open(archive, 'rt', encoding='utf-8') as gz:
                for line in gz:
                    line = line.strip()
                    if not line or line.startswith('--'):
                        # 解析头部获取表名
                        if line.startswith('-- Table:'):
                            if target_table is None:
                                target_table = line.split(':', 1)[1].strip()
                        continue

                    if target_table is None:
                        return {'error': '无法确定目标表名'}

                    row_data = json.loads(line)
                    columns = list(row_data.keys())
                    placeholders = ', '.join(['?' for _ in columns])
                    col_names = ', '.join([f'"{c}"' for c in columns])
                    values = [row_data[c] for c in columns]

                    try:
                        conn.execute(f'INSERT OR IGNORE INTO "{target_table}" ({col_names}) VALUES ({placeholders})', values)
                        rows_restored += 1
                    except sqlite3.Error:
                        pass  # 跳过冲突行

                conn.commit()

            return {
                'table': target_table,
                'rows_restored': rows_restored,
                'archive_file': str(archive),
            }

        finally:
            conn.close()

    def list_archives(self, table: str = None) -> List[Dict[str, Any]]:
        """列出归档文件"""
        archives = []
        for f in sorted(self.archive_dir.glob('*.gz'), reverse=True):
            if table and not f.name.startswith(table):
                continue
            archives.append({
                'name': f.name,
                'path': str(f),
                'size_mb': round(f.stat().st_size / 1024 / 1024, 2),
                'created': datetime.fromtimestamp(f.stat().st_ctime).isoformat(),
            })
        return archives

    def _get_columns(self, conn: sqlite3.Connection, table: str) -> List[str]:
        """获取表列名"""
        cursor = conn.execute(f'PRAGMA table_info("{table}")')
        return [row[1] for row in cursor.fetchall()]

    def _save_archive_metadata(self, archive_path: Path, table: str, rows: int, cutoff: str):
        """保存归档元数据"""
        meta_path = archive_path.with_suffix('.meta.json')
        meta = {
            'table': table,
            'rows': rows,
            'cutoff_date': cutoff,
            'archived_at': datetime.now().isoformat(),
            'archive_file': str(archive_path),
        }
        with open(meta_path, 'w') as f:
            json.dump(meta, f, indent=2)

    def get_stats(self) -> Dict[str, Any]:
        """获取压缩统计"""
        archives = list(self.archive_dir.glob('*.gz'))
        total_size = sum(a.stat().st_size for a in archives)
        return {
            'archive_dir': str(self.archive_dir),
            'total_archives': len(archives),
            'total_size_mb': round(total_size / 1024 / 1024, 2),
        }
