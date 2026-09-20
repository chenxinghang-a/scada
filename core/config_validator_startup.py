"""
配置启动验证
在系统启动时检查关键配置的完整性和正确性，
防止因配置错误导致运行时故障。

使用方式:
    from core.config_validator_startup import validate_startup_config
    validate_startup_config()
"""

# ============================================================================
# 接线状态：未接线（WIRED = False）
# ============================================================================
# 本模块在生产代码（run.py / 各业务层 / 其它 core 模块）中**没有任何 import 引用**。
# 模块本身可用，但当前没有调用方 —— 也就是说它宣称的这项能力**当前并未生效**。
#
# 为什么保留而不删除：删掉即丢能力，模块本身有测试价值；这里只把「没接线」显式化、
# 可追踪，避免「代码在库里」被误读成「功能在跑」。
#
# 自动化复核（防止本标注过期）：
#   tests/test_core_regressions.py::test_unwired_marker_matches_reality
#   —— 该用例用 AST 扫描全仓库 import。一旦有人把本模块接进生产代码，
#      而这里仍写着 WIRED = False，用例即失败，强制文档与事实同步。
#
# 接线建议（需改 run.py / 各层，core 内部无权自行接线）：
#     在 run.py 启动早期调用 `validate_startup_config()`，按返回的 is_valid 决定是否继续启动。
# ============================================================================
WIRED = False


import os
import logging
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


class ConfigValidationError:
    """配置验证错误"""
    def __init__(self, key: str, message: str, severity: str = 'error'):
        self.key = key
        self.message = message
        self.severity = severity  # 'error' | 'warning'

    def __str__(self):
        return f"[{self.severity.upper()}] {self.key}: {self.message}"


def validate_startup_config() -> Tuple[bool, List[ConfigValidationError]]:
    """
    验证启动配置

    Returns:
        (is_valid, errors): 是否有效，错误列表
    """
    errors = []

    # 1. 检查关键环境变量
    required_env = ['JWT_SECRET']
    optional_env = ['CSRF_SECRET', 'ADMIN_PASSWORD', 'SMTP_HOST', 'SMTP_USER']

    for var in required_env:
        if not os.environ.get(var):
            errors.append(ConfigValidationError(
                f'env:{var}',
                f'环境变量 {var} 未设置，将使用随机值（重启后失效）',
                'warning'
            ))

    # 2. 检查配置文件存在性
    config_dir = Path('配置')
    critical_configs = ['devices.yaml', 'alarms.yaml']
    optional_configs = ['system.yaml', 'industry40.yaml']

    for cfg in critical_configs:
        path = config_dir / cfg
        if not path.exists():
            errors.append(ConfigValidationError(
                f'config:{cfg}',
                f'关键配置文件不存在: {path}',
                'error'
            ))
        elif path.stat().st_size == 0:
            errors.append(ConfigValidationError(
                f'config:{cfg}',
                f'配置文件为空: {path}',
                'error'
            ))

    for cfg in optional_configs:
        path = config_dir / cfg
        if not path.exists():
            errors.append(ConfigValidationError(
                f'config:{cfg}',
                f'可选配置文件不存在: {path}（部分功能可能不可用）',
                'warning'
            ))

    # 3. 检查数据库可访问性
    db_paths = ['data/scada.db', 'data/realtime.db']
    for db_path in db_paths:
        path = Path(db_path)
        if path.exists():
            try:
                conn = sqlite3.connect(str(path), timeout=5)
                conn.execute("SELECT 1")
                conn.close()
            except Exception as e:
                errors.append(ConfigValidationError(
                    f'db:{db_path}',
                    f'数据库无法访问: {e}',
                    'error'
                ))
        else:
            errors.append(ConfigValidationError(
                f'db:{db_path}',
                f'数据库文件不存在: {path}（将自动创建）',
                'warning'
            ))

    # 4. 检查日志目录
    log_dir = Path('logs')
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        # 测试写入
        test_file = log_dir / '.write_test'
        test_file.write_text('test')
        test_file.unlink()
    except Exception as e:
        errors.append(ConfigValidationError(
            'log:dir',
            f'日志目录不可写: {e}',
            'error'
        ))

    # 5. 检查数据目录
    data_dir = Path('data')
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        errors.append(ConfigValidationError(
            'data:dir',
            f'数据目录创建失败: {e}',
            'error'
        ))

    # 6. 检查端口可用性
    # 解析本身必须包在 try 里：旧实现 `int(os.environ.get('WEB_PORT', '5000'))` 写在
    # try 之外，只要 WEB_PORT 是非数字（如 "5000 "、":5000"、空串）就抛 ValueError，
    # 而本函数是启动前校验，异常会直接把进程打挂 —— 校验器自己成了故障源。
    web_port: Optional[int] = None
    try:
        raw_port = os.environ.get('WEB_PORT', '5000')
        web_port = int(raw_port)
        if not (1 <= web_port <= 65535):
            raise ValueError(f"端口 {web_port} 不在合法范围 1-65535")
    except (TypeError, ValueError) as e:
        web_port = None
        errors.append(ConfigValidationError(
            'port:web',
            f'WEB_PORT 取值非法({os.environ.get("WEB_PORT")!r}): {e}；'
            f'已跳过端口占用检查（将回退默认端口）',
            'error'
        ))

    if web_port is not None:
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('127.0.0.1', web_port))
            sock.close()
            if result == 0:
                errors.append(ConfigValidationError(
                    'port:web',
                    f'Web端口 {web_port} 已被占用',
                    'warning'
                ))
        except Exception as e:
            # 端口探测本身失败 → 该项检查被跳过，本次配置验证结果不完整
            logger.warning("Web端口 %s 可用性检查被跳过(验证结果不完整): %s", web_port, e)

    # 汇总结果
    has_errors = any(e.severity == 'error' for e in errors)
    is_valid = not has_errors

    if errors:
        for err in errors:
            if err.severity == 'error':
                logger.error("配置验证失败: %s", err)
            else:
                logger.warning("配置验证警告: %s", err)
    else:
        logger.info("配置验证通过，无异常")

    return is_valid, errors


def get_validation_summary(errors: List[ConfigValidationError]) -> dict:
    """获取验证结果摘要"""
    return {
        'valid': not any(e.severity == 'error' for e in errors),
        'errors': [str(e) for e in errors if e.severity == 'error'],
        'warnings': [str(e) for e in errors if e.severity == 'warning'],
        'total': len(errors),
    }
