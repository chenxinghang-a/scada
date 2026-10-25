"""
数据库模块
实现SQLite数据库操作（含连接池优化）
"""

import sqlite3
import logging
import threading
import time
from typing import Any, Dict
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import contextmanager
import paths
from core.safe_numeric import try_int, try_finite_float, require_positive_int

logger = logging.getLogger(__name__)


#: 分块删除的块大小。5000 行约几十毫秒锁，块间提交放锁，
#: 让并发采集写入能挤进来（round 248 实测：单条 DELETE 持锁 14 秒）。
_DELETE_CHUNK = 5000


def _delete_older_than_chunked(conn, table: str, column: str, cutoff: str) -> int:
    """分块删除 ``table`` 中 ``column < cutoff`` 的行，返回删除总数。

    与 ``core/ops_tools._delete_older_than`` 同因同法：单条 DELETE 在大表上
    整个事务持锁，把并发采集写入饿死（``database is locked``，等满
    ``busy_timeout=30000`` 后失败）。这里不跨模块复用那个函数，是为了避免
    ``存储层`` 依赖 ``core.ops_tools``（运维工具层）—— 同样的十几行，
    两边各一份比引错方向好。

    用 ``rowid IN (SELECT ... LIMIT ?)`` 而不是 ``DELETE ... LIMIT``：
    后者需要编译期开启 ``SQLITE_ENABLE_UPDATE_DELETE_LIMIT``，
    不是所有构建都带（本项目不能假设）。
    """
    total = 0
    while True:
        cursor = conn.execute(
            f"DELETE FROM {table} WHERE rowid IN ("
            f"SELECT rowid FROM {table} WHERE {column} < ? LIMIT ?)",
            (cutoff, _DELETE_CHUNK),
        )
        total += cursor.rowcount
        conn.commit()          # 每块提交：放锁，让采集写入插进来
        if cursor.rowcount < _DELETE_CHUNK:
            return total

#: 历史查询单次返回的硬上限（原始行或聚合桶都算）。
#: 没有上限时，`?start_time=2000-01-01&end_time=2030-01-01&interval=raw`
#: 会把 30 年数据一次性塞进内存并 JSON 序列化 —— 进程直接 OOM。
MAX_HISTORY_POINTS = 10000

#: 支持的聚合粒度。`''` / `'raw'` / `'none'` = 不聚合，返回原始采样点。
RAW_INTERVALS = ('', 'raw', 'none')
SUPPORTED_INTERVALS = RAW_INTERVALS + ('1min', '5min', '1hour', '1day')


#: 全局唯一的归档表名（**真源**）。
#:
#: 2026-09 审计发现同一份归档散落在三处、三个名字：
#:   - ``database.archive_old_data``      -> ``history_archive``
#:   - ``data_archive.archive_data``      -> ``history_data_archive``
#:   - ``data_lifecycle._process_table``  -> ``{table}_archive``
#: 结果是同一批数据被写进不同表，``get_archive_data`` / ``get_database_stats`` /
#: ``tools/vacuum_db.py`` / 归档测试都只认 ``history_archive``，
#: 另两张表成了查不到、删不掉的孤儿。现在只保留这一个常量，
#: 所有模块（含 ``data_archive`` / ``data_lifecycle``）都必须引用它。
#:
#: 表结构是**按天聚合**（avg/min/max/sample_count + archive_date），
#: 不是原始采样副本 —— 别再给它建 ``_raw`` 之类的第二张表。
ARCHIVE_TABLE = 'history_archive'

#: 空闲页占比告警阈值。
#:
#: 2026-09 实测：库文件膨胀到 3.3GB 而实际数据仅 67MB 时，
#: ``freelist_count / page_count`` = 0.98 —— 即 98% 的页是已删除数据的空洞。
#: 膨胀会让 SQLite 写入变慢甚至失败（表现为"采集在跑但数据进不了库"），
#: 所以超过这个比例就要告警，提示执行 VACUUM。
BLOAT_FREE_PAGE_RATIO_THRESHOLD = 0.30

#: 取"真实数据字节"的 SQL（`dbstat` 虚表）。
#: 抽成模块级常量是为了给测试一个**接缝**：把它 monkeypatch 成一条非法查询，
#: 就能在**任何**机器上复现「dbstat 不可用」这条分支
#: （CI 的 SQLite 本来就没有该虚表，本机有 —— 不接缝就没法在两种环境都测到）。
_DBSTAT_DATA_BYTES_SQL = 'SELECT SUM(pgsize) FROM dbstat'

#: 同一库的膨胀告警最小间隔（秒）。
#: ``get_database_stats`` 会被健康检查接口和 WebSocket 状态推送周期性调用
#: （最频繁 10 秒一次），没有节流的话膨胀库会每 10 秒刷一条同样的 warning，
#: 反而淹没真正的新告警 —— 而"日志噪声盖住信号"正是当初 3.3GB 膨胀
#: 迟迟没被发现的场景之一。
BLOAT_WARN_INTERVAL_SECONDS = 3600


def adapt_datetime(dt: datetime) -> str:
    """把 datetime 适配为 SQLite 文本（写入路径）。

    输出格式与 Python 3.12 之前 sqlite3 默认适配器完全一致，即
    ``'YYYY-MM-DD HH:MM:SS.ffffff'``（空格分隔、微秒为 0 时省略小数部分），
    因此库中已有字符串的格式保持不变，新旧数据可以混用、可直接比较排序。
    """
    return dt.isoformat(sep=' ')


# 显式注册适配器，替代 Python 3.12 起废弃的 sqlite3 默认 datetime 适配器。
# 注册后本进程内所有 sqlite3 连接（含本项目其它模块）写 datetime 参数时
# 都走这里，不再触发 DeprecationWarning。
sqlite3.register_adapter(datetime, adapt_datetime)


