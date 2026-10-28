# -*- coding: utf-8 -*-
"""round 309：把「改了一半的契约」和「看起来有规则在跑」收口。

本轮从 `tools/_scan_dropped_bool.py`（扫「返回 bool 的函数被当语句调用」）
的 583 处里筛出**真有后果**的三类。其余多为误报（`_persist_item` /
`_enqueue_drop_oldest` 内部已计数 + 打日志；`connect()` 是库函数抛异常；
`clear_external_alarm` 已被 try 包住），已在提交信息里写明为什么不动。

三类真缺陷：

1. **上一轮 round 308 留下的半成品**：`_start_device_collection()` 改成返回 bool 了，
   但三个 API 调用点还在当语句调 —— 接口回「已添加 N 台」，其中采集没起来的
   那些**永远没有数据**。
2. **声光线程退出清理**：round 286 修了循环内的失败检查，漏了「退出时」那一笔。
   线程退出后再没有人会写这个 DO，蜂鸣器关不掉就是**一直响**。
3. **边缘决策 5 条内置规则绑在不存在的设备上**（`siemens_plc_01`，设备清单里没有）
   —— 条件永不匹配，启动日志却说「已加载 3 条规则, 2 条联锁, 1 个 PID」。
"""

import logging
from unittest.mock import MagicMock

import pytest

from 报警层.alarm_output import AlarmOutput
from 智能层.edge_decision import EdgeDecisionEngine


# ---------------------------------------------------------------------------
# 1. 添加设备：采集没起来必须说清楚
# ---------------------------------------------------------------------------

@pytest.fixture
def presets_app(app):
    """挂一个假的 simulation_initializer，并让采集启动失败。"""
    app.simulation_initializer = MagicMock()
    return app


def test_add_preset_reports_collection_failure(presets_app, client, auth_headers, monkeypatch):
    """单台预设：采集没起来 → `collection_started: False` + message 明确提示。"""
    from 展示层.api import api_devices

    presets_app.simulation_initializer.add_preset_device.return_value = {
        'success': True, 'device_id': 'd1', 'message': '设备已添加',
    }
    monkeypatch.setattr(api_devices, '_start_device_collection', lambda _d: False)

    resp = client.post('/api/devices/presets/add',
                       json={'preset_id': 'p1'}, headers=auth_headers)
    body = resp.get_json()

    assert resp.status_code == 200
    assert body['collection_started'] is False
    assert '未启动' in body['message'], (
        '采集没起来，接口却只说「已添加」—— 界面上这台设备永远没有数据')


def test_add_preset_ok_when_collection_started(presets_app, client, auth_headers, monkeypatch):
    """反向对照：采集起来了就不该出现「未启动」的提示。"""
    from 展示层.api import api_devices

    presets_app.simulation_initializer.add_preset_device.return_value = {
        'success': True, 'device_id': 'd1', 'message': '设备已添加',
    }
    monkeypatch.setattr(api_devices, '_start_device_collection', lambda _d: True)

    resp = client.post('/api/devices/presets/add',
                       json={'preset_id': 'p1'}, headers=auth_headers)
    body = resp.get_json()

    assert body['collection_started'] is True
    assert '未启动' not in body['message']


def test_batch_add_lists_devices_whose_collection_failed(presets_app, client, auth_headers,
                                                         monkeypatch):
    """批量添加最容易掩盖「添加成功但采集没起」—— 必须逐台列出来。"""
    from 展示层.api import api_devices

    presets_app.simulation_initializer.add_preset_batch.return_value = {
        'success': True, 'message': '已添加 3 台',
        'results': [
            {'success': True, 'device_id': 'd1'},
            {'success': True, 'device_id': 'd2'},
            {'success': True, 'device_id': 'd3'},
        ],
    }
    # d2 采集起不来
    monkeypatch.setattr(api_devices, '_start_device_collection',
                        lambda did: did != 'd2')

    resp = client.post('/api/devices/presets/batch-add',
                       json={'preset_ids': ['a', 'b', 'c']}, headers=auth_headers)
    body = resp.get_json()

    assert body['collection_failed'] == ['d2']
    assert 'd2' in body['message'] and '未启动' in body['message']


def test_add_all_reports_collection_failures(presets_app, client, auth_headers, monkeypatch):
    """一键添加全部：同样是逐台收集。"""
    from 展示层.api import api_devices

    presets_app.simulation_initializer.presets = [{'id': 'p1'}, {'id': 'p2'}]
    presets_app.simulation_initializer.add_preset_batch.return_value = {
        'success': True, 'message': '已添加 2 台',
        'results': [
            {'success': True, 'device_id': 'd1'},
            {'success': True, 'device_id': 'd2'},
        ],
    }
    monkeypatch.setattr(api_devices, '_start_device_collection', lambda _d: False)

    resp = client.post('/api/devices/presets/add-all', headers=auth_headers)
    body = resp.get_json()

    assert body['collection_failed'] == ['d1', 'd2']


# ---------------------------------------------------------------------------
# 2. 声光线程「退出时」那一笔：关不掉必须留痕
# ---------------------------------------------------------------------------

def _bare_output(write_ok=True):
    """裸对象：绕过 __init__ 只装配本组用例用到的字段。

    `get_status()` 会读熔断相关的 `_fail_streak` / `_circuit_open_until`，
    这里必须补齐 —— 否则断言的是 AttributeError 而不是被测行为。
    """
    import threading

    out = AlarmOutput.__new__(AlarmOutput)
    out._lock = threading.RLock()
    out._io_lock = threading.RLock()
    out.do_red, out.do_yellow, out.do_green, out.do_buzzer = 0, 1, 2, 3
    out.enabled = True
    out.simulation = False
    out.current_state = {}
    out._fail_streak = 0
    out._circuit_open_until = None
    out._write_do = MagicMock(return_value=write_ok)
    return out


