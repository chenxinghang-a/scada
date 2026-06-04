"""
配置版本控制工具
用于管理配置文件的版本和变更历史

使用方法:
    python tools/config_version.py snapshot [message]   # 创建配置快照
    python tools/config_version.py history               # 查看配置历史
    python tools/config_version.py diff <version1> <version2>  # 比较版本差异
    python tools/config_version.py restore <version>     # 恢复到指定版本
    python tools/config_version.py export <output_dir>   # 导出当前配置
"""

import os
import sys
import json
import hashlib
import shutil
import argparse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

CONFIG_DIR = project_root / '配置'
VERSIONS_DIR = project_root / '.config_versions'
HISTORY_FILE = VERSIONS_DIR / 'history.json'


class ConfigVersionManager:
    """配置版本管理器"""

    def __init__(self, config_dir: str = None, versions_dir: str = None):
        self.config_dir = Path(config_dir) if config_dir else CONFIG_DIR
        self.versions_dir = Path(versions_dir) if versions_dir else VERSIONS_DIR
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self.history = self._load_history()

    def _load_history(self) -> List[Dict[str, Any]]:
        """加载版本历史"""
        if HISTORY_FILE.exists():
            return json.loads(HISTORY_FILE.read_text(encoding='utf-8'))
        return []

    def _save_history(self):
        """保存版本历史"""
        HISTORY_FILE.write_text(
            json.dumps(self.history, indent=2, ensure_ascii=False),
            encoding='utf-8'
        )

    def _calculate_checksum(self, filepath: Path) -> str:
        """计算文件校验和"""
        content = filepath.read_bytes()
        return hashlib.sha256(content).hexdigest()

    def _get_config_files(self) -> Dict[str, str]:
        """获取所有配置文件内容"""
        configs = {}
        for file in self.config_dir.glob('*.yaml'):
            configs[file.name] = file.read_text(encoding='utf-8')
        for file in self.config_dir.glob('*.yml'):
            configs[file.name] = file.read_text(encoding='utf-8')
        for file in self.config_dir.glob('*.json'):
            configs[file.name] = file.read_text(encoding='utf-8')
        return configs

    def snapshot(self, message: str = '') -> int:
        """创建配置快照"""
        # 生成版本号
        version = len(self.history) + 1
        timestamp = datetime.now().isoformat()

        # 创建版本目录
        version_dir = self.versions_dir / f'v{version:04d}'
        version_dir.mkdir(parents=True, exist_ok=True)

        # 复制配置文件
        configs = self._get_config_files()
        checksums = {}

        for filename, content in configs.items():
            dest = version_dir / filename
            dest.write_text(content, encoding='utf-8')
            checksums[filename] = hashlib.sha256(content.encode()).hexdigest()

        # 记录历史
        entry = {
            'version': version,
            'timestamp': timestamp,
            'message': message,
            'files': list(configs.keys()),
            'checksums': checksums,
        }
        self.history.append(entry)
        self._save_history()

        print(f"配置快照已创建: v{version:04d}")
        print(f"  时间: {timestamp}")
        print(f"  文件: {len(configs)} 个")
        if message:
            print(f"  说明: {message}")

        return version

    def history(self) -> List[Dict[str, Any]]:
        """查看配置历史"""
        return self.history

    def diff(self, version1: int, version2: int) -> Dict[str, Any]:
        """比较两个版本的差异"""
        v1_dir = self.versions_dir / f'v{version1:04d}'
        v2_dir = self.versions_dir / f'v{version2:04d}'

        if not v1_dir.exists():
            print(f"版本 v{version1:04d} 不存在")
            return {}
        if not v2_dir.exists():
            print(f"版本 v{version2:04d} 不存在")
            return {}

        # 获取两个版本的文件
        v1_files = {f.name: f.read_text(encoding='utf-8') for f in v1_dir.glob('*') if f.is_file()}
        v2_files = {f.name: f.read_text(encoding='utf-8') for f in v2_dir.glob('*') if f.is_file()}

        # 比较差异
        diff_result = {
            'added': [],
            'removed': [],
            'modified': [],
        }

        all_files = set(v1_files.keys()) | set(v2_files.keys())

        for filename in all_files:
            if filename not in v1_files:
                diff_result['added'].append(filename)
            elif filename not in v2_files:
                diff_result['removed'].append(filename)
            elif v1_files[filename] != v2_files[filename]:
                diff_result['modified'].append(filename)

        return diff_result

    def restore(self, version: int) -> bool:
        """恢复到指定版本"""
        version_dir = self.versions_dir / f'v{version:04d}'

        if not version_dir.exists():
            print(f"版本 v{version:04d} 不存在")
            return False

        # 备份当前配置
        backup_version = self.snapshot(f'备份当前配置（恢复到v{version:04d}前）')

        # 恢复配置文件
        restored = 0
        for file in version_dir.glob('*'):
            if file.is_file():
                dest = self.config_dir / file.name
                shutil.copy2(file, dest)
                restored += 1

        print(f"已恢复到版本 v{version:04d}")
        print(f"  恢复文件: {restored} 个")
        print(f"  备份版本: v{backup_version:04d}")

        return True

    def export(self, output_dir: str) -> bool:
        """导出当前配置"""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        configs = self._get_config_files()

        for filename, content in configs.items():
            dest = output_path / filename
            dest.write_text(content, encoding='utf-8')

        # 导出版本历史
        history_dest = output_path / 'version_history.json'
        history_dest.write_text(
            json.dumps(self.history, indent=2, ensure_ascii=False),
            encoding='utf-8'
        )

        print(f"配置已导出到: {output_path}")
        print(f"  文件: {len(configs)} 个")

        return True


