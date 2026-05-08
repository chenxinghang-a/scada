"""
数据归档模块
实现数据压缩和归档功能
支持多种压缩算法：滑动平均、最大值保留、最小值保留、LTTB降采样
"""

import logging
import math
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple
from collections import defaultdict

logger = logging.getLogger(__name__)


class DataArchive:
    """
    数据归档类
    负责数据压缩和归档
    
    压缩算法：
    1. 滑动平均 (moving_average) - 适用于平稳数据
    2. 最大值保留 (max_keep) - 适用于需要监控峰值的场景
    3. 最小值保留 (min_keep) - 适用于需要监控谷值的场景
    4. LTTB (Largest Triangle Three Buckets) - 适用于趋势图显示
    5. 统计聚合 (statistical) - 保留均值、最大、最小、标准差
    """

    def __init__(self, database):
        """
        初始化数据归档

        Args:
            database: 数据库实例
        """
        self.database = database
        
        # 压缩算法映射
        self.compress_algorithms = {
            'moving_average': self._compress_moving_average,
            'max_keep': self._compress_max_keep,
            'min_keep': self._compress_min_keep,
            'lttb': self._compress_lttb,
            'statistical': self._compress_statistical,
        }

    def archive_data(self, retention_days: int = 30, archive_table: str = 'history_data_archive'):
        """
        归档旧数据
        
        将超过保留天数的数据移动到归档表，然后删除原表数据

        Args:
            retention_days: 数据保留天数
            archive_table: 归档表名
        """
        logger.info(f"开始归档 {retention_days} 天前的数据...")
        
        cutoff_date = datetime.now() - timedelta(days=retention_days)
        
        # 创建归档表（如果不存在）
        self._create_archive_table(archive_table)
        
        # 移动数据到归档表
        moved_count = self._move_to_archive(cutoff_date, archive_table)
        
        # 删除原表中的旧数据
        deleted_count = self.database.cleanup_old_data(retention_days)
        
        logger.info(f"数据归档完成: 移动 {moved_count} 条到归档表，删除 {deleted_count} 条旧数据")
        
        return {
            'moved_to_archive': moved_count,
            'deleted_from_main': deleted_count,
            'cutoff_date': cutoff_date.isoformat()
        }

    def _create_archive_table(self, archive_table: str):
        """创建归档表"""
        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f'''
                CREATE TABLE IF NOT EXISTS {archive_table} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    register_name TEXT NOT NULL,
                    value REAL,
                    unit TEXT,
                    timestamp DATETIME NOT NULL,
                    archived_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # 创建索引
            cursor.execute(f'''
                CREATE INDEX IF NOT EXISTS idx_{archive_table}_device_time 
                ON {archive_table}(device_id, register_name, timestamp)
            ''')

    def _move_to_archive(self, cutoff_date: datetime, archive_table: str) -> int:
        """移动数据到归档表"""
        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            
            # 插入到归档表
            cursor.execute(f'''
                INSERT INTO {archive_table} (device_id, register_name, value, unit, timestamp)
                SELECT device_id, register_name, value, unit, timestamp
                FROM history_data
                WHERE timestamp < ?
            ''', (cutoff_date.isoformat(),))
            
            moved_count = cursor.rowcount
            
            return moved_count

    def compress_data(self, device_id: str, register_name: str,
                      start_time: datetime, end_time: datetime,
                      interval: str = '1hour', algorithm: str = 'statistical') -> Dict[str, Any]:
        """
        压缩数据

        Args:
            device_id: 设备ID
            register_name: 寄存器名称
            start_time: 开始时间
            end_time: 结束时间
            interval: 压缩间隔 (1min, 5min, 15min, 1hour, 1day)
            algorithm: 压缩算法 (moving_average, max_keep, min_keep, lttb, statistical)

        Returns:
            压缩结果
        """
        # 获取原始数据
        raw_data = self.database.get_history_data(
            device_id=device_id,
            register_name=register_name,
            start_time=start_time,
            end_time=end_time,
            interval='1min'  # 获取1分钟粒度的原始数据
        )
        
        if not raw_data:
            return {
                'device_id': device_id,
                'register_name': register_name,
                'original_count': 0,
                'compressed_count': 0,
                'compression_ratio': 0,
                'data': []
            }
        
        # 解析时间间隔
        interval_seconds = self._parse_interval(interval)
        
        # 按时间桶分组
        buckets = self._group_by_interval(raw_data, interval_seconds)
        
        # 应用压缩算法
        if algorithm in self.compress_algorithms:
            compressed_data = self.compress_algorithms[algorithm](buckets)
        else:
            logger.warning(f"未知压缩算法: {algorithm}，使用默认统计聚合")
            compressed_data = self._compress_statistical(buckets)
        
        # 计算压缩比
        original_count = len(raw_data)
        compressed_count = len(compressed_data)
        compression_ratio = (1 - compressed_count / original_count) * 100 if original_count > 0 else 0
        
        logger.info(f"数据压缩完成: {device_id}/{register_name}, "
                    f"原始 {original_count} 条 -> 压缩后 {compressed_count} 条, "
                    f"压缩率 {compression_ratio:.1f}%")
        
        return {
            'device_id': device_id,
            'register_name': register_name,
            'original_count': original_count,
            'compressed_count': compressed_count,
            'compression_ratio': round(compression_ratio, 1),
            'interval': interval,
            'algorithm': algorithm,
            'data': compressed_data
        }

    def _parse_interval(self, interval: str) -> int:
        """
        解析时间间隔为秒数

        Args:
            interval: 时间间隔字符串

        Returns:
            秒数
        """
        interval_map = {
            '1min': 60,
            '5min': 300,
            '15min': 900,
            '30min': 1800,
            '1hour': 3600,
            '6hour': 21600,
            '12hour': 43200,
            '1day': 86400,
        }
        return interval_map.get(interval, 3600)

    def _group_by_interval(self, data: List[Dict], interval_seconds: int) -> Dict[int, List[Dict]]:
        """
        按时间间隔分组

        Args:
            data: 原始数据
            interval_seconds: 间隔秒数

        Returns:
            分组后的数据
        """
        buckets = defaultdict(list)
        
        for item in data:
            timestamp = item.get('timestamp')
            if isinstance(timestamp, str):
                timestamp = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            
            # 计算时间桶
            epoch = timestamp.timestamp()
            bucket_key = int(epoch // interval_seconds) * interval_seconds
            
            buckets[bucket_key].append(item)
        
        return buckets

    def _compress_moving_average(self, buckets: Dict[int, List[Dict]]) -> List[Dict]:
        """
        滑动平均压缩
        
        对每个时间桶内的数据取平均值

        Args:
            buckets: 分组后的数据

        Returns:
            压缩后的数据
        """
        result = []
        
        for bucket_key, items in sorted(buckets.items()):
            values = [item['value'] for item in items if item.get('value') is not None]
            
            if values:
                avg_value = sum(values) / len(values)
                timestamp = datetime.fromtimestamp(bucket_key)
                
                result.append({
                    'timestamp': timestamp.isoformat(),
                    'value': round(avg_value, 4),
                    'unit': items[0].get('unit', ''),
                    'count': len(values)
                })
        
        return result

    def _compress_max_keep(self, buckets: Dict[int, List[Dict]]) -> List[Dict]:
        """
        最大值保留压缩
        
        保留每个时间桶内的最大值

        Args:
            buckets: 分组后的数据

        Returns:
            压缩后的数据
        """
        result = []
        
        for bucket_key, items in sorted(buckets.items()):
            values = [item['value'] for item in items if item.get('value') is not None]
            
            if values:
                max_value = max(values)
                timestamp = datetime.fromtimestamp(bucket_key)
                
                result.append({
                    'timestamp': timestamp.isoformat(),
                    'value': round(max_value, 4),
                    'unit': items[0].get('unit', ''),
                    'count': len(values)
                })
        
        return result

    def _compress_min_keep(self, buckets: Dict[int, List[Dict]]) -> List[Dict]:
        """
        最小值保留压缩
        
        保留每个时间桶内的最小值

        Args:
            buckets: 分组后的数据

        Returns:
            压缩后的数据
        """
        result = []
        
        for bucket_key, items in sorted(buckets.items()):
            values = [item['value'] for item in items if item.get('value') is not None]
            
            if values:
                min_value = min(values)
                timestamp = datetime.fromtimestamp(bucket_key)
                
                result.append({
                    'timestamp': timestamp.isoformat(),
                    'value': round(min_value, 4),
                    'unit': items[0].get('unit', ''),
                    'count': len(values)
                })
        
        return result

    def _compress_lttb(self, buckets: Dict[int, List[Dict]], threshold: int = 100) -> List[Dict]:
        """
        LTTB (Largest Triangle Three Buckets) 压缩
        
        适用于趋势图显示，保留数据的视觉特征

        Args:
            buckets: 分组后的数据
            threshold: 目标数据点数量

        Returns:
            压缩后的数据
        """
        # 先按时间排序
        sorted_buckets = sorted(buckets.items())
        
        if len(sorted_buckets) <= threshold:
            # 数据量小于阈值，直接返回平均值
            return self._compress_moving_average(buckets)
        
        # 计算每个桶的平均值和时间戳
        points = []
        for bucket_key, items in sorted_buckets:
            values = [item['value'] for item in items if item.get('value') is not None]
            if values:
                avg_value = sum(values) / len(values)
                timestamp = datetime.fromtimestamp(bucket_key)
                points.append({
                    'timestamp': timestamp,
                    'value': avg_value,
                    'unit': items[0].get('unit', ''),
                    'count': len(values)
                })
        
        if len(points) <= threshold:
            return [{
                'timestamp': p['timestamp'].isoformat(),
                'value': round(p['value'], 4),
                'unit': p['unit'],
                'count': p['count']
            } for p in points]
        
        # LTTB算法
        result = [points[0]]  # 保留第一个点
        
        for i in range(1, threshold - 1):
            # 计算当前桶的范围
            bucket_start = int((i - 1) * len(points) / threshold)
            bucket_end = int(i * len(points) / threshold)
            next_bucket_start = int(i * len(points) / threshold)
            next_bucket_end = int((i + 1) * len(points) / threshold)
            
            # 计算下一个桶的平均点
            next_bucket_points = points[next_bucket_start:next_bucket_end]
            if next_bucket_points:
                avg_x = sum(p['timestamp'].timestamp() for p in next_bucket_points) / len(next_bucket_points)
                avg_y = sum(p['value'] for p in next_bucket_points) / len(next_bucket_points)
                avg_point = {'timestamp': datetime.fromtimestamp(avg_x), 'value': avg_y}
            else:
                avg_point = points[-1]
            
            # 在当前桶中找到面积最大的点
            max_area = -1
            max_point = None
            
            for point in points[bucket_start:bucket_end]:
                # 计算三角形面积
                area = abs(
                    (result[-1]['timestamp'].timestamp() - avg_point['timestamp'].timestamp()) *
                    (point['value'] - result[-1]['value']) -
                    (result[-1]['timestamp'].timestamp() - point['timestamp'].timestamp()) *
                    (avg_point['value'] - result[-1]['value'])
                )
                
                if area > max_area:
                    max_area = area
                    max_point = point
            
            if max_point:
                result.append(max_point)
        
        result.append(points[-1])  # 保留最后一个点
        
        return [{
            'timestamp': p['timestamp'].isoformat(),
            'value': round(p['value'], 4),
            'unit': p.get('unit', ''),
            'count': p.get('count', 1)
        } for p in result]

    def _compress_statistical(self, buckets: Dict[int, List[Dict]]) -> List[Dict]:
        """
        统计聚合压缩
        
        保留均值、最大、最小、标准差、计数

        Args:
            buckets: 分组后的数据

        Returns:
            压缩后的数据
        """
        result = []
        
        for bucket_key, items in sorted(buckets.items()):
            values = [item['value'] for item in items if item.get('value') is not None]
            
            if values:
                avg_value = sum(values) / len(values)
                max_value = max(values)
                min_value = min(values)
                
                # 计算标准差
                if len(values) > 1:
                    variance = sum((v - avg_value) ** 2 for v in values) / (len(values) - 1)
                    std_dev = math.sqrt(variance)
                else:
                    std_dev = 0
                
                timestamp = datetime.fromtimestamp(bucket_key)
                
                result.append({
                    'timestamp': timestamp.isoformat(),
                    'avg': round(avg_value, 4),
                    'max': round(max_value, 4),
                    'min': round(min_value, 4),
                    'std': round(std_dev, 4),
                    'count': len(values),
                    'unit': items[0].get('unit', '')
                })
        
        return result

    def get_compression_stats(self, device_id: str = None, 
                              start_time: datetime = None, 
                              end_time: datetime = None) -> Dict[str, Any]:
        """
        获取压缩统计信息

        Args:
            device_id: 设备ID（可选）
            start_time: 开始时间（可选）
            end_time: 结束时间（可选）

        Returns:
            统计信息
        """
        with self.database.get_connection() as conn:
            cursor = conn.cursor()
            
            # 构建查询条件
            conditions = []
            params = []
            
            if device_id:
                conditions.append("device_id = ?")
                params.append(device_id)
            
            if start_time:
                conditions.append("timestamp >= ?")
                params.append(start_time.isoformat())
            
            if end_time:
                conditions.append("timestamp <= ?")
                params.append(end_time.isoformat())
            
            where_clause = " AND ".join(conditions) if conditions else "1=1"
            
            # 查询历史数据统计
            cursor.execute(f'''
                SELECT 
                    device_id,
                    register_name,
                    COUNT(*) as total_records,
                    MIN(timestamp) as earliest_record,
                    MAX(timestamp) as latest_record
                FROM history_data
                WHERE {where_clause}
                GROUP BY device_id, register_name
            ''')
            
            stats = []
            for row in cursor.fetchall():
                stats.append({
                    'device_id': row['device_id'],
                    'register_name': row['register_name'],
                    'total_records': row['total_records'],
                    'earliest_record': row['earliest_record'],
                    'latest_record': row['latest_record']
                })
            
            return {
                'total_devices': len(set(s['device_id'] for s in stats)),
                'total_registers': len(stats),
                'total_records': sum(s['total_records'] for s in stats),
                'details': stats
            }
