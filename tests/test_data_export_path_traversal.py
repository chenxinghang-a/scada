"""导出路径穿越的回归测试。

对应 CodeQL 的 4 条 `py/path-injection`（#40/#41/#42/#61）。

背景：`DataExport` 曾经用 `self.export_dir / filename` 直接拼，
而 `filename` 有一部分来自请求体（`/api/data/export` 的 device_id
会进到 `f'{device_id}_{timestamp}'`），传 `../../x` 就能写到导出目录外；
`delete_export('../victim.txt')` 更能删目录外任意文件。

现在统一走 `_resolve_target()`：只取 basename + resolve 后复核父目录。

⚠️ CodeQL 不认这套自定义校验（也没有用 `werkzeug.utils.secure_filename`，
因为它会把中文设备名整段吃掉：`温度变送器_20261019.csv` -> `20261019.csv`）。
所以用这组测试把「穿越一定失败」钉成可执行的不变量 ——
将来谁改了 `_resolve_target`，这里会红。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from 存储层.data_export import DataExport  # noqa: E402


@pytest.fixture()
def exporter(tmp_path):
    return DataExport(tmp_path / 'exports')


class TestExportPathTraversal:
    """写文件：任何越界路径都必须落在 export_dir 内。"""

    def test_normal_filename_lands_in_export_dir(self, exporter):
        out = exporter.export_csv([{'a': 1}], filename='ok.csv')
        assert out
        assert Path(out).parent == exporter.export_dir
        assert Path(out).name == 'ok.csv'

    @pytest.mark.parametrize('evil', [
        '../../evil.csv',
        '../evil.csv',
        'sub/../evil.csv',
        '....//evil.csv',
        'sub\\..\\..\\evil.csv',
    ])
    def test_traversal_is_flattened_into_export_dir(self, exporter, evil, tmp_path):
        out = exporter.export_csv([{'a': 1}], filename=evil)
        # 不得在导出目录之外落任何文件
        outside = {p for p in tmp_path.rglob('*') if p.is_file() and p.parent != exporter.export_dir}
        assert not outside, f'{evil} 写到了导出目录外: {outside}'
        if out:
            assert Path(out).parent == exporter.export_dir

    def test_empty_filename_falls_back_to_generated(self, exporter):
        """空串是「没给名字」，走自动生成分支（原有行为），不是穿越。"""
        out = exporter.export_csv([{'a': 1}], filename='')
        assert out
        assert Path(out).parent == exporter.export_dir
        assert Path(out).name.startswith('export_')

    @pytest.mark.parametrize('bad', ['.', '..', '/', '\\'])
    def test_dot_and_separator_only_rejected(self, exporter, bad):
        assert exporter.export_csv([{'a': 1}], filename=bad) in ('', None)

    def test_chinese_filename_preserved(self, exporter):
        """中文设备名不能被 sanitize 掉 —— 这是不用 secure_filename 的原因。"""
        out = exporter.export_csv([{'a': 1}], filename='温度变送器_20261019.csv')
        assert out
        assert Path(out).name == '温度变送器_20261019.csv'


class TestDeletePathTraversal:
    """删除文件：越界一律拒绝，且保持 bool 契约（不抛异常）。"""

    def test_delete_normal(self, exporter):
        exporter.export_csv([{'a': 1}], filename='del.csv')
        assert exporter.delete_export('del.csv') is True
        assert not (exporter.export_dir / 'del.csv').exists()

    def test_delete_traversal_rejected(self, exporter, tmp_path):
        victim = tmp_path / 'victim.txt'
        victim.write_text('important', encoding='utf-8')
        assert exporter.delete_export('../victim.txt') is False
        assert victim.exists(), '越界删除竟然生效了'

    def test_delete_missing_returns_false(self, exporter):
        assert exporter.delete_export('nope.csv') is False


class TestResolveTargetDirectly:
    """直接把判据钉在 _resolve_target 上，便于定位。"""

    def test_resolve_keeps_inside(self, exporter):
        p = exporter._resolve_target('a.csv')
        assert p == exporter.export_dir / 'a.csv'

    def test_resolve_flattens(self, exporter):
        p = exporter._resolve_target('../../a.csv')
        assert p.parent == exporter.export_dir

    def test_resolve_rejects_blank(self, exporter):
        with pytest.raises(ValueError):
            exporter._resolve_target('   ')
