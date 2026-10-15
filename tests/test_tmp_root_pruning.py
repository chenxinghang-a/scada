# -*- coding: utf-8 -*-
"""`.pytest_tmp-*` 保留策略（round 216）。

背景
====
`tests/conftest.py` 用「目录名带 PID」换取"零删除"（规避环境的批量删除保护），
代价是**每次运行留一个目录且永不回收** —— 本机实测堆积 **860 个**（数 GB）。

`_prune_old_tmp_roots` 补上保留策略。本文件钉住两件事：

1. **选哪些删**（纯函数 `_select_stale_tmp_roots`）—— 必须保留**最新**的 N 个，
   而不是"随便删几个"。
2. **真的删得掉**（外部进程通道可用）—— 否则策略只是"看起来在跑"，
   这正是本仓反复栽过的形态（见 `BUGS.md`：能力存在但没接线 / 断言不碰实质）。

⚠️ 判据用 mtime 排序而非名字：PID 大小与新旧无关（PID 会回绕）。
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

from tests.conftest import (
    TMP_ROOT_ACTIVE_WINDOW,
    TMP_ROOT_KEEP,
    TMP_ROOT_PRUNE_MAX,
    _delete_dirs_bulk,
    _dir_mtime,
    _prune_old_tmp_roots,
    _select_stale_tmp_roots,
)

_BASE_MTIME = 1_700_000_000.0


def _make(root: Path, names, mtimes=None) -> list:
    """造一批 `.pytest_tmp-*` 目录，返回路径列表（按给定顺序）。"""
    out = []
    for i, n in enumerate(names):
        d = root / n
        d.mkdir()
        (d / "f.txt").write_text("x", encoding="utf-8")
        if mtimes is not None:
            os.utime(d, (mtimes[i], mtimes[i]))
        out.append(d)
    return out


class TestSelectStale:
    """纯函数：保留最新的 keep 个，返回其余（要删的）。"""

    def test_keeps_newest_and_returns_the_rest(self, tmp_path):
        _make(tmp_path, [f".pytest_tmp-{i}" for i in range(1, 6)],
              mtimes=[_BASE_MTIME + i for i in range(5)])
        stale = sorted(d.name for d in _select_stale_tmp_roots(tmp_path, keep=2))
        assert stale == ['.pytest_tmp-1', '.pytest_tmp-2', '.pytest_tmp-3'], (
            f"应删最旧的 3 个（保留 4/5），实得 {stale}")

    def test_order_is_by_mtime_not_by_pid(self, tmp_path):
        """PID 大小与新旧无关 —— 名字最小的必须能被保留。"""
        # .pytest_tmp-9 最旧，.pytest_tmp-1 最新
        _make(tmp_path, ['.pytest_tmp-9', '.pytest_tmp-1'],
              mtimes=[_BASE_MTIME, _BASE_MTIME + 100])
        stale = [d.name for d in _select_stale_tmp_roots(tmp_path, keep=1)]
        assert stale == ['.pytest_tmp-9'], (
            f"应按 mtime 而非名字排序，实得 {stale}")

    def test_noop_when_within_keep(self, tmp_path):
        _make(tmp_path, ['.pytest_tmp-1', '.pytest_tmp-2'])
        assert _select_stale_tmp_roots(tmp_path, keep=3) == []

    def test_empty_root(self, tmp_path):
        assert _select_stale_tmp_roots(tmp_path, keep=3) == []

    def test_ignores_files_and_other_dirs(self, tmp_path):
        _make(tmp_path, ['.pytest_tmp-1', '.pytest_tmp-2',
                         '.pytest_tmp-3', '.pytest_tmp-4'],
              mtimes=[_BASE_MTIME + i for i in range(4)])   # 旧时间，避开活跃窗口
        (tmp_path / '.pytest_tmp-not-a-dir').write_text('x', encoding='utf-8')
        (tmp_path / 'some_other_dir').mkdir()
        stale = _select_stale_tmp_roots(tmp_path, keep=2)
        assert len(stale) == 2, "同名文件与无关目录都不该被算进来"


class TestGuardHasTeeth:
    """元守卫：证明判据真能区分正反例（防"扫到 0 个 = 假绿"）。"""

    def test_keep_zero_selects_all(self, tmp_path):
        _make(tmp_path, ['.pytest_tmp-1', '.pytest_tmp-2'],
              mtimes=[_BASE_MTIME, _BASE_MTIME + 1])   # 旧时间，避开活跃窗口
        assert len(_select_stale_tmp_roots(tmp_path, keep=0)) == 2

    def test_keep_huge_selects_none(self, tmp_path):
        _make(tmp_path, ['.pytest_tmp-1', '.pytest_tmp-2'],
              mtimes=[_BASE_MTIME, _BASE_MTIME + 1])
        assert _select_stale_tmp_roots(tmp_path, keep=99) == []

    def test_default_keep_is_positive(self):
        assert TMP_ROOT_KEEP >= 1, "保留数为 0 会把刚跑完的现场也删掉"


class TestPerRunLimit:
    """单次上限：防止历史积压把 `pytest_unconfigure` 拖死。"""

    def test_limit_caps_selection_and_takes_oldest_first(self, tmp_path):
        _make(tmp_path, [f'.pytest_tmp-{i}' for i in range(1, 8)],
              mtimes=[_BASE_MTIME + i for i in range(7)])
        stale = sorted(d.name for d in _select_stale_tmp_roots(tmp_path, keep=1, limit=3))
        assert stale == ['.pytest_tmp-1', '.pytest_tmp-2', '.pytest_tmp-3'], (
            f"限额下应取**最旧**的几个，实得 {stale}")

    def test_limit_none_means_unlimited(self, tmp_path):
        _make(tmp_path, [f'.pytest_tmp-{i}' for i in range(1, 6)],
              mtimes=[_BASE_MTIME + i for i in range(6)])
        assert len(_select_stale_tmp_roots(tmp_path, keep=1, limit=None)) == 4

    def test_default_max_is_positive_and_bounded(self):
        assert TMP_ROOT_PRUNE_MAX >= 1, "上限为 0 = 永不清理"


@pytest.mark.skipif(os.name != 'nt', reason="外部删除通道仅 Windows 实现")
class TestRealDeletion:
    """真跑：外部进程通道必须**真的**删得掉（否则策略是空转）。"""

    def test_prune_removes_old_and_keeps_newest(self, tmp_path):
        _make(tmp_path, [f'.pytest_tmp-{i}' for i in range(1, 5)],
              mtimes=[_BASE_MTIME + i for i in range(4)])
        _prune_old_tmp_roots(keep=1, root=tmp_path)
        left = sorted(d.name for d in tmp_path.iterdir() if d.is_dir())
        assert left == ['.pytest_tmp-4'], f"应只剩最新的 1 个，实得 {left}"

    def test_prune_noop_when_within_keep(self, tmp_path):
        _make(tmp_path, ['.pytest_tmp-1', '.pytest_tmp-2'],
              mtimes=[_BASE_MTIME, _BASE_MTIME + 1])
        _prune_old_tmp_roots(keep=5, root=tmp_path)
        left = sorted(d.name for d in tmp_path.iterdir() if d.is_dir())
        assert left == ['.pytest_tmp-1', '.pytest_tmp-2']

    def test_prune_never_touches_other_dirs(self, tmp_path):
        _make(tmp_path, ['.pytest_tmp-1', '.pytest_tmp-2'],
              mtimes=[_BASE_MTIME, _BASE_MTIME + 1])
        (tmp_path / 'keep_me').mkdir()
        _prune_old_tmp_roots(keep=1, root=tmp_path)
        assert (tmp_path / 'keep_me').is_dir(), "无关目录不该被删"


class TestFailurePaths:
    """round 217 补的失败路径用例。

    原 13 例只覆盖「选对了 + 删得掉」两个**正面路径** ——
    而本文件 docstring 自己写着"否则策略只是看起来在跑"，
    它担心的 F1/F3/F4 恰恰一个都没测。这里把三个都钉住。
    """

    # ---- F1：临时产物必须被清干净（原实现只删 list/ps1，empty 目录按 PID 堆积）
    def test_delete_cleans_temp_artifacts(self, tmp_path):
        target = tmp_path / 'victim'
        target.mkdir()
        (target / 'f.txt').write_text('x', encoding='utf-8')
        _delete_dirs_bulk([target])
        tmp = Path(tempfile.gettempdir())
        pid = os.getpid()
        assert not (tmp / f'scada_prune_empty_{pid}').exists(), 'F1: empty 目录残留在 TEMP'
        assert not (tmp / f'scada_prune_{pid}.txt').exists(), 'F1: list 文件残留'
        assert not (tmp / f'scada_prune_{pid}.ps1').exists(), 'F1: ps1 文件残留'

    # ---- F3：删除失败必须被计数报告（原实现 SilentContinue + 无输出 = 静默失败）
    def test_bulk_delete_parses_ps_counts(self, monkeypatch):
        """F3 核心：PowerShell 的计数输出必须被解析（原实现直接丢弃 → 静默）。"""
        import tests.conftest as cf

        class _FakeProc:
            stdout = ("SCADA_PRUNE_OK=2\n"
                      "SCADA_PRUNE_FAIL=1\n"
                      "SCADA_PRUNE_FAILED_PATH=C:\\x\\stuck-dir\n")
            returncode = 0

        monkeypatch.setattr(cf.subprocess, 'run', lambda *a, **k: _FakeProc())
        ok, fail, failed = cf._delete_dirs_bulk([Path('C:/whatever')])
        assert (ok, fail) == (2, 1), f'计数应被解析，实得 ok={ok} fail={fail}'
        assert failed == ['C:\\x\\stuck-dir'], f'失败清单应被解析，实得 {failed}'

    def test_bulk_delete_missing_target_counts_as_done(self, tmp_path):
        """目标本就不存在 = 目的已达成（PS 侧 Test-Path=false → 计入 ok）。

        注意：这是**有意的语义** —— 保留策略只关心"目录还在不在"，
        不在就说明目的达成，不该报成失败（否则日志会被噪声淹没）。
        """
        missing = tmp_path / 'never-created'
        ok, fail, failed = _delete_dirs_bulk([missing])
        assert (ok, fail) == (1, 0), f'实得 ok={ok} fail={fail}'
        assert failed == []

    def test_prune_returns_counts(self, tmp_path):
        """`_prune_old_tmp_roots` 要回传计数（原返回 None，调用方无从得知）。"""
        _make(tmp_path, [f'.pytest_tmp-{i}' for i in range(1, 4)],
              mtimes=[_BASE_MTIME + i for i in range(3)])
        result = _prune_old_tmp_roots(keep=1, root=tmp_path)
        assert result['selected'] == 2, f'应选中 2 个待删，实得 {result}'
        assert result['ok'] + result['fail'] == 2, f'计数应自洽，实得 {result}'

    # ---- F4：单个坏目录不该毁掉整次清理（原实现 stat 抛异常被上层吞 → 整次跳过）
    def test_dir_mtime_tolerates_missing(self, tmp_path):
        assert _dir_mtime(tmp_path / 'never-existed') == 0.0

    def test_select_tolerates_vanishing_dir(self, tmp_path):
        """模拟"glob 之后、stat 之前"目录被并发会话删掉。"""
        made = _make(tmp_path, ['.pytest_tmp-1', '.pytest_tmp-2', '.pytest_tmp-3'],
                     mtimes=[_BASE_MTIME + i for i in range(3)])
        for f in made[1].iterdir():   # 清空后删掉中间那个（模拟并发消失）
            f.unlink()
        made[1].rmdir()
        stale = _select_stale_tmp_roots(tmp_path, keep=1)
        assert len(stale) == 1, f'剩下 2 个目录、keep=1 → 应返回 1 个，实得 {stale}'
        assert all(d.exists() for d in stale), '返回的必须是仍存在的目录'

    # ---- F5：活跃窗口内的目录不删（保护仍在跑的并发会话）
    def test_select_skips_recently_active_dirs(self, tmp_path):
        now = time.time()
        _make(tmp_path, ['.pytest_tmp-new', '.pytest_tmp-active', '.pytest_tmp-old'],
              mtimes=[now - 10, now - 30, now - 7200])
        stale = sorted(d.name for d in _select_stale_tmp_roots(tmp_path, keep=1, now=now))
        assert stale == ['.pytest_tmp-old'], (
            f'活跃窗口内的目录不该被删（now-30 在 {TMP_ROOT_ACTIVE_WINDOW}s 窗口内），实得 {stale}')

    def test_active_window_can_be_disabled(self, tmp_path):
        """窗口设为 0 = 退回旧行为（证明窗口确实在起作用，防"假绿"）。"""
        now = time.time()
        _make(tmp_path, ['.pytest_tmp-new', '.pytest_tmp-active', '.pytest_tmp-old'],
              mtimes=[now - 10, now - 30, now - 7200])
        stale = _select_stale_tmp_roots(tmp_path, keep=1, now=now, active_window=0)
        assert len(stale) == 2, f'窗口为 0 时应返回 2 个，实得 {stale}'

    def test_active_window_default_is_positive(self):
        assert TMP_ROOT_ACTIVE_WINDOW > 0, '窗口为 0 = 保护失效'
