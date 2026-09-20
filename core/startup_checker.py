"""
启动依赖检查模块
确保系统启动前所有依赖和配置就绪
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
#     在 launcher.py / run.py 的启动入口调用 `run_startup_checks()`，失败即拒绝启动。
# ============================================================================
WIRED = False


import os
import sys
import logging
import sqlite3
from pathlib import Path
from typing import Dict, List, Any, Callable

logger = logging.getLogger(__name__)


class StartupCheck:
    """启动检查项"""

    def __init__(self, name: str, check_fn: Callable[[], bool], message: str, critical: bool = True):
        self.name = name
        self.check_fn = check_fn
        self.message = message
        self.critical = critical  # 关键检查失败时阻止启动


class StartupChecker:
    """启动依赖检查器"""

    def __init__(self):
        self.checks: List[StartupCheck] = []
        self._register_default_checks()

    def _register_default_checks(self):
        """注册默认检查项"""
        # Python版本检查
        self.register(StartupCheck(
            'python_version',
            lambda: sys.version_info >= (3, 11),
            'Python版本需要3.11+',
            critical=True
        ))

        # 数据目录检查
        self.register(StartupCheck(
            'data_directory',
            lambda: Path('data').exists() or Path('data').mkdir(exist_ok=True) or True,
            '数据目录不存在且无法创建',
            critical=True
        ))

        # 配置目录检查
        self.register(StartupCheck(
            'config_directory',
            lambda: Path('配置').exists(),
            '配置目录不存在',
            critical=True
        ))

        # 数据库文件检查
        self.register(StartupCheck(
            'database_file',
            lambda: self._check_database(),
            '数据库文件损坏或不可访问',
            critical=True
        ))

        # 必要配置文件检查
        self.register(StartupCheck(
            'config_files',
            lambda: self._check_config_files(),
            '必要配置文件缺失',
            critical=True
        ))

        # 磁盘空间检查
        self.register(StartupCheck(
            'disk_space',
            lambda: self._check_disk_space(100),  # 至少100MB
            '磁盘空间不足',
            critical=False
        ))

        # 端口可用性检查
        self.register(StartupCheck(
            'port_available',
            lambda: self._check_port_available(5000),
            '端口5000被占用',
            critical=False
        ))

    def register(self, check: StartupCheck):
        """注册检查项"""
        self.checks.append(check)

    def run_all(self) -> Dict[str, Any]:
        """运行所有检查"""
        results = {
            'passed': [],
            'failed': [],
            'warnings': [],
            'can_start': True,
        }

        for check in self.checks:
            try:
                if check.check_fn():
                    results['passed'].append(check.name)
                else:
                    if check.critical:
                        results['failed'].append({
                            'name': check.name,
                            'message': check.message,
                        })
                        results['can_start'] = False
                    else:
                        results['warnings'].append({
                            'name': check.name,
                            'message': check.message,
                        })
            except Exception as e:
                if check.critical:
                    results['failed'].append({
                        'name': check.name,
                        'message': f'{check.message}: {e}',
                    })
                    results['can_start'] = False
                else:
                    results['warnings'].append({
                        'name': check.name,
                        'message': f'{check.message}: {e}',
                    })

        return results

    def _check_database(self) -> bool:
        """检查数据库完整性"""
        db_path = Path('data/scada.db')
        if not db_path.exists():
            return True  # 不存在时会自动创建
        try:
            conn = sqlite3.connect(str(db_path), timeout=5)
            cursor = conn.execute("PRAGMA integrity_check")
            result = cursor.fetchone()[0]
            conn.close()
            return result == 'ok'
        except Exception as e:
            # 返回 False 是 fail-safe，但**必须留痕**：否则"数据库打不开"和
            # "integrity_check 不通过"在日志里长得一模一样，排障时无从下手。
            logger.warning("启动检查 database_file 执行失败(判定为未通过): %s", e)
            return False

    def _check_config_files(self) -> bool:
        """检查必要配置文件"""
        config_dir = Path('配置')
        if not config_dir.exists():
            return False
        # 至少需要devices.yaml
        return (config_dir / 'devices.yaml').exists()

    def _check_disk_space(self, min_mb: int) -> bool:
        """检查磁盘空间

        旧实现在异常时 `return True  # 无法检查时假设通过` —— 典型的 fail-open：
        检查器坏掉了，却报告"磁盘空间充足"，等于把风险藏起来。
        该项是 non-critical，返回 False 只会产生一条启动 WARNING，不会阻止启动。
        """
        try:
            import shutil
            total, used, free = shutil.disk_usage('.')
            return free > min_mb * 1024 * 1024
        except Exception as e:
            logger.warning("启动检查 disk_space 无法执行，判定为未通过（不再假装通过）: %s", e)
            return False

    def _check_port_available(self, port: int) -> bool:
        """检查端口可用性

        与 _check_disk_space 同理：探测不了就**不能**声称端口空闲。
        该项是 non-critical，返回 False 只产生启动 WARNING。
        """
        import socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(('127.0.0.1', port))
            sock.close()
            return result != 0  # 端口未被占用
        except Exception as e:
            logger.warning("启动检查 port_available 无法执行，判定为未通过: %s", e)
            return False


def run_startup_checks() -> bool:
    """运行启动检查，返回是否可以启动"""
    checker = StartupChecker()
    results = checker.run_all()

    # 打印结果
    if results['passed']:
        logger.info(f"启动检查通过: {', '.join(results['passed'])}")

    if results['warnings']:
        for w in results['warnings']:
            logger.warning(f"启动警告 [{w['name']}]: {w['message']}")

    if results['failed']:
        for f in results['failed']:
            logger.error(f"启动失败 [{f['name']}]: {f['message']}")
        return False

    return True
