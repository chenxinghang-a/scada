"""开发工具自测：SZZ 报告 与 变异抽查。

两个工具都会调用子进程（git / pytest），这里用临时仓库与玩具模块验证：
* SZZ：能把"修复提交删掉的行"映射回"最初写入它的提交"；
* 变异：能识别被测试杀死的变异，且**必须还原原文件**（否则会污染工作区）。
"""

import io
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.mutation_spotcheck import find_mutants, spotcheck  # noqa: E402
from tools.szz_report import build_report  # noqa: E402


def _run(repo, *args):
    return subprocess.run(
        ['git', *args], cwd=str(repo), check=True,
        capture_output=True, text=True, encoding='utf-8',
    ).stdout


def _commit(repo, message):
    _run(repo, '-c', 'user.name=T', '-c', 'user.email=t@example.com',
         'commit', '-am', message)


@pytest.fixture()
def temp_repo(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    _run(repo, 'init')
    target = repo / 'mod.py'
    target.write_text('def f():\n    return 1\n', encoding='utf-8')
    _run(repo, 'add', '.')
    _commit(repo, 'init: add f')
    first = _run(repo, 'rev-parse', 'HEAD').strip()

    target.write_text('def f():\n    return 2\n', encoding='utf-8')
    _run(repo, 'add', '.')
    _commit(repo, 'fix: correct return value')
    return repo, first


def test_szz_maps_fix_back_to_introducer(temp_repo):
    repo, first = temp_repo
    report = build_report(str(repo), limit=10)

    assert report['fix_commits'] == 1
    assert report['mappings'], '没有产出任何映射'
    hit = [m for m in report['mappings'] if m['file'] == 'mod.py']
    assert hit, report['mappings']
    assert hit[0]['introduced_by'] == first[:8]
    assert 'init' in hit[0]['introduced_subject']


def test_find_mutants_covers_operators():
    src = (
        'def f(x):\n'
        '    if x >= 10 and x != 3:\n'
        '        return True\n'
        '    return None\n'
    )
    labels = {m.label for m in find_mutants(src, max_mutants=10)}
    assert 'ge→gt' in labels
    assert 'and→or' in labels
    assert 'True→False' in labels


def test_spotcheck_restores_file_and_reports(tmp_path):
    module = tmp_path / 'calc.py'
    module.write_text(
        'def positive(x):\n'
        '    if x > 0:\n'
        '        return True\n'
        '    return False\n',
        encoding='utf-8',
    )
    test_file = tmp_path / 'test_calc.py'
    test_file.write_text(
        'import sys, pathlib\n'
        'sys.path.insert(0, str(pathlib.Path(__file__).parent))\n'
        'from calc import positive\n\n'
        'def test_positive():\n'
        '    assert positive(1) is True\n'
        '    assert positive(-1) is False\n',
        encoding='utf-8',
    )
    before = module.read_text(encoding='utf-8')

    result = spotcheck(str(module), str(test_file), max_mutants=2)

    assert result['total'] >= 1
    assert result['killed'] + len(result['survived']) == result['total']
    assert module.read_text(encoding='utf-8') == before, '工具没有还原原文件'
