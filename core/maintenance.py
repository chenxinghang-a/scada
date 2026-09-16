"""后台维护任务：注册与生命周期管理。

背景（2026-09-16 代码质量审计 P1-5）
------------------------------------
``core/scheduled_tasks.py`` 里的 ``task_manager`` 是一个功能完整的调度器
（注册 / 启动 / 暂停 / 状态查询），但**全项目零引用** —— 没有任何地方注册过
任务，也没有任何地方调用过 ``start_all()``。

与此同时，各模块的清理函数都写好了却同样没人调用：

| 结构 | 清理函数 | 不清理的后果 |
|---|---|---|
| ``jwt_blacklist`` | ``AuthManager.cleanup_expired_blacklist`` | 表无界增长；且查询代价随时间线性上升 |
| API 响应缓存 | ``core.api_cache.cleanup_expired`` | 内存无界增长 |
| 请求签名 nonce | ``core.request_signature.cleanup_nonce_cache`` | 内存无界增长 |
| 用户限流计数 | ``core.user_rate_limiter.cleanup_expired_entries`` | 内存无界增长 |
| WebSocket 离线队列 | ``offline_queue.cleanup_expired`` | 内存无界增长 |
| 分层缓存 | ``tiered_cache.cleanup_expired`` | 内存无界增长 |
| 历史数据 | ``Database.archive_old_data`` + ``wal_checkpoint`` | 库文件无界增长 |

``run.py`` 里原本手写了一个 ``while True: sleep(86400)`` 的裸线程做归档，
既没有停止路径，也和其他清理任务各写一套。本模块把它们统一收敛到
``task_manager`` 上，并提供 ``start_maintenance()`` / ``stop_maintenance()``。

设计约定
--------
- **依赖缺失就跳过并记日志**，不抛异常 —— 维护任务不该阻断启动。
- **可重复调用**：``start_maintenance()`` 幂等，重复调用不会起第二套线程。
- 只在真正的入口（``run.py``）里启动，不在 ``create_app()`` 里启动，
  避免测试创建应用时凭空多出一堆后台线程。
"""

import logging
from typing import Any, Callable, Dict, List, Optional

from core.scheduled_tasks import task_manager

logger = logging.getLogger(__name__)

#: 各维护任务的默认执行间隔（秒）
DEFAULT_INTERVALS: Dict[str, float] = {
    'jwt_blacklist_cleanup': 3600,        # 1 小时
    'api_cache_cleanup': 300,             # 5 分钟
    'nonce_cache_cleanup': 600,           # 10 分钟
    'rate_limiter_cleanup': 600,          # 10 分钟
    'ws_offline_queue_cleanup': 300,      # 5 分钟
    'cache_tier_cleanup': 600,            # 10 分钟
    'data_archive': 86400,                # 24 小时
}


