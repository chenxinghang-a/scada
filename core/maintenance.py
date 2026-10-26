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
| 请求签名 nonce | ``core.request_signature.cleanup_nonce_cache`` | 内存无界增长 |
| 用户限流计数 | ``core.user_rate_limiter.cleanup_expired_entries`` | 内存无界增长 |
| WebSocket 离线队列 | ``offline_queue.cleanup_expired`` | 内存无界增长 |
| 历史数据 | ``Database.archive_old_data`` + ``wal_checkpoint`` | 库文件无界增长 |

（``core.api_cache`` 与 ``core.cache_tier`` 两套缓存实现已于 round 247 删除：
两者都**只有自己的清理函数被这里调用过**，没有任何业务代码写入或读取，
属于「清理一个永远为空的缓存」的假维护。真需要 API 缓存时，应带着真实
消费方一起加进来，而不是先留一套没人用的实现。）

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
    'nonce_cache_cleanup': 600,           # 10 分钟
    'rate_limiter_cleanup': 600,          # 10 分钟
    'ws_offline_queue_cleanup': 300,      # 5 分钟
    'performance_metrics': 300,           # 5 分钟（性能历史采样）
    'data_archive': 86400,                # 24 小时
    'data_freshness_watch': 30,           # 30 秒（采集中断看门狗）
    'device_config_watch': 10,            # 10 秒（devices.yaml 热重载）
    'backup_restore_drill': 86400,        # 24 小时（备份恢复演练）
}


