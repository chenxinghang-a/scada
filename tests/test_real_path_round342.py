"""真实链路的「假成功」与「假数据」（round 342）。

round 341 把真实设备**接得上**了；这一轮修的是接上之后
「**系统说成功，其实没成**」的四种形状。它们共同点：不报错、不告警、
日志干净，只有现场数据是错的。

1. **PID 输出从未写进设备**（`run.py`）—— 回调里调
   `device_manager.adjust_device(device_id, str(address), value)`，
   而 `adjust_device` 的第二个参数是**点位名**（内部 `reg['name'] == name`）。
   传 `"10"` 永远匹配不到，返回 `{'success': False, 'message': '寄存器 10 不存在'}`，
   而返回值被完全忽略（只 catch 异常）。闭环一直在空转。

2. **REST / OPC UA / MQTT 采集恒成功**（`data_collector`）——
   `_collect_from_cache` 未连接时静默 return，调用方无条件
   `successful_collections += 1; return True`。于是网关断电、Broker 断线
   都算「采集成功」，断路器永不打开、`failed_collections` 恒 0。

3. **MQTT retained 被当实时值** —— Broker 缓存的上一条状态（可能是几天前的）
   被打上 `now()` 时间戳，以 GOOD 质量码入库，并参与报警与联锁。

4. **设备离线不产生任何告警** —— `update_device_health()` 是唯一累加
   `_comm_failures`、触发设备安全状态与离线告警的入口，但生产代码零调用方。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO = Path(__file__).resolve().parent.parent


def _code_only(path: Path) -> str:
    """去掉注释与 docstring 的源码（结构守卫用，避免被说明文字误判）。"""
    import ast
    src = path.read_text(encoding='utf-8')
    tree = ast.parse(src)
    lines = src.splitlines()
    keep = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = getattr(node, 'body', [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                start, end = body[0].lineno, body[0].end_lineno
                for i in range(start - 1, end):
                    lines[i] = ''
    for line in lines:
        keep.append(line.split('#', 1)[0])
    return '\n'.join(keep)


# ============================================================
# 1. PID 写路径
# ============================================================

class TestPidWritePath:
    """边缘决策的写回调必须走安全层，且必须检查返回值。"""

    def test_pid_does_not_call_adjust_device(self):
        """结构守卫：`run.py` 不许再出现 `device_manager.adjust_device`。

        它绕过急停/联锁/量程/审计/回读，而且参数传的是 `str(address)`——
        `adjust_device` 按**点位名**匹配，传地址永远匹配不到，
        所以这条写路径**从未成功过**，返回值又被忽略，全程静默。
        """
        code = _code_only(REPO / 'run.py')
        assert 'device_manager.adjust_device(' not in code, (
            'PID 写回调又走回了 adjust_device —— 绕过安全层且参数语义不符')

    def test_pid_uses_write_with_verification(self):
        """必须走 `write_with_verification`，并把失败/回读不一致报出来。"""
        code = _code_only(REPO / 'run.py')
        assert 'write_with_verification(' in code, 'PID 写回调没有走安全层'
        # 返回值必须被检查（否则失败仍然静默）
        assert "result.get('success')" in code or 'result.get("success")' in code, (
            'PID 写回调没有检查返回值 —— 写失败会静默')
        assert 'verified' in code, (
            'PID 写回调没有检查回读结果 —— 写下去了但设备没变也看不出来')


# ============================================================
# 2. 缓存型协议的采集成败
# ============================================================

class TestCacheCollectionResult:
    """`_collect_from_cache` 必须返回实际条数，调用方据此计成败。"""

    def test_returns_zero_when_disconnected(self):
        """未连接 → 返回 0（原实现返回 None，上层当成功）。"""
        from 采集层.data_collector import DataCollector

        client = MagicMock()
        client.connected = False
        collector = DataCollector.__new__(DataCollector)
        produced = collector._collect_from_cache(client, 'dev1', __import__('datetime').datetime.now())
        assert produced == 0, f'未连接时应返回 0 条，实际 {produced!r}'

    def test_returns_zero_when_cache_empty(self):
        from 采集层.data_collector import DataCollector
        from datetime import datetime

        client = MagicMock()
        client.connected = True
        client.get_latest_data.return_value = {}
        collector = DataCollector.__new__(DataCollector)
        collector.data_queue = __import__('queue').Queue()
        assert collector._collect_from_cache(client, 'dev1', datetime.now()) == 0

    def test_counts_enqueued_items(self):
        from 采集层.data_collector import DataCollector
        from datetime import datetime
        import queue

        client = MagicMock()
        client.connected = True
        client.get_latest_data.return_value = {
            'temp': {'value': 25.5, 'unit': '°C'},
            'press': {'value': 0.101, 'unit': 'MPa'},
            'bad': {'value': float('nan'), 'unit': ''},   # 坏值不入队
        }
        collector = DataCollector.__new__(DataCollector)
        collector.data_queue = queue.Queue()
        assert collector._collect_from_cache(client, 'dev1', datetime.now()) == 2

    def test_caller_counts_failure_on_zero(self):
        """结构守卫：调用方必须按返回值判失败，不能无条件 +1。"""
        src = (REPO / '采集层' / 'data_collector.py').read_text(encoding='utf-8')
        assert 'produced == 0' in src, (
            '调用方没检查 _collect_from_cache 的返回值 —— 未连接也会被计成功')


# ============================================================
# 3. MQTT retained
# ============================================================

class TestMqttRetained:
    """retained 消息不是「刚发生的事件」。"""

    def test_retain_flag_propagates_to_cache(self):
        """`msg.retain=True` 必须落到 `latest_data[*]['retained']`。"""
        from 采集层.mqtt_client import MQTTClient

        client = MQTTClient.__new__(MQTTClient)
        # 用 defaultdict 模拟 __init__ 里建好的计数器（这里用 __new__ 跳过了构造）
        client.stats = __import__('collections').defaultdict(int)
        client.latest_data = {}
        client._data_callbacks = []
        client._subscriptions = {}
        client.config = {}

        msg = MagicMock()
        msg.topic = 'factory/motor/temp'
        msg.payload = b'{"device_id": "dev1", "register": "temp", "value": 88.5}'
        msg.retain = True

        client._on_message(None, None, msg)
        entry = client.latest_data.get('temp') or {}
        assert entry.get('retained') is True, (
            f'retained 标记没传到缓存：{entry} —— 旧值会被当实时值入库')

    def test_normal_message_not_marked_retained(self):
        from 采集层.mqtt_client import MQTTClient

        client = MQTTClient.__new__(MQTTClient)
        # 用 defaultdict 模拟 __init__ 里建好的计数器（这里用 __new__ 跳过了构造）
        client.stats = __import__('collections').defaultdict(int)
        client.latest_data = {}
        client._data_callbacks = []
        client._subscriptions = {}
        client.config = {}

        msg = MagicMock()
        msg.topic = 'factory/motor/temp'
        msg.payload = b'{"device_id": "dev1", "register": "temp", "value": 88.5}'
        msg.retain = False

        client._on_message(None, None, msg)
        assert (client.latest_data.get('temp') or {}).get('retained') is False

    def test_retained_marked_stale_in_collector(self):
        """retained 的数据入队时必须带 `stale=True`（质量码降级）。"""
        from 采集层.data_collector import DataCollector
        from datetime import datetime
        import queue

        client = MagicMock()
        client.connected = True
        client.get_latest_data.return_value = {
            'temp': {'value': 88.5, 'unit': '°C', 'retained': True},
            'press': {'value': 0.1, 'unit': 'MPa', 'retained': False},
        }
        collector = DataCollector.__new__(DataCollector)
        collector.data_queue = queue.Queue()
        collector._collect_from_cache(client, 'dev1', datetime.now())

        items = {}
        while not collector.data_queue.empty():
            it = collector.data_queue.get_nowait()
            items[it['register_name']] = it
        assert items['temp'].get('stale') is True, (
            'retained 的值没打 stale —— 几天前的旧值会以 GOOD 质量码入库')
        assert not items['press'].get('stale'), '正常消息不该被标 stale'


# ============================================================
# 4. 设备健康上报
# ============================================================

class TestDeviceHealthReporting:
    """采集必须上报健康状态，否则设备掉线不产生告警。"""

    def test_update_device_health_has_caller(self):
        """结构守卫：`update_device_health` 必须有生产调用方。

        round 342 前它是**零调用方**的死方法 —— 设备离线不告警、
        健康面板永远空。这是「算了但没人读」的又一个实例。
        """
        hits = []
        for py in (REPO / '采集层').rglob('*.py'):
            src = py.read_text(encoding='utf-8')
            if 'update_device_health(' in src and 'def update_device_health' not in src:
                hits.append(py.name)
        assert hits, 'update_device_health 仍然没有生产调用方 —— 设备离线不会告警'

    def test_collect_reports_health(self):
        """采集成功/失败都要上报（成功传 True，失败传 False）。"""
        from 采集层.data_collector import DataCollector

        control = MagicMock()
        collector = DataCollector.__new__(DataCollector)
        collector.device_control = control

        collector._report_device_health('dev1', True, 12.5)
        collector._report_device_health('dev1', False, 3000.0)

        assert control.update_device_health.call_count == 2
        calls = [c.args for c in control.update_device_health.call_args_list]
        assert calls[0][1] is True and calls[1][1] is False

    def test_health_report_failure_does_not_break_collection(self):
        """健康上报自己炸了，不能影响采集。"""
        from 采集层.data_collector import DataCollector

        control = MagicMock()
        control.update_device_health.side_effect = RuntimeError('boom')
        collector = DataCollector.__new__(DataCollector)
        collector.device_control = control

        # 不抛异常即通过；同时断言上报路径**真的走到了**
        # （否则这条测试会因为「压根没调」而假绿）
        collector._report_device_health('dev1', True, 1.0)
        assert control.update_device_health.called, '上报路径没被走到，这条测试没覆盖到异常分支'

    def test_no_control_is_silent(self):
        """没有安全模块时静默跳过（不抛 AttributeError）。"""
        from 采集层.data_collector import DataCollector

        collector = DataCollector.__new__(DataCollector)
        collector.device_control = None
        # 返回 None 且不抛 AttributeError 即正确（有安全模块时才会写库）
        assert collector._report_device_health('dev1', True, 1.0) is None
        assert collector.device_control is None, '前提不成立：这个用例要求没有安全模块'


class TestOpcuaSubscriptionStats:
    """OPC UA 订阅失败必须能被上层看见（round 345）。

    `node_id` 写错（现场最常见：照手册抄成 `ns=2;s=温度`，设备实际是
    `ns=2;i=2`）时 `subscribe_data_change` 抛 `BadNodeIdUnknown`。原实现
    每个节点打一条 ERROR 就继续，于是：`connected=True`（界面显示在线）、
    `latest_data` 永远空、日志里没有一条说「全失败了」。

    实测（`tools/opcua_e2e_smoke.py`）：node_id 猜错时 2 个节点全部失败，
    而 `connected=True`。
    """

    def test_counts_subscribed_nodes(self):
        """结构守卫：必须统计成功订阅数并置 `subscription_failed`。"""
        src = (REPO / '采集层' / 'opcua_client.py').read_text(encoding='utf-8')
        assert 'nodes_subscribed_ok' in src, '没有统计订阅成功数 —— 上层无法判断设备是否真可用'
        assert 'subscription_failed' in src, '全部订阅失败时没有置标志位'
        assert '全部订阅失败' in src, '全部失败时没有一条明确的总结日志（只有 N 条单点 ERROR）'
