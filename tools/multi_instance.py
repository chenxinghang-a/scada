"""
多实例部署管理工具
用于在同一台服务器上运行多个SCADA实例

使用方法:
    python tools/multi_instance.py create <instance_name> [port]  # 创建新实例
    python tools/multi_instance.py start <instance_name>          # 启动实例
    python tools/multi_instance.py stop <instance_name>           # 停止实例
    python tools/multi_instance.py status                         # 查看所有实例状态
    python tools/multi_instance.py delete <instance_name>         # 删除实例
"""

import os
import sys
import json
import shutil
import argparse
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# 添加项目根目录到路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

INSTANCES_DIR = project_root / 'instances'
BASE_PORT = 5000


class MultiInstanceManager:
    """多实例管理器"""

    def __init__(self):
        self.instances_dir = INSTANCES_DIR
        self.instances_dir.mkdir(parents=True, exist_ok=True)

    def get_instance_dir(self, name: str) -> Path:
        """获取实例目录"""
        return self.instances_dir / name

    def get_instance_config(self, name: str) -> Optional[Dict]:
        """获取实例配置"""
        config_file = self.get_instance_dir(name) / 'instance.json'
        if config_file.exists():
            return json.loads(config_file.read_text(encoding='utf-8'))
        return None

    def save_instance_config(self, name: str, config: Dict):
        """保存实例配置"""
        config_file = self.get_instance_dir(name) / 'instance.json'
        config_file.write_text(
            json.dumps(config, indent=2, ensure_ascii=False),
            encoding='utf-8'
        )

    def get_next_port(self) -> int:
        """获取下一个可用端口"""
        used_ports = set()

        for instance_dir in self.instances_dir.iterdir():
            if instance_dir.is_dir():
                config = self.get_instance_config(instance_dir.name)
                if config:
                    used_ports.add(config.get('port', 0))

        port = BASE_PORT + 1
        while port in used_ports:
            port += 1

        return port

    def create(self, name: str, port: int = None) -> bool:
        """创建新实例"""
        instance_dir = self.get_instance_dir(name)

        if instance_dir.exists():
            print(f"实例 '{name}' 已存在")
            return False

        if port is None:
            port = self.get_next_port()

        # 创建实例目录结构
        instance_dir.mkdir(parents=True, exist_ok=True)
        (instance_dir / 'data').mkdir(exist_ok=True)
        (instance_dir / 'logs').mkdir(exist_ok=True)
        (instance_dir / '配置').mkdir(exist_ok=True)

        # 复制配置文件模板
        config_template = project_root / '配置'
        if config_template.exists():
            for file in config_template.glob('*.yaml'):
                shutil.copy2(file, instance_dir / '配置' / file.name)
            for file in config_template.glob('*.yml'):
                shutil.copy2(file, instance_dir / '配置' / file.name)

        # 创建实例配置
        config = {
            'name': name,
            'port': port,
            'created_at': datetime.now().isoformat(),
            'status': 'stopped',
            'pid': None,
            'database': str(instance_dir / 'data' / 'scada.db'),
            'config_dir': str(instance_dir / '配置'),
            'log_dir': str(instance_dir / 'logs'),
        }
        self.save_instance_config(name, config)

        # 创建启动脚本
        self._create_start_script(name, config)

        print(f"实例 '{name}' 已创建")
        print(f"  端口: {port}")
        print(f"  目录: {instance_dir}")

        return True

    def _create_start_script(self, name: str, config: Dict):
        """创建实例启动脚本"""
        instance_dir = self.get_instance_dir(name)

        # Windows启动脚本
        bat_content = f'''@echo off
cd /d "{project_root}"
set SCADA_INSTANCE={name}
set SCADA_PORT={config['port']}
set SCADA_DATA_DIR={config['database']}
set SCADA_CONFIG_DIR={config['config_dir']}
set SCADA_LOG_DIR={config['log_dir']}
python run.py
'''
        bat_file = instance_dir / 'start.bat'
        bat_file.write_text(bat_content, encoding='utf-8')

        # Linux启动脚本
        sh_content = f'''#!/bin/bash
cd "{project_root}"
export SCADA_INSTANCE="{name}"
export SCADA_PORT="{config['port']}"
export SCADA_DATA_DIR="{config['database']}"
export SCADA_CONFIG_DIR="{config['config_dir']}"
export SCADA_LOG_DIR="{config['log_dir']}"
python run.py
'''
        sh_file = instance_dir / 'start.sh'
        sh_file.write_text(sh_content, encoding='utf-8')
        os.chmod(str(sh_file), 0o755)

    def start(self, name: str) -> bool:
        """启动实例"""
        config = self.get_instance_config(name)

        if not config:
            print(f"实例 '{name}' 不存在")
            return False

        if config.get('status') == 'running':
            print(f"实例 '{name}' 已在运行中")
            return False

        # 设置环境变量
        env = os.environ.copy()
        env['SCADA_INSTANCE'] = name
        env['SCADA_PORT'] = str(config['port'])
        env['SCADA_DATA_DIR'] = config['database']
        env['SCADA_CONFIG_DIR'] = config['config_dir']
        env['SCADA_LOG_DIR'] = config['log_dir']

        # 启动进程
        try:
            process = subprocess.Popen(
                [sys.executable, 'run.py'],
                cwd=str(project_root),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            config['status'] = 'running'
            config['pid'] = process.pid
            config['started_at'] = datetime.now().isoformat()
            self.save_instance_config(name, config)

            print(f"实例 '{name}' 已启动")
            print(f"  PID: {process.pid}")
            print(f"  端口: {config['port']}")
            print(f"  访问: http://localhost:{config['port']}")

            return True

        except Exception as e:
            print(f"启动失败: {e}")
            return False

    def stop(self, name: str) -> bool:
        """停止实例"""
        config = self.get_instance_config(name)

        if not config:
            print(f"实例 '{name}' 不存在")
            return False

        if config.get('status') != 'running':
            print(f"实例 '{name}' 未在运行")
            return False

        pid = config.get('pid')
        if pid:
            try:
                # Windows
                if sys.platform == 'win32':
                    subprocess.run(['taskkill', '/F', '/PID', str(pid)],
                                 capture_output=True)
                else:
                    os.kill(pid, 15)  # SIGTERM
            except Exception as e:
                print(f"停止进程失败: {e}")

        config['status'] = 'stopped'
        config['pid'] = None
        config['stopped_at'] = datetime.now().isoformat()
        self.save_instance_config(name, config)

        print(f"实例 '{name}' 已停止")
        return True

    def status(self) -> List[Dict]:
        """查看所有实例状态"""
        instances = []

        for instance_dir in self.instances_dir.iterdir():
            if instance_dir.is_dir():
                config = self.get_instance_config(instance_dir.name)
                if config:
                    # 检查进程是否实际在运行
                    if config.get('status') == 'running' and config.get('pid'):
                        pid = config['pid']
                        try:
                            # Windows
                            if sys.platform == 'win32':
                                result = subprocess.run(
                                    ['tasklist', '/FI', f'PID eq {pid}'],
                                    capture_output=True, text=True
                                )
                                is_running = str(pid) in result.stdout
                            else:
                                os.kill(pid, 0)
                                is_running = True
                        except:
                            is_running = False

                        if not is_running:
                            config['status'] = 'stopped'
                            config['pid'] = None
                            self.save_instance_config(instance_dir.name, config)

                    instances.append(config)

        return instances

    def delete(self, name: str) -> bool:
        """删除实例"""
        instance_dir = self.get_instance_dir(name)

        if not instance_dir.exists():
            print(f"实例 '{name}' 不存在")
            return False

        config = self.get_instance_config(name)
        if config and config.get('status') == 'running':
            self.stop(name)

        shutil.rmtree(instance_dir)

        print(f"实例 '{name}' 已删除")
        return True


def show_status(manager: MultiInstanceManager):
    """显示所有实例状态"""
    instances = manager.status()

    if not instances:
        print("暂无实例")
        return

    print("实例状态:")
    print("-" * 60)
    print(f"{'名称':<15} {'端口':<8} {'状态':<10} {'PID':<10} {'创建时间'}")
    print("-" * 60)

    for inst in instances:
        name = inst.get('name', '?')
        port = inst.get('port', '?')
        status = inst.get('status', '?')
        pid = inst.get('pid', '-')
        created = inst.get('created_at', '?')[:19]

        status_icon = '●' if status == 'running' else '○'

        print(f"{status_icon} {name:<13} {port:<8} {status:<10} {pid:<10} {created}")


def main():
    parser = argparse.ArgumentParser(description='多实例部署管理工具')
    subparsers = parser.add_subparsers(dest='command', help='命令')

    # create命令
    create_parser = subparsers.add_parser('create', help='创建新实例')
    create_parser.add_argument('name', help='实例名称')
    create_parser.add_argument('port', nargs='?', type=int, help='端口号')

    # start命令
    start_parser = subparsers.add_parser('start', help='启动实例')
    start_parser.add_argument('name', help='实例名称')

    # stop命令
    stop_parser = subparsers.add_parser('stop', help='停止实例')
    stop_parser.add_argument('name', help='实例名称')

    # status命令
    subparsers.add_parser('status', help='查看所有实例状态')

    # delete命令
    delete_parser = subparsers.add_parser('delete', help='删除实例')
    delete_parser.add_argument('name', help='实例名称')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    manager = MultiInstanceManager()

    if args.command == 'create':
        manager.create(args.name, args.port)
    elif args.command == 'start':
        manager.start(args.name)
    elif args.command == 'stop':
        manager.stop(args.name)
    elif args.command == 'status':
        show_status(manager)
    elif args.command == 'delete':
        manager.delete(args.name)


if __name__ == '__main__':
    main()
