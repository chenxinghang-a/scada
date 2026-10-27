# -*- coding: utf-8 -*-
"""删除设备时必须清掉它的**全部**运行期状态（round 274）。

缺陷形态
--------
`remove_device_task()` 会清 `_last_values` / `_last_times` / `_circuit_state` /
`_failure_counts`，但**漏了 `_device_intervals`**。后果：

* `get_stats()['device_intervals']` 一直列出**已删除**的设备 ——
  运维在界面上看到一台不存在的设备"间隔 5s"，会去查一个不存在的问题；
* 重新添加同名设备时，在第一次采集之前会**继承旧间隔**（旧设备可能处于
  停机态 30s），表现为"新设备刚加上就采得很慢"。

这类"删了没清干净"的缺陷有个共同点：**每加一个新的按设备状态容器，就要记得
加一行清理**，而漏掉时没有任何报错。所以本文件除了行为用例，还有一条
**结构性守卫**：扫源码里所有 `self._xxx[device_id] = ...` 的赋值，
发现某个容器没登记进 `PER_DEVICE_STATE_ATTRS` 就红。
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

COLLECTOR_SRC = Path(__file__).resolve().parents[1] / '采集层' / 'data_collector.py'


def _bare_collector():
    """只装配删除/统计需要的字段（不连设备、不起线程）。"""
    from 采集层.data_collector import DataCollector

    c = DataCollector.__new__(DataCollector)
    c.tasks = {}
    c._tasks_lock = threading.Lock()
    c._tracking_lock = threading.Lock()
    c._cancelled_devices = set()
    c._last_values = {}
    c._last_times = {}
    c._circuit_state = {}
    c._failure_counts = {}
    c._device_intervals = {}
    c.stats = {}
    c._stats_lock = threading.Lock()
    c.running = True

    class _QueueStub:
        def qsize(self):
            return 0

    c.data_queue = _QueueStub()
    return c


def _seed(c, device_id='dev1', other='dev2'):
    """给两台设备都塞满状态。"""
    for dev in (device_id, other):
        c._last_values[f'{dev}:temp'] = 20.0
        c._last_times[f'{dev}:temp'] = 123.0
        c._circuit_state[dev] = {'opened_at': 1.0}
        c._failure_counts[dev] = 2
        c._device_intervals[dev] = 5


# ---------------------------------------------------------------------------
# 1. 行为：删除后状态必须干净
# ---------------------------------------------------------------------------

def test_remove_clears_all_per_device_state():
    c = _bare_collector()
    _seed(c, 'dev1', 'dev2')

    c.remove_device_task('dev1')

    assert 'dev1:temp' not in c._last_values
    assert 'dev1:temp' not in c._last_times
    assert 'dev1' not in c._circuit_state
    assert 'dev1' not in c._failure_counts
    assert 'dev1' not in c._device_intervals, (
        '删除设备后 _device_intervals 仍残留 —— get_stats 会列出不存在的设备')


def test_remove_does_not_touch_other_devices():
    c = _bare_collector()
    _seed(c, 'dev1', 'dev2')

    c.remove_device_task('dev1')

    assert 'dev2:temp' in c._last_values
    assert c._failure_counts['dev2'] == 2
    assert c._device_intervals['dev2'] == 5


def test_device_intervals_not_reported_after_removal():
    """统计接口里不该再出现已删除设备的间隔。"""
    c = _bare_collector()
    _seed(c, 'dev1', 'dev2')

    c.remove_device_task('dev1')

    intervals = c.get_stats().get('device_intervals', {})
    assert 'dev1' not in intervals
    assert 'dev2' in intervals


# ---------------------------------------------------------------------------
# 2. 结构性守卫：新容器必须登记
# ---------------------------------------------------------------------------

def _containers_assigned_by_device_id() -> set[str]:
    """扫源码，找出所有 `self._xxx[device_id] = ...` 的容器名。"""
    code = re.sub(r'#.*$', '', COLLECTOR_SRC.read_text(encoding='utf-8'), flags=re.M)
    return set(re.findall(r'self\.(_[A-Za-z_]+)\[device_id\]\s*=', code))


def test_every_per_device_container_is_registered():
    """任何按设备索引的状态容器都必须出现在清理清单里。

    round 289 起有两个类别，生命周期**相反**，都要登记，但登记到不同清单：

    * `PER_DEVICE_STATE_ATTRS` —— 设备活着时积累、删除时清空；
    * `CANCELLED_REGISTRY_ATTRS` —— **删除时才写入**（挡住复活），靠
      `_prune_cancelled()` 到期回收。塞进前者会让删除瞬间抹掉刚写的登记，
      round 219 的僵尸采集会全部回归。

    所以这里的判据是「落在两个清单之一」，而不是「落在清理清单里」。
    """
    from 采集层.data_collector import DataCollector

    assigned = _containers_assigned_by_device_id()
    registered = set(DataCollector.PER_DEVICE_STATE_ATTRS)
    registry = set(DataCollector.CANCELLED_REGISTRY_ATTRS)

    missing = assigned - registered - registry
    assert not missing, (
        f'这些按设备索引的容器没有登记：{sorted(missing)}\n'
        '删设备时清理的 → 加进 PER_DEVICE_STATE_ATTRS 并在 _forget_device_state() 里清；\n'
        '删除时才写入的登记表 → 加进 CANCELLED_REGISTRY_ATTRS 并由 _prune_cancelled() 回收')
    # 反向：登记在「取消登记表」里的，不能同时又出现在清理清单里（生命周期互斥）
    both = registered & registry
    assert not both, f'同一容器被登记进两个生命周期互斥的清单：{sorted(both)}'


def test_registered_containers_are_actually_cleared():
    """反向：清单里列的容器，删除后必须真的为空（防"登记了但没清"）。"""
    c = _bare_collector()
    _seed(c, 'dev1', 'dev2')
    c.remove_device_task('dev1')

    from 采集层.data_collector import DataCollector
    for attr in DataCollector.PER_DEVICE_STATE_ATTRS:
        container = getattr(c, attr, {})
        leftover = [k for k in container if str(k).startswith('dev1')]
        assert not leftover, f'{attr} 里还留着 {leftover}'


def test_remove_path_delegates_to_forget_helper():
    """删除路径只该调用 `_forget_device_state`，不许自己再抄一遍 pop 链。

    （断路器**恢复**时也会 pop `_circuit_state`，那是另一回事，不算重复 ——
    所以这里只看 `remove_device_task` 的函数体。）
    """
    code = re.sub(r'#.*$', '', COLLECTOR_SRC.read_text(encoding='utf-8'), flags=re.M)
    start = code.find('def remove_device_task')
    end = code.find('def _forget_device_state')
    assert start != -1 and end != -1 and start < end
    body = code[start:end]

    assert '_forget_device_state(device_id)' in body, '删除路径没有走统一清理'
    leftovers = re.findall(r'self\._\w+\.pop\(device_id', body)
    assert not leftovers, (
        f'删除路径里还有零散的清理：{leftovers} —— 应统一放进 _forget_device_state()')
