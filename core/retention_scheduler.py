"""数据保留策略的执行与调度。

为什么需要这个模块（2026-10-10，round 209）
-------------------------------------------
`配置/system.yaml` 声明了数据保留策略：

    database:
      retention:
        raw_data_days: 30
        alarm_days: 90
        compressed_data_days: 365

`存储层/database.py` 也有实现（`enforce_retention_policy` / `enforce_archive_retention`，
docstring 完整、有测试）。**但生产路径上从来没有人调用它们，全仓也没有任何调度器**：

    $ grep -rn "enforce_retention_policy|cleanup_old_data" --include=*.py . | grep -v tests/
    core/index_bootstrap.py:46:  #   - 存储层/database.py:811 cleanup_old_data  → DELETE ...
    core/index_bootstrap.py:48:  #   - 存储层/database.py:1003 enforce_retention_policy → DELETE ...
    （两条命中都是**注释**）
    $ grep -rn "add_job|BackgroundScheduler|IntervalTrigger|CronTrigger" --include=*.py .
    （零命中）

实测后果（只读查 `data/scada_simulated.db`，679 MB）：

| 表 | 行数 | 最早 | 最晚 | 按配置应删 |
|---|---|---|---|---|
| `history_data` | 1,991,423 | 2026-09-18 | 2026-10-09 | 0 行（还没到 30 天） |
| `alarm_records` | **105,028** | **2026-05-08** | **2026-05-31** | **105,028 = 100%** |

**10.5 万条报警全部是 5 月的（约 150 天前），配置说只留 90 天却一行没删。**
按当前增速（20 天 1.99M 行 / 679 MB）约 **34 MB/天 ≈ 12 GB/年**，
而系统里有 `disk` 健康检查 —— 磁盘写满是这条链路的必然终点。

设计口径（刻意收紧）
--------------------
* **唯一真源 = `配置/system.yaml` 的 `database.retention`**（版本受控、按表分粒度）。
  刻意**不**用 `core/ops_tools.py` 的 `RuntimeConfigManager.data_retention_days` ——
  那是个单数字旋钮，表达不了「历史 30 / 报警 90 / 归档 365」，
  而且它会变成**第二个真源**（round 173 / 206 / 207 反复踩过）。
* **fail-safe，不是 fail-delete**：配置段缺失、或 `enabled` 不为真 →
  **什么都不删**，只打一条日志说明为什么没跑。删数据是不可逆操作，
  宁可「因为配置没读到所以没删」，也不要「因为读不到所以按默认值删」。
* **每次运行重新读配置**（`reload=True`）：改保留天数不需要重启。
* **每次运行记日志**（删了多少行）；删 0 行也记，否则「跑了没跑」看不出来。
* `enforce_retention_policy` 保证**不碰 `realtime_data`**（其 docstring 有完整理由：
  realtime 是「最新值缓存」，按时间删会让看板整片空白）。

用法
----
    from core.retention_scheduler import start_retention_scheduler
    start_retention_scheduler(database)      # 起一个 daemon 线程，不阻塞启动
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: 默认运行间隔（小时）。数据保留不是实时功能，6 小时足够；
#: 太频繁会让大表上的 DELETE 反复占用 IO。
DEFAULT_INTERVAL_HOURS = 6

#: 首次运行前的延迟（秒）。刻意**不**在启动瞬间就跑：
#: 启动阶段数据库刚建索引、采集线程刚起来，此时做大批量 DELETE
#: 会和启动争 IO；而且启动瞬间就跑会让「重启频繁」的部署反复触发。
DEFAULT_FIRST_DELAY_SEC = 60

#: 配置里承载保留策略的路径（`配置/system.yaml`）。
RETENTION_PATH = ("database", "retention")

#: 各窗口对应的配置键与默认值。默认值只用于「键缺失」时的兜底**提示**，
#: 不用于「整段缺失」——整段缺失时本模块**什么都不删**（见模块 docstring）。
WINDOW_KEYS = {
    "history_days": "raw_data_days",
    "alarm_days": "alarm_days",
    "archive_days": "compressed_data_days",
}

#: 兜底默认值（仅在 `enabled: true` 且个别键缺失时使用）。
WINDOW_DEFAULTS = {"history_days": 30, "alarm_days": 90, "archive_days": 365}


def read_retention_config(config_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """读取并校验保留策略配置。

    Returns:
        形如 ``{'history_days': 30, 'alarm_days': 90, 'archive_days': 365}``；
        **配置段缺失 / 未启用 / 非法**时返回 ``None``（调用方据此「什么都不做」）。
    """
    try:
        import paths
        from core.config_manager import ConfigManager

        path = config_path or paths.resolve('配置/system.yaml')
        # reload=True：每次运行重新读，改保留天数不需要重启。
        # 顺带让 `${VAR}` 占位符走 ConfigManager 的展开逻辑（与其它配置一致）。
        cfg = ConfigManager.load_yaml(path, reload=True) or {}
    except Exception as e:  # noqa: BLE001 —— 配置读不到绝不能把调度线程搞死
        logger.warning("读取保留策略配置失败（本次不执行任何删除）：%s", e)
        return None

    section = cfg
    for key in RETENTION_PATH:
        if not isinstance(section, dict) or key not in section:
            logger.info(
                "保留策略未配置（配置里没有 %s）—— 本次不执行任何删除。"
                "如需启用，请在 配置/system.yaml 里加 database.retention.enabled: true",
                ".".join(RETENTION_PATH),
            )
            return None
        section = section[key]

    if not isinstance(section, dict):
        logger.warning("database.retention 不是映射，忽略（本次不执行任何删除）")
        return None

    if not section.get("enabled", False):
        logger.info(
            "保留策略未启用（database.retention.enabled 不为真）—— 本次不执行任何删除。"
        )
        return None

    out: Dict[str, Any] = {}
    for out_key, cfg_key in WINDOW_KEYS.items():
        raw = section.get(cfg_key)
        try:
            days = int(raw)
        except (TypeError, ValueError):
            days = WINDOW_DEFAULTS[out_key]
            logger.warning(
                "保留天数 %s=%r 非法，回退默认 %d 天", cfg_key, raw, days,
            )
        if days <= 0:
            # 0/负数当成「非法」而不是「立刻删光」——删数据的方向上宁可保守。
            days = WINDOW_DEFAULTS[out_key]
            logger.warning(
                "保留天数 %s=%r 不是正数，回退默认 %d 天（不按 0 处理，避免误删全部）",
                cfg_key, raw, days,
            )
        out[out_key] = days

    return out


def enforce_once(database: Any, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    """执行一次保留策略。``cfg`` 为 None 时自己去读配置。

    Returns:
        形如 ``{'history_deleted': n, 'alarm_deleted': m, 'archive_deleted': k}``；
        **未启用/配置缺失时返回空字典**（表示「本次什么都没做」）。
    """
    if cfg is None:
        cfg = read_retention_config()
    if not cfg:
        return {}

    result: Dict[str, int] = {}
    try:
        main = database.enforce_retention_policy(
            history_days=cfg["history_days"], alarm_days=cfg["alarm_days"],
        )
        result.update(main or {})
    except Exception as e:  # noqa: BLE001 —— 主表失败不应连带归档表也不跑
        logger.error("主表保留策略执行失败：%s", e, exc_info=True)

    try:
        result["archive_deleted"] = database.enforce_archive_retention(days=cfg["archive_days"])
    except Exception as e:  # noqa: BLE001
        logger.error("归档表保留策略执行失败：%s", e, exc_info=True)

    logger.info(
        "数据保留策略执行：历史 %s 天 / 报警 %s 天 / 归档 %s 天 → 删除 %s",
        cfg["history_days"], cfg["alarm_days"], cfg["archive_days"], result,
    )
    return result


def _loop(database: Any, interval_hours: float, first_delay_sec: float,
          stop_event: threading.Event) -> None:
    if first_delay_sec > 0 and stop_event.wait(first_delay_sec):
        return
    while not stop_event.is_set():
        try:
            enforce_once(database)
        except Exception as e:  # noqa: BLE001 —— 一轮失败不能让线程退出
            logger.error("保留策略调度轮次失败（下轮继续）：%s", e, exc_info=True)
        if stop_event.wait(max(1.0, interval_hours * 3600)):
            return


def start_retention_scheduler(database: Any,
                              interval_hours: float = DEFAULT_INTERVAL_HOURS,
                              first_delay_sec: float = DEFAULT_FIRST_DELAY_SEC
                              ) -> Optional[threading.Thread]:
    """起一个 daemon 线程按间隔执行保留策略。

    ⚠️ 刻意**不**在这里做「配置是否启用」的判断 —— 判断放在每轮 ``enforce_once``
    里，这样「跑起来之后运维才去配置里打开」也能生效，不需要重启。

    返回的线程对象上挂了一个 ``stop_event`` 属性（``threading.Event``），
    置位即让调度循环退出。**测试必须用它收尾** —— 否则「留下一个活着的线程」
    会让用例在「线程恰好不是 daemon」时**挂死**而不是失败
    （round 209 实测：变异把 ``daemon=True`` 改成 ``False`` 后，
     pytest 进程卡了 600 秒才被超时掐掉，而不是干脆地报红）。

    Returns:
        起好的线程（``t.stop_event`` 可用于优雅停止）。
    """
    stop_event = threading.Event()
    t = threading.Thread(
        target=_loop, args=(database, interval_hours, first_delay_sec, stop_event),
        daemon=True, name="retention_scheduler",
    )
    #: 供调用方/测试优雅停止调度循环（daemon 线程在进程退出时也会被回收，
    #: 但「能停」让测试可以确定性地收尾，不必依赖 daemon 语义）。
    t.stop_event = stop_event  # type: ignore[attr-defined]
    t.start()
    logger.info(
        "数据保留策略调度已启动：首次延迟 %.0fs，之后每 %.1f 小时一次",
        first_delay_sec, interval_hours,
    )
    return t


__all__ = [
    "read_retention_config", "enforce_once", "start_retention_scheduler",
    "DEFAULT_INTERVAL_HOURS", "DEFAULT_FIRST_DELAY_SEC",
    "WINDOW_KEYS", "WINDOW_DEFAULTS", "RETENTION_PATH",
]
