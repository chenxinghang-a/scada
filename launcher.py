"""
SCADA 桌面应用启动器
====================
Flask 后台运行 + pywebview 原生窗口显示
双击打开就是桌面软件，不需要命令行和浏览器
"""

import sys
import os
import time
import threading
from pathlib import Path

# ============================================================
# 路径处理
# ============================================================

def get_base_dir():
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return Path(__file__).parent

def get_work_dir():
    if getattr(sys, 'frozen', False):
        return Path(os.path.dirname(sys.executable))
    return Path(__file__).parent

BASE_DIR = get_base_dir()
WORK_DIR = get_work_dir()

sys.path.insert(0, str(BASE_DIR))

# 确保工作目录
for subdir in ['data', 'logs', 'exports']:
    (WORK_DIR / subdir).mkdir(parents=True, exist_ok=True)

# 复制配置
if not (WORK_DIR / '配置').exists() and (BASE_DIR / '配置').exists():
    import shutil
    shutil.copytree(BASE_DIR / '配置', WORK_DIR / '配置')

if not (WORK_DIR / '.env').exists() and (BASE_DIR / '.env.example').exists():
    import shutil
    shutil.copy2(BASE_DIR / '.env.example', WORK_DIR / '.env')

# 覆盖路径
import paths
paths.DATA_DIR = WORK_DIR / 'data'
paths.LOG_DIR = WORK_DIR / 'logs'
paths.EXPORT_DIR = WORK_DIR / 'exports'
paths.CONFIG_DIR = WORK_DIR / '配置' if (WORK_DIR / '配置').exists() else BASE_DIR / '配置'

# ============================================================
# 启动 Flask + 窗口
# ============================================================

PORT = 5000


def start_flask(mode: str):
    """后台线程启动 Flask"""
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(paths.LOG_DIR / 'scada.log'), encoding='utf-8')
        ]
    )

    # 设置参数
    if mode == 'real':
        if '--real' not in sys.argv:
            sys.argv.append('--real')
    elif mode == 'simulator':
        if '--simulator' not in sys.argv:
            sys.argv.append('--simulator')

    try:
        from run import main as run_main
        run_main()
    except Exception as e:
        logging.error(f"Flask 启动失败: {e}", exc_info=True)


def wait_for_server(port: int, timeout: float = 30.0) -> bool:
    """等待 Flask 服务就绪"""
    import socket
    start = time.time()
    while time.time() - start < timeout:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('127.0.0.1', port))
            sock.close()
            if result == 0:
                return True
        except:
            pass
        time.sleep(0.5)
    return False


def main():
    # 解析模式
    mode = 'simulated'
    if '--real' in sys.argv:
        mode = 'real'
    elif '--simulator' in sys.argv:
        mode = 'simulator'

    port = 5001 if mode == 'real' else 5000

    print(f"SCADA 系统启动中... 模式: {mode}")

    # 后台启动 Flask
    flask_thread = threading.Thread(target=start_flask, args=(mode,), daemon=True)
    flask_thread.start()

    # 等待 Flask 就绪
    print(f"等待服务启动 (端口 {port})...")
    if not wait_for_server(port, timeout=30):
        print("服务启动超时")
        input("按回车退出...")
        return

    print(f"服务已就绪，打开窗口...")

    # 打开原生窗口
    try:
        import webview

        window = webview.create_window(
            title='工业数据采集与监控系统 SCADA v2.1',
            url=f'http://127.0.0.1:{port}',
            width=1400,
            height=900,
            min_size=(1024, 768),
            resizable=True,
            confirm_close=True,
            text_select=True,
        )
        webview.start(debug=False)

    except ImportError:
        # 没有 pywebview，降级用浏览器
        print("pywebview 未安装，使用浏览器打开...")
        import webbrowser
        webbrowser.open(f'http://127.0.0.1:{port}')
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

    print("SCADA 系统已停止")


if __name__ == '__main__':
    main()
