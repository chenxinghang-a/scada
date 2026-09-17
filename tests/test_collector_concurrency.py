"""采集层共享状态的并发安全回归测试。

背景（2026-09 审计遗留项）
--------------------------
两处共享字典被多线程访问，但**关键路径漏了锁**：

1. `DeviceManager.reload_config()` 原先写成 `self.devices.clear()` 后再逐条填。
   重连循环正在 `for device_id, config in self.devices.items()` ——
   中间态（空表 / 半张表）会让它抛
   `RuntimeError: dictionary changed size during iteration`，
   或者静默漏掉一整批设备。

2. `DataCollector.remove_device_task()` 里对 `_last_values` / `_last_times` /
   `_circuit_state` / `_failure_counts` 的清理**没走 `_tracking_lock`**，
   而采集线程在 `_handle_normal_collection()` 里正往同一批字典写
   （`self._last_values[key] = value`）。
   撞上就是 RuntimeError，导致**设备删除失败、追踪数据残留**。

修法：前者改成「先在临时字典里解析完，再原子替换」；
后者把所有访问统一收进 `_tracking_lock`。

本测试用「一边读一边改」来复现这两类竞态。
"""
import threading
import time
from unittest.mock import MagicMock

import pytest
import yaml

from 采集层.device_manager import DeviceManager
from 采集层.data_collector import DataCollector

#: 设备数量 —— 多一点能把「半张表」的窗口撑大，更容易暴露问题
DEVICE_COUNT = 40


@pytest.fixture
def dm_with_devices(tmp_path):
    cfg = tmp_path / 'devices.yaml'
    cfg.write_text(
        yaml.dump(
            {'devices': [
                {'id': f'dev_{i:03d}', 'name': f'Device {i}',
                 'protocol': 'modbus_tcp', 'host': '127.0.0.1', 'port': 502 + i,
                 'enabled': True, 'registers': []}
                for i in range(DEVICE_COUNT)
            ]},
            allow_unicode=True,
        ),
        encoding='utf-8',
    )
    return DeviceManager(config_path=str(cfg), simulation_mode=True,
                         use_enhanced_simulation=False)


def _run_concurrently(worker, readers, iterations, timeout=30):
    """跑若干读者线程 + 一个写者，收集所有异常。"""
    errors = []
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                worker()
        except Exception as e:      # pragma: no cover - 只有出问题才走到
            errors.append(e)
            stop.set()

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(readers)]
    for t in threads:
        t.start()

    try:
        for _ in range(iterations):
            if stop.is_set():
                break
            time.sleep(0.001)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=timeout)

    return errors


# ============================================================
# 1. DeviceManager.reload_config 的原子性
# ============================================================

def test_reload_config_never_exposes_partial_table(dm_with_devices):
    """热重载期间，读者**永远**只能看到完整的一张设备表。

    修复前是 clear() 后再逐条填，读者会看到 0 个或不足 40 个设备，
    甚至直接抛 `RuntimeError: dictionary changed size during iteration`。
    """
    dm = dm_with_devices
    assert len(dm.devices) == DEVICE_COUNT

    observed_sizes = []
    errors = []

    def reader():
        try:
            # dict(dm.devices) 在「迭代中被改」时会直接抛 RuntimeError —— 正是要抓的
            observed_sizes.append(len(dict(dm.devices)))
        except Exception as e:      # pragma: no cover
            errors.append(e)

    stop = threading.Event()

    def spin():
        while not stop.is_set():
            reader()

    threads = [threading.Thread(target=spin, daemon=True) for _ in range(4)]
    for t in threads:
        t.start()

    try:
        for _ in range(30):
            dm.reload_config()
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=30)

    assert not errors, f'热重载期间读者抛异常: {errors[:3]}'
    assert observed_sizes, '读者一次都没跑起来'
    bad = [n for n in observed_sizes if n != DEVICE_COUNT]
    assert not bad, (
        f'热重载暴露了不完整的设备表：观察到 {len(bad)} 次异常大小，'
        f'样本 {sorted(set(bad))[:10]}（期望恒为 {DEVICE_COUNT}）'
    )


def test_reload_config_keeps_devices_intact(dm_with_devices):
    """重载后设备集合必须与配置一致（别把内容搞丢）"""
    dm = dm_with_devices
    before = set(dm.devices)

    dm.reload_config()

    assert set(dm.devices) == before
    assert len(dm.devices) == DEVICE_COUNT


