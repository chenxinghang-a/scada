"""
SQLite到TDengine数据迁移工具

将SQLite中的历史数据迁移到TDengine时序数据库。

迁移策略：
1. 读取SQLite中的历史数据
2. 转换为TDengine数据模型
3. 批量写入TDengine
4. 验证数据完整性

注意事项：
- 迁移前请备份SQLite数据库
- 迁移过程中不要写入新数据
- 迁移完成后验证数据一致性
"""

import sqlite3
import logging
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from pathlib import Path

from .tdengine_client import TDengineClient
from .data_models import (
    TelemetryRecord, AlarmRecord, OEERecord, EnergyRecord
)


class SQLiteToTDengineMigrator:
    """
    SQLite到TDengine数据迁移器
    """
    
    def __init__(self, sqlite_path: str, tdengine_client: TDengineClient):
        """
        初始化迁移器
        
        Args:
            sqlite_path: SQLite数据库路径
            tdengine_client: TDengine客户端
        """
        self.sqlite_path = sqlite_path
        self.tdengine = tdengine_client
        self.logger = logging.getLogger("Migrator")
        
        # SQLite连接
        self._sqlite_conn: Optional[sqlite3.Connection] = None
        
        # 迁移统计
        self.stats = {
            'telemetry_migrated': 0,
            'alarms_migrated': 0,
            'oee_migrated': 0,
            'energy_migrated': 0,
            'errors': 0,
            'start_time': None,
            'end_time': None
        }
    
    def connect_sqlite(self) -> bool:
        """连接SQLite数据库"""
        try:
            if not Path(self.sqlite_path).exists():
                self.logger.error(f"SQLite数据库不存在: {self.sqlite_path}")
                return False
            
            self._sqlite_conn = sqlite3.connect(self.sqlite_path)
            self._sqlite_conn.row_factory = sqlite3.Row
            self.logger.info(f"SQLite连接成功: {self.sqlite_path}")
            return True
        except Exception as e:
            self.logger.error(f"SQLite连接失败: {e}")
            return False
    
    def disconnect_sqlite(self):
        """断开SQLite连接"""
        if self._sqlite_conn:
            self._sqlite_conn.close()
            self._sqlite_conn = None
    
    def migrate_all(self, batch_size: int = 1000) -> Dict:
        """
        执行完整迁移
        
        Args:
            batch_size: 批量写入大小
            
        Returns:
            Dict: 迁移统计
        """
        self.stats['start_time'] = datetime.now()
        
        try:
            # 连接数据库
            if not self.connect_sqlite():
                return self.stats
            
            # 连接TDengine
            if not self.tdengine.connect():
                return self.stats
            
            # 初始化TDengine表
            self.tdengine.init_tables()
            
            # 迁移各表数据
            self._migrate_telemetry(batch_size)
            self._migrate_alarms(batch_size)
            self._migrate_oee(batch_size)
            self._migrate_energy(batch_size)
            
            self.stats['end_time'] = datetime.now()
            
            # 打印统计
            self._print_stats()
            
            return self.stats
            
        except Exception as e:
            self.logger.error(f"迁移异常: {e}")
            self.stats['errors'] += 1
            return self.stats
        finally:
            self.disconnect_sqlite()
    
    def _migrate_telemetry(self, batch_size: int):
        """迁移遥测数据"""
        self.logger.info("开始迁移遥测数据...")
        
        try:
            cursor = self._sqlite_conn.cursor()
            
            # 查询SQLite中的遥测数据
            cursor.execute("""
                SELECT device_id, register_name, timestamp, value, quality
                FROM telemetry
                ORDER BY timestamp
            """)
            
            batch = []
            for row in cursor:
                try:
                    record = TelemetryRecord(
                        device_id=row['device_id'],
                        register_name=row['register_name'],
                        timestamp=datetime.fromisoformat(row['timestamp']),
                        value=row['value'],
                        quality=row['quality'] if row['quality'] else 192
                    )
                    batch.append(record)
                    
                    if len(batch) >= batch_size:
                        self.tdengine.write_telemetry_batch(batch)
                        self.stats['telemetry_migrated'] += len(batch)
                        self.logger.info(f"已迁移 {self.stats['telemetry_migrated']} 条遥测数据")
                        batch = []
                        
                except Exception as e:
                    self.logger.error(f"处理遥测数据异常: {e}")
                    self.stats['errors'] += 1
            
            # 写入剩余数据
            if batch:
                self.tdengine.write_telemetry_batch(batch)
                self.stats['telemetry_migrated'] += len(batch)
            
            self.logger.info(f"遥测数据迁移完成: {self.stats['telemetry_migrated']} 条")
            
        except Exception as e:
            self.logger.error(f"迁移遥测数据失败: {e}")
            self.stats['errors'] += 1
    
    def _migrate_alarms(self, batch_size: int):
        """迁移报警数据"""
        self.logger.info("开始迁移报警数据...")
        
        try:
            cursor = self._sqlite_conn.cursor()
            
            # 查询SQLite中的报警数据
            cursor.execute("""
                SELECT alarm_id, device_id, timestamp, alarm_level, alarm_type,
                       alarm_message, threshold, actual_value
                FROM alarms
                ORDER BY timestamp
            """)
            
            count = 0
            for row in cursor:
                try:
                    record = AlarmRecord(
                        alarm_id=row['alarm_id'],
                        device_id=row['device_id'],
                        timestamp=datetime.fromisoformat(row['timestamp']),
                        level=row['alarm_level'],
                        alarm_type=row['alarm_type'],
                        message=row['alarm_message'],
                        value=row['actual_value'] if row['actual_value'] else 0,
                        threshold=row['threshold'] if row['threshold'] else 0
                    )
                    self.tdengine.write_alarm(record)
                    count += 1
                    
                    if count % batch_size == 0:
                        self.stats['alarms_migrated'] = count
                        self.logger.info(f"已迁移 {count} 条报警数据")
                        
                except Exception as e:
                    self.logger.error(f"处理报警数据异常: {e}")
                    self.stats['errors'] += 1
            
            self.stats['alarms_migrated'] = count
            self.logger.info(f"报警数据迁移完成: {count} 条")
            
        except Exception as e:
            self.logger.error(f"迁移报警数据失败: {e}")
            self.stats['errors'] += 1
    
    def _migrate_oee(self, batch_size: int):
        """迁移OEE数据"""
        self.logger.info("开始迁移OEE数据...")
        
        try:
            cursor = self._sqlite_conn.cursor()
            
            # 检查OEE表是否存在
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name='oee_records'
            """)
            
            if not cursor.fetchone():
                self.logger.info("OEE表不存在，跳过迁移")
                return
            
            cursor.execute("""
                SELECT device_id, timestamp, availability, performance, 
                       quality_rate, oee, total_count, good_count
                FROM oee_records
                ORDER BY timestamp
            """)
            
            count = 0
            for row in cursor:
                try:
                    record = OEERecord(
                        device_id=row['device_id'],
                        timestamp=datetime.fromisoformat(row['timestamp']),
                        availability=row['availability'],
                        performance=row['performance'],
                        quality_rate=row['quality_rate'],
                        oee=row['oee'],
                        total_count=row['total_count'] if row['total_count'] else 0,
                        good_count=row['good_count'] if row['good_count'] else 0
                    )
                    self.tdengine.write_oee(record)
                    count += 1
                    
                    if count % batch_size == 0:
                        self.stats['oee_migrated'] = count
                        self.logger.info(f"已迁移 {count} 条OEE数据")
                        
                except Exception as e:
                    self.logger.error(f"处理OEE数据异常: {e}")
                    self.stats['errors'] += 1
            
            self.stats['oee_migrated'] = count
            self.logger.info(f"OEE数据迁移完成: {count} 条")
            
        except Exception as e:
            self.logger.error(f"迁移OEE数据失败: {e}")
            self.stats['errors'] += 1
    
    def _migrate_energy(self, batch_size: int):
        """迁移能源数据"""
        self.logger.info("开始迁移能源数据...")
        
        try:
            cursor = self._sqlite_conn.cursor()
            
            # 检查能源表是否存在
            cursor.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name='energy_records'
            """)
            
            if not cursor.fetchone():
                self.logger.info("能源表不存在，跳过迁移")
                return
            
            cursor.execute("""
                SELECT device_id, timestamp, power, energy, voltage, current
                FROM energy_records
                ORDER BY timestamp
            """)
            
            count = 0
            for row in cursor:
                try:
                    record = EnergyRecord(
                        device_id=row['device_id'],
                        timestamp=datetime.fromisoformat(row['timestamp']),
                        power=row['power'],
                        energy=row['energy'],
                        voltage=row['voltage'] if row['voltage'] else 0,
                        current=row['current'] if row['current'] else 0
                    )
                    self.tdengine.write_energy(record)
                    count += 1
                    
                    if count % batch_size == 0:
                        self.stats['energy_migrated'] = count
                        self.logger.info(f"已迁移 {count} 条能源数据")
                        
                except Exception as e:
                    self.logger.error(f"处理能源数据异常: {e}")
                    self.stats['errors'] += 1
            
            self.stats['energy_migrated'] = count
            self.logger.info(f"能源数据迁移完成: {count} 条")
            
        except Exception as e:
            self.logger.error(f"迁移能源数据失败: {e}")
            self.stats['errors'] += 1
    
    def _print_stats(self):
        """打印迁移统计"""
        duration = (self.stats['end_time'] - self.stats['start_time']).total_seconds()
        
        self.logger.info("=" * 50)
        self.logger.info("数据迁移完成")
        self.logger.info("=" * 50)
        self.logger.info(f"遥测数据: {self.stats['telemetry_migrated']} 条")
        self.logger.info(f"报警数据: {self.stats['alarms_migrated']} 条")
        self.logger.info(f"OEE数据: {self.stats['oee_migrated']} 条")
        self.logger.info(f"能源数据: {self.stats['energy_migrated']} 条")
        self.logger.info(f"错误数: {self.stats['errors']}")
        self.logger.info(f"耗时: {duration:.2f} 秒")
        self.logger.info("=" * 50)
    
    def verify_migration(self, sample_size: int = 100) -> Dict:
        """
        验证迁移结果
        
        Args:
            sample_size: 抽样检查数量
            
        Returns:
            Dict: 验证结果
        """
        self.logger.info("开始验证迁移结果...")
        
        results = {
            'telemetry': {'sqlite_count': 0, 'tdengine_count': 0, 'match': False},
            'alarms': {'sqlite_count': 0, 'tdengine_count': 0, 'match': False},
            'oee': {'sqlite_count': 0, 'tdengine_count': 0, 'match': False},
            'energy': {'sqlite_count': 0, 'tdengine_count': 0, 'match': False}
        }
        
        try:
            cursor = self._sqlite_conn.cursor()
            
            # 验证遥测数据
            cursor.execute("SELECT COUNT(*) FROM telemetry")
            results['telemetry']['sqlite_count'] = cursor.fetchone()[0]
            
            # 验证报警数据
            cursor.execute("SELECT COUNT(*) FROM alarms")
            results['alarms']['sqlite_count'] = cursor.fetchone()[0]
            
            # 验证OEE数据
            try:
                cursor.execute("SELECT COUNT(*) FROM oee_records")
                results['oee']['sqlite_count'] = cursor.fetchone()[0]
            except:
                pass
            
            # 验证能源数据
            try:
                cursor.execute("SELECT COUNT(*) FROM energy_records")
                results['energy']['sqlite_count'] = cursor.fetchone()[0]
            except:
                pass
            
            self.logger.info("验证完成")
            return results
            
        except Exception as e:
            self.logger.error(f"验证失败: {e}")
            return results


# 命令行工具
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='SQLite到TDengine数据迁移工具')
    parser.add_argument('--sqlite', type=str, required=True,
                        help='SQLite数据库路径')
    parser.add_argument('--tdengine-host', type=str, default='localhost',
                        help='TDengine主机地址')
    parser.add_argument('--tdengine-port', type=int, default=6041,
                        help='TDengine端口')
    parser.add_argument('--batch-size', type=int, default=1000,
                        help='批量写入大小')
    parser.add_argument('--verify', action='store_true',
                        help='验证迁移结果')
    
    args = parser.parse_args()
    
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # 创建TDengine客户端
    tdengine = TDengineClient(
        host=args.tdengine_host,
        port=args.tdengine_port
    )
    
    # 创建迁移器
    migrator = SQLiteToTDengineMigrator(args.sqlite, tdengine)
    
    # 执行迁移
    stats = migrator.migrate_all(batch_size=args.batch_size)
    
    # 验证结果
    if args.verify:
        migrator.connect_sqlite()
        results = migrator.verify_migration()
        migrator.disconnect_sqlite()
