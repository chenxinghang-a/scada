"""
自动化运维脚本
用于日常运维任务的自动化执行

使用方法:
    python tools/auto_ops.py health-check      # 健康检查
    python tools/auto_ops.py backup            # 自动备份
    python tools/auto_ops.py cleanup           # 清理旧数据
    python tools/auto_ops.py report            # 生成日报
    python tools/auto_ops.py all               # 执行所有任务
"""

import os
import sys
import json
import sqlite3
import argparse
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


class AutoOps:
    """自动化运维"""

    def __init__(self):
        self.project_root = project_root
        self.log_dir = project_root / 'logs'
        self.data_dir = project_root / 'data'
        self.backup_dir = project_root / 'backups'

        # 确保目录存在
        self.log_dir.mkdir(exist_ok=True)
        self.backup_dir.mkdir(exist_ok=True)

    def health_check(self) -> Dict[str, Any]:
        """执行健康检查"""
        results = {
            'timestamp': datetime.now().isoformat(),
            'checks': {},
            'status': 'healthy',
        }

        # 1. 检查数据库
        db_path = self.data_dir / 'scada.db'
        if db_path.exists():
            try:
                conn = sqlite3.connect(str(db_path))
                cursor = conn.execute("SELECT COUNT(*) FROM sqlite_master")
                table_count = cursor.fetchone()[0]
                conn.close()

                results['checks']['database'] = {
                    'status': 'ok',
                    'tables': table_count,
                    'size_mb': round(db_path.stat().st_size / (1024 * 1024), 2),
                }
            except Exception as e:
                results['checks']['database'] = {'status': 'error', 'message': str(e)}
                results['status'] = 'degraded'
        else:
            results['checks']['database'] = {'status': 'missing'}
            results['status'] = 'unhealthy'

        # 2. 检查日志目录
        log_files = list(self.log_dir.glob('*.log'))
        total_log_size = sum(f.stat().st_size for f in log_files)
        results['checks']['logs'] = {
            'status': 'ok',
            'count': len(log_files),
            'total_size_mb': round(total_log_size / (1024 * 1024), 2),
        }

        # 3. 检查配置文件
        config_dir = project_root / '配置'
        if config_dir.exists():
            config_files = list(config_dir.glob('*.yaml'))
            results['checks']['config'] = {
                'status': 'ok',
                'count': len(config_files),
            }
        else:
            results['checks']['config'] = {'status': 'missing'}
            results['status'] = 'degraded'

        # 4. 检查磁盘空间
        try:
            import shutil
            total, used, free = shutil.disk_usage(str(self.project_root))
            results['checks']['disk'] = {
                'status': 'ok',
                'total_gb': round(total / (1024**3), 2),
                'used_gb': round(used / (1024**3), 2),
                'free_gb': round(free / (1024**3), 2),
                'usage_percent': round(used / total * 100, 1),
            }

            if results['checks']['disk']['usage_percent'] > 90:
                results['checks']['disk']['status'] = 'warning'
                results['status'] = 'degraded'
        except Exception as e:
            results['checks']['disk'] = {'status': 'error', 'message': str(e)}

        return results

    def backup(self) -> Dict[str, Any]:
        """执行自动备份"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = self.backup_dir / f'backup_{timestamp}'
        backup_path.mkdir(parents=True, exist_ok=True)

        result = {
            'timestamp': datetime.now().isoformat(),
            'backup_path': str(backup_path),
            'files': [],
        }

        # 1. 备份数据库
        db_path = self.data_dir / 'scada.db'
        if db_path.exists():
            import shutil
            dest = backup_path / 'scada.db'
            shutil.copy2(db_path, dest)
            result['files'].append({
                'file': 'scada.db',
                'size_mb': round(dest.stat().st_size / (1024 * 1024), 2),
            })

        # 2. 备份配置文件
        config_dir = project_root / '配置'
        if config_dir.exists():
            import shutil
            config_backup = backup_path / '配置'
            config_backup.mkdir(exist_ok=True)

            for file in config_dir.glob('*.yaml'):
                shutil.copy2(file, config_backup / file.name)
                result['files'].append({
                    'file': f'配置/{file.name}',
                    'size_kb': round(file.stat().st_size / 1024, 2),
                })

        # 3. 压缩备份
        try:
            import shutil
            archive_path = shutil.make_archive(
                str(backup_path),
                'zip',
                str(backup_path)
            )
            result['archive'] = archive_path
            result['archive_size_mb'] = round(
                Path(archive_path).stat().st_size / (1024 * 1024), 2
            )

            # 删除未压缩的备份目录
            shutil.rmtree(backup_path)
        except Exception as e:
            result['archive_error'] = str(e)

        return result

    def cleanup(self, days: int = 30) -> Dict[str, Any]:
        """清理旧数据"""
        result = {
            'timestamp': datetime.now().isoformat(),
            'cleaned': {},
        }

        cutoff = datetime.now() - timedelta(days=days)

        # 1. 清理旧日志
        log_cleaned = 0
        for log_file in self.log_dir.glob('*.log'):
            if datetime.fromtimestamp(log_file.stat().st_mtime) < cutoff:
                log_file.unlink()
                log_cleaned += 1
        result['cleaned']['logs'] = log_cleaned

        # 2. 清理旧备份
        backup_cleaned = 0
        for backup_file in self.backup_dir.glob('*.zip'):
            if datetime.fromtimestamp(backup_file.stat().st_mtime) < cutoff:
                backup_file.unlink()
                backup_cleaned += 1
        result['cleaned']['backups'] = backup_cleaned

        # 3. 清理旧数据（如果数据库存在）
        db_path = self.data_dir / 'scada.db'
        if db_path.exists():
            try:
                conn = sqlite3.connect(str(db_path))
                cursor = conn.cursor()

                # 清理旧的历史数据
                cursor.execute(
                    "DELETE FROM history_data WHERE timestamp < ?",
                    (cutoff.isoformat(),)
                )
                result['cleaned']['history_rows'] = cursor.rowcount

                # 清理旧的报警记录
                cursor.execute(
                    "DELETE FROM alarm_records WHERE timestamp < ? AND acknowledged = 1",
                    (cutoff.isoformat(),)
                )
                result['cleaned']['alarm_rows'] = cursor.rowcount

                conn.commit()
                conn.close()
            except Exception as e:
                result['cleaned']['database_error'] = str(e)

        return result

    def generate_report(self) -> Dict[str, Any]:
        """生成日报"""
        report = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'timestamp': datetime.now().isoformat(),
        }

        # 健康检查
        report['health'] = self.health_check()

        # 数据库统计
        db_path = self.data_dir / 'scada.db'
        if db_path.exists():
            try:
                conn = sqlite3.connect(str(db_path))

                # 设备统计
                try:
                    cursor = conn.execute("SELECT COUNT(*) FROM devices")
                    report['devices'] = {'total': cursor.fetchone()[0]}
                except:
                    report['devices'] = {'total': 0}

                # 报警统计
                try:
                    cursor = conn.execute(
                        "SELECT COUNT(*) FROM alarm_records WHERE date(timestamp) = date('now')"
                    )
                    report['alarms'] = {'today': cursor.fetchone()[0]}
                except:
                    report['alarms'] = {'today': 0}

                conn.close()
            except Exception as e:
                report['database_error'] = str(e)

        return report

    def run_all(self) -> Dict[str, Any]:
        """执行所有运维任务"""
        results = {
            'timestamp': datetime.now().isoformat(),
            'tasks': {},
        }

        # 1. 健康检查
        print("执行健康检查...")
        results['tasks']['health_check'] = self.health_check()

        # 2. 自动备份
        print("执行自动备份...")
        results['tasks']['backup'] = self.backup()

        # 3. 清理旧数据
        print("清理旧数据...")
        results['tasks']['cleanup'] = self.cleanup()

        # 4. 生成报告
        print("生成日报...")
        results['tasks']['report'] = self.generate_report()

        return results


def main():
    parser = argparse.ArgumentParser(description='自动化运维脚本')
    parser.add_argument('command', choices=['health-check', 'backup', 'cleanup', 'report', 'all'],
                       help='执行的命令')
    parser.add_argument('--days', type=int, default=30, help='清理天数')

    args = parser.parse_args()

    ops = AutoOps()

    if args.command == 'health-check':
        result = ops.health_check()
    elif args.command == 'backup':
        result = ops.backup()
    elif args.command == 'cleanup':
        result = ops.cleanup(args.days)
    elif args.command == 'report':
        result = ops.generate_report()
    elif args.command == 'all':
        result = ops.run_all()

    # 输出结果
    print(json.dumps(result, indent=2, ensure_ascii=False))

    # 保存到日志
    log_file = ops.log_dir / f'ops_{datetime.now().strftime("%Y%m%d")}.json'
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write(json.dumps(result, ensure_ascii=False) + '\n')


if __name__ == '__main__':
    main()
