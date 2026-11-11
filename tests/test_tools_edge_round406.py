"""工具边界用例：中文路径、整文件删除、CRLF 还原。

这三条都是对抗性复查指出的"静默漏采/污染工作区"场景，必须有回归。
"""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.mutation_spotcheck import spotcheck  # noqa: E402
from tools.szz_report import build_report  # noqa: E402


def _run(repo, *args):
    return subprocess.run(['git', *args], cwd=str(repo), check=True,
                          capture_output=True, text=True, encoding='utf-8').stdout


def _commit(repo, message):
    _run(repo, '-c', 'user.name=T', '-c', 'user.email=t@example.com',
         'commit', '-am', message)


def _init_repo(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    _run(repo, 'init')
    return repo


def test_szz_handles_chinese_path(tmp_path):
    repo = _init_repo(tmp_path)
    folder = repo / '采集层'
    folder.mkdir()
    target = folder / 'client.py'
    target.write_text('VALUE = 1\n', encoding='utf-8')
    _run(repo, 'add', '.')
    _commit(repo, 'init: add client')
    target.write_text('VALUE = 2\n', encoding='utf-8')
    _run(repo, 'add', '.')
    _commit(repo, 'fix: correct value')

    report = build_report(str(repo), limit=10)
    files = [m['file'] for m in report['mappings']]
    assert any(f.endswith('client.py') for f in files), f'中文路径被漏采: {files}'
    # 路径不能是被转义的形式
    assert not any('\\346' in f for f in files), files


def test_szz_handles_deleted_file(tmp_path):
    repo = _init_repo(tmp_path)
    target = repo / 'gone.py'
    target.write_text('A = 1\nB = 2\n', encoding='utf-8')
    _run(repo, 'add', '.')
    _commit(repo, 'init: add gone')
    target.unlink()
    _run(repo, 'add', '-A')
    _commit(repo, 'fix: remove obsolete module')

    report = build_report(str(repo), limit=10)
    assert any(m['file'] == 'gone.py' for m in report['mappings']), report['mappings']
    assert report['failed_hunks'] == 0, report


def test_spotcheck_preserves_crlf(tmp_path):
    module = tmp_path / 'calc_crlf.py'
    module.write_bytes(
        b'def positive(x):\r\n'
        b'    if x > 0:\r\n'
        b'        return True\r\n'
        b'    return False\r\n')
    test_file = tmp_path / 'test_calc_crlf.py'
    test_file.write_text(
        'import sys, pathlib\n'
        'sys.path.insert(0, str(pathlib.Path(__file__).parent))\n'
        'from calc_crlf import positive\n\n'
        'def test_positive():\n'
        '    assert positive(1) is True\n',
        encoding='utf-8')

    before = module.read_bytes()
    spotcheck(str(module), str(test_file), max_mutants=1)
    assert module.read_bytes() == before, 'CRLF 文件被工具改写（换行/内容不一致）'
