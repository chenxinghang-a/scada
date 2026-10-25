"""
结构化日志配置 - 等保2.0要求
GB/T 22239: 安全审计日志必须可机器解析

增强功能：
- 请求ID追踪
- 用户上下文
- 性能指标
- 业务事件标记
"""
import sys
import json
import logging
import threading
import paths
from datetime import datetime
from typing import Optional, Dict, Any
from contextvars import ContextVar

# 上下文变量（用于跨函数传递追踪信息）
request_id_var: ContextVar[str] = ContextVar('request_id', default='')
user_id_var: ContextVar[str] = ContextVar('user_id', default='')
device_id_var: ContextVar[str] = ContextVar('device_id', default='')

# 日志上下文管理器
_log_context = threading.local()


def set_log_context(**kwargs):
    """设置日志上下文"""
    for key, value in kwargs.items():
        setattr(_log_context, key, value)


def get_log_context() -> Dict[str, Any]:
    """获取日志上下文"""
    context = {}
    for key in ['request_id', 'user_id', 'device_id', 'action', 'target']:
        value = getattr(_log_context, key, None)
        if value:
            context[key] = value
    return context


def clear_log_context():
    """清除日志上下文"""
    for key in ['request_id', 'user_id', 'device_id', 'action', 'target']:
        if hasattr(_log_context, key):
            delattr(_log_context, key)


class InterceptHandler(logging.Handler):
    """把**标准 logging** 的记录转发进 loguru。

    为什么必须有它（2026-10-10，round 212）
    --------------------------------------
    `setup_logging()` 只配置了 **loguru** 自己的 sink；而仓库里**大量模块**用的是
    **标准 logging**（`logging.getLogger(__name__)`）—— 两者之间**没有任何桥**。
    那些日志落进标准 logging 的 root logger，而 root **没有 handler**
    → Python 退回 `logging.lastResort`，它**只输出 WARNING 及以上**
    → **所有 INFO/DEBUG 被静默丢弃**。

    实测证据（wb 在**部署版**上查的）：`scada_2026-10-10.log` 305 行里
    `module` 字段只有 `[run×300, structured_logging×5]` ——
    连 `config_manager` 的「加载配置文件」（标准 logging INFO）都 **0 命中**。

    最严重的后果：**数据保留策略（会删数据）的日志全部不可见** ——
    「调度已启动」/「执行：…删除 {…}」/「未启用」一个都看不到，
    跑没跑、删了什么、删了多少，运维**无从审计**（高危行为必须可审计）。

    实现用 loguru 官方推荐姿势：从标准 logging 的 record 取真实调用点（`depth`），
    保留异常信息；**自身绝不抛**（日志桥把应用搞挂是最糟的结果）。
    """

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            from loguru import logger as _loguru

            try:
                level = _loguru.level(record.levelname).name
            except (ValueError, AttributeError, KeyError):
                level = record.levelno

            # 调用点**直接取自标准 logging 的 record**，不猜 depth。
            #
            # ⚠️ loguru 官方那套 `depth` 算术在这里不可靠 —— 实测两种写法都错过：
            #    `depth=2` 起手（官方版本）→ 日志指向 logging 包内部（实测落在其
            #    `__init__.py` 的第 1737 行）；「一路 f_back 直到走出 logging」
            #    → 指向 `<unknown>` 第 0 行。
            #    原因是中间隔了几层 logging 内部帧（Logger.info → _log → handle →
            #    callHandlers → Handler.handle）会随调用方式变化。
            #    而标准 record 里 `module` / `funcName` / `lineno` **本来就是准的** ——
            #    直接 patch 上去，零算术、零猜测。
            _loguru.patch(lambda r: r.update(
                name=record.name,
                module=record.module,
                function=record.funcName,
                line=record.lineno,
            )).opt(exception=record.exc_info).log(level, record.getMessage())
        except Exception as e:  # noqa: BLE001 —— 日志桥自己绝不能抛
            # 兜底：桥接失败**不能**让「被桥接的那条日志调用」炸掉业务代码。
            # 但也**不能静默** —— 本仓库最忌「静默假成功」（`tests/test_silent_exception_guard.py`
            # 就是钉这条的守卫，它抓到过这里的第一版）。
            # 留痕走 loguru 原始 sink（不经桥接，不会递归）；loguru 自己也挂了
            # 才退到 stderr。两级都失败才会真的没有痕迹，而那已经不属于「静默」。
            try:
                from loguru import logger as _lg
                _lg.opt(exception=e).debug("标准 logging 桥接失败，该条日志已丢弃: {}", e)
            except Exception as inner:  # noqa: BLE001
                print(f"[structured_logging] 桥接失败（loguru 亦不可用）: {inner}",
                      file=sys.stderr)