class Database:
    """
    SQLite数据库管理类
    使用线程本地连接池，避免每次操作都 open/close
    """

    #: 归档表名（类属性形式，便于调用方按实例引用；值与模块常量同源）
    ARCHIVE_TABLE = ARCHIVE_TABLE

    def __init__(self, db_path: str = 'data/scada.db'):
        # 解析为绝对路径：库文件必须落在项目根下，而不是「当前工作目录」
        # （从服务/计划任务/冻结产物启动时 CWD 并非项目根，会导致开错库或新建空库）。
        # 注意保持 str：下面第 70 行用 `self.db_path + '-wal'` 做字符串拼接。
        self.db_path = str(paths.resolve(db_path))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        # 线程本地存储：每个线程复用一个连接
        self._local = threading.local()

        # 落库层拒掉的非法记录数（内存计数）。database 走线程本地连接，
        # 没有全局锁可用，这里单独一把小锁保护一个 int。
        # 光记日志不够 —— 长期跑下来没人会去翻日志，必须在 stats 里可查。
        self._skip_stats_lock = threading.Lock()
        self._skipped_invalid_rows = 0

        # 上次膨胀告警时间（单调时钟），用于告警节流。
        #
        # ⚠️ 初值必须是 -inf（语义 =「从未告警」），**不能写 0.0**。
        # `time.monotonic()` 在 Windows 上返回的是**开机以来的秒数**
        # （GetTickCount64），不是 Unix 时间戳。写成 0.0 等于断言
        # 「在开机后第 0 秒告警过」，于是在任何开机不足
        # ``BLOAT_WARN_INTERVAL_SECONDS``(3600) 的机器上，
        # ``now - 0.0 >= 3600`` 恒为假 —— 首条膨胀告警被节流吃掉，
        # 最长静默 1 小时。CI runner 每次都是全新开机的机器，
        # 所以这条路径在 CI 上**必然**被走到。
        # 现场设备重启后同样会掉进这个窗口，等于「缺陷 7（膨胀无自动检测）」
        # 只修了一半。用 -inf 让首次检测永远通过节流。
        self._last_bloat_warn_at = float('-inf')

        # 清理残留锁（崩溃后可能遗留的 stale WAL 锁）
        self._cleanup_stale_locks()

        # 初始化数据库（用独立连接设置 WAL 等全局 PRAGMA）
        self._init_pragmas()
        self._init_database()

    def close(self):
        """关闭当前线程的数据库连接"""
        if hasattr(self._local, 'connection') and self._local.connection:
            try:
                self._local.connection.close()
            except Exception as e:
                # 关闭失败只影响本线程连接的回收，连接对象随后会被丢弃，无数据风险
                logger.debug(f"关闭数据库连接失败（连接将被丢弃）: {e}")
            self._local.connection = None

    # 向后兼容别名
    close_thread_connection = close

    def _cleanup_stale_locks(self):
        """清理崩溃后遗留的数据库锁文件（stale lock）"""
        import os
        lock_files = [self.db_path + '-wal', self.db_path + '-shm']
        for lf in lock_files:
            if os.path.exists(lf):
                try:
                    # 尝试以独占方式打开，检测是否被其他进程锁定
                    with open(lf, 'r+b') as f:
                        pass  # 能打开说明没有活跃锁
                except (PermissionError, OSError):
                    logger.warning(f"数据库锁文件可能被其他进程占用: {lf}")

    def _init_pragmas(self):
        """初始化全局 PRAGMA（只需执行一次）"""
        import time
        start = time.time()
        try:
            conn = sqlite3.connect(self.db_path, timeout=15)
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA synchronous=NORMAL')
            conn.execute('PRAGMA cache_size=-32000')  # 32MB 缓存（高吞吐场景）
            conn.execute('PRAGMA wal_autocheckpoint=1000')  # 每1000页(~4MB)自动checkpoint
            conn.execute('PRAGMA busy_timeout=10000')  # 10秒超时（不卡太久）
            conn.execute('PRAGMA temp_store=MEMORY')   # 临时表放内存
            conn.execute('PRAGMA mmap_size=268435456')  # 256MB 内存映射I/O（读密集场景提速2-5倍）
            conn.close()
            elapsed = time.time() - start
            if elapsed > 5:
                logger.warning(f"数据库 PRAGMA 初始化耗时较长: {elapsed:.1f}s，可能存在锁竞争")
        except sqlite3.OperationalError as e:
            logger.error(f"数据库 PRAGMA 初始化失败: {e}")
            logger.error("可能原因: 数据库文件被其他进程锁定，请关闭所有 Python 进程后重试")
            raise

    @contextmanager
    def get_connection(self, readonly=False):
        """
        获取数据库连接（线程本地复用 + 死锁检测）

        Args:
            readonly: True表示只读操作，不提交事务

        Yields:
            sqlite3.Connection: 数据库连接
        """
        import time
        # 每个线程复用一个连接，不再每次 open/close
        conn = getattr(self._local, 'connection', None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA busy_timeout=30000')
            conn.execute('PRAGMA synchronous=NORMAL')
            conn.execute('PRAGMA cache_size=-32000')
            conn.execute('PRAGMA temp_store=MEMORY')
            self._local.connection = conn

        # 死锁检测：记录操作开始时间
        start_time = time.time()
        operation_timeout = 60  # 操作超时60秒

        try:
            yield conn
            if not readonly:
                conn.commit()
        except Exception as e:
            # 死锁检测：检查是否是长时间阻塞
            elapsed = time.time() - start_time
            if elapsed > operation_timeout:
                logger.error(f"数据库操作超时 ({elapsed:.1f}s)，可能存在死锁: {e}")
            if not readonly:
                try:
                    conn.rollback()
                except Exception as rollback_err:
                    # 回滚失败意味着本线程连接可能残留未提交事务，后续复用会出错
                    logger.warning(
                        f"事务回滚失败，本线程连接可能残留未提交事务 "
                        f"(原始错误: {e}; 回滚错误: {rollback_err})"
                    )
            raise e

    def check_health(self) -> Dict[str, Any]:
        """检查数据库连接健康状态"""
        try:
            with self.get_connection(readonly=True) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT 1')
                result = cursor.fetchone()

                # 检查WAL状态
                cursor.execute('PRAGMA wal_checkpoint')
                wal_status = cursor.fetchone()

                return {
                    'status': 'healthy',
                    'connection': 'ok',
                    'wal_checkpoint': wal_status[0] if wal_status else 'unknown',
                    'database_path': self.db_path,
                }
        except Exception as e:
            return {
                'status': 'unhealthy',
                'error': str(e),
                'database_path': self.db_path,
            }

    def _init_database(self):
        """初始化数据库表结构"""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # ⚠️ 迁移必须放在**最前面**（round 260）：下面有一堆索引引用
            # `alarm_records` 的 `acknowledged` / `cleared` 等列，
            # 而 `CREATE TABLE IF NOT EXISTS` 遇到老库会直接跳过 ——
            # 老库缺列时先建索引会抛 "no such column"，Database 初始化失败、
            # 服务起不来。先补列、再建表/建索引才对。
            self._migrate_alarm_table(cursor)
            self._migrate_realtime_quality(cursor)

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
                    quality INTEGER DEFAULT 192,
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
                    trigger_count INTEGER DEFAULT 1,
                    last_trigger_time DATETIME,
                    last_value REAL,
                    acknowledged BOOLEAN DEFAULT 0,
                    acknowledged_at DATETIME,
                    acknowledged_by TEXT,
                    -- round 260：条件是否已恢复正常（与"是否已确认"是两件事：
                    -- 一条报警可以没被任何人确认、自己恢复了）
                    cleared BOOLEAN DEFAULT 0,
                    cleared_at DATETIME,
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
            # 表名必须用 ARCHIVE_TABLE 常量：仓储/清理/统计/tools 都按这一个名字找表
            cursor.execute(f'''
                CREATE TABLE IF NOT EXISTS {ARCHIVE_TABLE} (
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

            cursor.execute(f'''
                CREATE INDEX IF NOT EXISTS idx_archive_device_date
                ON {ARCHIVE_TABLE}(device_id, archive_date)
            ''')

            # ⚠️ 归档表必须对「设备 + 寄存器 + 日期」唯一，否则 `INSERT OR IGNORE`
            #    没有任何冲突可忽略 —— 归档任务每天跑一次，同一天会被反复插入：
            #    第 7 天归档一次、第 8 天又一次……直到第 30 天原始数据被删，
            #    同一个 (设备, 寄存器, 日期) 最多堆出 24 条重复行，
            #    归档查询返回重复点、归档表体积也涨 24 倍。
            #    历史库里可能已经堆了重复行，所以**先按组保留 id 最小的一条**再去重，
            #    否则 CREATE UNIQUE INDEX 会直接失败（启动报错）。
            cursor.execute(f'''
                DELETE FROM {ARCHIVE_TABLE}
                WHERE id NOT IN (
                    SELECT MIN(id) FROM {ARCHIVE_TABLE}
                    GROUP BY device_id, register_name, archive_date
                )
            ''')
            cursor.execute(f'''
                CREATE UNIQUE INDEX IF NOT EXISTS idx_archive_unique
                ON {ARCHIVE_TABLE}(device_id, register_name, archive_date)
            ''')

            # realtime_data 的唯一约束迁移：必须放在**表创建之后**
            # （新库的建表语句里已带 UNIQUE，这条只对老库生效）。
            self._migrate_realtime_unique(cursor)

            # 复合索引优化常见查询模式
            # （列补齐迁移已在 `_init_database` 开头做过）
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_alarm_device_ack
                ON alarm_records(device_id, acknowledged)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_alarm_id_device_register
                ON alarm_records(alarm_id, device_id, register_name)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_history_device_register_time
                ON history_data(device_id, register_name, timestamp)
            ''')

            logger.info("数据库初始化完成")

    # 预构建迁移SQL（避免f-string拼接，消除SQL注入风险）
    _MIGRATE_SQL = [
        ('trigger_count', 'ALTER TABLE alarm_records ADD COLUMN trigger_count INTEGER DEFAULT 1'),
        ('last_trigger_time', 'ALTER TABLE alarm_records ADD COLUMN last_trigger_time DATETIME'),
        ('last_value', 'ALTER TABLE alarm_records ADD COLUMN last_value REAL'),
        # round 260：确认三列原先**不在迁移清单里** —— 一旦遇到更老的库
        # （建表时还没有这三列），后面的 `CREATE INDEX ... (alarm_level,
        # acknowledged)` 会直接抛 "no such column: acknowledged"，
        # 整个 Database 初始化失败，服务起不来。补上。
        ('acknowledged', 'ALTER TABLE alarm_records ADD COLUMN acknowledged BOOLEAN DEFAULT 0'),
        ('acknowledged_at', 'ALTER TABLE alarm_records ADD COLUMN acknowledged_at DATETIME'),
        ('acknowledged_by', 'ALTER TABLE alarm_records ADD COLUMN acknowledged_by TEXT'),
        # round 260：报警「恢复」状态。此前表里根本没有"已恢复"这个概念 ——
        # 条件恢复正常时只删内存里的 alarm_states，数据库那行永远是
        # `acknowledged = 0`，于是：
        #   · 历史里每条报警看起来都"还没结束"，分不清已恢复与仍在报警；
        #   · `get_database_stats()` 的「未确认报警」是个**只增不减**的数；
        #   · 同一条报警几小时后再触发时，`insert_alarm` 会把它当成
        #     "同一条未确认记录"，只把 trigger_count +1 —— 第二次报警
        #     连开始时间都留不下来。
        ('cleared', 'ALTER TABLE alarm_records ADD COLUMN cleared BOOLEAN DEFAULT 0'),
        ('cleared_at', 'ALTER TABLE alarm_records ADD COLUMN cleared_at DATETIME'),
    ]

    def _migrate_alarm_table(self, cursor):
        """给alarm_records表添加新列（兼容旧数据库）"""
        for col_name, sql in self._MIGRATE_SQL:
            try:
                cursor.execute(sql)
                logger.info(f"数据库迁移: 添加列 alarm_records.{col_name}")
            except Exception as e:
                # 最常见原因是列已存在（预期内）；但也可能是磁盘/锁等真失败，
                # 此时迁移没做成，故把错误原文一并记录以便区分
                logger.debug(
                    f"迁移列 alarm_records.{col_name} 未执行"
                    f"（通常为列已存在，需排查时看此错误原文）: {e}"
                )

    def _migrate_realtime_quality(self, cursor):
        """给 realtime_data 表补 ``quality`` 列（兼容旧数据库）。

        **为什么需要这个迁移**：采集层早在 ``data_collector._handle_normal_collection``
        里就算出了 OPC UA 质量码（192/104/0/4/6/8/80/64），但 ``insert_data_batch``
        的 ``valid_rows`` 只取 5 个字段，把它丢掉了 → 前端 Dashboard 那套
        「质量圆点 + Good/Uncertain/Bad 三档」UI 永远拿不到数据，恒定走降级分支。

        旧库（建表时没有 quality 列）需要补列；补完默认值 192(GOOD)，
        因为**已存在的行确实无法追溯真实质量**，给 GOOD 是保守选择 ——
        宁可显示"好"也不要凭空报"坏"（后者会引发无谓的运维告警）。
        下一轮采集覆盖后即为真实值。
        """
        try:
            cursor.execute('ALTER TABLE realtime_data ADD COLUMN quality INTEGER DEFAULT 192')
            logger.info("数据库迁移: 添加列 realtime_data.quality")
        except Exception as e:
            # 列已存在属预期；其他错误原文保留以便排查
            logger.debug(
                f"迁移列 realtime_data.quality 未执行"
                f"（通常为列已存在，需排查时看此错误原文）: {e}"
            )

    def _migrate_realtime_unique(self, cursor):
        """确保 ``realtime_data`` 有 ``(device_id, register_name)`` 唯一约束。

        **为什么需要这个迁移**：建表语句里明明写了
        ``UNIQUE(device_id, register_name)``，但 ``CREATE TABLE IF NOT EXISTS``
        遇到**已存在的老表**直接跳过 —— 而 SQLite **不支持**
        ``ALTER TABLE ... ADD CONSTRAINT``，所以老库永远拿不到这个约束。

        后果不是报错，是**静默退化**：``insert_data_batch`` 里的
        ``INSERT ... ON CONFLICT(device_id, register_name) DO UPDATE``
        找不到冲突目标 → 每次都 INSERT 新行。

        实测 ``data/scada.db``（旧 schema，无 autoindex）：
        20 台设备、166 个点位，堆了 **239,815 行**，单点位最多 13,929 行。
        而 ``cleanup_old_data`` 的注释写着「realtime_data 有 UNIQUE 且走 UPSERT，
        行的数量上界 = 点位总数，根本不会随时间无界增长」—— 恰好是反的，
        于是这个表**从来不被清理**，一路膨胀到 495 MB。

        做法与归档表（``idx_archive_unique``，round 229）一致：
        先删重复（每个点位保留 ``id`` 最大的一行 = 最新），再建唯一索引。
        幂等，可反复执行；已有覆盖这两列的唯一索引（含建表自动生成的
        autoindex）时直接返回。
        """
        try:
            # 已有覆盖 (device_id, register_name) 的唯一索引 → 无需迁移
            for _seq, name, unique, _origin, _partial in cursor.execute(
                    'PRAGMA index_list(realtime_data)').fetchall():
                if not unique:
                    continue
                cols = [r[2] for r in cursor.execute(f'PRAGMA index_info("{name}")')]
                if cols == ['device_id', 'register_name']:
                    return

            before = cursor.execute('SELECT COUNT(*) FROM realtime_data').fetchone()[0]
            cursor.execute('''
                DELETE FROM realtime_data
                WHERE id NOT IN (
                    SELECT MAX(id) FROM realtime_data GROUP BY device_id, register_name
                )
            ''')
            after = cursor.execute('SELECT COUNT(*) FROM realtime_data').fetchone()[0]

            cursor.execute('''
                CREATE UNIQUE INDEX IF NOT EXISTS ux_realtime_point
                ON realtime_data(device_id, register_name)
            ''')

            logger.warning(
                "数据库迁移: realtime_data 缺少 (device_id, register_name) 唯一约束，"
                "已补建 —— 删除重复行 %d 条，保留 %d 个点位的最新值。"
                "（此前 UPSERT 退化成 INSERT，该表一直在无界增长）",
                before - after, after,
            )
        except Exception as e:
            # 迁移失败不阻塞启动：服务照常可用，只是继续膨胀。下次启动会重试。
            logger.error(
                "数据库迁移失败: 无法为 realtime_data 补建唯一约束（%s）。"
                "该表会继续无界增长，请手工排查后重启。", e,
            )

    def insert_data(self, device_id: str, register_name: str,
                    value: float, timestamp: datetime, unit: str = ''):
        """
        插入数据（单条）
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

            cursor.execute('''
                INSERT INTO realtime_data (device_id, register_name, value, unit, timestamp)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(device_id, register_name) DO UPDATE SET
                    value = excluded.value, unit = excluded.unit, timestamp = excluded.timestamp
            ''', (device_id, register_name, value, unit, timestamp))

            cursor.execute('''
                INSERT INTO history_data (device_id, register_name, value, unit, timestamp)
                VALUES (?, ?, ?, ?, ?)
            ''', (device_id, register_name, value, unit, timestamp))

    def _inc_skipped_invalid(self, n: int = 1) -> None:
        """累计「被落库层拒掉的非法记录数」"""
        with self._skip_stats_lock:
            self._skipped_invalid_rows += n

    def get_skipped_invalid_count(self) -> int:
        """被落库层拒掉的非法记录总数（进程生命周期内累计）。

        正常应为 0 且长期不增长；持续增长说明上游（采集/灌数脚本/新协议客户端）
        在往库里塞非数值，值得排查。
        """
        with self._skip_stats_lock:
            return self._skipped_invalid_rows

    def insert_data_batch(self, batch: list[dict]):
        """批量插入数据（单事务，性能比逐条快 10-50 倍）。

        在单个事务中将数据同时写入 ``realtime_data``（UPSERT，每个设备
        +寄存器只保留最新一条）和 ``history_data``（INSERT，保留全量历史）。
        无效记录（缺少必需字段）会被跳过并记录警告日志。

        Args:
            batch: 数据记录列表，每条记录为字典，需包含以下字段：
                - ``device_id`` (str): 设备 ID。
                - ``register_name`` (str): 寄存器名称。
                - ``value`` (float): 数据值。
                - ``timestamp`` (datetime): 时间戳。
                - ``unit`` (str, optional): 单位，默认空字符串。

        Returns:
            None

        Side Effects:
            - 向 ``realtime_data`` 表 UPSERT 数据。
            - 向 ``history_data`` 表 INSERT 数据。
            - 提交数据库事务。
            - 跳过无效记录时记录 ``logger.warning``。

        Exceptions:
            ``batch`` 为空时直接返回，不执行任何操作。
            单条记录字段缺失不影响其他记录的插入。
        """
        if not batch:
            return

        # Validate each record individually to avoid losing entire batch on one bad record
        valid_rows = []
        skipped_invalid = 0
        for d in batch:
            try:
                # quality 为 OPC UA 质量码（int）。采集层算出的值经此落库，
                # 前端实时数据的质量圆点才有数据源。
                # 缺省 192 = GOOD：仅当调用方没给质量码（如老代码路径、
                # 手工灌数）时才用它；**不要**在这里做"值不合理就降级"的猜测，
                # 质量判定属采集层 DataQualityAssessor 的职责。
                q = d.get('quality', 192)
                # 兜底：允许字符串形式的数字，但显式拒掉 'BAD'/'good' 这类
                # 非数值标记 —— 存进去只会让前端比较逻辑静默判错。
                q = try_int(q)
                if q is None:
                    q = 192

                # `value` 必须是**有限数值**。这是落库的最后一道关，也是最该把关
                # 的地方 —— SQLite 是**弱类型**，非法值不会报错，只会静默变味：
                #   float('nan') → 被 sqlite3 绑成 **NULL**（typeof='null'），
                #                  读回来是 None，与「没有数据」无法区分；
                #   float('inf') → 原样存成 real，趋势图画出无穷远点、
                #                  AVG/MAX 统计全被污染成 inf；
                #   'abc'        → 原样存成 **TEXT**（typeof='text'），
                #                  SUM/AVG 遇到它按 0 处理，聚合结果静默偏小。
                # 上游（采集层）已经修过两处，但上游永远修不完：手工灌数、
                # 新协议客户端、导入脚本都会绕过它。落库层不校验 = 污染无底。
                value = try_finite_float(d['value'])
                if value is None:
                    skipped_invalid += 1
                    logger.warning(
                        "跳过非数值记录: device=%r register=%r value=%r（类型 %s）",
                        d.get('device_id'), d.get('register_name'),
                        d['value'], type(d['value']).__name__,
                    )
                    continue

                valid_rows.append((
                    d['device_id'], d['register_name'], value,
                    d.get('unit', ''), d['timestamp'], q,
                ))
            except (KeyError, TypeError) as e:
                skipped_invalid += 1
                logger.warning(f"跳过无效记录: {e}")

        if skipped_invalid:
            # 计数落进 stats，让「有多少条被落库层拒了」在监控里可查 ——
            # 只有日志的话，长期跑下来没人会去翻。
            self._inc_skipped_invalid(skipped_invalid)

        if not valid_rows:
            return

        with self.get_connection() as conn:
            cursor = conn.cursor()

            # realtime_data: 批量UPSERT（保留稳定id）
            cursor.executemany('''
                INSERT INTO realtime_data (device_id, register_name, value, unit, timestamp, quality)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id, register_name) DO UPDATE SET
                    value = excluded.value, unit = excluded.unit,
                    timestamp = excluded.timestamp, quality = excluded.quality
            ''', valid_rows)

            # history_data: 批量INSERT
            cursor.executemany('''
                INSERT INTO history_data (device_id, register_name, value, unit, timestamp)
                VALUES (?, ?, ?, ?, ?)
            ''', [row[:5] for row in valid_rows])

    def delete_device_data(self, device_id: str):
        """
        删除指定设备的所有数据（实时数据 + 历史数据）
        
        Args:
            device_id: 设备ID
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM realtime_data WHERE device_id = ?', (device_id,))
            cursor.execute('DELETE FROM history_data WHERE device_id = ?', (device_id,))
            logger.info(f"已清除设备 {device_id} 的数据库数据")

    def get_realtime_data(self, device_id: str | None = None,
                          limit: int = 100) -> list[dict[str, Any]]:
        """
        获取实时数据

        Args:
            device_id: 设备ID（可选）
            limit: 返回数量限制

        Returns:
            list[dict[str, Any]]: 数据列表
        """
        with self.get_connection(readonly=True) as conn:
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
                        register_name: str | None = None) -> dict[str, Any] | None:
        """
        获取最新数据

        Args:
            device_id: 设备ID
            register_name: 寄存器名称（可选，不指定则返回所有寄存器最新值）

        Returns:
            dict[str, Any]: 最新数据（单个寄存器返回dict，所有寄存器返回{register_name: dict}）
        """
        with self.get_connection(readonly=True) as conn:
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

    def get_latest_data_all(self) -> dict[str, dict[str, dict]]:
        """
        一次性获取所有设备的最新数据（避免 N+1 查询）

        Returns:
            {device_id: {register_name: {value, timestamp, unit, ...}}}
        """
        with self.get_connection(readonly=True) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT r1.* FROM realtime_data r1
                INNER JOIN (
                    SELECT device_id, register_name, MAX(timestamp) as max_ts
                    FROM realtime_data
                    GROUP BY device_id, register_name
                ) r2 ON r1.device_id = r2.device_id
                     AND r1.register_name = r2.register_name
                     AND r1.timestamp = r2.max_ts
            ''')
            rows = cursor.fetchall()
            result = {}
            for row in rows:
                row_dict = dict(row)
                dev_id = row_dict['device_id']
                if dev_id not in result:
                    result[dev_id] = {}
                result[dev_id][row_dict['register_name']] = row_dict
            return result

    def get_device_registers(self, device_id: str) -> list[str]:
        """
        获取设备的所有寄存器名称

        Args:
            device_id: 设备ID

        Returns:
            list[str]: 寄存器名称列表
        """
        with self.get_connection(readonly=True) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT DISTINCT register_name
                FROM history_data
                WHERE device_id = ?
                ORDER BY register_name
            ''', (device_id,))
            return [row['register_name'] for row in cursor.fetchall()]

    def get_history_data(self, device_id: str, register_name: str | None,
                         start_time: datetime, end_time: datetime,
                         interval: str = '1min', limit: int = 10000,
                         offset: int = 0) -> list[dict[str, Any]]:
        """获取历史数据（支持时间聚合）。

        从 ``history_data`` 表查询指定设备和时间范围内的历史数据，
        并按指定时间间隔进行聚合（平均值、最小值、最大值、采样数）。

        Args:
            device_id: 设备 ID。
            register_name: 寄存器名称。为 ``None`` 时查询该设备所有寄存器。
            start_time: 查询起始时间（含）。
            end_time: 查询结束时间（含）。
            interval: 时间聚合间隔，支持以下值：
                - ``'1min'``: 按分钟聚合。
                - ``'5min'``: 按 5 分钟聚合。
                - ``'1hour'``: 按小时聚合。
                - ``'1day'``: 按天聚合。
                - ``''`` / ``'raw'`` / ``'none'``: **不聚合**，返回原始采样点
                  （字段为 ``timestamp`` / ``value`` / ``unit``）。
                - 其它值: 抛 ``ValueError`` —— 拼错的粒度不能静默变成
                  「按秒聚合」（那会把 10Hz 数据压成 1Hz，图表看不出来）。

        Returns:
            list[dict[str, Any]]: 聚合后的历史数据列表，每条记录包含：
                - ``time_bucket`` (str): 时间桶（格式取决于 interval）。
                - ``avg_value`` (float): 平均值。
                - ``min_value`` (float): 最小值。
                - ``max_value`` (float): 最大值。
                - ``sample_count`` (int): 采样数。

        Side Effects:
            无（只读查询，使用 ``readonly=True`` 连接）。

        Exceptions:
            不会主动抛出异常。空结果返回空列表。
        """
        interval = (interval or '').strip().lower()
        if interval not in SUPPORTED_INTERVALS:
            raise ValueError(
                f'不支持的时间聚合粒度: {interval!r}'
                f'（可选: raw / 1min / 5min / 1hour / 1day）'
            )

        with self.get_connection(readonly=True) as conn:
            cursor = conn.cursor()

            # 构造动态 WHERE：register_name 为 None 时不限制
            base_where = 'WHERE device_id = ? AND timestamp BETWEEN ? AND ?'
            params = [device_id, start_time, end_time]
            if register_name is not None:
                base_where += ' AND register_name = ?'
                params.append(register_name)

            # ---- 原始明细：不聚合 ----
            # 前端「原始」传的是空字符串；此前它落到下面的 else 分支被
            # `GROUP BY 秒` 聚合了 —— 10Hz 采样变成 1Hz 平均值，界面上却标着
            # 「原始明细（不做聚合）」，且 `sample_count` 把同秒多条压成 1 条。
            if interval in RAW_INTERVALS:
                cursor.execute(f'''
                    SELECT timestamp, value, unit
                    FROM history_data
                    {base_where}
                    ORDER BY timestamp
                    LIMIT ? OFFSET ?
                ''', params + [limit, offset])
                return [dict(row) for row in cursor.fetchall()]

            # 根据时间间隔进行数据聚合
            if interval == '1min':
                group_format = '%Y-%m-%d %H:%M:00'
            elif interval == '5min':
                # 5分钟聚合：将分钟数除以5取整
                cursor.execute(f'''
                    SELECT
                        strftime('%Y-%m-%d %H:', timestamp) ||
                        printf('%02d', (CAST(strftime('%M', timestamp) AS INTEGER) / 5) * 5) || ':00' as time_bucket,
                        AVG(value) as avg_value,
                        MIN(value) as min_value,
                        MAX(value) as max_value,
                        COUNT(*) as sample_count
                    FROM history_data
                    {base_where}
                    GROUP BY time_bucket
                    ORDER BY time_bucket
                    LIMIT ? OFFSET ?
                ''', params + [limit, offset])
                return [dict(row) for row in cursor.fetchall()]
            elif interval == '1hour':
                group_format = '%Y-%m-%d %H:00:00'
            elif interval == '1day':
                group_format = '%Y-%m-%d'
            else:
                group_format = '%Y-%m-%d %H:%M:%S'

            params.insert(0, group_format)
            cursor.execute(f'''
                SELECT
                    strftime(?, timestamp) as time_bucket,
                    AVG(value) as avg_value,
                    MIN(value) as min_value,
                    MAX(value) as max_value,
                    COUNT(*) as sample_count
                FROM history_data
                {base_where}
                GROUP BY time_bucket
                ORDER BY time_bucket
                LIMIT ? OFFSET ?
            ''', params + [limit, offset])

            return [dict(row) for row in cursor.fetchall()]

    def insert_alarm(self, alarm_id: str, device_id: str, register_name: str,
                     alarm_level: str, alarm_message: str, threshold: float,
                     actual_value: float, timestamp: datetime):
        """
        插入或更新报警记录

        同一报警（alarm_id + device_id + register_name）**仍处于活跃状态**
        （未确认 **且** 未恢复）时只更新计数和最新值，不重复插入新记录。
        确认后、或条件已恢复正常后再次触发，才新建记录。

        round 260：判据里补了 `cleared = 0` —— 否则一条「自己恢复了、没人确认」
        的报警会被后来的触发当成"同一条还没结束的记录"，只把 trigger_count +1，
        第二次报警的开始时间、持续时间全都丢失。

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

            # 查找同一报警**仍然活跃**的记录（未确认 且 未恢复）
            cursor.execute('''
                SELECT id, trigger_count FROM alarm_records
                WHERE alarm_id = ? AND device_id = ? AND register_name = ?
                  AND acknowledged = 0 AND cleared = 0
                ORDER BY timestamp DESC LIMIT 1
            ''', (alarm_id, device_id, register_name))

            existing = cursor.fetchone()

            if existing:
                # 已有未确认记录 → 更新计数和最新值
                alarm_row_id = existing['id']
                new_count = (existing['trigger_count'] or 1) + 1
                cursor.execute('''
                    UPDATE alarm_records
                    SET trigger_count = ?,
                        last_trigger_time = ?,
                        last_value = ?,
                        actual_value = ?
                    WHERE id = ?
                ''', (new_count, timestamp, actual_value, actual_value, alarm_row_id))
            else:
                # 无未确认记录 → 新建
                cursor.execute('''
                    INSERT INTO alarm_records
                    (alarm_id, device_id, register_name, alarm_level, alarm_message,
                     threshold, actual_value, timestamp, trigger_count, last_trigger_time, last_value)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ''', (alarm_id, device_id, register_name, alarm_level, alarm_message,
                      threshold, actual_value, timestamp, timestamp, actual_value))

    def get_alarm_records(self, device_id: str | None = None, alarm_level: str | None = None,
                          start_time: datetime | None = None, end_time: datetime | None = None,
                          acknowledged: bool | None = None, limit: int = 100,
                          offset: int = 0) -> list[dict[str, Any]]:
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
            list[dict[str, Any]]: 报警记录列表
        """
        with self.get_connection(readonly=True) as conn:
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

            query += ' ORDER BY timestamp DESC LIMIT ? OFFSET ?'
            params.append(limit)
            params.append(offset)

            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def acknowledge_alarm(self, alarm_id: str, acknowledged_by: str,
                          device_id: str | None = None, register_name: str | None = None) -> bool:
        """
        确认报警

        Args:
            alarm_id: 报警ID
            acknowledged_by: 确认人
            device_id: 设备ID（可选，用于精确定位）
            register_name: 寄存器名称（可选，用于精确定位）

        Returns:
            bool: 确认是否成功
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # 如果提供了device_id和register_name，使用更精确的查询
            if device_id and register_name:
                cursor.execute('''
                    UPDATE alarm_records
                    SET acknowledged = 1, acknowledged_at = ?, acknowledged_by = ?
                    WHERE alarm_id = ? AND device_id = ? AND register_name = ? AND acknowledged = 0
                ''', (datetime.now(), acknowledged_by, alarm_id, device_id, register_name))
            else:
                # 否则只使用alarm_id（向后兼容）
                cursor.execute('''
                    UPDATE alarm_records
                    SET acknowledged = 1, acknowledged_at = ?, acknowledged_by = ?
                    WHERE alarm_id = ? AND acknowledged = 0
                ''', (datetime.now(), acknowledged_by, alarm_id))

            return cursor.rowcount > 0

    def clear_alarm(self, alarm_id: str, device_id: str | None = None,
                    register_name: str | None = None,
                    cleared_at: datetime | None = None) -> bool:
        """标记报警**已恢复正常**（round 260）。

        与 `acknowledge_alarm` 是两件事：确认 = 有人看过了；恢复 = 条件已回到
        正常范围。一条报警可以没人确认就自己恢复，也可以确认之后仍在持续。

        此前**没有任何地方记录"恢复"**：条件正常时只删掉内存里的
        `alarm_states`，数据库那行永远停在 `acknowledged = 0`。后果：
        * 历史里每条报警都像"还没结束"，报表分不清活跃与历史；
        * `get_db_stats()` 的「未确认报警」只增不减；
        * 同一报警几小时后再触发会被 `insert_alarm` 当成"同一条未结束的记录"，
          只把 trigger_count +1 —— 第二次报警的开始时间直接丢掉。

        Args:
            alarm_id: 报警ID
            device_id: 设备ID（给了就精确定位，否则按 alarm_id 匹配）
            register_name: 寄存器名称
            cleared_at: 恢复时刻，缺省取当前时间

        Returns:
            bool: 是否真的更新到了记录（没有活跃记录时返回 False ——
                例如进程重启后内存里没状态，或重复清除）
        """
        cleared_at = cleared_at or datetime.now()
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if device_id and register_name:
                cursor.execute('''
                    UPDATE alarm_records
                    SET cleared = 1, cleared_at = ?
                    WHERE alarm_id = ? AND device_id = ? AND register_name = ?
                      AND cleared = 0
                ''', (cleared_at, alarm_id, device_id, register_name))
            else:
                cursor.execute('''
                    UPDATE alarm_records
                    SET cleared = 1, cleared_at = ?
                    WHERE alarm_id = ? AND cleared = 0
                ''', (cleared_at, alarm_id))
            return cursor.rowcount > 0

    def get_device_summary(self) -> list[dict[str, Any]]:
        """
        获取设备数据摘要

        Returns:
            list[dict[str, Any]]: 设备摘要列表
        """
        with self.get_connection(readonly=True) as conn:
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

    def cleanup_old_data(self, retention_days: int = 30) -> int:
        """清理超过保留期限的历史数据（**只删不归档**）。

        删除 ``history_data`` 表中时间戳早于 ``retention_days`` 天前的记录。

        **realtime_data 不按时间清理** —— 理由（2026-09 定稿，别再改回去）：
        ``realtime_data`` 上有 ``UNIQUE(device_id, register_name)`` 且写入走
        UPSERT，每个「设备×寄存器」恒占一行，行的**数量上界 = 点位总数**，
        根本不会随时间无界增长。它存的是"当前值"而不是历史样本，
        时间戳只表示"这个值多久没更新了"。按时间戳清理等于把设备列表上
        所有当前值一起抹掉 —— 前端会集体显示空白，而磁盘一个字节都省不下来。
        确实要清某台设备的当前值，走 ``delete_device_data``（按设备清）。

        ⚠️ **上面这条「上界 = 点位总数」依赖 `_migrate_realtime_unique`**。
        SQLite 不支持 ``ALTER TABLE ADD CONSTRAINT``，所以老库的
        ``realtime_data`` 一直**没有**这个唯一约束 —— UPSERT 静默退化成
        INSERT。实测某库 166 个点位堆了 239,815 行。现在启动时会自动迁移
        （去重 + 补唯一索引），迁移成功后这个前提才真的成立。
        如果日志里出现「无法为 realtime_data 补建唯一约束」，本方法的
        「不清理」决策就**不成立**，必须先去修迁移。

        职责边界（三者不要互相重叠）：
            - ``archive_old_data``：归档聚合 + 按更宽的窗口删原始数据（维护任务入口）
            - ``cleanup_old_data``：纯删除历史数据，不产生归档（本方法）
            - ``enforce_retention_policy``：history + alarm 的表级保留策略，同样不碰 realtime_data

        Args:
            retention_days: 数据保留天数，默认 30 天。

        Returns:
            int: 本次删除的 ``history_data`` 行数。
                （原来没有 return，调用方 ``data_archive.archive_data`` 拿到的
                ``deleted_from_main`` 恒为 None，等于对外报告了一条假数据。）

        Side Effects:
            - 删除 ``history_data`` 表中的过期记录。
            - 提交数据库事务。
            - 记录清理结果的日志。

        Exceptions:
            不会主动抛出异常。数据库错误会被连接上下文管理器捕获并回滚。
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # realtime_data 是"最新值"缓存，不按时间清理（理由见 docstring）
            realtime_deleted = 0

            # 清理历史数据。cutoff 必须用 isoformat(sep=' ') —— 与 adapt_datetime
            # 的写入格式一致，否则 'T' 分隔会让文本比较多删近一天数据。
            history_cutoff = datetime.now() - timedelta(days=retention_days)
            cursor.execute('''
                DELETE FROM history_data 
                WHERE timestamp < ?
            ''', (history_cutoff.isoformat(sep=' '),))

            history_deleted = cursor.rowcount

            logger.info(f"清理旧数据: 实时数据 {realtime_deleted} 条, 历史数据 {history_deleted} 条")

            return history_deleted

    def get_database_stats(self) -> dict[str, Any]:
        """
        获取数据库统计信息

        除各表行数外，还带上磁盘膨胀指标（``free_page_ratio`` 等，见
        ``get_fragmentation_stats``）。空闲页占比过高时会打 warning 日志 ——
        这是"文件 3.3GB、实际数据 67MB"那类膨胀的**自动发现**入口
        （原先只能靠人发现写入变慢后手工跑 ``tools/vacuum_db.py``）。

        Returns:
            dict[str, Any]: 统计信息
        """
        with self.get_connection(readonly=True) as conn:
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

            # 未确认报警统计（round 260：只算**仍然活跃**的 —— 已恢复的报警
            # 不该再算"当前未确认"，否则这个数只增不减）
            cursor.execute('SELECT COUNT(*) FROM alarm_records '
                           'WHERE acknowledged = 0 AND cleared = 0')
            unacknowledged_count = cursor.fetchone()[0]

            # 当前活跃报警（未恢复，不论是否已确认）
            cursor.execute('SELECT COUNT(*) FROM alarm_records WHERE cleared = 0')
            active_alarm_count = cursor.fetchone()[0]

            # 归档数据统计
            try:
                cursor.execute(f'SELECT COUNT(*) FROM {ARCHIVE_TABLE}')
                archive_count = cursor.fetchone()[0]
            except Exception as e:
                logger.debug(f"查询归档数据统计失败（可能表不存在）: {e}")
                archive_count = 0

            # 总记录数（所有表合计）
            total_records = realtime_count + history_count + alarm_count + archive_count

        # 膨胀指标（不占着只读连接跑 dbstat）
        fragmentation = self.check_bloat()

        stats = {
            'realtime_records': realtime_count,
            'history_records': history_count,
            'alarm_records': alarm_count,
            'unacknowledged_alarms': unacknowledged_count,
            'active_alarms': active_alarm_count,
            'archive_records': archive_count,
            'total_records': total_records,
        }
        stats.update(fragmentation)
        return stats

    def get_fragmentation_stats(self, include_dbstats: bool = True) -> dict[str, Any]:
        """统计库文件的膨胀程度（只读，不触发告警）。

        膨胀的根因是「删了数据但不回收页」：SQLite 的 DELETE 只把页挂到
        freelist，文件本身不会变小。所以判断膨胀看的是
        ``freelist_count / page_count``，而不是行数。

        Args:
            include_dbstats: 是否额外用 ``dbstat`` 虚表算出"真实数据字节"。
                仅在检测到疑似膨胀时建议开启（正常库没必要扫全库）；
                该虚表需要 SQLite 编译时开启 ``SQLITE_ENABLE_DBSTAT_VTAB``，
                不可用时相关字段为 None，不影响其余指标。

        Returns:
            dict[str, Any]:
                - ``database_size_mb``: 主库文件大小（不含 WAL）。
                - ``wal_size_mb``: WAL 文件大小（未 checkpoint 的增量）。
                - ``total_size_mb``: 主库 + WAL = 真实磁盘占用。
                - ``page_size`` / ``page_count`` / ``free_page_count``。
                - ``free_page_ratio``: 空闲页占比 0~1（核心膨胀指标，与 WAL 无关）。
                - ``free_bytes_mb``: 空闲页对应的字节数（= 可回收空间）。
                - ``data_bytes_mb``: 实测的数据字节。优先 ``dbstat``；不可用时退回
                  "非空闲页 × 页大小"（可移植口径，略大于 dbstat），**不再为 None**。
                - ``data_bytes_source``: 上面那个数来自哪个口径 ——
                  ``'dbstat'`` / ``'page_count'`` / ``None``（没请求或算不出）。
                  **口径必须可辨**，否则读者会以为是 dbstat 的精确值。
                - ``size_to_data_ratio``: 磁盘占用 / 实际数据字节（1 表示没有空洞，
                  越大越膨胀；3.3GB/67MB 那次约 50）。数据量取不到时为 None。
                - ``is_bloated``: 是否超过 ``BLOAT_FREE_PAGE_RATIO_THRESHOLD``。

        Side Effects:
            无（只读 PRAGMA，不改库）。
            dbstat 需扫描整库页，大库上耗时与库大小成正比。
        """
        file_size = Path(self.db_path).stat().st_size if Path(self.db_path).exists() else 0
        wal_path = Path(self.db_path + '-wal')
        wal_size = wal_path.stat().st_size if wal_path.exists() else 0

        page_size = 0
        page_count = 0
        free_pages = 0
        with self.get_connection(readonly=True) as conn:
            page_size = conn.execute('PRAGMA page_size').fetchone()[0] or 0
            page_count = conn.execute('PRAGMA page_count').fetchone()[0] or 0
            free_pages = conn.execute('PRAGMA freelist_count').fetchone()[0] or 0

            data_bytes = None
            data_bytes_source = None
            if include_dbstats:
                try:
                    data_bytes = conn.execute(_DBSTAT_DATA_BYTES_SQL).fetchone()[0]
                    if data_bytes is not None:
                        data_bytes_source = 'dbstat'
                except sqlite3.Error as e:
                    # dbstat 是编译期可选项；拿不到只是少一个更精确的口径，
                    # PRAGMA 口径（freelist 占比）仍然可用，所以降级而非报错。
                    logger.debug(f"dbstat 不可用，跳过真实数据量统计: {e}")

            # 兜底：dbstat 不可用时改用**可移植**口径 —— 非空闲页 × 页大小。
            #
            # 为什么必须有兜底：`dbstat` 需要 SQLite 编译期开启
            # `SQLITE_ENABLE_DBSTAT_VTAB`，**CI 的 runner 就没有**。
            # 没有兜底时 `data_bytes_mb` / `size_to_data_ratio` 在 CI 上恒为 None，
            # 于是依赖它们的断言只能写成 `if ... is not None:` 的**静默跳过** ——
            # 那两条指标在 CI 上**永远没被校验过**（属"覆盖缺口"，不是"通过"）。
            #
            # 口径差异（**必须如实标注，不能让读者以为是 dbstat 口径**）：
            #   非空闲页 = page_count - freelist_count，含 btree / 溢出 / pointer-map /
            #   lock-byte 页；dbstat 只统计 btree 页 → 本口径**略大**。
            #   用于 `size_to_data_ratio` 这种"量级判断"足够，精确值仍以 dbstat 为准。
            if include_dbstats and data_bytes is None and page_count:
                used_pages = max(page_count - free_pages, 0)
                if used_pages:
                    data_bytes = used_pages * page_size
                    data_bytes_source = 'page_count'

        # 空闲页占比：膨胀的**唯一判据**。
        # 不能用"文件大小 / 数据量"当判据 —— WAL 未 checkpoint 时主库文件
        # 可能远小于真实数据量（甚至为 0 字节），比值会 < 1 甚至为负。
        free_ratio = (free_pages / page_count) if page_count else 0.0
        total_size = file_size + wal_size
        size_to_data = (total_size / data_bytes) if (data_bytes and total_size) else None

        return {
            'database_size_mb': round(file_size / (1024 * 1024), 2),
            'wal_size_mb': round(wal_size / (1024 * 1024), 2),
            'total_size_mb': round(total_size / (1024 * 1024), 2),
            'page_size': page_size,
            'page_count': page_count,
            'free_page_count': free_pages,
            'free_page_ratio': round(free_ratio, 4),
            'free_bytes_mb': round(free_pages * page_size / (1024 * 1024), 2),
            'data_bytes_mb': (round(data_bytes / (1024 * 1024), 2)
                              if data_bytes is not None else None),
            'data_bytes_source': data_bytes_source,
            'size_to_data_ratio': round(size_to_data, 2) if size_to_data else None,
            'is_bloated': free_ratio >= BLOAT_FREE_PAGE_RATIO_THRESHOLD,
        }

    def check_bloat(self, threshold: float = BLOAT_FREE_PAGE_RATIO_THRESHOLD) -> dict[str, Any]:
        """检测库文件膨胀，超过阈值时打 warning 日志。

        只做"发现 + 告警"，不自动 VACUUM：VACUUM 需要独占重写整个库文件、
        期间文件体积翻倍（3.3GB 库就是再要 3.3GB 空闲磁盘），无人值守时
        静默执行的风险远大于收益。压缩留给运维/工具显式执行，
        本方法只保证"不再悄无声息"。

        Args:
            threshold: 空闲页占比告警阈值，默认 ``BLOAT_FREE_PAGE_RATIO_THRESHOLD``。

        Returns:
            dict[str, Any]: ``get_fragmentation_stats()`` 的结果。
                （疑似膨胀时才额外跑 dbstat，正常库只花 3 次 O(1) PRAGMA。）

        Side Effects:
            空闲页占比超阈值时打一条 warning。**首次检测永远不节流**，
            之后同一实例 1 小时内最多一条（见 ``BLOAT_WARN_INTERVAL_SECONDS``）。
            **不会**自动 VACUUM。
        """
        # 先用 PRAGMA 口径探一下（O(1)）；确认异常再上 dbstat 量化
        quick = self.get_fragmentation_stats(include_dbstats=False)
        if not quick['page_count']:
            # 库文件还没建成/读不到页统计，没必要继续
            return quick

        stats = quick
        if quick['free_page_ratio'] >= threshold:
            stats = self.get_fragmentation_stats(include_dbstats=True)
            now = time.monotonic()
            if now - self._last_bloat_warn_at >= BLOAT_WARN_INTERVAL_SECONDS:
                self._last_bloat_warn_at = now
                logger.warning(
                    "数据库文件膨胀: %s 中 %.1f%% 的页是空闲页"
                    "（文件 %.1fMB，其中可回收 %.1fMB，空闲页 %d/%d）"
                    "；实际数据约 %sMB。膨胀会导致写入变慢甚至失败（表现为"
                    "'采集在跑但数据进不了库'）。请执行 VACUUM 回收空间："
                    "Database.vacuum_database() 或 tools/vacuum_db.py（后者用 "
                    "VACUUM INTO 生成紧凑副本，更安全）。",
                    self.db_path,
                    stats['free_page_ratio'] * 100,
                    stats['database_size_mb'],
                    stats['free_bytes_mb'],
                    stats['free_page_count'],
                    stats['page_count'],
                    stats['data_bytes_mb'] if stats['data_bytes_mb'] is not None else '?',
                )
        return stats

    def wal_checkpoint(self):
        """手动执行WAL checkpoint（将WAL数据合并到主数据库文件）"""
        try:
            with self.get_connection() as conn:
                conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            logger.info("WAL checkpoint 完成")
        except Exception as e:
            logger.warning(f"WAL checkpoint 失败: {e}")

    def archive_old_data(self, archive_days: int = 7, delete_days: int = 30):
        """
        归档旧数据

        将超过archive_days天的历史数据按天聚合后存入归档表，
        然后删除已归档的原始数据。

        归档表是 ``ARCHIVE_TABLE``（``history_archive``，按天聚合结构），
        全项目只有这一张归档表 —— 见模块顶部 ``ARCHIVE_TABLE`` 的说明。

        Args:
            archive_days: 归档天数（默认7天前的数据归档）
            delete_days: 删除天数（默认30天前的数据删除）
        """
        # ⚠️ 归档截止时间必须**对齐到天边界**（当天 00:00）。
        #    否则「此刻减 7 天」会把一个**只过了几小时的那一天**也归档进去：
        #    第 8 天 10:00 跑归档时，第 1 天只有 00:00–10:00 的采样落在 cutoff 之前，
        #    于是归档表里存下的是「半天均值」；等第 9 天整天都过了，
        #    `INSERT OR IGNORE` 又因为已有记录而**忽略**掉完整那一天的聚合。
        #    结果：那一天的日均值永远只有半天数据，且没有任何提示。
        archive_cutoff = (datetime.now() - timedelta(days=archive_days)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        delete_cutoff = datetime.now() - timedelta(days=delete_days)

        with self.get_connection() as conn:
            cursor = conn.cursor()

            # 1. 归档：对archive_days之前的所有数据按天聚合存入归档表（INSERT OR IGNORE 防止重复归档）
            cursor.execute(f'''
                INSERT OR IGNORE INTO {ARCHIVE_TABLE}
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
                WHERE timestamp < ?
                GROUP BY device_id, register_name, DATE(timestamp)
            ''', (archive_cutoff,))

            archived_rows = cursor.rowcount

            # 2. 删除过期原始数据
            #
            # ⚠️ 必须用 `delete_cutoff`，**不能**用 `archive_cutoff`。
            # 本方法的契约是「archive_days 之前的数据归档，delete_days 之前的
            # 数据删除」（默认 7 / 30）。原先这里写的是 archive_cutoff，
            # 等于**原始采样只保留 7 天** —— 每天静默丢掉 23 天的明细，
            # 只剩 history_archive 里的日均值，无法再回溯单条采样、无法做审计。
            # `delete_cutoff` 此前算出来就是个没人用的死变量。
            cursor.execute('''
                DELETE FROM history_data
                WHERE timestamp < ?
            ''', (delete_cutoff,))

            deleted_rows = cursor.rowcount

            # 3. realtime_data 是"最新值"缓存，不按时间清理
            realtime_deleted = 0

            logger.info(f"数据归档完成: 归档 {archived_rows} 条, 删除历史 {deleted_rows} 条")

            return {
                'archived': archived_rows,
                'deleted_history': deleted_rows,
                'deleted_realtime': realtime_deleted
            }

    def get_archive_data(self, device_id: str, register_name: str,
                         start_date: str, end_date: str) -> list[dict[str, Any]]:
        """
        获取归档数据

        Args:
            device_id: 设备ID
            register_name: 寄存器名称
            start_date: 开始日期 (YYYY-MM-DD)
            end_date: 结束日期 (YYYY-MM-DD)

        Returns:
            list[dict[str, Any]]: 归档数据列表
        """
        with self.get_connection(readonly=True) as conn:
            cursor = conn.cursor()
            cursor.execute(f'''
                SELECT * FROM {ARCHIVE_TABLE}
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
                conn.commit()  # 提交任何待处理事务，VACUUM不能在事务内执行
                conn.execute('VACUUM')
            logger.info("数据库压缩完成")
            return True
        except Exception as e:
            logger.error(f"数据库压缩失败: {e}")
            return False

    def enforce_retention_policy(self, history_days: int = 30,
                                  alarm_days: int = 90) -> dict[str, int]:
        """执行按表的时间保留策略（history + alarm）。

        **本方法不清理 realtime_data** —— 这是 2026-09 定稿的统一结论，
        与 ``cleanup_old_data`` / ``archive_old_data`` 保持一致，理由：

        1. ``realtime_data`` 是"最新值缓存"（``UNIQUE(device_id, register_name)``
           + UPSERT），行数上界是点位总数，本来就不会随时间膨胀；
        2. 它没有备份、没有归档，按时间删就是**直接丢数据**：设备列表上
           所有当前值会集体消失，直到下一次采集才恢复。巡检凌晨跑一次清理，
           白天的看板就可能整片空白；
        3. 原先 ``enforce_retention_policy(realtime_hours=24)`` 会删掉
           "24 小时没更新"的点位 —— 而对于停机设备/慢采样点位，这正是
           最需要看到"最后值 + 最后更新时间"的场景。

        需要清理某台设备的当前值时走 ``delete_device_data``（按设备，不按时间）。

        Args:
            history_days: 历史数据保留天数（默认 30 天）。
            alarm_days: 报警记录保留天数（默认 90 天）。

        Returns:
            dict[str, int]: ``{'history_deleted': n, 'alarm_deleted': m}``。
                （不再返回 ``realtime_deleted``：本方法已不再删除 realtime 数据，
                留一个恒为 0 的字段只会让人以为"确实清理过"。）

        Side Effects:
            删除 ``history_data`` / ``alarm_records`` 中的过期记录并提交事务。
            删除量为 0 时不打日志。

        ⚠️ 入参校验（round 248）：``history_days`` / ``alarm_days`` 必须是
        >= 1 的整数。定时路径（``retention_scheduler``）本来就校验，
        但 ``Database`` 是公开 API，别的调用方传 0 就是一次全表删除 ——
        在**删数据的方向上**，方法自己要能站住，不能只依赖上游好心。
        """
        history_days = require_positive_int(history_days, 'history_days')
        alarm_days = require_positive_int(alarm_days, 'alarm_days')

        now = datetime.now()
        results: dict[str, int] = {}

        with self.get_connection() as conn:
            # 清理过期历史数据（cutoff 用空格分隔格式，与写入格式一致）。
            # 分块删除：单条 DELETE 在 200 万行的库上持锁 14 秒，
            # 并发采集写入等 30 秒后 ``database is locked`` 失败
            # （round 248 实测）。分块后块间放锁，采集能挤进来。
            history_cutoff = now - timedelta(days=history_days)
            results['history_deleted'] = _delete_older_than_chunked(
                conn, 'history_data', 'timestamp',
                history_cutoff.isoformat(sep=' '))

            # 清理过期报警记录
            alarm_cutoff = now - timedelta(days=alarm_days)
            results['alarm_deleted'] = _delete_older_than_chunked(
                conn, 'alarm_records', 'timestamp',
                alarm_cutoff.isoformat(sep=' '))

        total_deleted = sum(results.values())
        if total_deleted > 0:
            logger.info(f"数据保留策略执行完成: {results}")

        return results

    def enforce_archive_retention(self, days: int = 365) -> int:
        """按天聚合归档表（``history_archive``）的时间保留策略。

        为什么单独一个方法而不是塞进 ``enforce_retention_policy``
        ----------------------------------------------------------
        ``enforce_retention_policy`` 的签名与返回结构**有测试钉着**
        （``tests/test_database_operations.py``），
        而归档表的保留窗口与主表**不同量级**（主表 30/90 天、归档 365 天），
        混进同一个返回字典会让「哪一项对应哪个窗口」变得含糊。
        两者都由 ``core/retention_scheduler.py`` 统一按配置调用。

        判据用 ``archive_date``（``DATE`` 列，按天聚合的自然键），
        不是 ``timestamp`` —— 归档表没有逐条时间戳，只有聚合日期。

        Args:
            days: 归档保留天数（默认 365，与 ``配置/system.yaml`` 的
                ``database.retention.compressed_data_days`` 一致）。

        Returns:
            int: 删除的行数。

        ⚠️ ``days`` 必须 >= 1（round 248）：``days=0`` 会让 cutoff 变成今天，
        把**整个归档表**删光 —— 而归档表是原始采样删掉之后唯一还剩的东西。
        方法自己校验，不依赖上游（``retention_scheduler``）好心。
        """
        days = require_positive_int(days, 'days')
        cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()
        with self.get_connection() as conn:
            deleted = _delete_older_than_chunked(
                conn, ARCHIVE_TABLE, 'archive_date', cutoff)
        if deleted > 0:
            logger.info(f"归档表保留策略执行完成: {ARCHIVE_TABLE} 删除 {deleted} 行（保留 {days} 天）")
        return deleted

    def backup_database(self, backup_dir: str = 'data/backups') -> str | None:
        """
        备份数据库

        Args:
            backup_dir: 备份目录

        Returns:
            备份文件路径，失败返回None
        """
        import shutil
        from pathlib import Path

        try:
            backup_path = paths.resolve(backup_dir)
            backup_path.mkdir(parents=True, exist_ok=True)

            # 生成备份文件名
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            db_name = Path(self.db_path).stem
            backup_file = backup_path / f"{db_name}_{timestamp}.db"

            # 使用 sqlite3.Connection.backup() API（零SQL注入风险，热备份）
            with self.get_connection() as src_conn:
                dst_conn = sqlite3.connect(str(backup_file))
                try:
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()

            logger.info(f"数据库备份成功: {backup_file}")

            # 清理旧备份（保留最近7个）
            self._cleanup_old_backups(backup_path, db_name, keep=7)

            return str(backup_file)

        except Exception as e:
            logger.error(f"数据库备份失败: {e}")
            return None

    #: 恢复时必须存在的表（缺任何一个说明备份不是本系统的库）
    _REQUIRED_TABLES = ('realtime_data', 'history_data', 'alarm_records')

    def restore_database(self, backup_file: str, *,
                         safety_copy: bool = True) -> tuple[bool, str]:
        """从备份文件恢复数据库（round 261）。

        为什么必须补这一步：`backup_database()` 早就有了，但**从来没有恢复路径** ——
        现场出事后只能手工拷文件，而手工拷有三个坑，每一个都能把数据彻底弄丢：

        1. **备份本身可能是坏的**（备份时磁盘满、进程被杀），从没人验证过；
        2. **WAL 模式**下只拷 `.db`、留下旧的 `-wal`，SQLite 会把旧日志应用到
           新文件上 → 库直接损坏；
        3. 直接覆盖正在使用的库，替换到一半失败就**两头空**。

        所以这里按顺序做：校验备份 → 备份当前库（可选）→ 写临时文件 →
        `os.replace` 原子替换 → 清掉残留的 `-wal`/`-shm`。

        ⚠️ 调用前必须**停掉写入方**（服务/采集线程），并关闭本实例的连接；
        运行中恢复会得到"database is locked"或更糟的半新半旧状态。

        Args:
            backup_file: 备份文件路径。
            safety_copy: 是否先把当前库另存为 `<db>.pre-restore-<时间>.db`。

        Returns:
            `(是否成功, 说明)`。失败时**不动**当前库。
        """
        import os
        import shutil
        from pathlib import Path

        src = Path(backup_file)
        if not src.exists():
            return False, f'备份文件不存在: {src}'
        if src.stat().st_size == 0:
            return False, f'备份文件是空的（0 字节）: {src}'

        # 1) 备份必须能打开、完整、且有本系统的表
        try:
            probe = sqlite3.connect(f'file:{src.as_posix()}?mode=ro', uri=True)
            try:
                check = probe.execute('PRAGMA integrity_check').fetchone()[0]
                if check != 'ok':
                    return False, f'备份完整性校验失败: {check}'
                tables = {r[0] for r in probe.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                missing = [t for t in self._REQUIRED_TABLES if t not in tables]
                if missing:
                    return False, f'备份缺少必需的表: {missing}'
            finally:
                probe.close()
        except sqlite3.Error as exc:
            return False, f'备份无法读取: {exc}'

        target = Path(self.db_path)
        try:
            # 2) 先把当前库另存一份（万一新备份是"更早但更糟"的状态）
            if safety_copy and target.exists():
                stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                safety = target.with_name(f'{target.stem}.pre-restore-{stamp}{target.suffix}')
                # ⚠️ `with sqlite3.connect(...)` **不会关闭连接**（只提交事务），
                #    Windows 上留着句柄会让后面的 os.replace 报 WinError 5。
                live = sqlite3.connect(str(target))
                dst = sqlite3.connect(str(safety))
                try:
                    live.backup(dst)
                finally:
                    dst.close()
                    live.close()
                logger.warning("恢复前已保存当前库副本: %s", safety)

            # 3) 写到临时文件再原子替换 —— 替换失败时当前库仍然完好
            staging = target.with_name(target.name + '.restoring')
            shutil.copyfile(src, staging)
            # Windows 上偶发句柄未释放（杀毒/索引服务短暂持有），重试几次
            last_exc: Exception | None = None
            for attempt in range(5):
                try:
                    os.replace(staging, target)
                    last_exc = None
                    break
                except PermissionError as exc:
                    last_exc = exc
                    time.sleep(0.1 * (attempt + 1))
            if last_exc is not None:
                try:
                    staging.unlink()
                except OSError:
                    pass
                raise last_exc

            # 4) 清掉残留的 WAL/SHM：否则旧日志会被应用到新文件上，直接损坏
            for suffix in ('-wal', '-shm'):
                stale = Path(str(target) + suffix)
                if stale.exists():
                    stale.unlink()
                    logger.warning("恢复后清理了残留文件: %s", stale.name)

            logger.warning("数据库已从备份恢复: %s → %s", src, target)
            return True, f'已恢复: {src} → {target}'
        except Exception as exc:      # noqa: BLE001 —— 恢复失败必须原样报给调用方
            logger.error("数据库恢复失败: %s", exc)
            return False, f'恢复失败: {exc}'

    def _cleanup_old_backups(self, backup_dir: Path, db_name: str, keep: int = 7):
        """清理旧备份文件"""
        try:
            backups = sorted(
                backup_dir.glob(f"{db_name}_*.db"),
                key=lambda f: f.stat().st_mtime,
                reverse=True
            )

            for old_backup in backups[keep:]:
                old_backup.unlink()
                logger.debug(f"删除旧备份: {old_backup}")

        except Exception as e:
            logger.warning(f"清理旧备份失败: {e}")

    # 白名单预构建查询（消除f-string SQL拼接）
    _TABLE_SIZE_QUERIES = {
        'realtime_data': 'SELECT COUNT(*) FROM realtime_data',
        'history_data': 'SELECT COUNT(*) FROM history_data',
        'alarm_records': 'SELECT COUNT(*) FROM alarm_records',
        ARCHIVE_TABLE: f'SELECT COUNT(*) FROM {ARCHIVE_TABLE}',
        'users': 'SELECT COUNT(*) FROM users',
        'operation_logs': 'SELECT COUNT(*) FROM operation_logs',
    }

    def get_table_sizes(self) -> dict[str, int]:
        """
        获取各表的记录数

        Returns:
            dict[str, int]: 表名 -> 记录数
        """
        result = {}

        with self.get_connection(readonly=True) as conn:
            cursor = conn.cursor()
            for table, query in self._TABLE_SIZE_QUERIES.items():
                try:
                    cursor.execute(query)
                    result[table] = cursor.fetchone()[0]
                except Exception:
                    result[table] = 0

        return result