def test_shutdown_do_failure_is_counted(caplog):
    """退出清理写失败 → ERROR + `shutdown_failures` 计数（否则现场一直响没人知道）。"""
    out = _bare_output(write_ok=False)

    with caplog.at_level(logging.ERROR):
        ok = out._shutdown_do(3, '蜂鸣器')

    assert ok is False
    assert out.get_status()['do_channel']['shutdown_failures'] == 1
    assert any('不会再有重试' in r.getMessage() for r in caplog.records), (
        '退出清理失败没有任何日志 —— 现场蜂鸣器常响，而界面显示已关闭')


def test_shutdown_do_success_keeps_counter_zero():
    """反向对照：写成功时计数保持 0。"""
    out = _bare_output(write_ok=True)

    assert out._shutdown_do(3, '蜂鸣器') is True
    assert out.get_status()['do_channel']['shutdown_failures'] == 0


def test_buzzer_loop_uses_shutdown_helper(monkeypatch):
    """蜂鸣器线程退出时必须走 `_shutdown_do`（而不是丢弃结果的裸 `_write_do`）。"""
    import threading
    import time

    out = _bare_output(write_ok=True)
    out._buzzer_running = True
    out._shutdown_do = MagicMock(return_value=False)

    t = threading.Thread(target=out._buzzer_pulse_loop, args=(0.01, 0.01), daemon=True)
    t.start()
    time.sleep(0.05)
    out._buzzer_running = False
    t.join(timeout=2)

    assert out._shutdown_do.called, '蜂鸣器线程退出时没有做"最后一笔"关闭'


# ---------------------------------------------------------------------------
# 3. 边缘决策：默认规则不再绑定不存在的设备
# ---------------------------------------------------------------------------

def _engine(cfg=None):
    return EdgeDecisionEngine(MagicMock(), cfg or {})


def test_no_hardcoded_default_rules_without_config(caplog):
    """没配默认规则 → 一条都不加载，并且告警说明原因。

    历史实现硬编码 3 条规则 + 2 条联锁 + 1 个 PID，全部绑 `siemens_plc_01`；
    而设备清单里没有这个设备（真实是 siemens_1500_01 等）→ 条件永不匹配。
    启动日志却写着「已加载 3条规则, 2条联锁, 1个PID控制器」。
    """
    eng = _engine()
    with caplog.at_level(logging.WARNING):
        loaded = eng._load_default_rules()

    assert loaded is False
    assert eng.rules == {} and eng.interlocks == {} and eng.pid_controllers == {}
    assert any('死规则' in r.getMessage() for r in caplog.records)


def test_config_default_rules_are_loaded():
    """注入了就加载，且三类都能进来（规则 / 联锁 / PID）。"""
    cfg = {'default_rules': {
        'rules': [{'id': 'r1',
                   'condition': {'type': 'threshold', 'key': 'd1:temp',
                                 'operator': 'gt', 'value': 1},
                   'action': {'type': 'set_alarm', 'message': 'x'}}],
        'interlocks': [{'id': 'i1',
                        'condition': {'type': 'threshold', 'key': 'd1:temp',
                                      'operator': 'gt', 'value': 2},
                        'action': {'type': 'set_alarm', 'message': 'y'}}],
        'pid_controllers': [{'id': 'p1', 'input_key': 'd1:temp',
                             'output_key': 'd1:fan', 'setpoint': 10}],
    }}
    eng = _engine(cfg)

    assert eng._load_default_rules() is True
    assert list(eng.rules) == ['r1']
    assert list(eng.interlocks) == ['i1']
    assert list(eng.pid_controllers) == ['p1']


def test_partial_default_rules_are_reported(caplog):
    """配置里写坏的条目必须汇总告警，不能静默少注册。"""
    cfg = {'default_rules': {'rules': [
        {'id': 'good',
         'condition': {'type': 'threshold', 'key': 'd1:temp',
                       'operator': 'gt', 'value': 1},
         'action': {'type': 'set_alarm', 'message': 'x'}},
        {'id': 'bad'},          # 缺 condition/action
    ]}}
    eng = _engine(cfg)

    with caplog.at_level(logging.WARNING):
        eng._load_default_rules()

    assert list(eng.rules) == ['good']
    assert any('部分丢弃' in r.getMessage() for r in caplog.records)


def test_hardcoded_device_name_is_gone():
    """源码守卫：`siemens_plc_01` 不许再作为**代码里的字符串**出现。

    ⚠️ 不能直接 `assert name not in src` —— 本轮的注释与 docstring 里
    正是在解释「为什么删掉它」，直接扫源码会把自己写进说明文字里的名字
    当成违规（第一版就是这么红的）。这里用 AST 取**真正的字符串常量**，
    并排除 module/class/function 的 docstring。
    """
    import ast
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / '智能层' / 'edge_decision.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, 'body', None)
            if body and isinstance(body[0], ast.Expr) and isinstance(
                    body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))

    offenders = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        # 只看 `device:register` 形式的绑定键 —— 告警文案里提到这个名字
        # 是**说明**（"那批规则已移除"），不是绑定。
        and id(node) not in docstrings and 'siemens_plc_01:' in node.value
    ]
    assert not offenders, (
        'edge_decision 又硬编码了 siemens_plc_01 —— 设备清单里没有这个设备，'
        f'绑定它的规则永远不会触发: {offenders}')
