"""
自动化部署脚�?用于SCADA系统的自动化部署和更�?
使用方法:
    python tools/deploy.py check     # 检查部署环�?    python tools/deploy.py backup    # 备份当前版本
    python tools/deploy.py deploy    # 执行部署
    python tools/deploy.py rollback  # 回滚到上一版本
"""

import os
import sys
import json
import shutil
import argparse
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


class Deployer:
    """部署管理�?""

    def __init__(self):
        self.project_root = project_root
        self.deploy_dir = project_root / '.deploy'
        self.backup_dir = self.deploy_dir / 'backups'
        self.deploy_dir.mkdir(exist_ok=True)
        self.backup_dir.mkdir(exist_ok=True)

    def check_environment(self) -> Dict[str, Any]:
        """检查部署环�?""
        checks = {
            'timestamp': datetime.now().isoformat(),
            'checks': [],
            'status': 'ok',
        }

        # 1. 检查Python版本
        python_version = sys.version.split()[0]
        checks['checks'].append({
            'name': 'Python版本',
            'status': 'ok' if tuple(map(int, python_version.split('.')[:2])) >= (3, 11) else 'warning',
            'value': python_version,
            'message': f'Python {python_version}',
        })

        # 2. 检查依�?        requirements_file = self.project_root / 'requirements.txt'
        if requirements_file.exists():
            try:
                result = subprocess.run(
                    [sys.executable, '-m', 'pip', 'check'],
                    capture_output=True, text=True, timeout=30
                )
                checks['checks'].append({
                    'name': '依赖检�?,
                    'status': 'ok' if result.returncode == 0 else 'warning',
                    'message': result.stdout[:200] if result.returncode == 0 else '有依赖问�?,
                })
            except Exception:
                checks['checks'].append({
                    'name': '依赖检�?,
                    'status': 'error',
                    'message': '无法检查依�?,
                })

        # 3. 检查磁盘空�?        try:
            import shutil as sh
            total, used, free = sh.disk_usage(str(self.project_root))
            free_gb = free / (1024**3)
            checks['checks'].append({
                'name': '磁盘空间',
                'status': 'ok' if free_gb > 1 else 'warning',
                'value': f'{free_gb:.1f}GB',
                'message': f'可用空间: {free_gb:.1f}GB',
            })
        except Exception:
            pass

        # 4. 检查数据库
        db_path = self.project_root / 'data' / 'scada.db'
        if db_path.exists():
            import sqlite3
            try:
                conn = sqlite3.connect(str(db_path), timeout=5)
                cursor = conn.execute("PRAGMA integrity_check")
                result = cursor.fetchone()[0]
                conn.close()
                checks['checks'].append({
                    'name': '数据库完整�?,
                    'status': 'ok' if result == 'ok' else 'error',
                    'message': f'完整性检�? {result}',
                })
            except Exception as e:
                checks['checks'].append({
                    'name': '数据库完整�?,
                    'status': 'error',
                    'message': str(e),
                })

        # 5. 检查配置文�?        config_dir = self.project_root / '配置'
        if config_dir.exists():
            config_files = list(config_dir.glob('*.yaml'))
            checks['checks'].append({
                'name': '配置文件',
                'status': 'ok',
                'message': f'{len(config_files)} 个配置文�?,
            })

        # 计算总体状�?        for check in checks['checks']:
            if check['status'] == 'error':
                checks['status'] = 'error'
                break
            elif check['status'] == 'warning':
                checks['status'] = 'warning'

        return checks

    def backup(self, version_tag: str = None) -> Dict[str, Any]:
        """备份当前版本"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        tag = version_tag or timestamp
        backup_path = self.backup_dir / f'backup_{tag}'

        result = {
            'timestamp': datetime.now().isoformat(),
            'backup_path': str(backup_path),
            'files': [],
        }

        try:
            # 备份源代�?            src_backup = backup_path / 'src'
            src_backup.mkdir(parents=True, exist_ok=True)

            # 备份关键目录
            for dir_name in ['展示�?, '存储�?, '采集�?, '报警�?, '智能�?, '用户�?, 'core', 'tools']:
                src_dir = self.project_root / dir_name
                if src_dir.exists():
                    shutil.copytree(src_dir, src_backup / dir_name)
                    result['files'].append(dir_name)

            # 备份配置文件
            config_dir = self.project_root / '配置'
            if config_dir.exists():
                shutil.copytree(config_dir, backup_path / '配置')
                result['files'].append('配置')

            # 备份数据�?            db_path = self.project_root / 'data' / 'scada.db'
            if db_path.exists():
                data_backup = backup_path / 'data'
                data_backup.mkdir(exist_ok=True)
                shutil.copy2(db_path, data_backup / 'scada.db')
                result['files'].append('data/scada.db')

            # 保存版本信息
            version_info = {
                'tag': tag,
                'timestamp': timestamp,
                'files': result['files'],
            }
            (backup_path / 'version.json').write_text(
                json.dumps(version_info, indent=2), encoding='utf-8'
            )

            result['status'] = 'success'
            result['size_mb'] = round(
                sum(f.stat().st_size for f in backup_path.rglob('*') if f.is_file()) / (1024 * 1024), 2
            )

        except Exception as e:
            result['status'] = 'error'
            result['error'] = str(e)

        return result

    def deploy(self) -> Dict[str, Any]:
        """执行部署"""
        result = {
            'timestamp': datetime.now().isoformat(),
            'steps': [],
        }

        # 1. 环境检�?        env_check = self.check_environment()
        result['steps'].append({
            'name': '环境检�?,
            'status': env_check['status'],
            'details': env_check['checks'],
        })

        if env_check['status'] == 'error':
            result['status'] = 'failed'
            result['error'] = '环境检查失�?
            return result

        # 2. 备份
        backup_result = self.backup('pre_deploy')
        result['steps'].append({
            'name': '备份',
            'status': backup_result['status'],
            'details': backup_result,
        })

        # 3. 安装依赖
        try:
            req_file = self.project_root / 'requirements.txt'
            if req_file.exists():
                proc = subprocess.run(
                    [sys.executable, '-m', 'pip', 'install', '-r', str(req_file)],
                    capture_output=True, text=True, timeout=300
                )
                result['steps'].append({
                    'name': '安装依赖',
                    'status': 'ok' if proc.returncode == 0 else 'error',
                    'message': proc.stdout[-500:] if proc.returncode == 0 else proc.stderr[-500:],
                })
        except Exception as e:
            result['steps'].append({
                'name': '安装依赖',
                'status': 'error',
                'message': str(e),
            })

        # 4. 数据库迁�?        try:
            migrate_script = self.project_root / 'tools' / 'db_migrate.py'
            if migrate_script.exists():
                proc = subprocess.run(
                    [sys.executable, str(migrate_script), 'upgrade'],
                    capture_output=True, text=True, timeout=60
                )
                result['steps'].append({
                    'name': '数据库迁�?,
                    'status': 'ok' if proc.returncode == 0 else 'warning',
                    'message': proc.stdout[-500:] if proc.returncode == 0 else proc.stderr[-500:],
                })
        except Exception as e:
            result['steps'].append({
                'name': '数据库迁�?,
                'status': 'warning',
                'message': str(e),
            })

        # 5. 验证部署
        verify_check = self.check_environment()
        result['steps'].append({
            'name': '部署验证',
            'status': verify_check['status'],
            'details': verify_check['checks'],
        })

        # 计算总体状�?        for step in result['steps']:
            if step['status'] == 'error':
                result['status'] = 'failed'
                break
        else:
            result['status'] = 'success'

        return result

    def rollback(self) -> Dict[str, Any]:
        """回滚到上一版本"""
        result = {
            'timestamp': datetime.now().isoformat(),
        }

        # 找到最近的备份
        backups = sorted(self.backup_dir.glob('backup_*'), reverse=True)
        if not backups:
            result['status'] = 'error'
            result['error'] = '没有可用的备�?
            return result

        latest_backup = backups[0]
        version_file = latest_backup / 'version.json'

        if not version_file.exists():
            result['status'] = 'error'
            result['error'] = '备份版本信息不存�?
            return result

        try:
            # 恢复源代�?            src_backup = latest_backup / 'src'
            if src_backup.exists():
                for dir_name in ['展示�?, '存储�?, '采集�?, '报警�?, '智能�?, '用户�?, 'core', 'tools']:
                    src_dir = src_backup / dir_name
                    if src_dir.exists():
                        dest_dir = self.project_root / dir_name
                        if dest_dir.exists():
                            shutil.rmtree(dest_dir)
                        shutil.copytree(src_dir, dest_dir)

            # 恢复配置
            config_backup = latest_backup / '配置'
            if config_backup.exists():
                dest_config = self.project_root / '配置'
                if dest_config.exists():
                    shutil.rmtree(dest_config)
                shutil.copytree(config_backup, dest_config)

            # 恢复数据�?            db_backup = latest_backup / 'data' / 'scada.db'
            if db_backup.exists():
                dest_db = self.project_root / 'data' / 'scada.db'
                shutil.copy2(db_backup, dest_db)

            result['status'] = 'success'
            result['backup_used'] = str(latest_backup)

        except Exception as e:
            result['status'] = 'error'
            result['error'] = str(e)

        return result


def format_report(result: Dict[str, Any]) -> str:
    """格式化报�?""
    lines = []
    lines.append("=" * 60)

    if 'checks' in result:
        lines.append("部署环境检查报�?)
        lines.append(f"时间: {result['timestamp']}")
        lines.append(f"状�? {result['status'].upper()}")
        lines.append("-" * 60)
        for check in result['checks']:
            status_icon = '�? if check['status'] == 'ok' else '�? if check['status'] == 'warning' else '�?
            lines.append(f"  {status_icon} {check['name']}: {check['message']}")
    elif 'steps' in result:
        lines.append("部署报告")
        lines.append(f"时间: {result['timestamp']}")
        lines.append(f"状�? {result.get('status', 'unknown').upper()}")
        lines.append("-" * 60)
        for step in result['steps']:
            status_icon = '�? if step['status'] == 'ok' else '�? if step['status'] == 'warning' else '�?
            lines.append(f"  {status_icon} {step['name']}")
    elif 'backup_path' in result:
        lines.append("备份报告")
        lines.append(f"时间: {result['timestamp']}")
        lines.append(f"路径: {result['backup_path']}")
        lines.append(f"状�? {result.get('status', 'unknown').upper()}")
        lines.append(f"大小: {result.get('size_mb', '?')}MB")
        lines.append(f"文件: {', '.join(result.get('files', []))}")

    lines.append("=" * 60)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description='自动化部署脚�?)
    parser.add_argument('command', choices=['check', 'backup', 'deploy', 'rollback'], help='命令')
    parser.add_argument('--tag', help='版本标签')

    args = parser.parse_args()

    deployer = Deployer()

    if args.command == 'check':
        result = deployer.check_environment()
    elif args.command == 'backup':
        result = deployer.backup(args.tag)
    elif args.command == 'deploy':
        result = deployer.deploy()
    elif args.command == 'rollback':
        result = deployer.rollback()

    print(format_report(result))

    # 保存日志
    log_file = project_root / 'logs' / f'deploy_{datetime.now().strftime("%Y%m%d_%H%M%S")}.json'
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=str)

    print(f"\n日志已保�? {log_file}")


if __name__ == '__main__':
    main()
