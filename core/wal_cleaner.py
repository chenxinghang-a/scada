"""
数据库WAL自动清理
防止WAL文件无限增长，定期执行checkpoint。

使用方式:
    from core.wal_cleaner import WALCleaner
    cleaner = WALCleaner(db_path)
    cleaner.start()
"""

import os
import time
import sqlite3
import logging
import threading
from typing import Dict, Any, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class WALCleaner:
    """WAL文件自动清理器"""

    def __init__(
        self,
        db_path: str,
        check_interval: float = 300.0,  # 5分钟检查一次
        wal_size_threshold: int = 100 * 1024 * 1024,  # 100MB
        checkpoint_mode: str = 'TRUNCATE',
    ):
        self.db_path = db_path
        self.check_interval = check_interval
        self.wal_size_threshold = wal_size_threshold
        self.checkpoint_mode = checkpoint_mode
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stats = {
            'checkpoints': 0,
            'last_checkpoint': None,
            'last_wal_size': 0,
            'total_cleaned_bytes': 0,
        }

    def start(self):
        """启动自动清理"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name='wal-cleaner')
        self._thread.start()
        logger.info("WAL自动清理已启动 (间隔=%ds, 阈值=%dMB)", self.check_interval, self.wal_size_threshold // 1024 // 1024)

    def stop(self):
        """停止自动清理"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("WAL自动清理已停止")

    def _run(self):
        """主循环"""
        while self._running:
            try:
                self._check_and_clean()
            except Exception as e:
                logger.error(f"WAL清理异常: {e}")
            time.sleep(self.check_interval)

    def _check_and_clean(self):
        """检查并清理WAL"""
        wal_path = self.db_path + '-wal'
        if not os.path.exists(wal_path):
            return

        wal_size = os.path.getsize(wal_path)
        self._stats['last_wal_size'] = wal_size

        if wal_size < self.wal_size_threshold:
            return

        logger.info(f"WAL文件过大 ({wal_size // 1024 // 1024}MB)，执行checkpoint...")
        self._do_checkpoint()

    def _do_checkpoint(self):
        """执行WAL checkpoint"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute(f'PRAGMA wal_checkpoint({self.checkpoint_mode})')
            conn.close()

            self._stats['checkpoints'] += 1
            self._stats['last_checkpoint'] = time.time()

            # 计算清理了多少
            wal_path = self.db_path + '-wal'
            if os.path.exists(wal_path):
                new_size = os.path.getsize(wal_path)
                cleaned = self._stats['last_wal_size'] - new_size
                if cleaned > 0:
                    self._stats['total_cleaned_bytes'] += cleaned
                    logger.info(f"WAL checkpoint完成，清理 {cleaned // 1024}KB")
            else:
                self._stats['total_cleaned_bytes'] += self._stats['last_wal_size']
                logger.info("WAL checkpoint完成，WAL文件已清除")

        except Exception as e:
            logger.error(f"WAL checkpoint失败: {e}")

    def force_checkpoint(self) -> Dict[str, Any]:
        """强制执行checkpoint"""
        self._do_checkpoint()
        return self.get_stats()

    def get_stats(self) -> Dict[str, Any]:
        """获取统计"""
        wal_path = self.db_path + '-wal'
        wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0

        return {
            **self._stats,
            'current_wal_size': wal_size,
            'current_wal_size_mb': round(wal_size / 1024 / 1024, 2),
            'threshold_mb': round(self.wal_size_threshold / 1024 / 1024, 2),
            'running': self._running,
        }


# 全局实例
wal_cleaner: Optional[WALCleaner] = None


def init_wal_cleaner(db_path: str, **kwargs) -> WALCleaner:
    """初始化全局WAL清理器"""
    global wal_cleaner
    wal_cleaner = WALCleaner(db_path, **kwargs)
    wal_cleaner.start()
    return wal_cleaner
