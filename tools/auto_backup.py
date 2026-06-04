"""
自动备份工具
定时备份数据库和配置文件
"""

import os
import sys
import json
import shutil
import sqlite3
import argparse
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


class AutoBackup:
    """自动备份管理器"""

    def __init__(self, backup_dir: str = None):
        self.project_root = project_root
        self.backup_dir = Path(backup_dir) if backup_dir else project_root / 'backups'
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        self.db_path = project_root / 'data' / 'scada.db'
        self.config_dir = project_root / '配置'

        self._running = False
        self._thread = None

    def backup_database(self) -> Dict[str, Any]:
        """备份数据库"""
        result = {
            'timestamp': datetime.now().isoformat(),
            'type': 'database',
            'status': 'success',
        }

        try:
            if not self.db_path.exists():
                result['status'] = 'skipped'
                result['message'] = '数据库文件不存在'
                return result

            # 创建备份目录
            date_str = datetime.now().strftime('%Y%m%d')
            backup_subdir = self.backup_dir / date_str
            backup_subdir.mkdir(parents=True, exist_ok=True)

            # 备份文件名
            time_str = datetime.now().strftime('%H%M%S')
            backup_file = backup_subdir / f'scada_{time_str}.db'

            # 使用SQLite备份API（安全备份）
            source = sqlite3.connect(str(self.db_path), timeout=5)
            dest = sqlite3.connect(str(backup_file), timeout=5)
            source.backup(dest)
            source.close()
            dest.close()

            result['backup_path'] = str(backup_file)
            result['size_mb'] = round(backup_file.stat().st_size / (1024 * 1024), 2)

        except Exception as e:
            result['status'] = 'error'
            result['error'] = str(e)

        return result

    def backup_configs(self) -> Dict[str, Any]:
        """备份配置文件"""
        result = {
            'timestamp': datetime.now().isoformat(),
            'type': 'config',
            'status': 'success',
            'files': [],
        }

        try:
            if not self.config_dir.exists():
                result['status'] = 'skipped'
                result['message'] = '配置目录不存在'
                return result

            # 创建备份目录
            date_str = datetime.now().strftime('%Y%m%d')
            backup_subdir = self.backup_dir / date_str / 'config'
            backup_subdir.mkdir(parents=True, exist_ok=True)

            # 备份所有配置文件
            for file in self.config_dir.glob('*.yaml'):
                dest = backup_subdir / file.name
                shutil.copy2(file, dest)
                result['files'].append(file.name)

            for file in self.config_dir.glob('*.json'):
                dest = backup_subdir / file.name
                shutil.copy2(file, dest)
                result['files'].append(file.name)

        except Exception as e:
            result['status'] = 'error'
            result['error'] = str(e)

        return result

    def cleanup_old_backups(self, days: int = 30) -> Dict[str, Any]:
        """清理旧备份"""
        result = {
            'timestamp': datetime.now().isoformat(),
            'type': 'cleanup',
            'deleted': 0,
        }

        try:
            cutoff = datetime.now() - timedelta(days=days)

            for item in self.backup_dir.iterdir():
                if item.is_dir():
                    # 检查目录名是否是日期格式
                    try:
                        dir_date = datetime.strptime(item.name, '%Y%m%d')
                        if dir_date < cutoff:
                            shutil.rmtree(item)
                            result['deleted'] += 1
                    except ValueError:
                        continue

        except Exception as e:
            result['error'] = str(e)

        return result

    def run_backup(self) -> Dict[str, Any]:
        """执行完整备份"""
        results = {
            'timestamp': datetime.now().isoformat(),
            'database': self.backup_database(),
            'configs': self.backup_configs(),
            'cleanup': self.cleanup_old_backups(),
        }

        # 计算总体状态
        if results['database']['status'] == 'error' or results['configs']['status'] == 'error':
            results['status'] = 'partial'
        else:
            results['status'] = 'success'

        return results

    def start_auto_backup(self, interval_hours: int = 24):
        """启动自动备份"""
        self._running = True
        self._thread = threading.Thread(target=self._backup_loop, args=(interval_hours,), daemon=True)
        self._thread.start()

    def stop_auto_backup(self):
        """停止自动备份"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _backup_loop(self, interval_hours: int):
        """备份循环"""
        import time
        while self._running:
            try:
                result = self.run_backup()
                print(f"自动备份完成: {result['status']}")
            except Exception as e:
                print(f"自动备份失败: {e}")

            # 等待下次备份
            for _ in range(interval_hours * 3600):
                if not self._running:
                    break
                time.sleep(1)


def format_report(result: Dict[str, Any]) -> str:
    """格式化报告"""
    lines = []
    lines.append("=" * 60)
    lines.append("自动备份报告")
    lines.append(f"时间: {result['timestamp']}")
    lines.append(f"状态: {result.get('status', 'unknown').upper()}")
    lines.append("=" * 60)

    # 数据库备份
    db = result.get('database', {})
    lines.append(f"\n数据库备份:")
    lines.append(f"  状态: {db.get('status', 'unknown')}")
    if db.get('backup_path'):
        lines.append(f"  路径: {db['backup_path']}")
        lines.append(f"  大小: {db.get('size_mb', '?')}MB")

    # 配置备份
    configs = result.get('configs', {})
    lines.append(f"\n配置备份:")
    lines.append(f"  状态: {configs.get('status', 'unknown')}")
    if configs.get('files'):
        lines.append(f"  文件: {', '.join(configs['files'])}")

    # 清理
    cleanup = result.get('cleanup', {})
    lines.append(f"\n旧备份清理:")
    lines.append(f"  删除: {cleanup.get('deleted', 0)} 个")

    lines.append("=" * 60)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description='自动备份工具')
    parser.add_argument('command', choices=['backup', 'auto', 'cleanup', 'list'], help='命令')
    parser.add_argument('--interval', type=int, default=24, help='自动备份间隔（小时）')
    parser.add_argument('--days', type=int, default=30, help='保留天数')

    args = parser.parse_args()

    backup = AutoBackup()

    if args.command == 'backup':
        result = backup.run_backup()
        print(format_report(result))

    elif args.command == 'auto':
        try:
            print(f"启动自动备份（每{args.interval}小时）")
            backup.start_auto_backup(args.interval)
            while True:
                import time
                time.sleep(1)
        except KeyboardInterrupt:
            backup.stop_auto_backup()
            print("自动备份已停止")

    elif args.command == 'cleanup':
        result = backup.cleanup_old_backups(args.days)
        print(f"清理完成: 删除 {result['deleted']} 个旧备份")

    elif args.command == 'list':
        if backup.backup_dir.exists():
            backups = sorted([d.name for d in backup.backup_dir.iterdir() if d.is_dir()])
            print(f"备份列表 ({len(backups)} 个):")
            for b in backups:
                print(f"  {b}")
        else:
            print("备份目录不存在")


if __name__ == '__main__':
    main()
