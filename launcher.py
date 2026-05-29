"""
SCADA 启动器 — Flask + 自动开浏览器
"""

import sys
import os
import time
import webbrowser
import threading
from pathlib import Path

# 路径处理
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys._MEIPASS)
    WORK_DIR = Path(os.path.dirname(sys.executable))
else:
    BASE_DIR = Path(__file__).parent
    WORK_DIR = BASE_DIR

sys.path.insert(0, str(BASE_DIR))

for d in ['data', 'logs', 'exports']:
    (WORK_DIR / d).mkdir(parents=True, exist_ok=True)

if not (WORK_DIR / '.env').exists() and (BASE_DIR / '.env.example').exists():
    import shutil
    shutil.copy2(BASE_DIR / '.env.example', WORK_DIR / '.env')


def open_browser(port):
    time.sleep(2)
    webbrowser.open(f'http://localhost:{port}')


def main():
    mode = 'simulated'
    port = 5000
    if '--real' in sys.argv:
        mode = 'real'
        port = 5001
    elif '--simulator' in sys.argv:
        mode = 'simulator'

    print(f"模式: {mode} | 地址: http://localhost:{port}")
    print(f"账号: admin / admin123 | Ctrl+C 停止")

    # 自动开浏览器
    threading.Thread(target=open_browser, args=(port,), daemon=True).start()

    from run import main as run_main
    run_main()


if __name__ == '__main__':
    main()
