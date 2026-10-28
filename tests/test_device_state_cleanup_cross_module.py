# -*- coding: utf-8 -*-
"""round 310：设备删除后的**跨模块状态残留** —— 一族而不是一处。

## 缺陷形态

`device_manager.remove_device()` 只删配置表与客户端。而智能层 / 网关 / 采集层里
有 **十几个** `self._xxx[device_id] = ...` 容器，全都没有删除点
（`grep -c ".pop("` = 0）。设备删掉后：

* 报表/看板继续列出这台**不存在的设备**（OEE / 能耗 / 振动 / 预测维护 / 遥测缓存）；
* 重新添加**同名**设备时继承旧状态 —— 最硬的是
  `DeviceControlSafety._comm_failures`：删之前连续失败 5 次，新设备第一次采样前
  就被判离线、写操作被拒，**重启才恢复**；
* `DeviceManager._status_cache`（TTL 5 秒）会让同名新设备在 5 秒内
  返回旧设备的 `connected: True`。

这与 round 274 修 `DataCollector._device_intervals` 是同一族，区别是那次只清了
采集层内部，这次是跨模块统一入口 `_forget_device_everywhere()`。

## 本文件的两部分

1. 每个模块的 `forget_device()` 行为（含"复合 key"这个坑）；
2. **闸门**：新加一个 `self._x[device_id] = ...` 容器却忘了 `forget_device` → 红。
   没有这条闸门，下一个人加缓存时会原样复发。
"""

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. 各模块的 forget_device
# ---------------------------------------------------------------------------

def test_oee_forget_device_clears_all_four_containers():
    from 智能层.oee_calculator import OEECalculator

    oee = OEECalculator(MagicMock())
    oee.device_states['d1'] = {'state': 'running'}
    oee._last_production['d1'] = {'count': 10}
    oee.shift_data['d1'] = {'shift': 1}
    oee.theoretical_rates['d1'] = 120.0

    assert oee.forget_device('d1') is True
    assert 'd1' not in oee.device_states
    assert 'd1' not in oee._last_production
    assert 'd1' not in oee.shift_data
    assert 'd1' not in oee.theoretical_rates


def test_oee_forget_device_unknown_is_false():
    """反向对照：不存在的设备返回 False（不能一律 True）。"""
    from 智能层.oee_calculator import OEECalculator

    assert OEECalculator(MagicMock()).forget_device('nope') is False


def test_energy_forget_device_clears_power_and_baseline():
    from 智能层.energy_manager import EnergyManager

    em = EnergyManager(MagicMock())
    em.realtime_power['d1'] = {'power': 100.0}
    em._last_energy_reading['d1'] = 450.0
    em.energy_baseline['d1'] = 200.0

    assert em.forget_device('d1') is True
    assert 'd1' not in em.realtime_power
    assert 'd1' not in em._last_energy_reading
    assert 'd1' not in em.energy_baseline


def test_vibration_forget_device_clears_buffers():
    from 智能层.vibration_analyzer import VibrationAnalyzer

    va = VibrationAnalyzer()
    va._buffers['d1'] = []
    va._scores['d1'] = {'score': 80}
    va._device_sample_rates['d1'] = 1000.0
    va._feed_sample_rates['d1'] = 1000.0

    assert va.forget_device('d1') is True
    assert 'd1' not in va._buffers
    assert 'd1' not in va._scores
    assert 'd1' not in va._device_sample_rates
    assert 'd1' not in va._feed_sample_rates


def test_device_control_forget_device_clears_comm_failures():
    """最硬的一条：重新添加同名设备不许继承旧的失败计数。

    否则新设备第一次采样前就被判离线 / 写操作被拒。
    """
    from unittest.mock import MagicMock

    from 智能层.device_control import DeviceControlSafety

    dc = DeviceControlSafety(MagicMock(), config={})
    dc._load_preset_interlocks = lambda: None
    dc._comm_failures['d1'] = 5
    dc._device_health['d1'] = {'status': 'disconnected'}
    dc._device_locks['d1'] = MagicMock()
    dc._safe_state_handlers['d1'] = lambda *a, **k: None

    assert dc.forget_device('d1') is True
    assert 'd1' not in dc._comm_failures, '同名设备会继承旧的连续失败计数 → 立刻判离线'
    assert 'd1' not in dc._device_health
    assert 'd1' not in dc._device_locks
    assert 'd1' not in dc._safe_state_handlers


