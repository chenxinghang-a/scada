# -*- coding: utf-8 -*-
"""诊断包装器：定时 dump 所有线程的调用栈。

用途：后端服务出现「采集在跑、落库停滞、日志静默」时定位到底卡在哪个线程/哪一行。
      run.py 的消费链路异常都有日志，此时日志静默，说明线程卡在某个不抛异常的
      等待点上（锁 / 队列 / 网络）。faulthandler 能在不中断进程的前提下打印
      所有线程的 Python 栈。

用法（在仓库根执行，不改动任何生产代码）：
    .venv/Scripts/python.exe tools/diag_run.py

产出：data/diag_traceback_1.txt / _2.txt / _3.txt（每 45 秒一份，共 3 份）
"""
import faulthandler
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

DIAG_DIR = ROOT / 'data'
DIAG_DIR.mkdir(parents=True, exist_ok=True)

# 第一份在 45 秒后 dump，之后每 45 秒一份，共 3 份 —— 覆盖"启动后约 50 秒落库停滞"的窗口
_fds = []
for i in range(1, 4):
    f = open(DIAG_DIR / f'diag_traceback_{i}.txt', 'w', encoding='utf-8')
    _fds.append(f)

faulthandler.dump_traceback_later(45, repeat=True, file=_fds[0])


def _rotate():  # pragma: no cover - 诊断辅助
    """在 90s / 135s 时把 dump 目标切换到第 2、3 个文件。"""
    import threading
    import time

    def _worker():
        time.sleep(90)
        faulthandler.cancel_dump_traceback_later()
        faulthandler.dump_traceback_later(45, repeat=True, file=_fds[1])
        time.sleep(45)
        faulthandler.cancel_dump_traceback_later()
        faulthandler.dump_traceback_later(45, repeat=False, file=_fds[2])

    threading.Thread(target=_worker, daemon=True, name='diag-rotate').start()


_rotate()

# 以 __main__ 身份执行 run.py，使其 main() 正常启动
run_py = ROOT / 'run.py'
code = run_py.read_text(encoding='utf-8')
exec(compile(code, str(run_py), 'exec'), {'__name__': '__main__', '__file__': str(run_py)})
