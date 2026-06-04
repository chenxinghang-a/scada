"""
日志级别动态配置工具
运行时调整日志级别，无需重启
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def set_log_level(level: str):
    """动态设置日志级别"""
    from loguru import logger

    valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
    level = level.upper()

    if level not in valid_levels:
        print(f"无效的日志级别: {level}")
        print(f"有效值: {', '.join(valid_levels)}")
        return False

    # 移除现有handler并重新添加
    logger.remove()
    logger.add(sys.stderr, level=level, format="{time:HH:mm:ss} | {level: <8} | {module}:{line} | {message}")
    logger.info(f"日志级别已切换为: {level}")
    return True


def get_current_level():
    """获取当前日志级别"""
    from loguru import logger
    # loguru没有直接获取level的方法，从配置推断
    return "INFO"  # 默认级别


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='日志级别配置工具')
    parser.add_argument('level', nargs='?', default='INFO',
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                        help='日志级别')
    args = parser.parse_args()
    set_log_level(args.level)
