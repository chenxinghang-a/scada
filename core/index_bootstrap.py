"""数据库索引引导模块
====================

把"缺失的索引"在启动时幂等地补齐。

背景（P1-6）：表结构里只有主键/唯一约束自带的索引，而业务代码大量按
``device_id`` / ``timestamp`` / ``alarm_level`` / ``status`` 做 ``WHERE`` 过滤、
``ORDER BY`` 排序。数据量上来以后这些查询退化成全表扫描（``EXPLAIN QUERY PLAN``
显示 ``SCAN``），拖垮整个采集/展示链路。

``core/index_advisor.py`` 只负责"提建议"，从未被接进启动流程，建议也从未落地。
本模块把索引清单**显式固化**下来，并在 ``run.py`` 里数据库初始化完成之后调用，
保证每次启动都会把缺的索引补齐、已有的索引原样跳过。

用法::

    from core.index_bootstrap import ensure_indexes

    result = ensure_indexes(database)      # database 是 存储层.database.Database
    logger.info("索引引导完成: 新建 %d, 已存在 %d, 失败 %d",
                len(result['created']), len(result['already_existed']), len(result['failed']))

也可以直接传 ``sqlite3.Connection``。
"""

import logging
import re
import sqlite3
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 索引清单
#
# 说明：``存储层/database.py::_init_database`` 里已经有一批索引，本清单只补充
# **它没有的**索引，以及**旧库可能缺失**的兜底索引（旧库是在这些索引被写进
# schema 之前建的，例如 data/scada.db、data/scada_real.db）。
# 每条 DDL 都用 ``IF NOT EXISTS``，重复执行不会报错、不会重建。
# ---------------------------------------------------------------------------
INDEX_DDL: List[str] = [
    # ---- 新增索引（database.py 里完全没有）---------------------------------

    # 【为谁建】history_data 的"纯时间范围"扫描：
    #   - 存储层/database.py:811 cleanup_old_data  → DELETE FROM history_data WHERE timestamp < ?
    #   - 存储层/database.py:908 archive_old_data  → DELETE FROM history_data WHERE timestamp < ?
    #   - 存储层/database.py:1003 enforce_retention_policy → DELETE ... WHERE timestamp < ?
    #   - tools/capacity_planner.py:204 → SELECT COUNT(*) FROM history_data WHERE timestamp > ?
    #   - tools/auto_ops.py:203         → DELETE FROM history_data WHERE timestamp < ?
    # 【为什么缺】现有 idx_history_device_time(device_id, timestamp) 以 device_id 打头，
    #   而这几条查询只给 timestamp 条件，索引只能整段扫（实测 SCAN ... USING COVERING INDEX
    #   idx_history_device_time），history_data 是全库最大的表，代价最高。
    # 【预期收益】SCAN（全索引扫描）→ SEARCH ... USING COVERING INDEX idx_history_timestamp，
    #   由 O(N) 降为 O(log N + 命中行数)。
    'CREATE INDEX IF NOT EXISTS idx_history_timestamp ON history_data(timestamp)',

    # 【为谁建】alarm_records 的"纯时间范围"聚合：
    #   - core/report_generator.py:267 → SELECT COUNT(*) FROM alarm_records WHERE timestamp BETWEEN ? AND ?
    #   - core/report_generator.py:274 → SELECT alarm_level, COUNT(*) ... WHERE timestamp BETWEEN ? GROUP BY alarm_level
    #   - core/report_generator.py:297 → ... GROUP BY DATE(timestamp)
    #   - 报警层/alarm_kpi.py:133 _get_alarms_in_period → get_alarm_records(start_time, end_time)
    # 【为什么缺】idx_alarm_device_time 同样以 device_id 打头，时间区间查询用不上它；
    #   idx_alarm_level(alarm_level, acknowledged) 更糟 —— 实测 planner 直接
    #   SCAN alarm_records USING INDEX idx_alarm_level 扫整棵索引树。
    # 【预期收益】SCAN → SEARCH ... USING COVERING INDEX idx_alarm_timestamp。
    'CREATE INDEX IF NOT EXISTS idx_alarm_timestamp ON alarm_records(timestamp)',

    # 【为谁建】device_status 表此前**一个索引都没有**（只有主键）：
    #   查询形态为"取某设备最新状态"：WHERE device_id = ? ORDER BY timestamp DESC LIMIT 1
    #   （存储层/database.py:229 建表，展示层/websocket.py:265 的状态推送链路使用）。
    # 【为什么缺】建表语句后没有任何 CREATE INDEX。
    # 【预期收益】SCAN + USE TEMP B-TREE FOR ORDER BY → SEARCH ... USING INDEX
    #   idx_device_status_device_time (device_id=?)，同时消掉排序临时表。
    'CREATE INDEX IF NOT EXISTS idx_device_status_device_time ON device_status(device_id, timestamp)',

    # 【为谁建】"设备 + 报警级别 + 时间倒序"的过滤：
    #   - 存储层/database.py:706 get_alarm_records（device_id + alarm_level + ORDER BY timestamp DESC LIMIT）
    #   - 展示层/api/api_alarms.py:37 GET /alarms?device_id=..&alarm_level=..&limit=..
    # 【为什么缺】现有 idx_alarm_device_time(device_id, timestamp) 只吃得住 device_id 等值，
    #   alarm_level 退化为回表后逐行过滤（实测 SEARCH ... idx_alarm_device_time (device_id=?)）。
    # 【预期收益】SEARCH (device_id=?) → SEARCH (device_id=? AND alarm_level=?)，过滤条件全部下推。
    'CREATE INDEX IF NOT EXISTS idx_alarm_device_level_time ON alarm_records(device_id, alarm_level, timestamp)',

    # ---- 旧库兜底索引（database.py 的新库初始化会建，但历史库缺失）----------

    # 【为谁建】存储层/database.py:706 get_alarm_records 按 acknowledged 过滤
    #   （未确认报警列表是最热的展示查询）。
    # 【为什么缺】data/scada.db、data/scada_real.db 建于该索引加入 schema 之前，实测库里没有。
    'CREATE INDEX IF NOT EXISTS idx_alarm_device_ack ON alarm_records(device_id, acknowledged)',

    # 【为谁建】- 存储层/database.py:652 insert_alarm 的报警去重查询
    #     WHERE alarm_id=? AND device_id=? AND register_name=? AND acknowledged=0
    #   - 存储层/database.py:755 acknowledge_alarm 的确认更新（同一条件）
    # 【为什么缺】同上，旧库缺失；缺了会退化成 SCAN alarm_records。
    'CREATE INDEX IF NOT EXISTS idx_alarm_id_device_register ON alarm_records(alarm_id, device_id, register_name)',

    # 【为谁建】- 存储层/database.py:564 get_history_data（device_id + register_name + 时间区间）
    #   - 存储层/database.py:544 get_device_registers（device_id + register_name 去重排序）
    #   - tools/auto_alerts.py:330 告警基线统计
    # 【为什么缺】同上，旧库缺失（旧库只有 idx_history_register，列相同但那是后来补的，
    #   两个索引列完全一致，新库会重复建，属历史遗留，此处保持 IF NOT EXISTS 不冲突）。
    'CREATE INDEX IF NOT EXISTS idx_history_device_register_time ON history_data(device_id, register_name, timestamp)',
]