def test_spc_forget_device_handles_composite_keys():
    """SPC 的 key 是 `device_id:register` 复合键 —— 按裸 device_id pop 一个都删不掉。"""
    from 智能层.spc_analyzer import SPCAnalyzer

    spc = SPCAnalyzer(MagicMock())
    spc.control_limits['d1:temp'] = {'ucl': 100}
    spc.control_limits['d1:press'] = {'ucl': 10}
    spc.spec_limits['d1:temp'] = {'usl': 120}
    spc.violations['d1:temp'] = [{'rule': 1}]
    # 另一台设备的数据不许被误删
    spc.control_limits['d10:temp'] = {'ucl': 999}

    assert spc.forget_device('d1') is True
    assert list(spc.control_limits) == ['d10:temp'], '前缀匹配误伤了 d10 或没删干净'
    assert spc.spec_limits == {}
    assert dict(spc.violations) == {}


def test_predictive_forget_device_clears_health_scores():
    from unittest.mock import MagicMock

    from 智能层.predictive_maintenance import PredictiveMaintenance

    pm = PredictiveMaintenance(MagicMock(), {})
    pm.health_scores['d1'] = {'score': 62}

    assert pm.forget_device('d1') is True
    assert 'd1' not in pm.health_scores


def test_mqtt_subscriber_forget_device_clears_caches():
    from gateway.mqtt_subscriber import MQTTSubscriber

    sub = MQTTSubscriber.__new__(MQTTSubscriber)
    import logging
    sub.logger = logging.getLogger('test')
    sub._telemetry_cache = {'d1': object()}
    sub._status_cache = {'d1': object()}

    assert sub.forget_device('d1') is True
    assert sub._telemetry_cache == {}
    assert sub._status_cache == {}


def test_data_collector_forget_device_alias():
    """采集器的清理方法早就有（round 274），但名字是 `_forget_device_state` ——
    统一入口按 `forget_device` 找不到它。"""
    from unittest.mock import MagicMock

    from 采集层.data_collector import DataCollector

    dc = DataCollector(MagicMock(), MagicMock())
    dc.running = False
    dc._failure_counts['d1'] = 3

    dc.forget_device('d1')
    assert 'd1' not in dc._failure_counts, '统一入口没清到采集侧的失败计数'


# ---------------------------------------------------------------------------
# 2. 统一入口
# ---------------------------------------------------------------------------

def test_forget_device_everywhere_calls_each_module(app):
    """统一入口必须遍历所有**已装配**的模块。"""
    from unittest.mock import MagicMock

    from 展示层.api.api_devices import _forget_device_everywhere

    calls = []
    for attr in ('device_control', 'oee_calculator', 'energy_manager'):
        mod = MagicMock()
        mod.forget_device.side_effect = lambda did, a=attr: calls.append((a, did)) or True
        setattr(app, attr, mod)

    with app.app_context():
        result = _forget_device_everywhere('d1')

    assert {'device_control', 'oee_calculator', 'energy_manager'} <= set(result)
    assert len(calls) == 3


def test_forget_device_everywhere_tolerates_missing_module(app):
    """模块没装配 / 没有 forget_device → 跳过，不许抛。"""
    from 展示层.api.api_devices import _forget_device_everywhere

    from 展示层.api.api_devices import _FORGET_DEVICE_HOLDERS

    for attr in _FORGET_DEVICE_HOLDERS:
        setattr(app, attr, None)

    with app.app_context():
        assert _forget_device_everywhere('d1') == {}


def test_forget_device_everywhere_swallows_module_error(app):
    """某个模块清理抛异常 → 记下来，但不许影响删除结果。"""
    from unittest.mock import MagicMock

    from 展示层.api.api_devices import _forget_device_everywhere

    mod = MagicMock()
    mod.forget_device.side_effect = RuntimeError('boom')
    app.device_control = mod

    with app.app_context():
        result = _forget_device_everywhere('d1')

    assert '清理异常' in result['device_control']


def test_delete_device_endpoint_cleans_state(app, client, auth_headers, monkeypatch):
    """端到端：删除成功后必须调用统一清理入口。"""
    from unittest.mock import MagicMock

    from 展示层.api import api_devices

    app.device_manager = MagicMock()
    app.device_manager.devices = {'d1': {'id': 'd1'}}
    app.device_manager.remove_device.return_value = True
    app.simulation_initializer = None

    cleaned = []
    monkeypatch.setattr(api_devices, '_forget_device_everywhere',
                        lambda did: cleaned.append(did) or {})

    resp = client.delete('/api/devices/d1', headers=auth_headers)

    assert resp.status_code == 200
    assert cleaned == ['d1'], '设备删掉了但各模块的按设备状态没清'


# ---------------------------------------------------------------------------
# 3. 闸门：新容器不许再忘记清理
# ---------------------------------------------------------------------------