def show_history(manager: ConfigVersionManager):
    """显示配置历史"""
    history = manager.history

    if not history:
        print("暂无配置历史")
        return

    print("配置版本历史:")
    print("-" * 60)

    for entry in reversed(history[-10:]):  # 显示最近10个版本
        version = entry['version']
        timestamp = entry['timestamp'][:19]  # 截断毫秒
        message = entry.get('message', '')
        files_count = len(entry.get('files', []))

        print(f"  v{version:04d}  {timestamp}  ({files_count} 文件)")
        if message:
            print(f"         {message}")

    if len(history) > 10:
        print(f"  ... 还有 {len(history) - 10} 个历史版本")


def show_diff(manager: ConfigVersionManager, version1: int, version2: int):
    """显示版本差异"""
    diff_result = manager.diff(version1, version2)

    if not diff_result:
        return

    print(f"版本 v{version1:04d} 与 v{version2:04d} 的差异:")
    print("-" * 60)

    if diff_result['added']:
        print(f"  新增文件 ({len(diff_result['added'])}):")
        for f in diff_result['added']:
            print(f"    + {f}")

    if diff_result['removed']:
        print(f"  删除文件 ({len(diff_result['removed'])}):")
        for f in diff_result['removed']:
            print(f"    - {f}")

    if diff_result['modified']:
        print(f"  修改文件 ({len(diff_result['modified'])}):")
        for f in diff_result['modified']:
            print(f"    ~ {f}")

    if not diff_result['added'] and not diff_result['removed'] and not diff_result['modified']:
        print("  两个版本完全相同")


def main():
    parser = argparse.ArgumentParser(description='配置版本控制工具')
    subparsers = parser.add_subparsers(dest='command', help='命令')

    # snapshot命令
    snapshot_parser = subparsers.add_parser('snapshot', help='创建配置快照')
    snapshot_parser.add_argument('message', nargs='?', default='', help='快照说明')

    # history命令
    subparsers.add_parser('history', help='查看配置历史')

    # diff命令
    diff_parser = subparsers.add_parser('diff', help='比较版本差异')
    diff_parser.add_argument('version1', type=int, help='版本号1')
    diff_parser.add_argument('version2', type=int, help='版本号2')

    # restore命令
    restore_parser = subparsers.add_parser('restore', help='恢复到指定版本')
    restore_parser.add_argument('version', type=int, help='目标版本号')

    # export命令
    export_parser = subparsers.add_parser('export', help='导出当前配置')
    export_parser.add_argument('output_dir', help='输出目录')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    manager = ConfigVersionManager()

    if args.command == 'snapshot':
        manager.snapshot(args.message)
    elif args.command == 'history':
        show_history(manager)
    elif args.command == 'diff':
        show_diff(manager, args.version1, args.version2)
    elif args.command == 'restore':
        manager.restore(args.version)
    elif args.command == 'export':
        manager.export(args.output_dir)


if __name__ == '__main__':
    main()