def test_reload_config_replaces_object_not_mutates(dm_with_devices):
    """必须是「换对象」而不是「原地改」—— 这是原子性的实现前提"""
    dm = dm_with_devices
    old = dm.devices

    dm.reload_config()

    assert dm.devices is not old, (
        'reload_config 仍在原地修改 self.devices，读者会看到中间态'
    )


# ============================================================
# 2. DataCollector.remove_device_task 与采集线程的竞态
# ============================================================

@pytest.fixture
def collector():
    dm = MagicMock()
    dm.devices = {f'dev_{i:03d}': {'id': f'dev_{i:03d}', 'enabled': True,
                                   'protocol': 'modbus_tcp', 'registers': []}
                  for i in range(DEVICE_COUNT)}
    dm.get_device_status.return_value = {'connected': True, 'stats': {'state': 'running'}}
    return DataCollector(dm, MagicMock(), MagicMock())


def test_remove_device_task_races_safely_with_writers(collector):
    """删除设备时的追踪数据清理，不能和采集线程的写入撞上。

    修复前这段清理没走 `_tracking_lock`，而采集线程在往同一批字典写 ——
    会抛 `RuntimeError: dictionary changed size during iteration`，
    结果是**设备删除失败、追踪数据残留**（正是这段代码想防的内存泄漏）。
    """
    c = collector
    errors = []
    stop = threading.Event()

    def writer():
        """模拟采集线程持续更新 _last_values / _last_times。

        一次写多个键，把临界区拉宽 —— 否则窗口太小，
        无锁的读方几乎撞不上，测试就失去检出力（变异测试时发现过这个问题）。
        """
        try:
            i = 0
            while not stop.is_set():
                dev = f'dev_{i % DEVICE_COUNT:03d}'
                with c._tracking_lock:
                    for r in range(20):
                        key = f'{dev}:reg{r}'
                        c._last_values[key] = i
                        c._last_times[key] = time.time()
                i += 1
        except Exception as e:      # pragma: no cover
            errors.append(e)

    def remover():
        """模拟运维/API 并发删除设备"""
        try:
            i = 0
            while not stop.is_set():
                c.remove_device_task(f'dev_{i % DEVICE_COUNT:03d}')
                i += 1
        except Exception as e:      # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=writer, daemon=True) for _ in range(3)]
    threads += [threading.Thread(target=remover, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()

    time.sleep(2.0)
    stop.set()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f'并发删除/写入抛异常: {errors[:3]}'


def test_remove_device_task_clears_tracking_data(collector):
    """功能本身不能被锁改坏：删除后该设备的追踪数据必须清空"""
    c = collector
    dev = 'dev_001'
    with c._tracking_lock:
        c._last_values[f'{dev}:r1'] = 1.0
        c._last_values[f'{dev}:r2'] = 2.0
        c._last_times[f'{dev}:r1'] = time.time()
        c._failure_counts[dev] = 3
        c._circuit_state[dev] = {'opened_at': time.time()}
        # 另一台设备的键必须留着
        c._last_values['dev_002:r1'] = 9.0

    c.remove_device_task(dev)

    assert not [k for k in c._last_values if k.startswith(f'{dev}:')]
    assert not [k for k in c._last_times if k.startswith(f'{dev}:')]
    assert dev not in c._failure_counts
    assert dev not in c._circuit_state
    assert c._last_values.get('dev_002:r1') == 9.0, '误删了其他设备的数据'


def test_tracking_lock_is_held_during_cleanup(collector):
    """静态守卫：remove_device_task 的清理必须**真的**在 `_tracking_lock` 内。

    注意要匹配 `with self._tracking_lock:` 这个**语句**，
    而不是 `'_tracking_lock' in src` —— 后者会被注释/文档字符串里的同名词
    误判为通过（本测试第一版就是这么写的，变异测试时才发现它抓不到问题）。
    """
    import inspect
    src = inspect.getsource(collector.remove_device_task)
    assert 'with self._tracking_lock:' in src, (
        'remove_device_task 没有真的对追踪字典加锁 —— 会与采集线程竞态，'
        '导致设备删除失败、追踪数据残留'
    )
