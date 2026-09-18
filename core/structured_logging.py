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
from pathlib import Path
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


def setup_logging(
    log_dir: str = "logs",
    log_level: str = "INFO",
    json_format: bool = True,
    rotation: str = "100 MB",
    retention: str = "30 days",
    compression: str = "gz"
):
    """配置结构化日志"""
    from loguru import logger

    # 移除默认handler
    logger.remove()

    log_path = Path(log_dir)
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
    logger.add(
        sys.stderr,
        format="{time:HH:mm:ss} | {level: <8} | {module}:{line} | {message}",
        level=log_level,
        colorize=False
    )

    # 应用日志文件（JSON格式）
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
