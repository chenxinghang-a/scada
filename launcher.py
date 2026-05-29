"""
SCADA 系统启动器（PyInstaller 打包入口）
=========================================
打包后双击 exe 即可启动，自动打开浏览器。
"""

import sys
import os
import time
import webbrowser
import threading
from pathlib import Path

# ============================================================
# 路径处理：兼容 PyInstaller 打包和直接运行
# ============================================================

def get_base_dir():
    """获取基础目录（打包后为临时解压目录，开发时为项目根目录）"""
    if getattr(sys, 'frozen', False):
        # PyInstaller 打包后
        return Path(sys._MEIPASS)
    else:
        return Path(__file__).parent


def get_work_dir():
    """获取工作目录（exe 所在目录，用于存放 data/logs/exports）"""
    if getattr(sys, 'frozen', False):
        return Path(os.path.dirname(sys.executable))
    else:
        return Path(__file__).parent


BASE_DIR = get_base_dir()
WORK_DIR = get_work_dir()

# 将项目根目录加入 Python 路径
sys.path.insert(0, str(BASE_DIR))

# 确保工作目录下的必要子目录存在
for subdir in ['data', 'logs', 'exports']:
    (WORK_DIR / subdir).mkdir(parents=True, exist_ok=True)

# 复制 .env 到工作目录（如果不存在）
env_example = BASE_DIR / '.env.example'
env_file = WORK_DIR / '.env'
if env_example.exists() and not env_file.exists():
    import shutil
    shutil.copy2(env_example, env_file)
    print(f"[初始化] 已创建 {env_file}")

# 复制配置文件到工作目录（如果不存在）
config_dir = WORK_DIR / '配置'
if not config_dir.exists():
    src_config = BASE_DIR / '配置'
    if src_config.exists():
        import shutil
        shutil.copytree(src_config, config_dir)
        print(f"[初始化] 已复制配置目录到 {config_dir}")

# ============================================================
# 覆盖路径模块，指向工作目录
# ============================================================

import paths
paths.DATA_DIR = WORK_DIR / 'data'
paths.LOG_DIR = WORK_DIR / 'logs'
paths.EXPORT_DIR = WORK_DIR / 'exports'
paths.CONFIG_DIR = WORK_DIR / '配置' if (WORK_DIR / '配置').exists() else BASE_DIR / '配置'

# ============================================================
# 启动系统
# ============================================================

def open_browser(port: int, delay: float = 2.0):
    """延迟打开浏览器"""
    time.sleep(delay)
    webbrowser.open(f'http://localhost:{port}')


def main():
    print()
    print("=" * 50)
    print("  工业数据采集与监控系统 v2.1")
    print("  Industrial SCADA System")
    print("=" * 50)
    print()

    # 解析参数
    mode = 'simulated'
    port = 5000
    if '--real' in sys.argv:
        mode = 'real'
        port = 5001
    elif '--simulator' in sys.argv:
        mode = 'simulator'
        port = 5000

    print(f"  运行模式: {mode}")
    print(f"  访问地址: http://localhost:{port}")
    print(f"  默认账号: admin / admin123")
    print(f"  按 Ctrl+C 停止")
    print()

    # 延迟打开浏览器
    threading.Thread(target=open_browser, args=(port,), daemon=True).start()

    # 导入并启动系统
    try:
        from run import main as run_main
        # 修改 sys.argv 传递模式参数
        if mode == 'real' and '--real' not in sys.argv:
            sys.argv.append('--real')
        elif mode == 'simulator' and '--simulator' not in sys.argv:
            sys.argv.append('--simulator')
        run_main()
    except KeyboardInterrupt:
        print("\n系统已停止")
    except Exception as e:
        print(f"\n启动失败: {e}")
        import traceback
        traceback.print_exc()
        input("\n按回车键退出...")


if __name__ == '__main__':
    main()
