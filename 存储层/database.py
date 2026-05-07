"""
数据库模块
实现SQLite数据库操作
"""

import sqlite3
import logging
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import contextmanager

logger = logging.getLogger(__name__)


class Database:
    """
    SQLite数据库管理类
    负责数据存储、查询和维护
    """
    
    def __init__(self, db_path: str = 'data/scada.db'):
        """
        初始化数据库
        
        Args:
            db_path: 数据库文件路径
        """
        self.db_path = db_path
        
        # 确保目录存在
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        
        # 初始化数据库
        self._init_database()
    
    @contextmanager
    def get_connection(self):
        """
        获取数据库连接（上下文管理器）
        使用WAL模式提升并发性能，设置超时避免死锁
        
        Yields:
            sqlite3.Connection: 数据库连接
        """
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        # WAL模式：允许多个读连接和一个写连接并发
        conn.execute('PRAGMA journal_mode=WAL')
        # 忙等待超时30秒
        conn.execute('PRAGMA busy_timeout=30000')
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
    
    def _init_database(self):
        """初始化数据库表结构"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 创建实时数据表（每个设备+寄存器只保留最新一条）
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS realtime_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    register_name TEXT NOT NULL,
                    value REAL,
                    unit TEXT,
                    timestamp DATETIME NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(device_id, register_name)
                )
            ''')
            
            # 创建历史数据表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS history_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    register_name TEXT NOT NULL,
                    value REAL,
                    unit TEXT,
                    timestamp DATETIME NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 创建报警记录表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS alarm_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alarm_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    register_name TEXT NOT NULL,
                    alarm_level TEXT NOT NULL,
                    alarm_message TEXT,
                    threshold REAL,
                    actual_value REAL,
                    timestamp DATETIME NOT NULL,
                    acknowledged BOOLEAN DEFAULT 0,
                    acknowledged_at DATETIME,
                    acknowledged_by TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 创建设备状态表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS device_status (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT,
                    timestamp DATETIME NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 创建索引
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_realtime_device_time 
                ON realtime_data(device_id, timestamp)
            ''')
            
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_realtime_register 
                ON realtime_data(device_id, register_name, timestamp)
            ''')
            
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_history_device_time 
                ON history_data(device_id, timestamp)
            ''')
            
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_history_register 
                ON history_data(device_id, register_name, timestamp)
            ''')
            
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_alarm_device_time 
                ON alarm_records(device_id, timestamp)
            ''')
            
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_alarm_level 
                ON alarm_records(alarm_level, acknowledged)
            ''')
            
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_alarm_unacked 
                ON alarm_records(acknowledged, timestamp)
                WHERE acknowledged = 0
            ''')
            
            # 创建数据归档表（用于长期存储）
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS history_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    register_name TEXT NOT NULL,
                    avg_value REAL,
                    min_value REAL,
                    max_value REAL,
                    sample_count INTEGER,
                    archive_date DATE NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_archive_device_date 
                ON history_archive(device_id, archive_date)
            ''')
            
            logger.info("数据库初始化完成")
    
    def insert_data(self, device_id: str, register_name: str, 
                    value: float, timestamp: datetime, unit: str = ''):
        """
        插入数据
        - realtime_data: UPSERT，每个设备+寄存器只保留最新一条
        - history_data: INSERT，保留全量历史记录
        
        Args:
            device_id: 设备ID
            register_name: 寄存器名称
            value: 数据值
            timestamp: 时间戳
            unit: 单位
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # realtime_data: UPSERT — 每个(device_id, register_name)只保留最新值
            cursor.execute('''
                INSERT INTO realtime_data (device_id, register_name, value, unit, timestamp)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(device_id, register_name) DO UPDATE SET
                    value = excluded.value,
                    unit = excluded.unit,
                    timestamp = excluded.timestamp,
                    created_at = CURRENT_TIMESTAMP
            ''', (device_id, register_name, value, unit, timestamp))
            
            # history_data: INSERT — 保留全量历史
            cursor.execute('''
                INSERT INTO history_data (device_id, register_name, value, unit, timestamp)
                VALUES (?, ?, ?, ?, ?)
            ''', (device_id, register_name, value, unit, timestamp))
    
    def get_realtime_data(self, device_id: str = None, 
                          limit: int = 100) -> List[Dict]:
        """
        获取实时数据
        
        Args:
            device_id: 设备ID（可选）
            limit: 返回数量限制
            
        Returns:
            List[Dict]: 数据列表
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            if device_id:
                cursor.execute('''
                    SELECT * FROM realtime_data 
                    WHERE device_id = ?
                    ORDER BY timestamp DESC 
                    LIMIT ?
                ''', (device_id, limit))
            else:
                cursor.execute('''
                    SELECT * FROM realtime_data 
                    ORDER BY timestamp DESC 
                    LIMIT ?
                ''', (limit,))
            
            return [dict(row) for row in cursor.fetchall()]
    
    def get_latest_data(self, device_id: str, 
                        register_name: str = None) -> Optional[Dict]:
        """
        获取最新数据
        
        Args:
            device_id: 设备ID
            register_name: 寄存器名称（可选，不指定则返回所有寄存器最新值）
            
        Returns:
            Dict: 最新数据（单个寄存器返回dict，所有寄存器返回{register_name: dict}）
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            if register_name:
                cursor.execute('''
                    SELECT * FROM realtime_data 
                    WHERE device_id = ? AND register_name = ?
                    ORDER BY timestamp DESC 
                    LIMIT 1
                ''', (device_id, register_name))
                row = cursor.fetchone()
                return dict(row) if row else None
            else:
                # 返回设备所有寄存器的最新值
                cursor.execute('''
                    SELECT r1.* FROM realtime_data r1
                    INNER JOIN (
                        SELECT register_name, MAX(timestamp) as max_ts
                        FROM realtime_data
                        WHERE device_id = ?
                        GROUP BY register_name
                    ) r2 ON r1.register_name = r2.register_name 
                         AND r1.timestamp = r2.max_ts
                    WHERE r1.device_id = ?
                ''', (device_id, device_id))
                rows = cursor.fetchall()
                if not rows:
                    return None
                # 返回以寄存器名为key的字典
                result = {}
                for row in rows:
                    row_dict = dict(row)
                    result[row_dict['register_name']] = row_dict
                return result
    
    def get_device_registers(self, device_id: str) -> List[str]:
        """
        获取设备的所有寄存器名称
        
        Args:
            device_id: 设备ID
            
        Returns:
            List[str]: 寄存器名称列表
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT DISTINCT register_name 
                FROM history_data 
                WHERE device_id = ?
                ORDER BY register_name
            ''', (device_id,))
            return [row['register_name'] for row in cursor.fetchall()]
    
    def get_history_data(self, device_id: str, register_name: str,
                         start_time: datetime, end_time: datetime,
                         interval: str = '1min') -> List[Dict]:
        """
        获取历史数据
        
        Args:
            device_id: 设备ID
            register_name: 寄存器名称
            start_time: 开始时间
            end_time: 结束时间
            interval: 时间间隔（1min, 5min, 1hour, 1day）
            
        Returns:
            List[Dict]: 历史数据列表
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 根据时间间隔进行数据聚合
            if interval == '1min':
                group_format = '%Y-%m-%d %H:%M:00'
            elif interval == '5min':
                group_format = '%Y-%m-%d %H:%M:00'
                # 需要额外处理5分钟间隔
            elif interval == '1hour':
                group_format = '%Y-%m-%d %H:00:00'
            elif interval == '1day':
                group_format = '%Y-%m-%d'
            else:
                group_format = '%Y-%m-%d %H:%M:%S'
            
            cursor.execute('''
                SELECT 
                    strftime(?, timestamp) as time_bucket,
                    AVG(value) as avg_value,
                    MIN(value) as min_value,
                    MAX(value) as max_value,
                    COUNT(*) as sample_count
                FROM history_data
                WHERE device_id = ? AND register_name = ?
                    AND timestamp BETWEEN ? AND ?
                GROUP BY time_bucket
                ORDER BY time_bucket
            ''', (group_format, device_id, register_name, start_time, end_time))
            
            return [dict(row) for row in cursor.fetchall()]
    
    def insert_alarm(self, alarm_id: str, device_id: str, register_name: str,
                     alarm_level: str, alarm_message: str, threshold: float,
                     actual_value: float, timestamp: datetime):
        """
        插入报警记录
        
        Args:
            alarm_id: 报警ID
            device_id: 设备ID
            register_name: 寄存器名称
            alarm_level: 报警级别
            alarm_message: 报警消息
            threshold: 阈值
            actual_value: 实际值
            timestamp: 时间戳
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO alarm_records 
                (alarm_id, device_id, register_name, alarm_level, alarm_message, 
                 threshold, actual_value, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (alarm_id, device_id, register_name, alarm_level, alarm_message,
                  threshold, actual_value, timestamp))
    
    def get_alarm_records(self, device_id: str = None, alarm_level: str = None,
                          start_time: datetime = None, end_time: datetime = None,
                          acknowledged: bool = None, limit: int = 100) -> List[Dict]:
        """
        获取报警记录
        
        Args:
            device_id: 设备ID（可选）
            alarm_level: 报警级别（可选）
            start_time: 开始时间（可选）
            end_time: 结束时间（可选）
            acknowledged: 是否已确认（可选）
            limit: 返回数量限制
            
        Returns:
            List[Dict]: 报警记录列表
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            query = 'SELECT * FROM alarm_records WHERE 1=1'
            params = []
            
            if device_id:
                query += ' AND device_id = ?'
                params.append(device_id)
            
            if alarm_level:
                query += ' AND alarm_level = ?'
                params.append(alarm_level)
            
            if start_time:
                query += ' AND timestamp >= ?'
                params.append(start_time)
            
            if end_time:
                query += ' AND timestamp <= ?'
                params.append(end_time)
            
            if acknowledged is not None:
                query += ' AND acknowledged = ?'
                params.append(1 if acknowledged else 0)
            
            query += ' ORDER BY timestamp DESC LIMIT ?'
            params.append(limit)
            
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
    
    def acknowledge_alarm(self, alarm_id: str, acknowledged_by: str) -> bool:
        """
        确认报警
        
        Args:
            alarm_id: 报警ID
            acknowledged_by: 确认人
            
        Returns:
            bool: 确认是否成功
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE alarm_records 
                SET acknowledged = 1, acknowledged_at = ?, acknowledged_by = ?
                WHERE alarm_id = ? AND acknowledged = 0
            ''', (datetime.now(), acknowledged_by, alarm_id))
            
            return cursor.rowcount > 0
    
    def get_device_summary(self) -> List[Dict]:
        """
        获取设备数据摘要
        
        Returns:
            List[Dict]: 设备摘要列表
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT 
                    device_id,
                    COUNT(DISTINCT register_name) as register_count,
                    MAX(timestamp) as last_update,
                    COUNT(*) as total_records
                FROM realtime_data
                GROUP BY device_id
            ''')
            
            return [dict(row) for row in cursor.fetchall()]
    
    def cleanup_old_data(self, retention_days: int = 30):
        """
        清理旧数据
        
        Args:
            retention_days: 数据保留天数
        """
        cutoff_date = datetime.now() - timedelta(days=retention_days)
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 清理实时数据
            cursor.execute('''
                DELETE FROM realtime_data 
                WHERE timestamp < ?
            ''', (cutoff_date,))
            
            realtime_deleted = cursor.rowcount
            
            # 清理历史数据（保留更长时间）
            history_cutoff = datetime.now() - timedelta(days=retention_days * 12)
            cursor.execute('''
                DELETE FROM history_data 
                WHERE timestamp < ?
            ''', (history_cutoff,))
            
            history_deleted = cursor.rowcount
            
            logger.info(f"清理旧数据: 实时数据 {realtime_deleted} 条, 历史数据 {history_deleted} 条")
    
    def get_database_stats(self) -> Dict[str, Any]:
        """
        获取数据库统计信息
        
        Returns:
            Dict: 统计信息
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 实时数据统计
            cursor.execute('SELECT COUNT(*) FROM realtime_data')
            realtime_count = cursor.fetchone()[0]
            
            # 历史数据统计
            cursor.execute('SELECT COUNT(*) FROM history_data')
            history_count = cursor.fetchone()[0]
            
            # 报警记录统计
            cursor.execute('SELECT COUNT(*) FROM alarm_records')
            alarm_count = cursor.fetchone()[0]
            
            # 未确认报警统计
            cursor.execute('SELECT COUNT(*) FROM alarm_records WHERE acknowledged = 0')
            unacknowledged_count = cursor.fetchone()[0]
            
            # 归档数据统计
            try:
                cursor.execute('SELECT COUNT(*) FROM history_archive')
                archive_count = cursor.fetchone()[0]
            except Exception as e:
                logger.debug(f"查询归档数据统计失败（可能表不存在）: {e}")
                archive_count = 0
            
            # 数据库文件大小
            db_size = Path(self.db_path).stat().st_size if Path(self.db_path).exists() else 0
            
            return {
                'realtime_records': realtime_count,
                'history_records': history_count,
                'alarm_records': alarm_count,
                'unacknowledged_alarms': unacknowledged_count,
                'archive_records': archive_count,
                'database_size_mb': round(db_size / (1024 * 1024), 2)
            }
    
    def archive_old_data(self, archive_days: int = 7, delete_days: int = 30):
        """
        归档旧数据
        
        将超过archive_days天的历史数据按天聚合后存入归档表，
        然后删除超过delete_days天的原始数据。
        
        Args:
            archive_days: 归档天数（默认7天前的数据归档）
            delete_days: 删除天数（默认30天前的数据删除）
        """
        archive_cutoff = datetime.now() - timedelta(days=archive_days)
        delete_cutoff = datetime.now() - timedelta(days=delete_days)
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 1. 归档：按天聚合并存入归档表
            cursor.execute('''
                INSERT INTO history_archive 
                    (device_id, register_name, avg_value, min_value, max_value, sample_count, archive_date)
                SELECT 
                    device_id,
                    register_name,
                    AVG(value) as avg_value,
                    MIN(value) as min_value,
                    MAX(value) as max_value,
                    COUNT(*) as sample_count,
                    DATE(timestamp) as archive_date
                FROM history_data
                WHERE timestamp < ? AND timestamp >= ?
                GROUP BY device_id, register_name, DATE(timestamp)
            ''', (archive_cutoff, delete_cutoff))
            
            archived_rows = cursor.rowcount
            
            # 2. 删除已归档的旧数据
            cursor.execute('''
                DELETE FROM history_data 
                WHERE timestamp < ?
            ''', (delete_cutoff,))
            
            deleted_rows = cursor.rowcount
            
            # 3. 清理旧的实时数据（只保留最近24小时）
            realtime_cutoff = datetime.now() - timedelta(hours=24)
            cursor.execute('''
                DELETE FROM realtime_data 
                WHERE timestamp < ?
            ''', (realtime_cutoff,))
            
            realtime_deleted = cursor.rowcount
            
            logger.info(f"数据归档完成: 归档 {archived_rows} 条, 删除历史 {deleted_rows} 条, 删除实时 {realtime_deleted} 条")
            
            return {
                'archived': archived_rows,
                'deleted_history': deleted_rows,
                'deleted_realtime': realtime_deleted
            }
    
    def get_archive_data(self, device_id: str, register_name: str,
                         start_date: str, end_date: str) -> List[Dict]:
        """
        获取归档数据
        
        Args:
            device_id: 设备ID
            register_name: 寄存器名称
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)
            
        Returns:
            List[Dict]: 归档数据列表
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT * FROM history_archive
                WHERE device_id = ? AND register_name = ?
                    AND archive_date BETWEEN ? AND ?
                ORDER BY archive_date
            ''', (device_id, register_name, start_date, end_date))
            
            return [dict(row) for row in cursor.fetchall()]
    
    def vacuum_database(self):
        """
        压缩数据库文件
        释放已删除数据占用的空间
        """
        try:
            with self.get_connection() as conn:
                conn.execute('VACUUM')
            logger.info("数据库压缩完成")
            return True
        except Exception as e:
            logger.error(f"数据库压缩失败: {e}")
            return False
    
    def get_table_sizes(self) -> Dict[str, int]:
        """
        获取各表的记录数
        
        Returns:
            Dict: 表名 -> 记录数
        """
        tables = ['realtime_data', 'history_data', 'alarm_records', 'history_archive', 'users', 'operation_logs']
        result = {}
        
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for table in tables:
                try:
                    cursor.execute(f'SELECT COUNT(*) FROM {table}')
                    result[table] = cursor.fetchone()[0]
                except:
                    result[table] = 0
        
        return result
