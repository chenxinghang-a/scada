"""
配置迁移工具
处理配置文件版本升级和迁移
"""

import os
import sys
import yaml
import json
import shutil
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


class ConfigMigrator:
    """配置迁移器"""

    def __init__(self):
        self.project_root = project_root
        self.config_dir = project_root / '配置'
        self.backup_dir = project_root / '.config_backups'
        self.backup_dir.mkdir(exist_ok=True)

    def check_config_version(self) -> Dict[str, Any]:
        """检查配置版本"""
        version_file = self.config_dir / 'version.json'

        if version_file.exists():
            with open(version_file, 'r', encoding='utf-8') as f:
                return json.load(f)

        return {'version': '0.0.0', 'last_migration': None}

    def save_config_version(self, version: str):
        """保存配置版本"""
        version_file = self.config_dir / 'version.json'
        data = {
            'version': version,
            'last_migration': datetime.now().isoformat(),
            'migrated_by': 'config_migrator',
        }
        with open(version_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

    def backup_configs(self) -> str:
        """备份当前配置"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = self.backup_dir / f'config_backup_{timestamp}'
        backup_path.mkdir(parents=True, exist_ok=True)

        # 备份配置文件
        if self.config_dir.exists():
            for file in self.config_dir.glob('*.yaml'):
                shutil.copy2(file, backup_path / file.name)
            for file in self.config_dir.glob('*.json'):
                shutil.copy2(file, backup_path / file.name)

        # 保存版本信息
        version = self.check_config_version()
        with open(backup_path / 'version.json', 'w', encoding='utf-8') as f:
            json.dump(version, f, indent=2)

        return str(backup_path)

    def migrate_v1_to_v2(self) -> List[str]:
        """迁移 v1 到 v2"""
        changes = []

        # 1. devices.yaml 添加默认zone字段
        devices_file = self.config_dir / 'devices.yaml'
        if devices_file.exists():
            with open(devices_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}

            if 'devices' in config:
                for device in config['devices']:
                    if 'zone' not in device:
                        device['zone'] = 'default'
                        changes.append(f"设备 {device.get('id', '?')} 添加默认zone")

                with open(devices_file, 'w', encoding='utf-8') as f:
                    yaml.dump(config, f, allow_unicode=True, default_flow_style=False)

        # 2. alarms.yaml 添加默认dedup配置
        alarms_file = self.config_dir / 'alarms.yaml'
        if alarms_file.exists():
            with open(alarms_file, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}

            if 'dedup' not in config:
                config['dedup'] = {
                    'enabled': True,
                    'cooldown_seconds': 30,
                    'suppress_after_acknowledge': 300,
                }
                changes.append("添加报警去重默认配置")

                with open(alarms_file, 'w', encoding='utf-8') as f:
                    yaml.dump(config, f, allow_unicode=True, default_flow_style=False)

        # 3. 更新版本号
        self.save_config_version('2.0.0')
        changes.append("配置版本更新为 2.0.0")

        return changes

    def run_migration(self) -> Dict[str, Any]:
        """运行迁移"""
        result = {
            'timestamp': datetime.now().isoformat(),
            'backup_path': None,
            'changes': [],
            'status': 'success',
        }

        try:
            # 备份当前配置
            result['backup_path'] = self.backup_configs()

            # 检查当前版本
            version = self.check_config_version()
            current_version = version.get('version', '0.0.0')

            if current_version < '2.0.0':
                changes = self.migrate_v1_to_v2()
                result['changes'].extend(changes)

            if not result['changes']:
                result['changes'].append('配置已是最新版本，无需迁移')

        except Exception as e:
            result['status'] = 'error'
            result['error'] = str(e)

        return result


def main():
    parser = argparse.ArgumentParser(description='配置迁移工具')
    parser.add_argument('command', choices=['check', 'migrate', 'backup', 'version'], help='命令')

    args = parser.parse_args()

    migrator = ConfigMigrator()

    if args.command == 'check':
        version = migrator.check_config_version()
        print(f"当前配置版本: {version.get('version', '未知')}")
        print(f"最后迁移: {version.get('last_migration', '无')}")

    elif args.command == 'migrate':
        result = migrator.run_migration()
        print(f"状态: {result['status']}")
        if result.get('backup_path'):
            print(f"备份路径: {result['backup_path']}")
        if result.get('changes'):
            print("变更:")
            for change in result['changes']:
                print(f"  - {change}")

    elif args.command == 'backup':
        path = migrator.backup_configs()
        print(f"配置已备份到: {path}")

    elif args.command == 'version':
        version = migrator.check_config_version()
        print(json.dumps(version, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
