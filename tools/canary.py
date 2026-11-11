"""回归金丝雀：把**历史上的旧实现**装回去，看今天的测试会不会红。

不是注入假错误，而是回答一个真问题：
「我们修过的 bug，如果今天再出现一次，测试能不能发现？」

做法：
1. `git show <rev>:<path>` 取旧版本字节；
2. 临时覆盖目标文件（先备份当前字节）；
3. 跑指定用例，看是否失败；
4. **按字节还原**并用 `git status` 校验，保证工作区干净。

用法：
    python tools/canary.py 采集层/modbus_client.py "21e5751^" tests/test_sibling_paths_parity.py

退出码：0 = 旧实现也能通过（说明测试是假绿）；1 = 测试成功发现回归；2 = 参数错误。
"""

from __future__ import annotations

import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _git(*args: str, binary: bool = False):
    proc = subprocess.run(['git', *args], cwd=ROOT, capture_output=True)
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.decode('utf-8', 'replace'))
    return proc.stdout if binary else proc.stdout.decode('utf-8', 'replace')


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) < 3:
        print(__doc__)
        return 2
    rel, rev = argv[0], argv[1]
    cases = argv[2:]
    target = os.path.join(ROOT, rel.replace('/', os.sep))

    current = io.open(target, 'rb').read()
    old = _git('show', f'{rev}:{rel}', binary=True)
    print(f'[canary] {rel} @ {rev}: {len(old)}B vs 当前 {len(current)}B', flush=True)

    try:
        with io.open(target, 'wb') as handle:
            handle.write(old)
        proc = subprocess.run(
            [sys.executable, '-m', 'pytest', *cases, '-q', '--tb=no',
             '-p', 'no:cacheprovider', '--timeout=60'],
            cwd=ROOT, capture_output=True, text=True,
            encoding='utf-8', errors='replace')
        lines = [l for l in proc.stdout.splitlines()
                 if 'passed' in l or 'failed' in l]
        print('[canary] 旧实现:', lines[-1] if lines else proc.stdout.strip()[-200:])
        caught = proc.returncode != 0
        print('[canary] 判定:', '测试能发现该回归' if caught
              else '!! 旧实现照样通过 —— 这条测试是假绿')
        return 1 if caught else 0
    finally:
        with io.open(target, 'wb') as handle:
            handle.write(current)
        same = io.open(target, 'rb').read() == current
        status = _git('status', '--porcelain', '--', rel).strip()
        print(f'[canary] 还原一致={same} git={status or "clean"}')


if __name__ == '__main__':
    raise SystemExit(main())