def build_maintenance_tasks(database=None, auth_manager=None, *,
                            archive_days: int = 7, delete_days: int = 30,
                            alarm_manager=None, device_manager=None,
                            data_collector=None) -> List[tuple]:
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

    def _nonce_cache_cleanup():
        from core.request_signature import cleanup_nonce_cache
        return cleanup_nonce_cache()

    def _rate_limiter_cleanup():
        from core.user_rate_limiter import cleanup_expired_entries
        return cleanup_expired_entries()

    def _ws_offline_queue_cleanup():
        from core.ws_offline_queue import offline_queue
        return offline_queue.cleanup_expired()

    def _performance_metrics_sample():
        """性能指标采样 —— 写 `metrics/metrics.jsonl`（round 252）。

        为什么需要它：该文件此前**只有 `tools/auto_metrics.py`（独立 CLI）会写**，
        而生产环境没有任何地方调用那个 CLI（无 cron / 计划任务 / 维护任务）。
        于是 `/api/performance/metrics/history` 永远 `samples: 0`、
        `/summary` 永远「无历史数据」，前端性能页的历史曲线与摘要面板
        **在生产永远是空的** —— 读取侧完好、写入侧不存在。

        采样本身很轻（psutil + 文件统计），5 分钟一次；降级到 EMERGENCY 时
        也照跑 —— 性能数据恰是排障时最需要的东西。
        """
        from 展示层.api.api_performance import collector
        lines = collector.append_sample()
        if lines < 0:
            raise RuntimeError('性能指标采样失败（详见 ERROR 日志）')
        return {'metrics_lines': lines}

    tasks.extend([
        ('nonce_cache_cleanup', DEFAULT_INTERVALS['nonce_cache_cleanup'],
         '清理过期的请求签名 nonce', _nonce_cache_cleanup),
        ('rate_limiter_cleanup', DEFAULT_INTERVALS['rate_limiter_cleanup'],
         '清理过期的用户限流计数', _rate_limiter_cleanup),
        ('ws_offline_queue_cleanup', DEFAULT_INTERVALS['ws_offline_queue_cleanup'],
         '清理过期的 WebSocket 离线消息', _ws_offline_queue_cleanup),
        ('performance_metrics', DEFAULT_INTERVALS['performance_metrics'],
         '采样性能指标（写 metrics.jsonl，供 /performance/metrics/history 读取）',
         _performance_metrics_sample),
    ])

    if device_manager is not None:
        # 跨轮次保存的 mtime 基线（放闭包里，任务重启即重新建立基线）
        _device_cfg_state: Dict[str, Any] = {}

        def _device_config_watch():
            """设备配置文件自动热重载（round 285）。

            round 283 给了 `POST /api/devices/reload`，但那要**人去点一下**。
            现场改设备清单（加一台仪表、停一台旧设备）本质是"改配置 → 生效"，
            不该要求运维记得调接口、更不该重启进程。

            敢做成自动，是因为 round 284 的 fail-closed 守卫：
            写坏/清空的配置会被拒绝并保留原表，**最坏情况只是这次改动不生效**。
            """
            from core.device_config_watch import watch_once
            return watch_once(device_manager, data_collector,
                              state=_device_cfg_state)

        tasks.append((
            'device_config_watch',
            DEFAULT_INTERVALS['device_config_watch'],
            '监视 devices.yaml 的 mtime，变化即重载配置并同步采集任务',
            _device_config_watch,
        ))

    if database is not None and alarm_manager is not None:
        def _data_freshness_watch():
            """采集中断看门狗（round 263）。

            为什么不能只靠 `/api/health/status`：那条检查是**全局**口径
            （`MAX(timestamp)` 全库最新一条），25 台设备里只要还有一台在写就
            一直"新鲜"，**单台设备失联完全发现不了**；而且它只改 JSON 里的
            状态，不进报警历史、不响铃、不推送。

            这里按设备看，超时就走 `raise_external_alarm`（与其它报警同一条
            链路：去重 → 落库 → 声光 → 广播 → WebSocket → 升级），恢复自动清除。
            """
            # 刻意**不**做降级跳过：降级往往正是因为采集出了问题，
            # 这时候更需要知道"是哪台设备断了"。本任务只做一条聚合查询 + 状态比较。
            from core.freshness_watch import run_once
            # 传当前设备清单：设备删了历史还在，不传的话它会被永久判为"陈旧"，
            # 那条 data_stale_<dev> 报警**永远不会恢复**（幽灵报警）。
            known = None
            if device_manager is not None:
                try:
                    known = set(device_manager.get_all_devices().keys())
                except Exception as exc:      # noqa: BLE001
                    logger.debug("获取设备清单失败，按历史数据判断: %s", exc)
            result = run_once(database, alarm_manager, known_devices=known)
            if result['raised'] or result['cleared'] or result.get('cleared_ghosts'):
                logger.warning("采集新鲜度看门狗: 新失联 %s，恢复 %s，已删设备清理 %s",
                               result['raised'], result['cleared'],
                               result.get('cleared_ghosts'))
            return result

        tasks.append((
            'data_freshness_watch',
            DEFAULT_INTERVALS['data_freshness_watch'],
            '按设备检查采集中断并报警（全局口径发现不了单台失联）',
            _data_freshness_watch,
        ))

    if database is not None:
        def _backup_restore_drill():
            """每日备份恢复演练（round 273）。

            备份"有没有"和"能不能用"是两件事。生产上没人每天去点一次恢复，
            于是坏备份/备份停了/备份是空库这三种情况，**只会在真要恢复的那天**
            才暴露。这里每天只读校验一次最新备份（完整性 + 必需表 + 行数 +
            新鲜度），失败就报警。
            """
            from core.backup_drill import run_drill
            result = run_drill(database=database, alarm_manager=alarm_manager)
            if not result['ok']:
                logger.error("备份恢复演练未通过: %s", result['problems'])
            return result

        tasks.append((
            'backup_restore_drill',
            DEFAULT_INTERVALS['backup_restore_drill'],
            '每日校验最新备份可用（完整性/必需表/行数/新鲜度），失败报警',
            _backup_restore_drill,
        ))

        def _data_archive():
            # 降级到 EMERGENCY 时跳过归档：归档是「跨天范围删除 + WAL checkpoint」
            # 这类重操作，恰恰是在数据库已经吃紧时最不该跑的东西。
            # 跳过不等于丢数据 —— 历史数据还在，恢复 NORMAL 后下一次调度会补上。
            from core.degradation_manager import degradation_manager
            if not degradation_manager.is_feature_enabled('data_archive'):
                logger.info(
                    "系统处于 %s 降级状态，跳过本轮数据归档",
                    degradation_manager.level_name,
                )
                return {'skipped': 'degradation', 'feature': 'data_archive'}

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
                               intervals: Optional[Dict[str, float]] = None,
                               alarm_manager=None, device_manager=None,
                               data_collector=None) -> List[str]:
    """把所有维护任务注册到 ``task_manager``（不启动）。

    Args:
        database: ``存储层.database.Database`` 实例，用于归档任务；为 None 则跳过。
        auth_manager: ``用户层.auth.AuthManager`` 实例，用于黑名单清理；为 None 则跳过。
        archive_days: 归档阈值（天）。
        delete_days: 删除阈值（天）。
        intervals: 覆盖默认间隔，形如 ``{'nonce_cache_cleanup': 60}``。

    Returns:
        已注册的任务名列表。
    """
    overrides = intervals or {}
    names: List[str] = []
    for name, interval, description, func in build_maintenance_tasks(
            database, auth_manager, archive_days=archive_days,
            delete_days=delete_days, alarm_manager=alarm_manager,
            device_manager=device_manager):
        task_manager.add(name, func, overrides.get(name, interval), description)
        names.append(name)

    skipped = set(DEFAULT_INTERVALS) - set(names)
    if skipped:
        logger.info("维护任务跳过（依赖未提供）: %s", ', '.join(sorted(skipped)))
    logger.info("已注册 %d 个维护任务: %s", len(names), ', '.join(names))
    return names


def start_maintenance(database=None, auth_manager=None, *,
                      archive_days: int = 7, delete_days: int = 30,
                      intervals: Optional[Dict[str, float]] = None,
                      alarm_manager=None, device_manager=None,
                      data_collector=None) -> List[str]:
    """注册并启动全部维护任务（幂等）。

    重复调用不会产生第二套线程 —— 已处于 running 状态的任务会被
    ``TaskManager.start`` 直接跳过。
    """
    names = register_maintenance_tasks(
        database, auth_manager, archive_days=archive_days,
        delete_days=delete_days, intervals=intervals,
        alarm_manager=alarm_manager, device_manager=device_manager,
        data_collector=data_collector)
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
