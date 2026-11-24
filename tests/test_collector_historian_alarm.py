"""round 392（Q8）：写库失败时安全联锁照常执行（保持现状 + 补告警）。

架构评审结论（方案 A）
--------------------
控制平面与数据平面隔离：

  * **联锁属于实时控制** —— 不能因为时序库拥堵/断连/磁盘满就停止保护。
    写库失败的那一轮，联锁仍在当前采集周期内执行（round 385 已把联锁
    从「落库成功后才喂」的批次里拆出来），且重试批次不会重复下发；
  * **非幂等的统计/智能模块**（OEE、SPC、预测维护…）仍必须"落库成功
    后才喂"，否则重试会重复计数 —— 这正是不能改成 Q8-B「全部独立执行」
    的原因；
  * 额外补一条可观测性：连续 3 个批次写库失败触发
    ``HISTORIAN_WRITE_ERROR`` 系统告警（恢复后自动消警），
    让值班员知道这段时间历史数据有缺口。

本文件钉死的判据：
  1. 断开时序库后，联锁在一个周期内触发，且每条数据只下发一次；
  2. 写库失败期间 OEE 计数不增加（不喂半成品/重试数据）；
  3. 数据库恢复后智能统计只累加一次（重试不重复计数）；
  4. 连续失败 3 次告警一次、恢复后自动消警。
"""

import threading
import time
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from 采集层.data_collector import (
    DataCollector,
    HISTORIAN_ALARM_ID,
    HISTORIAN_ALARM_THRESHOLD,
)


def _make_collector(tmp_path, monkeypatch):
    monkeypatch.setenv('SCADA_QUEUE_PERSIST_DIR', str(tmp_path / 'queue'))
    dm = MagicMock()
    dm.get_device_status.return_value = {'connected': True, 'stopped': False}
    db = MagicMock()
    alarm = MagicMock()
    device_control = MagicMock()
    oee = MagicMock()
    collector = DataCollector(
        dm, db, alarm,
        device_control=device_control, oee_calculator=oee,
    )
    return collector, db, alarm, device_control, oee


def _item(value=970.0, name='actual_quantity'):
    return {
        'device_id': 'press_01',
        'register_name': name,
        'value': value,
        'timestamp': datetime.now(),
        'unit': '个',
    }


def _wait_until(predicate, timeout=10.0, message=''):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    raise AssertionError(f'等待超时: {message}')


@pytest.fixture
def loop(tmp_path, monkeypatch):
    """在后台线程里跑真实的 `_process_data` 主循环。"""
    collector, db, alarm, device_control, oee = _make_collector(
        tmp_path, monkeypatch)
    collector.running = True
    thread = threading.Thread(target=collector._process_data, daemon=True)
    thread.start()
    try:
        yield collector, db, alarm, device_control, oee
    finally:
        collector.running = False
        thread.join(timeout=10)
        try:
            collector._pool.shutdown(wait=False)
        except Exception:      # noqa: BLE001
            pass


# ============================================================
# 1. 告警计数：连续 3 次失败告警一次，恢复后自动消警
# ============================================================

def test_historian_alarm_raised_once_at_threshold_and_cleared_on_recovery(
        tmp_path, monkeypatch):
    collector, _db, alarm, _dc, _oee = _make_collector(tmp_path, monkeypatch)

    for _ in range(HISTORIAN_ALARM_THRESHOLD):
        collector._note_historian_write(False, 'sqlite: disk I/O error')

    alarm.raise_external_alarm.assert_called_once()
    alarm_id = alarm.raise_external_alarm.call_args[0][0]
    assert alarm_id == HISTORIAN_ALARM_ID == 'HISTORIAN_WRITE_ERROR'
    assert '历史数据' in alarm.raise_external_alarm.call_args[0][1]

    # 持续失败不刷屏：仍只有一条
    collector._note_historian_write(False, 'still down')
    assert alarm.raise_external_alarm.call_count == 1
    assert collector.stats['historian_write_failures'] == 4

    # 恢复 → 自动消警，计数归零
    collector._note_historian_write(True)
    alarm.clear_external_alarm.assert_called_once_with(
        HISTORIAN_ALARM_ID, device_id='historian', register_name='write')
    assert collector._historian_consecutive_failures == 0

    # 再次连续失败会重新告警（不是永久静音）
    for _ in range(HISTORIAN_ALARM_THRESHOLD):
        collector._note_historian_write(False, 'down again')
    assert alarm.raise_external_alarm.call_count == 2


# ============================================================
# 2. 时序库断连：联锁照常触发且只下发一次，智能统计不喂数
# ============================================================

def test_db_outage_interlock_fires_once_and_alarm_visible(loop):
    collector, db, alarm, device_control, oee = loop
    db.insert_data_batch.side_effect = RuntimeError('historian offline')

    collector.data_queue.put_nowait(_item(970.0))
    _wait_until(lambda: device_control.check_interlocks.call_count >= 1,
                message='写库失败时联锁没有触发')
    # 等这一条走完"失败→重试→再失败丢弃"，确认重试不会重复下发联锁
    _wait_until(lambda: collector.stats.get('dropped_db_retry', 0) >= 1,
                message='写库失败数据未被重试/丢弃')

    assert device_control.check_interlocks.call_count == 1, (
        '同一条数据在重试过程中重复下发了联锁动作')
    assert oee.record_production.call_count == 0, (
        '写库失败时仍把数据喂给了 OEE —— 重试后必然重复计数')

    # 再失败一批 → 达到连续 3 次阈值，告警可见
    collector.data_queue.put_nowait(_item(971.0))
    _wait_until(lambda: alarm.raise_external_alarm.called,
                message='连续写库失败没有产生 HISTORIAN_WRITE_ERROR 告警')
    assert alarm.raise_external_alarm.call_count == 1
    assert alarm.raise_external_alarm.call_args[0][0] == HISTORIAN_ALARM_ID

    # 第二条数据的联锁也只下发一次（重试中不再执行）
    _wait_until(lambda: collector.stats.get('dropped_db_retry', 0) >= 2,
                message='第二批数据未完成重试/丢弃')
    assert device_control.check_interlocks.call_count == 2

    # 全程没有历史数据落库成功 → OEE 一次都不该被喂
    assert oee.record_production.call_count == 0


# ============================================================
# 3. 数据库恢复：智能统计只累加一次（重试不重复计数）
# ============================================================

def test_intel_fed_once_after_db_recovers(loop):
    collector, db, alarm, device_control, oee = loop
    attempts = {'n': 0}

    def _write(batch):
        attempts['n'] += 1
        if attempts['n'] == 1:
            raise RuntimeError('historian offline')
        return len(batch)

    db.insert_data_batch.side_effect = _write

    collector.data_queue.put_nowait(_item(970.0))
    _wait_until(lambda: oee.record_production.called,
                message='数据库恢复后智能统计没有收到数据')
    _wait_until(lambda: attempts['n'] >= 2,
                message='写库只尝试了一次（未走重试路径）')
    time.sleep(0.2)   # 给第二次分发留出窗口，抓"重复累加"

    assert oee.record_production.call_count == 1, (
        f'OEE 被喂了 {oee.record_production.call_count} 次 —— 重试批次重复计数')
    assert oee.record_production.call_args.kwargs == {'count': 970}
    # 联锁同样只执行一次（首轮写库失败时补跑，重试成功时不重跑）
    assert device_control.check_interlocks.call_count == 1
    # 单次失败不到阈值，不应产生告警
    alarm.raise_external_alarm.assert_not_called()
    assert collector.stats['historian_write_failures'] == 1
