"""
断线缓存与自动补传
==================

当 TDengine 不可用时，将数据缓存到本地 SQLite。
连接恢复后自动批量补传，确保数据不丢失。

用法:
    buffer = OfflineBuffer(db_path="data/offline_buffer.db")
    buffer.start()

    # 写入数据（自动判断走 TDengine 还是缓存）
    buffer.write(records)

    # 停止时刷新剩余数据
    buffer.stop()
"""

import json
import time
import sqlite3
import logging
import threading
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


class OfflineBuffer:
    """断线缓存：TDengine 不可用时存本地，恢复后自动补传"""

    def __init__(self, tdengine_client=None, db_path: str = "data/offline_buffer.db",
                 max_buffer_size: int = 100000, flush_interval: float = 10.0,
                 batch_size: int = 500):
        """
        Args:
            tdengine_client: TDengineClient 实例
            db_path: 本地缓存数据库路径
            max_buffer_size: 最大缓存条数（超出则丢弃最旧数据）
            flush_interval: 补传检查间隔(秒)
            batch_size: 每次补传的批量大小
        """
        self.tdengine = tdengine_client
        self.db_path = db_path
        self.max_buffer_size = max_buffer_size
        self.flush_interval = flush_interval
        self.batch_size = batch_size

        self._running = False
        self._flush_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # 统计
        self.stats = {
            'buffered': 0,
            'flushed': 0,
            'dropped': 0,
            'errors': 0,
            'current_buffer_size': 0,
            'online': True,
            'last_flush_time': None,
        }

        # 初始化本地数据库
        self._init_db()

    def _init_db(self):
        """初始化本地 SQLite 缓存表"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                table_type TEXT NOT NULL,
                data TEXT NOT NULL,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_created_at ON pending_records(created_at)
        """)
        # 统计当前缓存量
        cursor = conn.execute("SELECT COUNT(*) FROM pending_records")
        self.stats['current_buffer_size'] = cursor.fetchone()[0]
        conn.close()
        logger.info(f"离线缓存初始化: {self.db_path}, 当前缓存 {self.stats['current_buffer_size']} 条")

    def start(self):
        """启动补传线程"""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True, name="offline-flush")
        self._flush_thread.start()
        logger.info("断线缓存补传线程已启动")

    def stop(self):
        """停止并尝试刷新"""
        self._running = False
        self._stop_event.set()
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=10)
        # 最后尝试刷新一次
        self._try_flush()

    def buffer_records(self, table_type: str, records: list[Any]):
        """
        将记录存入本地缓存

        Args:
            table_type: 表类型 (telemetry, alarm, oee, energy, predictive)
            records: 序列化后的记录列表（dict 或可 JSON 序列化的对象）
        """
        conn = sqlite3.connect(self.db_path)
        now = time.time()
        try:
            for record in records:
                data = json.dumps(record, default=str, ensure_ascii=False)
                conn.execute(
                    "INSERT INTO pending_records (table_type, data, created_at) VALUES (?, ?, ?)",
                    (table_type, data, now)
                )
            conn.commit()
            self.stats['buffered'] += len(records)
            self._update_buffer_size(conn)

            # 超限淘汰
            if self.stats['current_buffer_size'] > self.max_buffer_size:
                self._evict_old(conn)
        except Exception as e:
            logger.error(f"缓存写入失败: {e}")
            self.stats['errors'] += 1
        finally:
            conn.close()

    def _evict_old(self, conn):
        """淘汰最旧的数据"""
        excess = self.stats['current_buffer_size'] - self.max_buffer_size
        conn.execute(
            "DELETE FROM pending_records WHERE id IN (SELECT id FROM pending_records ORDER BY created_at ASC LIMIT ?)",
            (excess,)
        )
        conn.commit()
        self.stats['dropped'] += excess
        self._update_buffer_size(conn)
        logger.warning(f"缓存超限，淘汰 {excess} 条旧数据")

    def _update_buffer_size(self, conn=None):
        """更新缓存大小统计"""
        should_close = False
        if conn is None:
            conn = sqlite3.connect(self.db_path)
            should_close = True
        cursor = conn.execute("SELECT COUNT(*) FROM pending_records")
        self.stats['current_buffer_size'] = cursor.fetchone()[0]
        if should_close:
            conn.close()

    def _flush_loop(self):
        """补传主循环"""
        while self._running and not self._stop_event.is_set():
            try:
                self._try_flush()
            except Exception as e:
                logger.error(f"补传异常: {e}")
            self._stop_event.wait(self.flush_interval)

    def _try_flush(self):
        """尝试将缓存数据补传到 TDengine"""
        if not self.tdengine:
            return

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                "SELECT id, table_type, data FROM pending_records ORDER BY created_at ASC LIMIT ?",
                (self.batch_size,)
            )
            rows = cursor.fetchall()

            if not rows:
                self.stats['online'] = True
                return

            # 尝试写入 TDengine
            success_ids = []
            fail_ids = []

            for row_id, table_type, data_json in rows:
                try:
                    record = json.loads(data_json)
                    self._write_to_tdengine(table_type, record)
                    success_ids.append(row_id)
                except Exception as e:
                    logger.debug(f"补传单条失败 (id={row_id}): {e}")
                    fail_ids.append(row_id)
                    break  # 连接可能断了，停止批量

            # 删除成功的记录
            if success_ids:
                placeholders = ','.join('?' * len(success_ids))
                conn.execute(f"DELETE FROM pending_records WHERE id IN ({placeholders})", success_ids)
                conn.commit()
                self.stats['flushed'] += len(success_ids)
                self.stats['online'] = True
                logger.info(f"补传成功 {len(success_ids)} 条")

            if fail_ids:
                self.stats['online'] = False
                self.stats['errors'] += 1

            self._update_buffer_size(conn)

        except Exception as e:
            logger.error(f"补传流程异常: {e}")
            self.stats['online'] = False
            self.stats['errors'] += 1
        finally:
            conn.close()

    def _write_to_tdengine(self, table_type: str, record: dict):
        """根据类型写入 TDengine"""
        from .data_models import TelemetryRecord, AlarmRecord, OEERecord, EnergyRecord, PredictiveRecord

        if table_type == 'telemetry':
            r = TelemetryRecord(
                device_id=record['device_id'],
                register_name=record['register_name'],
                timestamp=datetime.fromisoformat(record['timestamp']),
                value=record['value'],
                quality=record.get('quality', 192),
                unit=record.get('unit', ''),
                protocol=record.get('protocol', ''),
                gateway_id=record.get('gateway_id', '')
            )
            self.tdengine.write_telemetry(r)
        elif table_type == 'alarm':
            r = AlarmRecord(**record)
            self.tdengine.write_alarm(r)
        elif table_type == 'oee':
            r = OEERecord(**record)
            self.tdengine.write_oee(r)
        elif table_type == 'energy':
            r = EnergyRecord(**record)
            self.tdengine.write_energy(r)
        elif table_type == 'predictive':
            r = PredictiveRecord(**record)
            self.tdengine.write_predictive(r)
        else:
            logger.warning(f"未知表类型: {table_type}")

    def get_stats(self) -> dict:
        """获取统计信息"""
        self._update_buffer_size()
        return self.stats.copy()

    def clear_buffer(self):
        """清空缓存（慎用）"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM pending_records")
        conn.commit()
        conn.close()
        self.stats['current_buffer_size'] = 0
        logger.warning("离线缓存已清空")
