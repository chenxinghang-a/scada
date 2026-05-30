"""
结构化日志配置 - 等保2.0要求
GB/T 22239: 安全审计日志必须可机器解析
"""
import sys
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional


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

    # 控制台输出（人类可读）
    logger.add(
        sys.stderr,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{module}</cyan>:<line> | "
            "<level>{message}</level>"
        ),
        level=log_level,
        colorize=True
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


def get_logger(name: str, audit: bool = False):
    """获取带模块标识的logger"""
    from loguru import logger
    return logger.bind(module=name, audit=audit)