def build_maintenance_tasks(database=None, auth_manager=None, *, archive_days: int = 7,
                            delete_days: int = 30) -> List[tuple]:
    """组装 (名称, 间隔, 说明, 可调用对象) 列表。

    依赖为 None 的任务会被跳过（并记录 debug 日志），这样调用方可以只传
    自己手头有的对象，不会因为缺一个可选组件就整套维护都跑不起来。
    """
    tasks: List[tuple] = []

    if auth_manager is not None:
        tasks.append((
            'jwt_blacklist_cleanup',
            DEFAULT_INTERVALS['jwt_blacklist_cleanup'],
            '清理过期的 JWT 黑名单条目',
            auth_manager.cleanup_expired_blacklist,
        ))

    def _api_cache_cleanup():
        from core.api_cache import cleanup_expired
        return cleanup_expired()

    def _nonce_cache_cleanup():
        from core.request_signature import cleanup_nonce_cache
        return cleanup_nonce_cache()

    def _rate_limiter_cleanup():
        from core.user_rate_limiter import cleanup_expired_entries
        return cleanup_expired_entries()

    def _ws_offline_queue_cleanup():
        from core.ws_offline_queue import offline_queue
        return offline_queue.cleanup_expired()

    def _cache_tier_cleanup():
        # 注意：TieredCache 上的方法是 cleanup()；
        # cleanup_expired() 属于内部的 MemoryCache，不能在这里调。
        from core.cache_tier import tiered_cache
        return tiered_cache.cleanup()

    tasks.extend([
        ('api_cache_cleanup', DEFAULT_INTERVALS['api_cache_cleanup'],
         '清理过期的 API 响应缓存', _api_cache_cleanup),
        ('nonce_cache_cleanup', DEFAULT_INTERVALS['nonce_cache_cleanup'],
         '清理过期的请求签名 nonce', _nonce_cache_cleanup),
        ('rate_limiter_cleanup', DEFAULT_INTERVALS['rate_limiter_cleanup'],
         '清理过期的用户限流计数', _rate_limiter_cleanup),
        ('ws_offline_queue_cleanup', DEFAULT_INTERVALS['ws_offline_queue_cleanup'],
         '清理过期的 WebSocket 离线消息', _ws_offline_queue_cleanup),
        ('cache_tier_cleanup', DEFAULT_INTERVALS['cache_tier_cleanup'],
         '清理过期的分层缓存', _cache_tier_cleanup),
    ])

    if database is not None:
        def _data_archive():
            result = database.archive_old_data(
                archive_days=archive_days, delete_days=delete_days)
            logger.info("自动归档完成: %s", result)
            database.wal_checkpoint()
            return result

        tasks.append((
            'data_archive',
            DEFAULT_INTERVALS['data_archive'],
            f'归档 {archive_days} 天前数据并删除 {delete_days} 天前数据 + WAL checkpoint',
            _data_archive,
        ))

    return tasks


def register_maintenance_tasks(database=None, auth_manager=None, *,
                               archive_days: int = 7, delete_days: int = 30,
                               intervals: Optional[Dict[str, float]] = None
                               ) -> List[str]:
    """把所有维护任务注册到 ``task_manager``（不启动）。

    Args:
        database: ``存储层.database.Database`` 实例，用于归档任务；为 None 则跳过。
        auth_manager: ``用户层.auth.AuthManager`` 实例，用于黑名单清理；为 None 则跳过。
        archive_days: 归档阈值（天）。
        delete_days: 删除阈值（天）。
        intervals: 覆盖默认间隔，形如 ``{'api_cache_cleanup': 60}``。

    Returns:
        已注册的任务名列表。
    """
    overrides = intervals or {}
    names: List[str] = []
    for name, interval, description, func in build_maintenance_tasks(
            database, auth_manager, archive_days=archive_days,
            delete_days=delete_days):
        task_manager.add(name, func, overrides.get(name, interval), description)
        names.append(name)

    skipped = set(DEFAULT_INTERVALS) - set(names)
    if skipped:
        logger.info("维护任务跳过（依赖未提供）: %s", ', '.join(sorted(skipped)))
    logger.info("已注册 %d 个维护任务: %s", len(names), ', '.join(names))
    return names


def start_maintenance(database=None, auth_manager=None, *,
                      archive_days: int = 7, delete_days: int = 30,
                      intervals: Optional[Dict[str, float]] = None) -> List[str]:
    """注册并启动全部维护任务（幂等）。

    重复调用不会产生第二套线程 —— 已处于 running 状态的任务会被
    ``TaskManager.start`` 直接跳过。
    """
    names = register_maintenance_tasks(
        database, auth_manager, archive_days=archive_days,
        delete_days=delete_days, intervals=intervals)
    started = [n for n in names if task_manager.start(n)]
    logger.info("维护任务已启动: %s", ', '.join(started) or '（无）')
    return started


def stop_maintenance() -> List[str]:
    """停止全部维护任务（幂等）。"""
    status = task_manager.get_status()
    stopped = [name for name, info in status.items()
               if info.get('status') in ('running', 'paused')]
    task_manager.stop_all()
    logger.info("维护任务已停止: %s", ', '.join(stopped) or '（无）')
    return stopped


def get_maintenance_status() -> Dict[str, Any]:
    """返回全部维护任务的状态（供运维接口使用）。"""
    return task_manager.get_status()


def run_task_now(name: str) -> bool:
    """立即执行指定维护任务一次（供运维接口使用）。"""
    return task_manager.run_now(name)
