# -*- coding: utf-8 -*-
"""round 300：三处「重复计入 / 假成功」（SPC 指纹单位 / 边缘决策 / 配置写盘）。

延续 299 的判据：**成功返回必须等于状态收敛；日志里写的结果必须等于真的发生了什么。**
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. SPC：指纹单位错 —— 同一条物理违规每轮换指纹，重复计入刷满 100 条上限
# ---------------------------------------------------------------------------

def _analyzer(window: int = 10, subgroup: int = 5):
    from 智能层.spc_analyzer import SPCAnalyzer
    return SPCAnalyzer(MagicMock(), {'window_size': window, 'subgroup_size': subgroup})


def test_align_window_keeps_absolute_subgroup_identity():
    """窗口每滑 1 点，同一个绝对子组的序号不许变。

    `_form_subgroups` 恒从 `data[0]` 按 n 切；缓冲区左移 1 格后，
    所有子组的**内容边界**整体错位 —— 只有按绝对位置对齐，子组才有稳定身份。
    """
    ana = _analyzer(window=6, subgroup=5)          # buffer = 30 点
    for i in range(30):
        ana.feed_data('d1', 't', float(i))

    raw = list(ana.data_buffers['d1:t'])
    aligned1, base1 = ana._align_window(raw, ana._total_fed['d1:t'], 5)
    assert base1 == 0, '缓冲区未挤数据时，首个子组的绝对序号应为 0'
    assert aligned1[10] == 10.0, '缓冲区未挤数据时不应丢掉头部'

    # 喂 1 个新点：窗口滑 1 格（不足一个子组），头部被挤掉 1 个
    ana.feed_data('d1', 't', 30.0)
    raw2 = list(ana.data_buffers['d1:t'])
    aligned2, base2 = ana._align_window(raw2, ana._total_fed['d1:t'], 5)

    # 同一个物理点 10.0，其在窗口内的下标变了，但绝对子组序号必须还是 2
    idx = aligned2.index(10.0)
    assert base2 + idx // 5 == 2, (
        f'同一个物理子组滑窗后换了绝对序号（base={base2}, idx={idx}）'
        ' —— 指纹会跟着变，同一条违规被反复计入')


def test_align_window_drops_partial_head():
    """对齐必须丢掉头部不足一个子组的残片，否则子组边界仍然漂。"""
    ana = _analyzer(window=6, subgroup=5)
    for i in range(33):
        ana.feed_data('d1', 't', float(i))

    raw = list(ana.data_buffers['d1:t'])           # maxlen=30，绝对 3..32
    aligned, base = ana._align_window(raw, ana._total_fed['d1:t'], 5)
    # dropped=3 → pad=2 → aligned[0] 是绝对位置 5
    assert aligned[0] == 5.0, f'头部残片没丢掉：aligned[0]={aligned[0]}'
    assert base == 1, f'首个子组绝对序号应为 1，实际 {base}'


def test_violations_not_flooded_while_window_slides():
    """滑窗过程中同一批物理子组不许每轮重入。

    修前 `base_index = 已喂入点数 - 子组数`：点数与子组数**单位不同**，
    且随 fed 逐点 +1 而子组边界 5 点才移一格 —— 窗口内每条违规每轮都拿到
    新指纹、全部重入。跑够几十轮就把 `violations` 的 100 条上限刷满，
    真正的违规被挤出去（用户看到的是「一堆重复的老毛病、新的不见了」）。
    """
    ana = _analyzer(window=10, subgroup=5)
    ana.set_spec_limits('d1:t', usl=100, lsl=0)

    # 45 个正常点铺底，再 20 个爆表点制造持续违规
    for i in range(45):
        ana.feed_data('d1', 't', 50.0 + (i % 3) * 0.05)
        ana.calculate_xbar_r_chart('d1', 't')
    for _ in range(20):
        ana.feed_data('d1', 't', 500.0)
        ana.calculate_xbar_r_chart('d1', 't')

    records = ana.violations['d1:t']
    assert records, '前提不成立：没有产生任何判异'

    fps = [(r['rule'], r['abs_index']) for r in records]
    assert len(fps) == len(set(fps)), '同一条违规以不同指纹重复入库'

    # 决定性判据：再喂 4 个点 —— 这段时间里完整子组**最多新增 1 个**
    # （5 点一个子组），所以判异最多新增 8 条（Western Electric 共 8 条规则）。
    #
    # 只喂 1 个点是抓不住的：第 66 点刚好让对齐起点跨过一个子组，
    # 「base +1」被「index -1」抵消，假绿。（第一版就是这么写的，被变异 M1b 抓出来。）
    before = len(records)
    for _ in range(4):
        ana.feed_data('d1', 't', 500.0)
        ana.calculate_xbar_r_chart('d1', 't')
    added = len(ana.violations['d1:t']) - before
    assert added <= 8, (
        f'4 个点里最多新增 1 个子组，判异却多了 {added} 条 —— '
        '同一条物理违规被重复计入（100 条上限就是这样被自己刷满的）')


# ---------------------------------------------------------------------------
# 2. 边缘决策：未注册回调 / 回调抛异常时，日志不许写成功
# ---------------------------------------------------------------------------

def _engine():
    from 智能层.edge_decision import EdgeDecisionEngine
    return EdgeDecisionEngine(MagicMock(), {})


def _last(eng):
    return eng.decision_log[-1]


def test_unregistered_action_is_logged_as_failure():
    """write_register 回调缺失时，旧代码连 result 都不写 —— 日志里一条无结果记录。"""
    eng = _engine()
    eng._execute_action({'type': 'write_register', 'target': 'd1:r1', 'value': 1},
                        'rule1', 'rule', {})
    entry = _last(eng)
    assert entry['ok'] is False, '回调没注册，日志却记成功'
    assert '未注册' in entry['result']
    assert eng.get_status()['action_failures'] == 1


def test_unregistered_set_alarm_not_reported_as_sent():
    """set_alarm 原来**无条件**写「报警: xxx」—— 一个函数都没调用也照样记已发。"""
    eng = _engine()
    eng._execute_action({'type': 'set_alarm', 'message': '超温'}, 'rule1', 'rule', {})
    entry = _last(eng)
    assert entry['ok'] is False
    assert '未注册' in entry['result']
    assert '报警: 超温' not in entry['result'], '没注册回调却记成「报警已发出」'


def test_callback_name_checked_not_action_type():
    """callback 类型查的是 callback_name；按 action_type 判会误以为已注册。"""
    eng = _engine()
    eng._execute_action({'type': 'callback', 'callback_name': 'not_registered'},
                        'rule1', 'rule', {})
    entry = _last(eng)
    assert entry['ok'] is False
    assert 'not_registered' in entry['result']


def test_registered_action_still_succeeds():
    """反向对照：注册了就正常执行、ok=True、不记失败。"""
    eng = _engine()
    calls = []
    eng.register_action('set_alarm', lambda msg, level: calls.append(msg))
    eng._execute_action({'type': 'set_alarm', 'message': '超温'}, 'rule1', 'rule', {})
    entry = _last(eng)
    assert entry['ok'] is True
    assert calls == ['超温']
    assert eng.get_status()['action_failures'] == 0


def test_callback_exception_marked_failed():
    """回调自己抛异常时，日志必须记失败（287 那一族：现场没响、日志说报了警）。"""
    eng = _engine()

    def boom(msg, level):
        raise RuntimeError('音柱离线')

    eng.register_action('set_alarm', boom)
    eng._execute_action({'type': 'set_alarm', 'message': '超温'}, 'rule1', 'rule', {})
    entry = _last(eng)
    assert entry['ok'] is False
    assert '报警失败' in entry['result']
    assert eng.get_status()['action_failures'] == 1


def test_pid_rejects_absurd_dt():
    """dt 越界（进程挂起 / 时钟跳变）时不许把积分项一步打满。"""
    eng = _engine()
    eng.add_pid_controller('c1', input_key='d1:t', output_key='d1:o',
                           setpoint=100.0, kp=1.0, ki=1.0, kd=0.0)
    ctrl = eng.pid_controllers['c1']
    ctrl['prev_time'] -= 3600          # 模拟上一拍在一小时前
    eng._execute_pid('c1', ctrl, {'d1:t': 0.0})
    assert ctrl['integral'] == 0, 'dt 异常大仍被积分 —— 积分项一步打满限幅'
    assert ctrl['output'] == 0


def test_pid_normal_dt_still_integrates():
    """反向对照：正常 dt 必须照常积分（别把保护写成"永远不算"）。"""
    import time as _time

    eng = _engine()
    eng.register_action('write_register', lambda d, r, v: None)
    eng.add_pid_controller('c1', input_key='d1:t', output_key='d1:o',
                           setpoint=100.0, kp=0.0, ki=1.0, kd=0.0,
                           output_min=-1e9, output_max=1e9)
    ctrl = eng.pid_controllers['c1']
    ctrl['prev_time'] = _time.monotonic() - 5.0    # 5 秒前
    eng._execute_pid('c1', ctrl, {'d1:t': 90.0})   # error=10
    assert ctrl['integral'] == pytest.approx(50.0, rel=0.15), '正常 dt 没有积分'


def test_pid_output_not_written_is_counted():
    """PID 算出了输出但没有 write_register 回调 —— 不许静默当成功。"""
    eng = _engine()
    eng.add_pid_controller('c1', input_key='d1:t', output_key='d1:o',
                           setpoint=100.0, kp=1.0, ki=0.0, kd=0.0)
    ctrl = eng.pid_controllers['c1']
    ctrl['prev_time'] -= 1.0
    before = eng.get_status()['action_failures']
    eng._execute_pid('c1', ctrl, {'d1:t': 90.0})
    assert eng.get_status()['action_failures'] == before + 1, (
        'PID 输出没写进设备，却什么都没记 —— 看板上 output 有值，像在控制')


def test_pid_uses_monotonic_clock():
    """源码级钉住：PID 的时间基准必须是单调时钟，否则 NTP 校时会把积分打满。"""
    src = (REPO / '智能层' / 'edge_decision.py').read_text(encoding='utf-8')
    seg = src.split('def _execute_pid', 1)[1][:1400]
    assert 'time.monotonic()' in seg, 'PID 仍在用墙钟算 dt'
    assert 'time.time()' not in seg, 'PID 里还残留 time.time()'


# ---------------------------------------------------------------------------
# 3. 配置写盘：6 处裸调用把返回值丢了
# ---------------------------------------------------------------------------

def _alarms_mod():
    import importlib
    return importlib.import_module('展示层.api.api_alarms')


def test_save_failure_returns_500_and_skips_runtime(monkeypatch, client,
                                                    auth_headers, app):
    """写盘失败时必须回错误，且**不许**改运行时（否则界面和文件是两套配置）。"""
    mod = _alarms_mod()
    monkeypatch.setattr(mod, 'save_yaml_config', lambda *a, **k: False)
    monkeypatch.setattr(mod, 'load_yaml_config',
                        lambda *a, **k: {'alarm_rules': []})
    app.alarm_manager.rules = {}

    resp = client.post('/api/alarm-rules', headers=auth_headers,
                       json={'id': 'r-300', 'name': '写盘失败用例'})

    assert resp.status_code == 500, (
        f'写盘失败却回了 {resp.status_code} —— 用户以为保存成功，重启后回滚')
    assert 'r-300' not in app.alarm_manager.rules, (
        '写盘失败还把规则塞进了运行时 —— 界面有、文件没有')


def test_save_success_still_returns_200(monkeypatch, client, auth_headers, app):
    """反向对照：写盘成功时照常保存并返回 200。"""
    mod = _alarms_mod()
    monkeypatch.setattr(mod, 'save_yaml_config', lambda *a, **k: True)
    monkeypatch.setattr(mod, 'load_yaml_config',
                        lambda *a, **k: {'alarm_rules': []})
    app.alarm_manager.rules = {}
    monkeypatch.setattr(app.alarm_manager, '_rebuild_rules_index', lambda: None)

    resp = client.post('/api/alarm-rules', headers=auth_headers,
                       json={'id': 'r-301', 'name': '正常用例'})

    assert resp.status_code == 200
    assert 'r-301' in app.alarm_manager.rules


def test_every_save_yaml_config_checks_return_value():
    """AST 守卫：任何裸调用 save_yaml_config 的地方都必须判断返回值。

    只判「文件里有没有 if not save_yaml_config」是不够的 —— 新增一处裸调用
    会照样通过。所以按**两件事**同时校验：裸调用数必须为 0，且判断数必须
    不少于 7（6 处原缺陷 + 710 行原本就正确的那处），防止守卫自己写空。
    """
    import ast

    src = (REPO / '展示层' / 'api' / 'api_alarms.py').read_text(encoding='utf-8')
    tree = ast.parse(src)

    bare, guarded = [], 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            fn = node.value.func
            if isinstance(fn, ast.Name) and fn.id == 'save_yaml_config':
                bare.append(node.lineno)
        if isinstance(node, ast.If):
            t = node.test
            if (isinstance(t, ast.UnaryOp) and isinstance(t.op, ast.Not)
                    and isinstance(t.operand, ast.Call)
                    and isinstance(t.operand.func, ast.Name)
                    and t.operand.func.id == 'save_yaml_config'):
                guarded += 1

    assert not bare, f'这些行丢掉了写盘结果：{bare}'
    assert guarded >= 7, f'只找到 {guarded} 处判断（应 ≥7），守卫自身可能失效'