#: 启动时一次性按配置构建、不支持热删设备的容器 —— 不需要 forget_device。
#: 每条都要写清为什么。
ALLOWED_WITHOUT_FORGET = {
    'gateway/base_gateway.py': '网关按启动时的配置构建容器，运行期不支持热删设备',
    'gateway/modbus_gateway.py': '同上（_register_configs / _byte_orders 启动构建）',
    'gateway/s7_gateway.py': '同上',
    'gateway/dnp3_gateway.py': '同上（_register_maps / _reverse_maps 启动构建）',
    'gateway/iec104_gateway.py': '同上（_ioa_maps / _reverse_ioa_maps 启动构建）',
    'gateway/opcua_gateway.py': '同上（_node_configs / _node_map 启动构建）',
    # `采集层/data_collector.py` 与 `device_manager.py` 已不在清单里 ——
    # 它们有 forget_device / 类内 pop，闸门自身就能判定合格（写在这里只会腐烂）。
}

SCAN_DIRS = ('智能层', 'gateway', '采集层', '报警层', '存储层', 'core')


def _classes_with_device_keyed_state():
    """返回 [(文件, 类名, 容器属性名)]：类里写了 `self._x[device_id] = ...`。"""
    import ast

    out = []
    for d in SCAN_DIRS:
        for path in (REPO / d).glob('*.py'):
            try:
                tree = ast.parse(path.read_text(encoding='utf-8', errors='ignore'))
            except SyntaxError:
                continue
            for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
                has_forget = any(
                    isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == 'forget_device' for n in cls.body)
                attrs = set()
                removed_attrs = set()
                for node in ast.walk(cls):
                    if (isinstance(node, ast.Assign)
                            and isinstance(node.targets[0], ast.Subscript)):
                        sub = node.targets[0]
                        if (isinstance(sub.value, ast.Attribute)
                                and isinstance(sub.value.value, ast.Name)
                                and sub.value.value.id == 'self'
                                and isinstance(sub.slice, ast.Name)
                                and sub.slice.id == 'device_id'):
                            attrs.add(sub.value.attr)
                    # 类内已有删除点也算合格（`self._x.pop(...)` / `del self._x[...]`）
                    if (isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Attribute)
                            and node.func.attr in ('pop', 'clear')
                            and isinstance(node.func.value, ast.Attribute)
                            and isinstance(node.func.value.value, ast.Name)
                            and node.func.value.value.id == 'self'):
                        removed_attrs.add(node.func.value.attr)
                    if isinstance(node, ast.Delete):
                        for tgt in node.targets:
                            if isinstance(tgt, ast.Subscript) and isinstance(
                                    tgt.value, ast.Attribute):
                                removed_attrs.add(tgt.value.attr)
                attrs -= removed_attrs
                if attrs and not has_forget:
                    out.append((str(path.relative_to(REPO)).replace('\\', '/'),
                                cls.name, sorted(attrs)))
    return out


def test_every_class_with_device_keyed_state_has_forget_device():
    """类里按 device_id 存状态，就必须有 `forget_device()`。

    这条闸门是 round 310 的产物：本轮一次性找出 9 个模块的 20 多个容器
    全都没有删除点。没有闸门的话，下一个人加缓存时会原样复发 ——
    而且**症状要等到"删设备再同名添加"才出现**，平时完全看不出来。
    """
    offenders = [
        f'{f}::{cls}  容器 {attrs}'
        for f, cls, attrs in _classes_with_device_keyed_state()
        if f not in ALLOWED_WITHOUT_FORGET
    ]
    assert not offenders, (
        '以下类按 device_id 存运行时状态，但没有 forget_device() —— '
        '设备删除后这些状态会残留（报表出现不存在的设备 / 同名新设备继承旧状态）：\n  '
        + '\n  '.join(offenders))


def test_allowlist_entries_are_not_stale():
    """允许清单不许腐烂：登记的类必须真的还有 device_id 容器。"""
    actual = {f for f, _c, _a in _classes_with_device_keyed_state()}
    stale = [f for f in ALLOWED_WITHOUT_FORGET if f not in actual]
    assert not stale, f'允许清单里这些文件已经没有 device_id 容器了，请移除: {stale}'


def test_guard_actually_detects_a_missing_forget_device(tmp_path):
    """反向对照：真造一个没有 forget_device 的类，闸门必须抓到。"""
    import ast

    src = (
        'class Bad:\n'
        '    def __init__(self):\n'
        '        self._cache = {}\n'
        '    def put(self, device_id, v):\n'
        '        self._cache[device_id] = v\n'
    )
    tree = ast.parse(src)
    cls = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)][0]
    has_forget = any(isinstance(n, ast.FunctionDef) and n.name == 'forget_device'
                     for n in cls.body)
    attrs = {
        n.targets[0].value.attr for n in ast.walk(cls)
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Subscript)
        and isinstance(n.targets[0].value, ast.Attribute)
        and isinstance(n.targets[0].slice, ast.Name)
        and n.targets[0].slice.id == 'device_id'
    }
    assert attrs == {'_cache'} and not has_forget, '闸门的检测逻辑失效了'
