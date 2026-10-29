# -*- coding: utf-8 -*-
"""round 321：`timeseries/mqtt_to_tsdb.py` —— 0% 覆盖的模块，补测试时抓出"静默丢数据"。

## 为什么审这个模块

用 `coverage` 扫全仓（见 `AUDIT-开源工具全审-20261029.md`）：
这个模块 **162 行、0% 覆盖**，却是 MQTT → TDengine 的**唯一写入通道**。
补测试时直接暴露一处数据丢失。

## 缺陷：写失败（返回 False）不回填，数据静默消失

```python
if self.tdengine.write_telemetry_batch(records):
    self.stats['telemetry_written'] += len(records)
else:
    self.stats['errors'] += len(records)      # ← 只计数
self.logger.debug(...)                        # records 已经 clear()，没了
```

round 298 在这里补过一刀：原来返回值被直接丢弃、`except` 分支永远不进，
所以那次改成"判返回值 + 计数"。但 **False 分支没有把 records 放回缓冲区** ——
而 round 298 的注释写的正是「写失败必须回填并计数」，代码只做了后一半。

后果：TDengine 重启 / 网络抖动的几十秒里，每 5 秒一批遥测**静默消失**，
统计里只有 `errors` 在涨，看不出"丢了多少"。

报警同理：`write_alarm` 返回 False 时 `continue` —— **报警直接丢掉**，
而报警丢了等于"这件事没发生过"。

修法：两个 False 分支都回填缓冲区，并新增 `*_requeued` 计数；
另外给 deque 满时"被挤掉的最旧记录"加 `*_dropped` 计数
（以前完全不可见）。
"""

import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from timeseries.mqtt_to_tsdb import MQTTToTSDBService


class _FakeSubscriber:
    def __init__(self):
        self.on_telemetry = None
        self.on_alarm = None
        self.calls = []
        self.started = False
        self.start_count = 0
        self.stopped = False

    def subscribe_telemetry(self):
        self.calls.append('subscribe_telemetry')

    def subscribe_alarms(self):
        self.calls.append('subscribe_alarms')

    def start(self):
        self.started = True
        self.start_count += 1

    def stop(self):
        self.stopped = True


class _FakeTDengine:
    def __init__(self, write_ok=True, raise_on_write=False):
        self.write_ok = write_ok
        self.raise_on_write = raise_on_write
        self.connected = False
        self.tables_inited = False
        self.disconnected = False
        self.telemetry_batches = []
        self.alarm_records = []

    def connect(self):
        self.connected = True
        return True

    def init_tables(self):
        self.tables_inited = True

    def disconnect(self):
        self.disconnected = True

    def write_telemetry_batch(self, records):
        if self.raise_on_write:
            raise RuntimeError('TDengine 掉线')
        self.telemetry_batches.append(list(records))
        return self.write_ok

    def write_alarm(self, record):
        if self.raise_on_write:
            raise RuntimeError('TDengine 掉线')
        self.alarm_records.append(record)
        return self.write_ok


@pytest.fixture
def svc():
    sub = _FakeSubscriber()
    td = _FakeTDengine()
    # flush_interval 给大一点：用例都直接调 `_flush_buffers()`，不依赖后台线程
    return MQTTToTSDBService(sub, td, batch_size=100, flush_interval=3600), sub, td


def _telemetry(device='d1', metrics=None, ts=None):
    return SimpleNamespace(
        DeviceID=device,
        Timestamp=(ts or datetime(2026, 10, 29, 10, 0, 0)).timestamp(),
        Metrics=metrics if metrics is not None else {
            'temp': {'value': 42.5, 'quality': 192, 'unit': '°C'},
        },
        Protocol='modbus',
        GatewayID='gw1',
    )


def _alarm(alarm_id='a1', device='d1'):
    return SimpleNamespace(
        AlarmID=alarm_id, DeviceID=device,
        Timestamp=datetime(2026, 10, 29, 10, 0, 0).timestamp(),
        Level='critical', Type='threshold', Message='超温',
        Value=95.0, Threshold=90.0, Acknowledged=False,
    )


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------

def test_start_stops_when_tdengine_unreachable(svc):
    """TDengine 连不上时**不许**进入 running 状态（否则会静默丢数据）。"""
    service, sub, td = svc
    td.connect = lambda: False
    service.start()
    assert service.running is False
    assert sub.started is False
    assert td.tables_inited is False


def test_start_subscribes_and_stop_flushes(svc):
    service, sub, td = svc
    service.start()
    try:
        assert service.running is True
        assert sub.started is True
        assert td.tables_inited is True
        assert 'subscribe_telemetry' in sub.calls and 'subscribe_alarms' in sub.calls
    finally:
        service.stop()
    assert sub.stopped is True
    assert td.disconnected is True
    assert service.running is False


def test_double_start_is_harmless(svc):
    """重复 `start()` 只打 warning —— 不许再起一个刷新线程、不许重复订阅。

    重复起线程会让两批 flush 并发跑同一份缓冲区（重复写/丢写），
    重复订阅会让同一条 MQTT 消息被写两遍。
    """
    service, sub, _td = svc
    service.start()
    try:
        first_thread = service._flush_thread
        service.start()
        assert service.running is True
        assert service._flush_thread is first_thread, '第二次 start 又起了一个刷新线程'
        assert sub.start_count == 1, f'订阅器被启动了 {sub.start_count} 次'
    finally:
        service.stop()


