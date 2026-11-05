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


def _resolve_port(mode: str) -> int:
    """与 run.py 用同一个端口真源（`config.FlaskConfig` ← `SCADA_PORT`/`SCADA_REAL_PORT`）。

    round 370（BUG-003）：原来这里硬编码 5000/5001，完全无视环境变量与
    `配置/system.yaml` —— 用自定义端口启动后，launcher 打印并自动打开的
    是**另一个端口**的浏览器地址（页面 404，看起来像服务没起来）。
    """
    try:
        from config import WebConfig
        return int(WebConfig.REAL_PORT if mode == 'real' else WebConfig.PORT)
    except Exception:  # noqa: BLE001 —— 配置导入失败也要能开浏览器
        return 5001 if mode == 'real' else 5000


def main():
    mode = 'simulated'
    if '--real' in sys.argv:
        mode = 'real'
    elif '--simulator' in sys.argv:
        mode = 'simulator'

    port = _resolve_port(mode)

    print(f"模式: {mode} | 地址: http://localhost:{port}")
    print(f"账号: admin / admin123 | Ctrl+C 停止")

    # 自动开浏览器
    threading.Thread(target=open_browser, args=(port,), daemon=True).start()

    from run import main as run_main
    run_main()


if __name__ == '__main__':
    main()