#: 噪声较大的第三方 logger：桥接后只放 WARNING 及以上。
#: 不设的话 werkzeug（每个 HTTP 请求一行）、engineio/socketio（每条心跳）等
#: 会把 INFO 刷满日志，把真正有价值的行淹掉 —— 那就等于又不可读了。
_NOISY_STDLIB_LOGGERS = (
    'werkzeug', 'engineio', 'socketio', 'urllib3', 'pika',
    'apscheduler', 'asyncio', 'matplotlib', 'PIL',
)


def _install_stdlib_logging_bridge(log_level: str = 'INFO') -> None:
    """把标准 logging 桥接进 loguru（**幂等**）。

    重复调用不会叠加 handler —— 否则一条日志会被输出多次。
    """
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not isinstance(h, InterceptHandler)]
    root.addHandler(InterceptHandler())
    try:
        root.setLevel(str(log_level).upper())
    except (ValueError, TypeError):
        root.setLevel(logging.INFO)
    for name in _NOISY_STDLIB_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def setup_logging(
    log_dir: str = "logs",
    log_level: str = "INFO",
    json_format: bool = True,
    rotation: str = "100 MB",
    retention: str = "30 days",
    compression: str = "gz",
    enable_console: bool = True,
    enable_file: bool = True,
):
    """配置结构化日志

    ``log_dir`` 经 :func:`paths.resolve` 解析。默认值 ``"logs"`` 是相对路径，
    会让日志目录取决于**启动时的 CWD** —— ``run.py`` 传的是
    ``LogConfig.LOG_DIR``（绝对），所以生产路径没事；但任何按默认值调用
    （测试、脚本、别的模块）都会把日志写到启动目录下，
    与 ``paths.LOG_DIR`` 分裂成两处，排障时"日志不见了"。
    """
    from loguru import logger

    # 移除默认handler
    logger.remove()

    log_path = paths.resolve(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    if json_format:
        # JSON格式 - 用于日志聚合和SIEM集成
        def json_formatter(record):
            log_entry = {
                "timestamp": record["time"].isoformat(),
                "level": record["level"].name,
                "module": record["module"],
                "function": record["function"],
                "line": record["line"],
                "message": record["message"],
                "process": record["process"].id,
                "thread": record["thread"].id,
            }
            # 添加extra字段
            if record["extra"]:
                log_entry["extra"] = record["extra"]
            # 添加异常信息
            if record["exception"]:
                log_entry["exception"] = {
                    "type": record["exception"].type.__name__ if record["exception"].type else None,
                    "value": str(record["exception"].value) if record["exception"].value else None,
                    "traceback": record["exception"].traceback is not None
                }
            record["extra"]["json"] = json.dumps(log_entry, ensure_ascii=False, default=str)
            return "{extra[json]}\n"

        formatter = json_formatter
    else:
        # 人类可读格式
        formatter = (
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{module}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>\n"
        )

    # 控制台输出（人类可读，无颜色标记避免环境兼容问题）
    # round 266：`配置/system.yaml` 的 `logging.console.enabled` 现在真的生效
    if enable_console:
        logger.add(
            sys.stderr,
            format="{time:HH:mm:ss} | {level: <8} | {module}:{line} | {message}",
            level=log_level,
            colorize=False
        )

    # 应用日志文件（JSON格式）
    # round 266：`logging.file.enabled=false` 时不再写文件（容器里只收 stdout 的场景）
    if not enable_file:
        logger.info("日志文件输出已关闭（logging.file.enabled=false）")
        return

    logger.add(
        str(log_path / "scada_{time:YYYY-MM-DD}.log"),
        format=formatter,
        level=log_level,
        rotation=rotation,
        retention=retention,
        compression=compression,
        encoding="utf-8",
        enqueue=True  # 线程安全
    )

    # 安全审计日志（单独文件，不轮转，保留更久）
    logger.add(
        str(log_path / "audit_{time:YYYY-MM-DD}.log"),
        format=formatter,
        level="WARNING",
        rotation="50 MB",
        retention="90 days",
        compression=compression,
        encoding="utf-8",
        enqueue=True,
        filter=lambda record: record["extra"].get("audit", False)
    )

    # 错误日志（单独文件）
    logger.add(
        str(log_path / "error_{time:YYYY-MM-DD}.log"),
        format=formatter,
        level="ERROR",
        rotation="50 MB",
        retention="60 days",
        compression=compression,
        encoding="utf-8",
        enqueue=True
    )

    # ⚠️ 必须放在**所有 sink 配好之后**：桥接进来的记录要走上面这些 sink 落盘。
    #    没有这一步，**标准 logging 的 INFO 会被静默丢弃**（见 InterceptHandler 的说明）。
    _install_stdlib_logging_bridge(log_level)

    logger.info(f"日志系统初始化完成: level={log_level}, json={json_format}, dir={log_dir}")
    return logger


class _CompatLogger:
    """把 loguru 包装成兼容 stdlib logging 调用风格的 logger。

    背景：`get_logger()` 原先直接返回 `loguru.logger.bind(...)`，而 loguru
    只认 `{}` 风格占位符。本项目 1400+ 处日志调用全是 stdlib 的 %-风格：

        logger.warning("读取 %s 失败: %s", device, err)
        # loguru 实际输出：读取 %s 失败: %s     ← 参数被静默丢弃（实测确认）

    结果是**所有带参数的日志都只剩字面 %s/%d** —— 排障时看不到任何变量值：
    设备 ID、异常内容、配置项名称全部丢失，日志里只有一句没有上下文的空话。

    这里做一层薄包装：有 args 时先做 %-格式化，再交给 loguru。
    """

    __slots__ = ('_logger',)

    def __init__(self, logger):
        self._logger = logger

    @staticmethod
    def _fmt(msg, args):
        if not args:
            return msg
        try:
            return msg % args
        except Exception:
            # 格式串与参数不匹配时，别把整条日志弄丢 —— 原样附在后面
            return str(msg) + ' ' + ' '.join(repr(a) for a in args)

    # 注意 `opt(depth=1)`：让 loguru 记**调用方**的文件与行号，而不是本包装器。
    # 不加的话所有日志都会指向 structured_logging.py，日志里的 `{module}:{line}`
    # 定位价值就全没了（实测踩到过）。

    def debug(self, msg, *args, **kwargs):
        self._logger.opt(depth=1).debug(self._fmt(msg, args), **kwargs)

    def info(self, msg, *args, **kwargs):
        self._logger.opt(depth=1).info(self._fmt(msg, args), **kwargs)

    def warning(self, msg, *args, **kwargs):
        self._logger.opt(depth=1).warning(self._fmt(msg, args), **kwargs)

    def error(self, msg, *args, **kwargs):
        self._logger.opt(depth=1).error(self._fmt(msg, args), **kwargs)

    def critical(self, msg, *args, **kwargs):
        self._logger.opt(depth=1).critical(self._fmt(msg, args), **kwargs)

    def exception(self, msg, *args, **kwargs):
        self._logger.opt(depth=1).exception(self._fmt(msg, args), **kwargs)

    def log(self, level, msg, *args, **kwargs):
        self._logger.opt(depth=1).log(level, self._fmt(msg, args), **kwargs)

    def bind(self, **kwargs):
        return _CompatLogger(self._logger.bind(**kwargs))


def get_logger(name: str, audit: bool = False):
    """获取带模块标识的logger（兼容 stdlib 的 %-格式化调用）"""
    from loguru import logger
    return _CompatLogger(logger.bind(module=name, audit=audit))
