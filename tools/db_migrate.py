"""
数据库迁移脚本管理工具
用于管理数据库schema变更和数据迁移

使用方法:
    python tools/db_migrate.py create <migration_name>  # 创建新迁移
    python tools/db_migrate.py upgrade                   # 执行所有待执行的迁移
    python tools/db_migrate.py downgrade <version>       # 回滚到指定版本
    python tools/db_migrate.py status                    # 查看迁移状态
"""

import os
import sys
import json
import sqlite3
import argparse
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

MIGRATIONS_DIR = project_root / 'migrations'
DB_PATH = project_root / 'data' / 'scada.db'


class MigrationManager:
    """数据库迁移管理器"""

    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(DB_PATH)
        self.migrations_dir = MIGRATIONS_DIR
        self.migrations_dir.mkdir(parents=True, exist_ok=True)
        self._init_migrations_table()

    def _init_migrations_table(self):
        """初始化迁移记录表"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    checksum TEXT
                )
            ''')
            conn.commit()

    def get_applied_versions(self) -> List[int]:
        """获取已应用的迁移版本"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute('SELECT version FROM schema_migrations ORDER BY version')
            return [row[0] for row in cursor.fetchall()]

    def get_pending_migrations(self) -> List[Dict[str, Any]]:
        """获取待执行的迁移"""
        applied = set(self.get_applied_versions())
        migrations = []

        for file in sorted(self.migrations_dir.glob('*.py')):
            if file.name.startswith('__'):
                continue

            # 从文件名解析版本号
            parts = file.name.split('_', 1)
            if len(parts) < 2 or not parts[0].isdigit():
                continue

            version = int(parts[0])
            if version not in applied:
                migrations.append({
                    'version': version,
                    'name': parts[1].replace('.py', ''),
                    'file': str(file),
                })

        return migrations

    def create_migration(self, name: str) -> str:
        """创建新迁移文件"""
        # 获取下一个版本号
        existing = list(self.migrations_dir.glob('*.py'))
        versions = []
        for f in existing:
            parts = f.name.split('_', 1)
            if parts[0].isdigit():
                versions.append(int(parts[0]))

        next_version = max(versions, default=0) + 1
        filename = f"{next_version:04d}_{name}.py"
        filepath = self.migrations_dir / filename

        # 生成迁移模板
        template = f'''"""
迁移: {name}
版本: {next_version:04d}
创建时间: {datetime.now().isoformat()}
"""

import sqlite3


def upgrade(conn: sqlite3.Connection):
    """执行迁移"""
    cursor = conn.cursor()

    # TODO: 在这里添加升级SQL
    # cursor.execute("""
    #     CREATE TABLE IF NOT EXISTS example (
    #         id INTEGER PRIMARY KEY AUTOINCREMENT,
    #         name TEXT NOT NULL,
    #         created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    #     )
    # """)

    conn.commit()


def downgrade(conn: sqlite3.Connection):
    """回滚迁移"""
    cursor = conn.cursor()

    # TODO: 在这里添加回滚SQL
    # cursor.execute("DROP TABLE IF EXISTS example")

    conn.commit()
'''

        filepath.write_text(template, encoding='utf-8')
        print(f"创建迁移文件: {filepath}")
        return str(filepath)

    def upgrade(self):
        """执行所有待执行的迁移"""
        pending = self.get_pending_migrations()

        if not pending:
            print("没有待执行的迁移")
            return

        print(f"发现 {len(pending)} 个待执行的迁移:")

        with sqlite3.connect(self.db_path) as conn:
            for migration in pending:
                print(f"  执行迁移 {migration['version']:04d}: {migration['name']}")

                try:
                    # 动态加载迁移模块
                    module = self._load_migration_module(migration['file'])

                    # 执行升级
                    module.upgrade(conn)

                    # 记录迁移
                    conn.execute(
                        'INSERT INTO schema_migrations (version, name) VALUES (?, ?)',
                        (migration['version'], migration['name'])
                    )
                    conn.commit()

                    print(f"    ✓ 迁移完成")

                except Exception as e:
                    conn.rollback()
                    print(f"    ✗ 迁移失败: {e}")
                    raise

        print("所有迁移执行完成")

    def downgrade(self, target_version: int):
        """回滚到指定版本"""
        applied = self.get_applied_versions()

        if target_version not in applied:
            print(f"版本 {target_version} 未应用，无需回滚")
            return

        # 找出需要回滚的迁移（按版本倒序）
        to_rollback = [v for v in sorted(applied, reverse=True) if v > target_version]

        if not to_rollback:
            print(f"已经是版本 {target_version}，无需回滚")
            return

        print(f"回滚 {len(to_rollback)} 个迁移:")

        with sqlite3.connect(self.db_path) as conn:
            for version in to_rollback:
                # 找到对应的迁移文件
                migration_file = self._find_migration_file(version)
                if not migration_file:
                    print(f"  警告: 找不到版本 {version} 的迁移文件，跳过")
                    continue

                print(f"  回滚迁移 {version:04d}")

                try:
                    module = self._load_migration_module(migration_file)
                    module.downgrade(conn)

                    conn.execute('DELETE FROM schema_migrations WHERE version = ?', (version,))
                    conn.commit()

                    print(f"    ✓ 回滚完成")

                except Exception as e:
                    conn.rollback()
                    print(f"    ✗ 回滚失败: {e}")
                    raise

        print(f"回滚完成，当前版本: {target_version}")

    def status(self):
        """显示迁移状态"""
        applied = self.get_applied_versions()
        pending = self.get_pending_migrations()

        print("数据库迁移状态:")
        print(f"  数据库: {self.db_path}")
        print(f"  已应用: {len(applied)} 个迁移")
        print(f"  待执行: {len(pending)} 个迁移")

        if applied:
            print("\n已应用的迁移:")
            for version in applied:
                print(f"  ✓ {version:04d}")

        if pending:
            print("\n待执行的迁移:")
            for m in pending:
                print(f"  ○ {m['version']:04d}: {m['name']}")

    def _load_migration_module(self, filepath: str):
        """动态加载迁移模块"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("migration", filepath)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _find_migration_file(self, version: int) -> str:
        """查找指定版本的迁移文件"""
        for file in self.migrations_dir.glob(f'{version:04d}_*.py'):
            return str(file)
        return None


def main():
    parser = argparse.ArgumentParser(description='数据库迁移管理工具')
    subparsers = parser.add_subparsers(dest='command', help='命令')

    # create命令
    create_parser = subparsers.add_parser('create', help='创建新迁移')
    create_parser.add_argument('name', help='迁移名称')

    # upgrade命令
    subparsers.add_parser('upgrade', help='执行所有待执行的迁移')

    # downgrade命令
    downgrade_parser = subparsers.add_parser('downgrade', help='回滚到指定版本')
    downgrade_parser.add_argument('version', type=int, help='目标版本号')

    # status命令
    subparsers.add_parser('status', help='查看迁移状态')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    manager = MigrationManager()

    if args.command == 'create':
        manager.create_migration(args.name)
    elif args.command == 'upgrade':
        manager.upgrade()
    elif args.command == 'downgrade':
        manager.downgrade(args.version)
    elif args.command == 'status':
        manager.status()


if __name__ == '__main__':
    main()
