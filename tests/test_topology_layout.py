# -*- coding: utf-8 -*-
"""厂区拓扑布局（round 351 补测）。

round 350 那轮把拓扑从「前端硬编码 7 台 + 手填像素坐标」改成
「配置驱动 + 后端算坐标」，功能与文档都写完了，但**没有一条 pytest**
（只有 tools/sim_*_selftest.py 这类需要起服务的手工脚本）。
这里把布局算法的关键契约钉住 —— 它们都是「错了不会报错、只是图难看」的那类。
"""

from __future__ import annotations

import pytest


def _layout():
    from 展示层.api.api_devices import _layout_line
    return _layout_line


def _line(stages):
    return {'id': 'l1', 'name': 'L1', 'stages': list(stages)}


def _dev(did, stage=None, role='equipment'):
    d = {'id': did, 'name': did, 'role': role}
    if stage is not None:
        d['stage'] = stage
    return d


def _by_id(out):
    return {n['device_id']: n for n in out['nodes']}


def test_empty_stage_column_is_skipped():
    """空工序列必须跳过并重新编号 —— 否则图上出现一段空白。"""
    out = _layout()(_line(['干燥', '注塑', '取件']),
                    [_dev('a', '干燥'), _dev('c', '取件')], {}, {})

    cols = {d: n['col'] for d, n in _by_id(out).items()}
    assert cols == {'a': 0, 'c': 1}, f'空列没被跳过: {cols}'


def test_device_without_stage_is_not_dropped():
    """没写 stage 的设备不能丢 —— 丢了就是「设备存在但图上找不到」。"""
    out = _layout()(_line(['干燥', '注塑']), [_dev('a', '干燥'), _dev('x')], {}, {})

    node = _by_id(out).get('x')
    assert node is not None, '没写 stage 的设备被直接丢弃了'
    assert node['col'] == 1, f'应挂到最后一列，实际 col={node["col"]}'


def test_controller_goes_to_separate_layer():
    """PLC 不许和工艺设备画在同一列（否则图上出现物理上不存在的箭头）。"""
    out = _layout()(_line(['干燥']),
                    [_dev('a', '干燥'), _dev('plc', role='controller')], {}, {})

    nodes = _by_id(out)
    assert nodes['plc']['layer'] == 'control'
    assert nodes['a']['layer'] == 'process'
    assert nodes['plc']['y'] > nodes['a']['y'], '控制器没有画在工艺线下方'


def test_instrument_sits_below_equipment_in_same_column():
    out = _layout()(_line(['注塑']),
                    [_dev('e1', '注塑'), _dev('i1', '注塑', role='instrument')], {}, {})

    nodes = _by_id(out)
    assert nodes['e1']['col'] == nodes['i1']['col'], '仪表被挪到了别的列'
    assert nodes['e1']['row'] < nodes['i1']['row'], '仪表没有排在设备下方'
    assert nodes['e1']['y'] < nodes['i1']['y']


def test_x_increases_with_column():
    out = _layout()(_line(['a', 'b', 'c']),
                    [_dev('1', 'a'), _dev('2', 'b'), _dev('3', 'c')], {}, {})

    xs = [n['x'] for n in sorted(out['nodes'], key=lambda n: n['col'])]
    assert len(xs) == 3 and xs == sorted(xs), f'列坐标不是递增的: {xs}'


def test_edges_only_between_adjacent_stages():
    out = _layout()(_line(['a', 'b', 'c']),
                    [_dev('1', 'a'), _dev('2', 'b'), _dev('3', 'c')], {}, {})

    pairs = {(e['from'], e['to']) for e in out['edges']}
    assert ('1', '2') in pairs and ('2', '3') in pairs
    assert ('1', '3') not in pairs, '出现了跨工序的直连（物理上不经过中间工序）'


def test_empty_line_is_marked():
    out = _layout()(_line(['a']), [], {}, {})

    assert out.get('empty') is True
    assert out['nodes'] == [] and out['edges'] == []


def test_controller_only_line_still_renders():
    """只有控制器、没有工艺设备的产线不能整条消失。"""
    out = _layout()(_line(['a']), [_dev('plc', role='controller')], {}, {})

    assert out.get('empty') is not True
    assert [n['device_id'] for n in out['nodes']] == ['plc']