# ---------------------------------------------------------------------------
# 遥测：转换与写入
# ---------------------------------------------------------------------------

def test_telemetry_is_converted_per_register(svc):
    """一条遥测里有几个寄存器就产几条记录，字段要映射对。"""
    service, _sub, td = svc
    service._on_telemetry(_telemetry(metrics={
        'temp': {'value': 42.5, 'quality': 192, 'unit': '°C'},
        'press': {'value': 1.2},
    }))
    assert len(service._telemetry_buffer) == 2
    assert service.stats['telemetry_received'] == 1

    service._flush_buffers()
    batch = td.telemetry_batches[0]
    names = sorted(r.register_name for r in batch)
    assert names == ['press', 'temp']
    temp = next(r for r in batch if r.register_name == 'temp')
    assert temp.value == 42.5
    assert temp.quality == 192
    assert temp.unit == '°C'
    assert temp.device_id == 'd1'
    assert service.stats['telemetry_written'] == 2


def test_batch_size_triggers_flush(svc):
    service, _sub, td = svc
    service.batch_size = 2
    service._on_telemetry(_telemetry(metrics={'a': {'value': 1}, 'b': {'value': 2}}))
    assert td.telemetry_batches, '达到 batch_size 没有触发写入'
    assert service.stats['telemetry_written'] == 2


def test_telemetry_write_failure_requeues_records(svc):
    """**本轮核心**：写入返回 False 时，记录必须回填缓冲区（原来直接丢）。"""
    service, _sub, td = svc
    td.write_ok = False

    service._on_telemetry(_telemetry())
    service._flush_buffers()

    assert len(service._telemetry_buffer) == 1, (
        '写入失败后记录被丢弃了 —— TDengine 抖动期间的数据会静默消失')
    assert service.stats['telemetry_requeued'] == 1
    assert service.stats['errors'] == 1
    assert service.stats['telemetry_written'] == 0

    # 恢复后重试应成功，且缓冲区清空
    td.write_ok = True
    service._flush_buffers()
    assert service.stats['telemetry_written'] == 1
    assert len(service._telemetry_buffer) == 0


def test_telemetry_write_exception_requeues_records(svc):
    """反向对照：抛异常的老路径也必须回填（不能被本轮改动破坏）。"""
    service, _sub, td = svc
    td.raise_on_write = True
    service._on_telemetry(_telemetry())
    service._flush_buffers()
    assert len(service._telemetry_buffer) == 1
    assert service.stats['errors'] >= 1


# ---------------------------------------------------------------------------
# 报警：立即写入 + 失败回填
# ---------------------------------------------------------------------------

def test_alarm_is_written_immediately(svc):
    service, _sub, td = svc
    service._on_alarm(_alarm())
    assert len(td.alarm_records) == 1, '报警没有立即写入'
    assert service.stats['alarms_written'] == 1


def test_alarm_write_failure_requeues(svc):
    """**本轮核心**：报警写失败必须回填 —— 报警丢了等于"没发生过"。"""
    service, _sub, td = svc
    td.write_ok = False
    service._on_alarm(_alarm())

    assert len(service._alarm_buffer) == 1, '报警写失败后被丢弃了'
    assert service.stats['alarms_requeued'] == 1
    assert service.stats['alarms_written'] == 0


def test_alarm_exception_counts_error(svc):
    service, _sub, td = svc
    td.raise_on_write = True
    service._on_alarm(_alarm())
    assert service.stats['errors'] >= 1


# ---------------------------------------------------------------------------
# 缓冲区满：以前完全不可见
# ---------------------------------------------------------------------------

def test_buffer_overflow_is_counted(svc):
    """deque 满时会挤掉最旧的 —— 必须能看出丢了多少条。"""
    service, _sub, _td = svc
    service._telemetry_buffer = type(service._telemetry_buffer)(maxlen=2)
    for i in range(5):
        service._on_telemetry(_telemetry(metrics={'a': {'value': i}}))

    assert service.stats['telemetry_dropped'] == 3, (
        f"丢 {service.stats['telemetry_dropped']} 条，应为 3 —— 溢出必须可观测")
    assert len(service._telemetry_buffer) == 2


def test_alarm_buffer_overflow_is_counted(svc):
    service, _sub, _td = svc
    service._alarm_buffer = type(service._alarm_buffer)(maxlen=1)
    service._flush_alarms = lambda: None      # 只验入队，不写库
    service._on_alarm(_alarm('a1'))
    service._on_alarm(_alarm('a2'))
    assert service.stats['alarms_dropped'] == 1


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def test_get_stats_exposes_new_counters(svc):
    service, _sub, _td = svc
    stats = service.get_stats()
    for key in ('telemetry_requeued', 'alarms_requeued',
                'telemetry_dropped', 'alarms_dropped'):
        assert key in stats, f'{key} 没出现在统计里 —— 运维看不到丢数据'
    assert stats['running'] is False
    assert stats['telemetry_buffer_size'] == 0