# 从 DDL 里抠出索引名，用于判断"已存在 / 新建 / 失败"
_INDEX_NAME_RE = re.compile(
    r'CREATE\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)',
    re.IGNORECASE,
)


def _index_name(ddl: str) -> str:
    """从 CREATE INDEX 语句里解析索引名（解析不出来时返回整条 DDL 的前 60 字符）。"""
    m = _INDEX_NAME_RE.search(ddl)
    return m.group(1) if m else ddl.strip()[:60]


def _existing_index_names(conn: sqlite3.Connection) -> set:
    """当前库里已经存在的索引名（含 sqlite_autoindex_*）。"""
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
    return {row[0] for row in rows}


def _apply(conn: sqlite3.Connection) -> Dict[str, Any]:
    """在给定连接上执行索引清单（调用方负责提交/回滚）。"""
    created: List[str] = []
    already_existed: List[str] = []
    failed: List[Dict[str, str]] = []

    existing = _existing_index_names(conn)

    for ddl in INDEX_DDL:
        name = _index_name(ddl)
        if name in existing:
            already_existed.append(name)
            continue
        try:
            conn.execute(ddl)
        except Exception as e:  # 单条失败不能中断其它索引
            logger.warning("创建索引 %s 失败（已跳过，不影响其它索引）: %s", name, e)
            failed.append({'index': name, 'error': str(e)})
            continue
        created.append(name)
        existing.add(name)

    return {'created': created, 'already_existed': already_existed, 'failed': failed}


def ensure_indexes(conn_or_db: Any) -> Dict[str, Any]:
    """幂等地补齐索引清单里的全部索引。

    Args:
        conn_or_db: 可以是 ``存储层.database.Database`` 实例（用它的连接池），
            也可以是裸的 ``sqlite3.Connection``。

    Returns:
        dict: ``{'created': [...], 'already_existed': [...], 'failed': [...]}``

        - ``created``: 本次真正新建的索引名列表。
        - ``already_existed``: 本次跳过（库里已有）的索引名列表。
        - ``failed``: 创建失败的 ``{'index': 名字, 'error': 原因}`` 列表；
          单条失败不会中断其它索引，也不会向外抛异常。

    Side Effects:
        在目标数据库上执行 CREATE INDEX 并提交事务；失败项写 ``logger.warning``。

    Exceptions:
        正常路径不抛异常；连接本身不可用（数据库打不开）时会把错误原样向上抛，
        因为那种情况下继续启动没有意义。
    """
    if isinstance(conn_or_db, sqlite3.Connection):
        result = _apply(conn_or_db)
        try:
            conn_or_db.commit()
        except Exception as e:
            logger.warning("索引引导：提交事务失败: %s", e)
        _log_summary(result)
        return result

    if hasattr(conn_or_db, 'get_connection'):
        # Database 实例：走它的线程本地连接池，上下文管理器负责 commit/rollback
        with conn_or_db.get_connection() as conn:
            result = _apply(conn)
        _log_summary(result)
        return result

    raise TypeError(
        "ensure_indexes() 需要 sqlite3.Connection 或带 get_connection() 的 Database 实例，"
        f"实际收到 {type(conn_or_db).__name__}"
    )


def _log_summary(result: Dict[str, Any]) -> None:
    logger.info(
        "索引引导完成: 新建 %d 个, 已存在 %d 个, 失败 %d 个",
        len(result['created']), len(result['already_existed']), len(result['failed']),
    )
    if result['created']:
        logger.info("  本次新建索引: %s", ', '.join(result['created']))
    if result['failed']:
        logger.warning("  创建失败索引: %s", ', '.join(f['index'] for f in result['failed']))
